# -*- coding: utf-8 -*-
"""章节库：以「文件类型」为维度维护章节（含同义词），并在审核时按章节裁剪送审正文。

设计要点
========
1. 存储：章节库经统一存储层持久化（collection = ``sections``，落在 backend/data 内，
   随 docker-compose 的 backend_data 卷一起持久化）。数据结构：

       {
         "id": "sec-bid-coverletter",           # 章节 id（预置以 sec-<类型>-<语义> 命名）
         "file_type_id": "ft-bid",              # 归属文件类型（章节以文件类型为维度）
         "name": "投标函",                       # 规范章节名（也是默认同义词）
         "synonyms": ["投标函", "投标书", ...],   # 同义写法（标题别名）
         "note": "",                            # 备注（仅展示，不参与匹配）
         "builtin": true,                       # 是否系统预置（预置可编辑、不可删除）
         "enabled": true,                       # 停用后不参与匹配（保留配置）
         "owner_id": "system",                  # 归属：system=系统共享
         "is_shared": true,
         "created_at": "...", "updated_at": "..."
       }

2. 规则关联：规则新增 ``section_ids: list[str]``（可留空）。审核时按「文档自身的
   file_type」取该类型下、且 id 落在 rule.section_ids 中的章节做匹配——因此规则
   未限定 doc_types 时也能按每个文件的实际类型生效。

3. 匹配策略（见 normalize_key / match_sections）：
   - 归一化：NFKC 全角转半角 → 转小写 → 去全部空白 → 去标点 → 剥离编号前缀
     （第X章 / （一）/ 1.2.3 / # ），因此「第一章 投标函」「1.1 投标函:」「投标 函」
     归一后等价。
   - 命中优先级：精确相等 > 标题以关键词开头 > 标题包含关键词；同优先级取更长关键词
     （最长匹配消歧：「投标人须知前附表」不会被「投标人须知」抢走）。
   - 一个文档标题最多归属一个章节（避免父子章节内容重复计入）。

4. 边界处理：
   - 章节不存在：文档未识别出任何关联章节 → 该文档/该规则跳过（不产生结论），
     并在任务进度中提示，避免"没审到却判 pass"。
   - 同名章节：文档中多处同名标题 → 内容合并。
   - 同义词冲突：保存时检测同文件类型内的重复（与其他章节的 name/synonym 归一化后
     相同）并以 warnings 返回；不阻断保存（章节名天然存在包含关系），匹配时按最长
     优先消歧。
   - 同义词含通配/空值：忽略空串与纯符号项。
"""
from __future__ import annotations

import re
import threading
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Any

from .. import storage  # noqa: E402  统一存储层（data/ 卷持久化）

_lock = threading.RLock()

