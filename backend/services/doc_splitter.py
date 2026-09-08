# -*- coding: utf-8 -*-
"""解析阶段章节拆分引擎（对齐 document-split 参考项目的处理方式）。

参考项目的四阶段拆分：
  Phase 1 解析（doc_parser 已完成，本模块消费其文本产物）
  Phase 2 工程化标题检测（保守正则 + 过滤器；DOCX 样式标题为权威）
  Phase 3 递归/顺序拆分（标题 → 章节体，超长章节由下游 truncate/分段兜底）
  Phase 4 LLM 复核（本系统不引入——审核场景标题检测靠确定性规则即可，
         避免 LLM 成本与不确定性）

与参考项目的关键对齐点：
  - DOCX：Word 样式/大纲级别标题视为权威（doc_parser 以 [第N章: 标题] 标记输出）；
  - PDF：严格标题正则（编号前缀限位、排除数据行）、句子结尾/逗号数/条款开头
    黑名单过滤器、目录页跳过（点线引导行过滤兜底）；
  - Excel：工作表名作为章节标题（投标文件常以工作表承载商务标/技术标）；
  - 章节体到「下一个任意层级标题」为止（叶子语义，与审核裁剪的旧口径一致）；
  - 每个章节携带 char_start/char_end（原文偏移，可直接供原文定位/预览复用）
    与所在页码。

解析文本中的位置标记（[第N页]/[表格N]/[工作表: x]/[第N章: 标题]）保持原样保留在
章节体内——与旧 scope_text 口径一致（标记随正文送审，模型可据此引用页码，
text_locator 原文定位不受影响）。

产出结构存入 file record 的 ``sections`` 字段（files_meta 持久化），审核引擎
（_section_scoped / _apply_structured_rules）优先消费，旧文件无该结构时回退
实时 scope_text。
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any

logger = logging.getLogger(__name__)

# ===================== 位置标记（与 doc_parser / text_locator 口径一致） =====================
_MARKER_RE = re.compile(
    r"^\s*\[(?:第(\d+)页|表格(\d+)|工作表:\s*([^\]]+)|(第\d+章[^\]]*))\]\s*$"
)

# doc_parser 对 docx 标题段落插入的标记：[第3章: 投标函]
_DOCX_CHAPTER_RE = re.compile(r"^\s*\[第\d+章[:：]\s*([^\]]*)\]\s*$")

# ===================== 标题检测（对齐参考项目的保守策略） =====================
# 一级：第X章/节/部/篇 / Chapter|Section|Part N / 附录 / 常用独立标题词
_L1_PATTERNS = [
    re.compile(r"^第\s*[0-9零一二三四五六七八九十百千]+\s*[章节部分篇]"),
    re.compile(r"^(?:Chapter|Section|Part|Appendix)\s+\d+", re.IGNORECASE),
    re.compile(r"^附录\s*[A-Z\d]?"),
    re.compile(r"^(?:前言|引言|摘要|绪论|总结|致谢|参考文献)$"),
]
# 二级：中文数字+顿号 / （中文数字）/ X.Y（编号前缀限 1-2 位，排除 100.00 数据行）
_L2_PATTERNS = [
    re.compile(r"^[一二三四五六七八九十]+\s*[、.．]\s*\S"),
    re.compile(r"^[（(]\s*[一二三四五六七八九十]+\s*[)）]"),
    re.compile(r"^\d{1,2}\.\d+(?!\.\d)[\s　]+\S"),
]
# 三级：X. / X、/ （X）/ ①-⑳（编号前缀限 1-3 位，排除数据/金额行）
_L3_PATTERNS = [
    re.compile(r"^\d{1,3}\s*[.．、]\s*\S"),
    re.compile(r"^[（(]\s*\d{1,3}\s*[)）]"),
    re.compile(r"^[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]"),
]

# 中文数字+顿号开头的「正文条款」黑名单首字（顿号后以这些字开头说明是条款而非标题），
# 对齐参考项目 _detect_pdf_heading_levels 的 clause_start_chars
_CLAUSE_START_CHARS = set(
    "在不若我如本配承所对报有无已未为与向从将应可需因由经据以及或但并受提申中当设"
    "现持完做出来到前后同共全该其另各每任凡兹尔予"
)

# 句子结尾标点（以这些结尾的行是正文不是标题）
_SENTENCE_END = ("。", "；", ".", ";", "！", "!", "？", "?", "：", ":")

# 目录页判定：独立成行的「目录/目 录」
_TOC_LINE_RE = re.compile(r"^\s*目\s*录\s*$")
# 点线引导行（目录条目）：「一、投标函 ……… 5」
_DOT_LEADER_RE = re.compile(r"[.．…·]{3,}\s*[0-9一二三四五六七八九十]*\s*$")

MAX_HEADING_LEN = 60  # 标题行最大长度（与 sections 旧口径一致，超过视为正文）
MAX_COMMAS = 3  # 逗号数上限（长句不是标题）


def _detect_level(line: str) -> tuple[int, str] | None:
    """非标记行的标题检测，返回 (层级, 来源) 或 None。

    过滤器（对齐参考项目的极保守策略）：
    1. 长度 2..60，过长视为正文；
    2. 以句子结尾标点结尾 → 正文；
    3. 逗号数 > 3 → 正文；
    4. 「中文数字+顿号」后跟条款黑名单首字 → 正文条款；
    5. 点线引导行（目录条目）→ 非标题。
    """
    s = line.strip()
    if len(s) < 2 or len(s) > MAX_HEADING_LEN:
        return None
    if s.endswith(_SENTENCE_END):
        return None
    if s.count("，") + s.count(",") > MAX_COMMAS:
        return None
    if _DOT_LEADER_RE.search(s):
        return None

    cn_clause = re.match(r"^([一二三四五六七八九十]+)\s*[、.．]\s*(.+)", s)
    if cn_clause and cn_clause.group(2)[:1] in _CLAUSE_START_CHARS:
        return None

    for pat in _L1_PATTERNS:
        if pat.match(s):
            return 1, "regex"
    for pat in _L2_PATTERNS:
        if pat.match(s):
            return 2, "regex"
    for pat in _L3_PATTERNS:
        if pat.match(s):
            return 3, "regex"
    return None


def _clean_title(raw: str) -> str:
    """从原始标题行提取可读章节名：剥 docx 标记外壳、编号前缀、页码尾巴。"""
    from .sections import _NUMBER_PREFIX_RE

    s = str(raw or "").strip()
    m = _DOCX_CHAPTER_RE.match(s)
    if m:
        s = (m.group(1) or "").strip()
    s = re.sub(r"\s*[.．…·]{2,}\s*[0-9一二三四五六七八九十]*\s*$", "", s)  # 目录页码尾巴
    s = _NUMBER_PREFIX_RE.sub("", s).strip()
    s = unicodedata.normalize("NFKC", s)
    return s


def split_document(text: str, ext: str = "") -> list[dict[str, Any]]:
    """把解析后的文档文本拆分为章节结构（解析阶段一次完成，审核阶段直接复用）。

    返回列表（按文档顺序）：
      [{
        "title": 可读章节名（""=标题前的封面/前言/目录等前置内容）,
        "raw": 原始标题行（前置内容为 ""）,
        "body": 章节正文（含标题行与内部位置标记，与旧 scope_text 口径一致）,
        "char_start": 章节体在原文中的起始偏移,
        "char_end": 结束偏移（不含尾部空白）,
        "page": 所在页码（无页标记的文档恒为 1）,
        "level": 标题层级 1-3（前置内容为 0）,
        "source": "style"（docx 样式标记）| "worksheet"（Excel 工作表）| "regex",
      }, ...]

    拆分失败时抛异常由调用方兜底（审核侧回退实时裁剪）。
    """
    text = str(text or "")
    if not text.strip():
        return []
    ext = (ext or "").lower()

    lines = text.split("\n")
    # 每行的起始偏移（含行尾 \n 的长度，供 char 定位）
    offsets: list[int] = []
    acc = 0
    for ln in lines:
        offsets.append(acc)
        acc += len(ln) + 1

    sections: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    page = 1
    toc_pages: set[int] = set()

    def close_cur(end_line_idx: int) -> None:
        """结束当前章节：body 到上一行（不含空行尾），截掉尾部空白。"""
        nonlocal cur
        if cur is None:
            return
        end_off = (
            offsets[end_line_idx] if end_line_idx < len(offsets) else len(text)
        )
        raw_end = end_off
        while raw_end > cur["_start"] and text[raw_end - 1 : raw_end] in ("\n", "\r", " "):
            raw_end -= 1
        body = text[cur["_start"] : max(raw_end, cur["_start"])]
        cur.pop("_start", None)
        if not body.strip():
            cur = None
            return
        cur["body"] = body
        cur["char_start"] = cur["_cs"]
        cur["char_end"] = cur["_cs"] + len(body)
        cur.pop("_cs", None)
        sections.append(cur)
        cur = None

    for i, line in enumerate(lines):
        stripped = line.strip()

        # —— 位置标记行 ——
        m = _MARKER_RE.match(line)
        if m:
            page_no, _tbl, sheet, chapter = m.groups()
            if page_no:
                page = int(page_no)  # 更新当前页，不切章节
                continue
            if chapter:  # [第N章: 标题] —— docx 样式标题，权威来源
                close_cur(i)
                cur = {
                    "title": _clean_title(line),
                    "raw": stripped,
                    "page": page,
                    "level": 1,
                    "source": "style",
                    "_start": offsets[i],
                    "_cs": offsets[i],
                }
                continue
            if sheet is not None:  # [工作表: x] —— Excel 工作表名作章节
                close_cur(i)
                cur = {
                    "title": (sheet or "").strip(),
                    "raw": stripped,
                    "page": 1,
                    "level": 1,
                    "source": "worksheet",
                    "_start": offsets[i],
                    "_cs": offsets[i],
                }
                continue
            # [表格N] 是内容标记，不切章节，自然落入当前章节体
            continue

        # —— 目录页跳过：独立「目录」行；其后本页内的行不参与标题检测 ——
        if _TOC_LINE_RE.match(stripped):
            toc_pages.add(page)
            continue
        if page in toc_pages:
            # 目录页内不产标题；遇到下一个页标记自动恢复（page 变更后不在集合中）
            continue

        # —— 常规标题检测（对齐参考项目保守策略）——
        det = _detect_level(line)
        if det:
            level, source = det
            close_cur(i)
            cur = {
                "title": _clean_title(line),
                "raw": stripped,
                "page": page if ext in (".pdf", "") else page,
                "level": level,
                "source": source,
                "_start": offsets[i],
                "_cs": offsets[i],
            }
            continue

        if cur is None:
            # 标题前的前置内容（封面/目录等）：作为 title="" 首段兜底保留
            cur = {
                "title": "",
                "raw": "",
                "page": page,
                "level": 0,
                "source": "preamble",
                "_start": offsets[i],
                "_cs": offsets[i],
            }

    close_cur(len(lines))
    return sections


def section_titles(sections: list[dict[str, Any]]) -> list[str]:
    """提取非空章节标题（调试/展示用）。"""
    return [s.get("title", "") for s in sections or [] if s.get("title")]


def stats(sections: list[dict[str, Any]]) -> dict[str, Any]:
    """拆分质量速览（日志/接口用）。"""
    titled = [s for s in sections or [] if s.get("title")]
    return {
        "total": len(sections or []),
        "titled": len(titled),
        "by_source": {
            src: sum(1 for s in titled if s.get("source") == src)
            for src in {s.get("source") for s in titled}
        },
        "chars": sum(len(s.get("body") or "") for s in sections or []),
    }