# ===================== 内置预置章节 =====================
# 说明：预置章节可在界面上改同义词、停用，但不可删除（builtin=True）。
_DEFAULT_SECTIONS: list[dict[str, Any]] = [
    # —— 招标文件 ——
    {"id": "sec-tender-announcement", "file_type_id": "ft-tender", "name": "招标公告",
     "synonyms": ["招标公告", "招标通告", "投标邀请书", "投标邀请函"]},
    {"id": "sec-tender-instructions", "file_type_id": "ft-tender", "name": "投标人须知",
     "synonyms": ["投标人须知", "投标须知", "须知"]},
    {"id": "sec-tender-instructions-table", "file_type_id": "ft-tender", "name": "投标人须知前附表",
     "synonyms": ["投标人须知前附表", "须知前附表", "前附表"]},
    {"id": "sec-tender-evaluation", "file_type_id": "ft-tender", "name": "评标办法",
     "synonyms": ["评标办法", "评标方法", "评分办法", "评审办法", "评标标准"]},
    {"id": "sec-tender-contract", "file_type_id": "ft-tender", "name": "合同条款",
     "synonyms": ["合同条款", "合同格式", "通用合同条款", "专用合同条款", "合同条款及格式"]},
    {"id": "sec-tender-spec", "file_type_id": "ft-tender", "name": "技术规格",
     "synonyms": ["技术规格", "技术要求", "技术需求", "采购需求", "货物需求一览表"]},
    {"id": "sec-tender-bill", "file_type_id": "ft-tender", "name": "工程量清单",
     "synonyms": ["工程量清单", "报价清单", "清单"]},
    {"id": "sec-tender-form", "file_type_id": "ft-tender", "name": "投标文件格式",
     "synonyms": ["投标文件格式", "投标文件的格式", "格式"]},
    # —— 投标文件 ——
    {"id": "sec-bid-coverletter", "file_type_id": "ft-bid", "name": "投标函",
     "synonyms": ["投标函", "投标书", "投标函及投标函附录", "投标函附录"]},
    {"id": "sec-bid-legalrep", "file_type_id": "ft-bid", "name": "法定代表人身份证明",
     "synonyms": ["法定代表人身份证明", "法定代表人身份证明书", "法人身份证明"]},
    {"id": "sec-bid-authorization", "file_type_id": "ft-bid", "name": "授权委托书",
     "synonyms": ["授权委托书", "法人授权委托书", "法定代表人授权委托书", "授权书"]},
    {"id": "sec-bid-deposit", "file_type_id": "ft-bid", "name": "投标保证金",
     "synonyms": ["投标保证金", "保证金", "保函", "投标保函"]},
    {"id": "sec-bid-qualification", "file_type_id": "ft-bid", "name": "资格审查资料",
     "synonyms": ["资格审查资料", "资格证明文件", "资格证明", "资质证明", "资格审查"]},
    {"id": "sec-bid-commercial", "file_type_id": "ft-bid", "name": "商务标",
     "synonyms": ["商务标", "商务文件", "报价文件", "投标报价", "已标价工程量清单"]},
    {"id": "sec-bid-technical", "file_type_id": "ft-bid", "name": "技术标",
     "synonyms": ["技术标", "技术文件", "施工组织设计", "技术方案", "服务方案"]},
    {"id": "sec-bid-team", "file_type_id": "ft-bid", "name": "项目管理机构",
     "synonyms": ["项目管理机构", "项目机构", "主要人员", "拟派人员", "项目部组成"]},
    {"id": "sec-bid-performance", "file_type_id": "ft-bid", "name": "类似业绩",
     "synonyms": ["类似业绩", "业绩", "近年完成的类似项目", "类似项目业绩"]},
    {"id": "sec-bid-commitment", "file_type_id": "ft-bid", "name": "承诺书",
     "synonyms": ["承诺书", "投标承诺", "声明", "承诺函"]},
    # —— 合同协议 ——
    {"id": "sec-contract-recital", "file_type_id": "ft-contract", "name": "鉴于条款",
     "synonyms": ["鉴于条款", "鉴于", "前言"]},
    {"id": "sec-contract-subject", "file_type_id": "ft-contract", "name": "标的与价款",
     "synonyms": ["标的与价款", "标的", "合同价款", "价款与支付", "价格条款"]},
    {"id": "sec-contract-performance", "file_type_id": "ft-contract", "name": "履行期限与方式",
     "synonyms": ["履行期限与方式", "履行期限", "交付与验收", "履行方式"]},
    {"id": "sec-contract-breach", "file_type_id": "ft-contract", "name": "违约责任",
     "synonyms": ["违约责任", "违约", "违约条款"]},
    {"id": "sec-contract-dispute", "file_type_id": "ft-contract", "name": "争议解决",
     "synonyms": ["争议解决", "争议解决方式", "仲裁", "法律适用与争议解决"]},
    {"id": "sec-contract-confidential", "file_type_id": "ft-contract", "name": "保密条款",
     "synonyms": ["保密条款", "保密", "保密义务"]},
    # —— 资质证明 ——
    {"id": "sec-qual-license", "file_type_id": "ft-qualification", "name": "营业执照",
     "synonyms": ["营业执照", "企业法人营业执照", "营业执照副本"]},
    {"id": "sec-qual-cert", "file_type_id": "ft-qualification", "name": "资质证书",
     "synonyms": ["资质证书", "资格证书", "等级证书"]},
    {"id": "sec-qual-performance", "file_type_id": "ft-qualification", "name": "业绩证明",
     "synonyms": ["业绩证明", "业绩材料", "合同业绩"]},
    {"id": "sec-qual-finance", "file_type_id": "ft-qualification", "name": "财务报表",
     "synonyms": ["财务报表", "审计报告", "财务状况表"]},
    # —— 通用文档 ——
    {"id": "sec-general-overview", "file_type_id": "ft-general", "name": "概述",
     "synonyms": ["概述", "总则", "前言", "说明"]},
    {"id": "sec-general-body", "file_type_id": "ft-general", "name": "正文",
     "synonyms": ["正文", "主要内容", "内容"]},
    {"id": "sec-general-attachment", "file_type_id": "ft-general", "name": "附件",
     "synonyms": ["附件", "附录", "附表"]},
    {"id": "sec-general-signature", "file_type_id": "ft-general", "name": "签署页",
     "synonyms": ["签署页", "签字盖章页", "落款"]},
]


# ===================== 归一化与匹配 =====================
_PUNCT_CHARS = (
    "：:，,。.、；;！!？?（）()【】[]{}《》<>「」『』\"'“”‘’—" + r"\-_~·|/\\ \t\r\n"
)
_PUNCT_RE = re.compile("[" + re.escape(_PUNCT_CHARS) + "]+")

# doc_parser 对 docx 标题段落插入的标记，形如「[第3章: 投标函]」
_DOCX_MARK_RE = re.compile(r"^\s*\[(?P<kind>第\s*\d+\s*章|表格\s*\d+|第\s*\d+\s*页)[:：]?\s*(?P<text>[^\]]*)\]\s*$")

# 编号前缀：第X章/第X节 / （一） / 1. 1.1 1.1.1 / 一、 / # 标题
# 注意：阿拉伯数字段必须带分隔符（"1." / "1.1"）才算编号，
# 否则 "3C认证" 这类以数字开头的章节名会被误剥。
_NUMBER_PREFIX_RE = re.compile(
    r"^\s*(?:"
    r"第\s*[0-9零一二三四五六七八九十百千]+\s*[章节部分篇条]"
    r"|[（(]\s*[0-9零一二三四五六七八九十]+\s*[)）]"
    r"|[0-9]+(?:\s*[.．]\s*[0-9]+)+\s*[.．、]?"
    r"|[0-9]+\s*[.．、]"
    r"|[零一二三四五六七八九十]+\s*[、]"
    r"|#{1,6}"
    r")\s*"
)

# 标题行判定：以编号/markdown 开头，或为 docx 章节标记
_HEADING_RE = re.compile(
    r"^\s*(?:"
    r"第\s*[0-9零一二三四五六七八九十百千]+\s*[章节部分篇]"
    r"|[（(]\s*[零一二三四五六七八九十]+\s*[)）]"
    r"|[0-9]+(?:\s*[.．]\s*[0-9]+)+\s*[.．、]?"
    r"|[0-9]+\s*[.．、]"
    r"|[零一二三四五六七八九十]+\s*[、]"
    r"|#{1,6}\s"
    r")"
)

# 标题行不会以这些标点结尾（正文行常以句号结尾，可据此降噪）
_SENTENCE_END = ("。", "；", "；", ".", ";", "！", "!", "？", "?")

MAX_HEADING_LEN = 60  # 标题行最大长度，超过则视为正文


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_key(text: str) -> str:
    """章节标题/同义词的匹配归一化键。

    步骤：NFKC（全角→半角）→ 小写 → 剥离编号前缀（须在去标点前，否则
    「（一）」「1.1」的分隔符会先被清掉而无法识别）→ 去标点 → 去空白。
    """
    s = unicodedata.normalize("NFKC", str(text or ""))
    s = s.strip().lower()
    s = _NUMBER_PREFIX_RE.sub("", s)
    s = _PUNCT_RE.sub("", s)
    s = re.sub(r"\s+", "", s)
    return s


def clean_title(raw: str) -> str:
    """从原始标题行提取「可读章节名」：去掉 docx 标记外壳与编号前缀。"""
    line = str(raw or "").strip()
    m = _DOCX_MARK_RE.match(line)
    if m:
        line = (m.group("text") or "").strip()
    line = _NUMBER_PREFIX_RE.sub("", line).strip()
    return line


def _is_heading(line: str) -> bool:
    s = line.strip()
    if not s or len(s) > MAX_HEADING_LEN:
        return False
    if _DOCX_MARK_RE.match(s):
        # 仅「第N章」标记算章节；页/表格标记不算（避免按页误切）
        return "章" in (_DOCX_MARK_RE.match(s).group("kind") or "")
    if not _HEADING_RE.match(s):
        return False
    if s.endswith(_SENTENCE_END):
        return False
    return True


def split_sections(text: str) -> list[dict[str, Any]]:
    """把文档正文按标题切分为章节列表。

    返回 [{"title": 可读章节名, "raw": 原始标题行, "body": 章节正文（含标题行）}]。
    标题之前的文本（封面/前言/目录等）作为 title="" 的首段保留，便于兜底。
    """
    lines = str(text or "").splitlines()
    sections: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for line in lines:
        if _is_heading(line):
            if cur is not None:
                sections.append(cur)
            title = clean_title(line)
            cur = {"title": title, "raw": line.strip(), "body": line.rstrip()}
        else:
            if cur is None:
                cur = {"title": "", "raw": "", "body": ""}
            cur["body"] += "\n" + line
    if cur is not None:
        sections.append(cur)
    # 去空并补 body 尾部
    out: list[dict[str, Any]] = []
    for s in sections:
        body = s["body"].strip("\n")
        if not body:
            continue
        out.append({**s, "body": body})
    return out


def _score(heading_key: str, kw_key: str) -> tuple[int, int] | None:
    """关键词与标题的匹配打分；None=未命中。返回 (优先级, 关键词长度)。"""
    if not heading_key or not kw_key:
        return None
    if heading_key == kw_key:
        return (3, len(kw_key))
    if heading_key.startswith(kw_key):
        return (2, len(kw_key))
    if kw_key in heading_key:
        return (1, len(kw_key))
    return None


def match_sections(
    text: str, section_defs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """在文档正文中匹配给定章节定义。

    返回命中列表（按文档中出现顺序）：
      [{"section_id", "name", "headings": [命中的原始标题...], "body": 合并后的正文}]
    一个标题最多归属一个章节（精确优先、同分取最长关键词）。
    """
    if not section_defs:
        return []
    # 预计算每个章节的关键词（规范名 + 同义词），归一化去重去空
    kw_index: list[tuple[str, str, str]] = []  # (section_id, name, kw_key)
    for sec in section_defs:
        sid = str(sec.get("id") or "")
        name = str(sec.get("name") or "")
        keys = [name] + [str(s) for s in (sec.get("synonyms") or [])]
        seen: set[str] = set()
        for k in keys:
            kk = normalize_key(k)
            if kk and kk not in seen:
                seen.add(kk)
                kw_index.append((sid, name, kk))

    buckets: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for sec in split_sections(text):
        hk = normalize_key(sec["title"])
        if not hk:
            continue
        best: tuple[int, int, str, str] | None = None  # (score, kwlen, sid, name)
        for sid, name, kk in kw_index:
            sc = _score(hk, kk)
            if sc is None:
                continue
            cand = (sc[0], sc[1], sid, name)
            if best is None or (cand[0], cand[1]) > (best[0], best[1]):
                best = cand
        if best is None:
            continue
        sid = best[2]
        if sid not in buckets:
            buckets[sid] = {
                "section_id": sid,
                "name": best[3],
                "headings": [],
                "body": "",
            }
            order.append(sid)
        buckets[sid]["headings"].append(sec["title"] or sec["raw"])
        buckets[sid]["body"] = (
            buckets[sid]["body"] + "\n\n" + sec["body"] if buckets[sid]["body"] else sec["body"]
        )
    return [buckets[sid] for sid in order]


def scope_text(text: str, section_defs: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """按章节定义裁剪正文：返回 (命中章节正文, 命中章节名列表)。

    未命中任何章节时返回 ("", []) —— 调用方据此跳过审核而非判 pass。
    """
    hits = match_sections(text, section_defs)
    if not hits:
        return "", []
    body = "\n\n".join(h["body"] for h in hits)
    names = [h["name"] for h in hits]
    return body, names


def parse_headings(text: str) -> list[dict[str, str]]:
    """解析文档中被识别为章节标题的行，供界面快速录入同义词。"""
    return [
        {"title": s["title"], "raw": s["raw"]}
        for s in split_sections(text)
        if s["title"]
    ]


# ===================== 存储层 =====================
def _normalize(s: dict[str, Any]) -> dict[str, Any]:
    s = dict(s)
    if "owner_id" not in s:
        s["owner_id"] = "system"
    if "is_shared" not in s:
        s["is_shared"] = s["owner_id"] == "system"
    if "enabled" not in s:
        s["enabled"] = True
    syns: list[str] = []
    for x in [s.get("name")] + list(s.get("synonyms") or []):
        v = str(x or "").strip()
        if v and v not in syns:
            syns.append(v)
    s["synonyms"] = syns
    return s


def _seeded() -> list[dict[str, Any]]:
    now = _now()
    return [
        _normalize(
            {
                **item,
                "note": "",
                "builtin": True,
                "enabled": True,
                "created_at": now,
                "updated_at": now,
            }
        )
        for item in _DEFAULT_SECTIONS
    ]


def _load() -> list[dict[str, Any]]:
    data = storage.read_collection("sections")
    if not isinstance(data, list):
        seeded = _seeded()
        _save(seeded)
        return seeded
    out: list[dict[str, Any]] = []
    changed = False
    for s in data:
        if not isinstance(s, dict):
            continue
        norm = _normalize(s)
        if norm != s:
            changed = True
        out.append(norm)
    if changed:
        _save(out)
    return out


def _save(items: list[dict[str, Any]]) -> None:
    try:
        storage.write_collection("sections", items)
    except Exception:  # noqa: BLE001
        pass


def _accessible(s: dict[str, Any], user_id: str | None) -> bool:
    if user_id is None:
        return True
    return s.get("owner_id") == user_id or bool(s.get("is_shared"))


def list_sections(
    file_type_id: str | None = None,
    user_id: str | None = None,
    enabled_only: bool = False,
) -> list[dict[str, Any]]:
    """列出章节；可按文件类型过滤（章节以文件类型为维度）。"""
    items = _load()
    out = []
    for s in items:
        if not _accessible(s, user_id):
            continue
        if file_type_id and str(s.get("file_type_id") or "") != file_type_id:
            continue
        if enabled_only and not s.get("enabled", True):
            continue
        out.append(s)
    out.sort(key=lambda s: (str(s.get("file_type_id") or ""), str(s.get("name") or "")))
    return out


def get(section_id: str, user_id: str | None = None) -> dict[str, Any] | None:
    for s in _load():
        if s.get("id") == section_id:
            return s if _accessible(s, user_id) else None
    return None


def get_many(section_ids: list[str] | None) -> list[dict[str, Any]]:
    """按 id 批量取章节（不鉴权，供审核引擎使用）。保持与入参一致的顺序。"""
    if not section_ids:
        return []
    pool = {str(s.get("id")): s for s in _load()}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for sid in section_ids:
        sid = str(sid)
        if sid in pool and sid not in seen:
            seen.add(sid)
            out.append(pool[sid])
    return out


def for_file_type(
    section_ids: list[str] | None, file_type_id: str | None
) -> list[dict[str, Any]]:
    """取「属于该文件类型」且 id 在 section_ids 中的启用章节。

    章节以文件类型为维度，因此规则没限定 doc_types 时，也会按每个文档自身的文件类型
    取交集中的章节；文档未指定类型时返回空（该文档不参与本规则审核）。
    """
    if not section_ids or not file_type_id:
        return []
    wanted = {str(x) for x in section_ids}
    return [
        s
        for s in _load()
        if str(s.get("id")) in wanted
        and str(s.get("file_type_id") or "") == str(file_type_id)
        and s.get("enabled", True)
    ]


def detect_conflicts(payload: dict[str, Any]) -> list[str]:
    """保存前的同义词/同名冲突检测，返回可读告警（不阻断）。

    检测范围：同一 file_type_id 下的其他章节，若其 name/synonym 与本条目的
    name/synonym 归一化后完全相同 → 报冲突（匹配时按最长优先消歧，故仅提示）。
    """
    warnings: list[str] = []
    ft = str(payload.get("file_type_id") or "")
    sid = str(payload.get("id") or "")
    mine = {normalize_key(x) for x in [payload.get("name")] + list(payload.get("synonyms") or [])}
    mine.discard("")
    if not ft or not mine:
        return warnings
    for s in _load():
        if str(s.get("id")) == sid or str(s.get("file_type_id") or "") != ft:
            continue
        others = {normalize_key(x) for x in [s.get("name")] + list(s.get("synonyms") or [])}
        others.discard("")
        dup = mine & others
        if dup:
            warnings.append(
                f"与章节「{s.get('name')}」存在重复的同义写法：{len(dup)} 项（匹配时按最长优先消歧）"
            )
    return warnings


def save_section(
    payload: dict[str, Any],
    user_id: str | None = None,
    is_shared: bool | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """新建或更新章节；返回 (记录, 冲突告警)。

    - 预置章节（builtin=True）允许原地改 name/synonyms/note/enabled，但不可删除；
    - 归属语义同文件类型：管理员创建→系统共享；普通用户创建→归属自己。
    """
    with _lock:
        items = _load()
        sid = str(payload.get("id") or "").strip()
        existing = next((s for s in items if s.get("id") == sid), None) if sid else None
        if existing:
            owner_id = existing.get("owner_id", "system")
            shared = existing.get("is_shared", True)
            builtin = bool(existing.get("builtin"))
            created_at = existing.get("created_at") or _now()
        else:
            sid = sid or f"sec-{uuid.uuid4().hex[:8]}"
            owner_id = "system" if user_id is None else user_id
            shared = True if user_id is None else bool(is_shared)
            builtin = bool(payload.get("builtin")) and user_id is None
            created_at = _now()

        name = str(payload.get("name") or "").strip()
        if not name:
            raise ValueError("章节名称不能为空")
        file_type_id = str(payload.get("file_type_id") or "").strip()
        if not file_type_id:
            raise ValueError("章节必须归属一个文件类型")
        syns: list[str] = []
        for x in payload.get("synonyms") or []:
            v = str(x or "").strip()
            if v and v not in syns:
                syns.append(v)
        if name not in syns:
            syns.insert(0, name)

        record = {
            "id": sid,
            "file_type_id": file_type_id,
            "name": name,
            "synonyms": syns,
            "note": str(payload.get("note") or "").strip(),
            "builtin": builtin,
            "enabled": bool(payload.get("enabled", True)),
            "owner_id": owner_id,
            "is_shared": shared,
            "created_at": created_at,
            "updated_at": _now(),
        }
        warnings = detect_conflicts(record)
        for i, s in enumerate(items):
            if s.get("id") == sid:
                items[i] = record
                break
        else:
            items.append(record)
        _save(items)
        return record, warnings


def delete_section(section_id: str, user_id: str | None = None) -> bool:
    """删除自定义章节；预置章节（builtin=True）不可删除（可停用）。"""
    with _lock:
        items = _load()
        target = next((s for s in items if s.get("id") == section_id), None)
        if not target:
            return False
        if target.get("builtin"):
            return False
        if user_id is not None and target.get("owner_id") != user_id:
            return False
        remaining = [s for s in items if s.get("id") != section_id]
        if len(remaining) == len(items):
            return False
        _save(remaining)
        return True


def sections_for_rules(
    rules: list[dict[str, Any]], user_id: str | None = None
) -> list[dict[str, Any]]:
    """取一批规则关联的全部章节（并集），供界面展示/校验。"""
    ids: list[str] = []
    for r in rules or []:
        for sid in r.get("section_ids") or []:
            if sid and str(sid) not in ids:
                ids.append(str(sid))
    return get_many(ids)
