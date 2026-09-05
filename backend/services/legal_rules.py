# -*- coding: utf-8 -*-
"""法规文件 → 临时审核规则集（Legal Ruleset）。

场景：用户不选用既有规则组/规则集，而是直接上传一份法律法规、规章或规范性文件，
由模型解析全文、自动抽取「可被用于审核招标/投标文件」的合规规则，生成一组
**临时规则集**，再拿这组规则去审核目标文档。

设计要点
--------
1. **不污染正式规则库**：临时规则集独立持久化在 ``data/legal_rulesets.json``，
   不写 ``rules_store.json``；只有用户显式调用「转正」才落一份正式规则集。
2. **复用既有审核引擎**：产出的规则 dict 与 ``rules_store._normalize_rule`` 同形
   （id/name/category/severity/description/checkpoints/...），可直接交给
   ``review_engine.run_review``，引擎侧零改动。
3. **分块抽取 + 全局合并**：法规全文动辄数十万字，单次送 LLM 必然撞
   ``llm_timeout`` 与 ``llm_call_hard_ceil``。故先按「条/章」切成语义完整的段，
   再按字符预算打包成块并发抽取，最后全局去重、打分、截断。
4. **防误判**：自动生成的规则强制 ``category`` 落在引擎白名单内，并规避
   ``is_consistency_rule`` 的误命中（id 不用 ``cons-`` 前缀、category 不取
   ``qualification`` + name 含「一致」的组合），避免意外触发一致性核查阶段。
"""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import config
from .. import storage
from . import file_store
from . import llm_client
from . import prompt_store

logger = logging.getLogger(__name__)

# legal_rulesets 集合经统一存储层持久化（data/storage_config.json 可切换后端）

_lock = threading.RLock()
_items: dict[str, dict[str, Any]] = {}
# 保活在途挖矿任务引用，避免被 GC 中途回收
_background: set[asyncio.Task] = set()

# 引擎侧 category 白名单（models/schemas.py RuleCategory 去掉 consistency）
ALLOWED_CATEGORIES = (
    "qualification", "commercial", "technical", "format",
    "tender_quality", "general_quality", "legal",
)
ALLOWED_SEVERITIES = ("critical", "major", "minor", "info")

_SEVERITY_WEIGHT = {"critical": 4, "major": 3, "minor": 2, "info": 1}

# 终止态：挖矿任务进入这些状态后不再变化
_TERMINAL = ("ready", "failed")


# --------------------------------------------------------------------------- #
# 持久化
# --------------------------------------------------------------------------- #
def _load() -> None:
    global _items
    raw = storage.read_collection("legal_rulesets")
    if not isinstance(raw, (dict, list)):
        return
    if isinstance(raw, dict):
        _items = raw
    elif isinstance(raw, list):  # 兼容早期列表形态
        _items = {r.get("id") or uuid.uuid4().hex: r for r in raw if isinstance(r, dict)}


def _save() -> None:
    try:
        storage.write_collection("legal_rulesets", _items)
    except Exception:  # noqa: BLE001
        logger.exception("临时规则集落盘失败")


def _recover_interrupted() -> None:
    """启动时自愈：进程重启会让在途挖矿任务失去执行者，记录将永久停在 mining。

    与 task_store._load 的做法一致，把非终态记录翻为 failed，
    避免前端「一直轮询、永远生成中」。
    """
    changed = False
    for rec in _items.values():
        if rec.get("status") in ("pending", "mining"):
            rec["status"] = "failed"
            rec["error"] = "服务重启导致生成中断，请重新生成"
            rec["progress_message"] = "生成中断（服务重启）"
            _log(rec, "服务重启导致生成中断，请重新生成", "error")
            changed = True
    if changed:
        _save()


def _persist_on_switch(_old_driver: str, _new_driver: str) -> None:
    """存储后端热切换时，把当前内存态整写到新后端（数据以内存为权威）。"""
    storage.write_collection("legal_rulesets", _items)

storage.on_structured_switch(_persist_on_switch)

_load()
# 注意：_recover_interrupted 内部会调用 _log（定义在下方），必须在 _log 之后执行；
# 此前放在定义前，一旦重启时存在在途挖矿记录就会 NameError 崩溃循环。
# （2026-09-04 修复：曾致容器 Restarting 循环）


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# 过程日志：与审核任务日志（task_store）同构（time / text / level），
# 前端直接复用审核界面的 ProgressPanel 渲染，让用户实时看到
# 「解析 → 切分 → 逐块抽取 → 去重 → 完成」的全过程，而不是只盯一个百分比。
# --------------------------------------------------------------------------- #
_LOG_MAX = 500  # 极端长法规（上百块）时防记录无限膨胀


def _log(rec: dict[str, Any], text: str, level: str = "info") -> None:
    """追加一条过程日志（调用方需已持有 _lock，或操作的是游离 rec）。"""
    logs = rec.setdefault("logs", [])
    logs.append(
        {
            "time": datetime.now().strftime("%H:%M:%S"),
            "text": text,
            "level": level,
        }
    )
    if len(logs) > _LOG_MAX:
        del logs[: len(logs) - _LOG_MAX]


def _log_rid(rid: str, text: str, level: str = "info") -> None:
    """按 id 追加日志并落盘（在锁外调用；_lock 是 RLock，嵌套调用同样安全）。"""
    with _lock:
        rec = _items.get(rid)
        if rec:
            _log(rec, text, level)
            rec["updated_at"] = _now()
            _save()


# 启动自愈：必须在 _log/_log_rid 定义之后调用（见上方注释）
_recover_interrupted()


def _chunk_progress(rec: dict[str, Any], total: int) -> float:
    """抽取阶段进度：5% → 85%。

    已完成块按 1 计、在途块按 0.5 计——大法规单块抽取消耗几分钟，
    若只按「完成数」推进，进度条会长时间一动不动，用户会以为卡死。
    该算式单调不回退（某块完成时 在途-1、完成+1，净增 0.5）。
    """
    stats = rec.setdefault("stats", {})
    done = int(stats.get("chunks_done") or 0)
    started = int(stats.get("chunks_started") or 0)
    inflight = max(0, started - done)
    eff = done + 0.5 * inflight
    return round(min(85.0, 5.0 + 80.0 * eff / max(1, total)), 1)


def _accessible(rec: dict[str, Any], user_id: str | None) -> bool:
    """管理员（user_id=None）看全部；普通用户看「自己的 + 系统共享的」。"""
    if user_id is None:
        return True
    return rec.get("owner_id") == user_id or bool(rec.get("is_shared"))


def get_set(ruleset_id: str, user_id: str | None = None) -> dict[str, Any] | None:
    with _lock:
        rec = _items.get(ruleset_id)
        if not rec or not _accessible(rec, user_id):
            return None
        return json.loads(json.dumps(rec))  # 深拷贝，避免调用方改到内存态


def list_sets(user_id: str | None = None) -> list[dict[str, Any]]:
    """列表概览（管理员看全部，普通用户看自己的 + 系统共享）。

    下拉框只需要 id/名称/状态/条数等概要，这里裁掉 `logs`（最多 500 行）
    与 `rules` 明细（换成 `rule_count`），避免规则集一多列表接口显著膨胀。
    """
    with _lock:
        out = []
        for r in _items.values():
            if not _accessible(r, user_id):
                continue
            slim = json.loads(json.dumps(r))
            slim.pop("logs", None)
            slim["rule_count"] = len(slim.get("rules") or [])
            slim.pop("rules", None)
            out.append(slim)
    out.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return out


def _persist(rec: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        rec["updated_at"] = _now()
        _items[rec["id"]] = rec
        _save()
    return json.loads(json.dumps(rec))


def delete_set(ruleset_id: str, user_id: str | None = None) -> bool:
    with _lock:
        rec = _items.get(ruleset_id)
        if not rec:
            return False
        if user_id is not None and rec.get("owner_id") != user_id:
            return False
        del _items[ruleset_id]
        _save()
        return True


# --------------------------------------------------------------------------- #
# 文本切分：按「条」优先，其次「章/节」，最后按段落窗口
# --------------------------------------------------------------------------- #
_ARTICLE_RE = re.compile(r"(?m)^[ \t　]*第[一二三四五六七八九十百千零〇0-9]{1,12}条")
_CHAPTER_RE = re.compile(r"(?m)^[ \t　]*第[一二三四五六七八九十百千]{1,8}[章节编]")


def _split_by_positions(text: str, positions: list[int]) -> list[str]:
    segs: list[str] = []
    head = text[: positions[0]].strip()
    if head:
        segs.append(head)
    for i, pos in enumerate(positions):
        end = positions[i + 1] if i + 1 < len(positions) else len(text)
        piece = text[pos:end].strip()
        if piece:
            segs.append(piece)
    return segs


def split_legal_text(text: str) -> tuple[list[str], str]:
    """把法规正文切成语义完整的小段，返回 (段列表, 切分方式)。

    优先按「第X条」切（法律法规的自然结构，一条一义）；
    无条款标记时退化为按「第X章/节」切；仍无则按空行段落切。
    """
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return [], "empty"

    positions = [m.start() for m in _ARTICLE_RE.finditer(text)]
    # 条款过少（如只有 1 条）说明该文件不是条款体，退化为章/节
    if len(positions) >= 3:
        return _split_by_positions(text, positions), "article"

    positions = [m.start() for m in _CHAPTER_RE.finditer(text)]
    if len(positions) >= 2:
        return _split_by_positions(text, positions), "chapter"

    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return (paras or [text.strip()]), "paragraph"


def pack_chunks(segments: list[str], budget: int) -> list[str]:
    """按字符预算把小段打包成块：不切断单段；超预算的段再按预算硬切。

    硬切时保留 ``overlap`` 字符重叠，避免阈值/主体被切断在边界上。
    """
    budget = max(2000, int(budget))
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for seg in segments:
        if len(seg) > budget:
            if buf:
                chunks.append("\n\n".join(buf))
                buf, size = [], 0
            step = budget - 200
            for i in range(0, len(seg), step):
                chunks.append(seg[i: i + budget])
            continue
        if size + len(seg) > budget and buf:
            chunks.append("\n\n".join(buf))
            buf, size = [], 0
        buf.append(seg)
        size += len(seg) + 2
    if buf:
        chunks.append("\n\n".join(buf))
    return [c for c in chunks if c.strip()]


# --------------------------------------------------------------------------- #
# 提示词
# --------------------------------------------------------------------------- #
def _mining_system() -> str:
    """法规挖掘 system 提示词（当前生效版本，后台 Prompt 管理页可自定义）。"""
    return prompt_store.render("legal_mining_system")


def _mining_user_message(chunk: str, *, mode: str, index: int, total: int) -> str:
    target = {
        "bid": "后续将用这些规则审核**投标文件**（含其附件）",
        "tender": "后续将用这些规则审核**招标文件**",
        "general": "后续将用这些规则审核招采相关文件",
    }.get(mode, "后续将用这些规则审核招采相关文件")
    return prompt_store.render(
        "legal_mining_user",
        {"index": index, "total": total, "target": target, "chunk": chunk},
    )
def _mining_user_message(chunk: str, *, mode: str, index: int, total: int) -> str:
    target = {
        "bid": "后续将用这些规则审核**投标文件**（含其附件）",
        "tender": "后续将用这些规则审核**招标文件**",
        "general": "后续将用这些规则审核招采相关文件",
    }.get(mode, "后续将用这些规则审核招采相关文件")
    return (
        f"以下是法规文件正文的第 {index}/{total} 段。{target}。\n"
        "请只依据本段原文抽取可核查规则，输出 JSON。\n\n"
        "---- 法规原文开始 ----\n"
        f"{chunk}\n"
        "---- 法规原文结束 ----"
    )


# --------------------------------------------------------------------------- #
# 版本/时效性元数据（确定性提取，不增加 LLM 调用）
# --------------------------------------------------------------------------- #
_DATE_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
# 文号两种主流形态：国办发〔2026〕12号 / 国务院令第613号
_DOC_NUMBER_RE = re.compile(
    r"([\u4e00-\u9fa5]{2,12})\s*[〔\[](\d{4})[〕\]]\s*(\d+)\s*号"
    r"|([\u4e00-\u9fa5]{2,10}令)\s*第\s*(\d+)\s*号"
)
_TITLE_BOOK_RE = re.compile(r"《([^《》]{2,40})》")
_TITLE_SUFFIX_RE = re.compile(r"[法条例办法规定规则细则通知意见决定公告]$")

_STALENESS_YEARS = float(config.get("legal_staleness_years", 8))


def _parse_date(t: tuple[str, str, str]) -> tuple[int, int, int]:
    return int(t[0]), int(t[1]), int(t[2])


def _extract_doc_meta(text: str) -> dict[str, Any] | None:
    """从法规文本中确定性提取名称、文号、版本日期等元数据。

    仅做纯文本规则匹配（不调用 LLM），用于预检和结果展示；匹配失败返回 None。
    """
    if not text or not text.strip():
        return None
    head = text[:1000]

    # 法规名称：优先首行标题（文书后缀结尾）；首行不可靠时回退书名号。
    # 这样可以避免修正决定文中的《关于修改...的决定》被误当成主法规名称。
    law_name = ""
    first = next((ln.strip() for ln in head.splitlines() if ln.strip()), "")
    if 5 <= len(first) <= 40 and _TITLE_SUFFIX_RE.search(first):
        law_name = first
    else:
        m = _TITLE_BOOK_RE.search(head)
        if m:
            law_name = m.group(1).strip()

    dates: list[tuple[int, int, int]] = [_parse_date(t) for t in _DATE_RE.findall(head)]

    # 文号：国办发〔2026〕12号 / 国务院令第613号
    doc_number = ""
    for m in _DOC_NUMBER_RE.finditer(head):
        # 形态1：国办发〔2026〕12号
        if m.group(1) and m.group(3):
            doc_number = f"{m.group(1).strip()}〔{m.group(2)}〕{m.group(3)}号"
            break
        # 形态2：国务院令第613号
        if m.group(4) and m.group(5):
            doc_number = f"{m.group(4).strip()}第{m.group(5)}号"
            break

    # 全文补充：查找「修正/修订」与「施行」日期，作为兜底
    for m in _DATE_RE.finditer(text):
        ctx = text[max(0, m.start() - 15) : m.end() + 20]
        if any(k in ctx for k in ("修正", "修订", "通过", "施行")):
            dates.append(_parse_date(m.groups()))

    latest: tuple[int, int, int] | None = max(dates) if dates else None
    out: dict[str, Any] = {}
    if law_name:
        out["law_name"] = law_name
    if doc_number:
        out["doc_number"] = doc_number
    if latest:
        out["latest_date"] = f"{latest[0]:04d}-{latest[1]:02d}-{latest[2]:02d}"
    return out if out else None


def _format_meta(meta: dict[str, Any]) -> str:
    parts = []
    if meta.get("law_name"):
        parts.append(f"《{meta['law_name']}》")
    if meta.get("doc_number"):
        parts.append(meta["doc_number"])
    if meta.get("latest_date"):
        parts.append(f"版本 {meta['latest_date']}")
    return " ".join(parts)


def _is_stale(meta: dict[str, Any]) -> tuple[bool, int]:
    """基于 latest_date 判断法规是否可能已过时；返回 (是否过时, 距今年数)。"""
    latest = meta.get("latest_date")
    if not latest:
        return False, 0
    try:
        from datetime import date, datetime

        d = datetime.strptime(str(latest), "%Y-%m-%d").date()
        years = (date.today() - d).days / 365.25
        return years > _STALENESS_YEARS, int(years)
    except Exception:  # noqa: BLE001
        return False, 0


def _stale_warning(meta: dict[str, Any]) -> str:
    """若法规版本老旧，返回警告文案；否则返回空串。"""
    stale, years = _is_stale(meta)
    if not stale:
        return ""
    title = f"《{meta.get('law_name')}》" if meta.get("law_name") else f"{meta.get('filename', '该文件')}"
    return (
        f"{title} 最近版本信息为 {meta.get('latest_date')}，距今已 {years} 年，"
        "可能已被修订或废止，请以现行有效版本为准。"
    )


# --------------------------------------------------------------------------- #
# 规则归一化
# --------------------------------------------------------------------------- #
_ARTICLE_NO_RE = re.compile(r"第[一二三四五六七八九十百千零〇0-9]{1,12}条")
_WS_RE = re.compile(r"[\s　]+")
_PUNCT_RE = re.compile(r"[，。；、：（）()【】\[\]“”\"'’‘·—\-—\s　]")


def _clean(text: Any, limit: int) -> str:
    s = _WS_RE.sub(" ", str(text or "")).strip()
    return s[:limit]


def _norm_name(name: str) -> str:
    return _PUNCT_RE.sub("", str(name or "")).lower()


def _has_quantity(rule: dict[str, Any]) -> bool:
    blob = f"{rule.get('description','')} {' '.join(rule.get('checkpoints') or [])}"
    return bool(re.search(r"\d", blob)) or bool(re.search(r"(百分之|‰|%|万元|工作日|日历天|日内)", blob))


def _sanitize_structured(raw: Any) -> dict[str, Any] | None:
    """白名单校验并归一化 structured / structured_hint 字段。

    复用 ``rules_store._normalize_structured``：只接受确定性引擎支持的条件形态
    （forbid_keywords / require_elements / regex_patterns / amount_thresholds /
    amount_pair_diff / consistency_elements），其余一律丢弃，防止模型幻觉字段
    直接进入确定性引擎。返回 None 表示无有效条件。
    """
    if not isinstance(raw, dict):
        return None
    from . import rules_store  # 延迟导入，与 promote_to_ruleset 一致

    try:
        return rules_store._normalize_structured(raw)
    except Exception:  # noqa: BLE001 —— hint 只是建议，任何异常都静默丢弃
        return None


def coerce_rule(
    raw: Any, *, seq: int, prefix: str, source_file: str, allow_structured: bool = False
) -> dict[str, Any] | None:
    """把模型输出的一条候选规则归一化成引擎可消费的规则 dict；不可核查则返回 None。

    - ``structured_hint``：模型对定量/禁止/必含条件给出的**建议**结构化条件，
      经 ``_sanitize_structured`` 白名单校验后随规则保存，引擎不消费；
      由用户在前端确认后才转为正式 ``structured``（半自动，默认关闭）。
    - ``allow_structured=False``（默认，模型输出路径）：模型直供的 ``structured``
      一律剥离——模型不可自行激活确定性引擎（防提示注入/幻觉锁定结论）。
    - ``allow_structured=True``（用户编辑路径 update_ruleset）：用户显式提交的
      ``structured``（通常由前端从 structured_hint 复制）经白名单校验后保留，
      该规则即转为确定性规则，审核时由 ``_apply_structured_rules`` 锁定结论。
    """
    if not isinstance(raw, dict):
        return None
    name = _clean(raw.get("name"), 60)
    if not name:
        return None

    category = str(raw.get("category") or "").strip()
    if category not in ALLOWED_CATEGORIES:
        category = "legal"
    severity = str(raw.get("severity") or "").strip()
    if severity not in ALLOWED_SEVERITIES:
        severity = "major"

    checkpoints: list[str] = []
    seen: set[str] = set()
    for c in (raw.get("checkpoints") or []):
        c = _clean(c, 200)
        if c and c not in seen:
            seen.add(c)
            checkpoints.append(c)
        if len(checkpoints) >= 6:
            break
    if not checkpoints:
        return None  # 无审核要点 = 不可核查，直接丢弃

    # 防误判一致性规则：rules_store.is_consistency_rule 在「name 含『一致』且
    # category 为空或 qualification」时会命中，进而触发额外的一致性核查阶段。
    if "一致" in name and category in ("", "qualification"):
        category = "legal"

    # ---- 半自动结构化：hint 始终保留（白名单校验），structured 仅用户路径可激活 ----
    hint = _sanitize_structured(raw.get("structured_hint"))
    structured = _sanitize_structured(raw.get("structured")) if allow_structured else None

    return {
        "id": f"{prefix}-{seq:03d}",
        "name": name,
        "category": category,
        "severity": severity,
        "description": _clean(raw.get("description"), 500),
        "checkpoints": checkpoints,
        # 依据已随规则内嵌，无需再检索外部知识库（避免多一轮 50~80s 的 KB 查询）
        "need_legal_basis": False,
        "enabled": True,
        "builtin": False,
        # doc_types 是用户自定义文件类型 id，模型无从得知，留空 = 适用于全部文件
        "doc_types": [],
        # ---- 以下为临时规则集专有字段，引擎不消费，仅供报告/前端展示 ----
        "legal_basis": _clean(raw.get("legal_basis"), 300),
        "applies_to": str(raw.get("applies_to") or "both").strip() or "both",
        "source_file": source_file,
        "auto_generated": True,
        # 确定性引擎（_apply_structured_rules）只消费 structured；hint 仅供前端提示
        **({"structured_hint": hint} if hint else {}),
        **({"structured": structured} if structured else {}),
    }


def _rule_score(rule: dict[str, Any]) -> tuple:
    """可核查性打分（降序排列用）：严重级别 > 含量化阈值 > 要点数。"""
    return (
        _SEVERITY_WEIGHT.get(rule.get("severity"), 1),
        1 if _has_quantity(rule) else 0,
        len(rule.get("checkpoints") or []),
    )


def _article_no(text: str) -> str:
    """从依据文本中提取条款号（如「第二十六条」）；无则返回空串。"""
    m = _ARTICLE_NO_RE.search(str(text or ""))
    return m.group(0) if m else ""


def dedup_rules(rules: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """全局去重：先按「条款号+规则名」精确去重，再按规则名模糊相似去重。

    相似判定用 difflib 的序列比（不引入额外依赖、无需 embedding）。
    保留信息更全（要点更多）的那条，并把被合并掉的依据条款号附加上。

    **阈值分档**：同一条款号下的两条规则，措辞差异（如「2%」与「百分之二」）
    更可能只是同一要求的不同写法，故阈值放宽到 0.70；跨条款的规则要判为重复
    则需要 0.86 的高相似度，避免把「投标保证金上限」与「履约保证金上限」
    这类真实存在的不同要求误合并。
    """
    kept: list[dict[str, Any]] = []
    removed = 0

    exact: dict[str, dict[str, Any]] = {}
    for r in rules:
        key = f"{_article_no(r.get('legal_basis'))}|{_norm_name(r.get('name'))}"
        prev = exact.get(key)
        if prev is None:
            exact[key] = r
            continue
        removed += 1
        if len(r.get("checkpoints") or []) > len(prev.get("checkpoints") or []):
            r["legal_basis"] = _merge_basis(r.get("legal_basis"), prev.get("legal_basis"))
            exact[key] = r
        else:
            prev["legal_basis"] = _merge_basis(prev.get("legal_basis"), r.get("legal_basis"))

    # 模糊去重：规则名高度相似视为同一要求
    for r in exact.values():
        n1 = _norm_name(r.get("name"))
        a1 = _article_no(r.get("legal_basis"))
        dup_of = None
        for k in kept:
            n2 = _norm_name(k.get("name"))
            if not n1 or not n2:
                continue
            a2 = _article_no(k.get("legal_basis"))
            threshold = 0.70 if (a1 and a1 == a2) else 0.86
            if difflib.SequenceMatcher(None, n1, n2).ratio() > threshold:
                dup_of = k
                break
        if dup_of is None:
            kept.append(r)
            continue
        removed += 1
        dup_of["legal_basis"] = _merge_basis(dup_of.get("legal_basis"), r.get("legal_basis"))
        # 合并要点（去重后不超过 6 条）
        merged = list(dup_of.get("checkpoints") or [])
        for c in r.get("checkpoints") or []:
            if c not in merged:
                merged.append(c)
        dup_of["checkpoints"] = merged[:6]
        if _SEVERITY_WEIGHT.get(r.get("severity"), 1) > _SEVERITY_WEIGHT.get(
            dup_of.get("severity"), 1
        ):
            dup_of["severity"] = r["severity"]

    return kept, removed


def _merge_basis(a: str | None, b: str | None) -> str:
    parts = [p for p in (str(a or "").strip(), str(b or "").strip()) if p]
    if not parts:
        return ""
    out = parts[0]
    for p in parts[1:]:
        if p not in out:
            out = f"{out}；{p}"[:300]
    return out


# --------------------------------------------------------------------------- #
# 挖矿主流程
# --------------------------------------------------------------------------- #
def _meta_identity_keys(metas: list[dict[str, Any]]) -> set[tuple[str, str]]:
    """提取法规版本信息的「身份键」集合：(法规名称, 文号)，剔除双空项。"""
    out: set[tuple[str, str]] = set()
    for m in metas or []:
        key = (
            str(m.get("law_name") or "").strip(),
            str(m.get("doc_number") or "").strip(),
        )
        if any(key):
            out.add(key)
    return out


def _text_fp(text: str) -> str:
    """正文内容指纹：去除全部空白字符后取 md5。

    用于复用判定的内容一致性校验——同名同文号的法规，正文指纹一致才允许复用
    历史解析结果。去空白可规避 PDF/DOCX 不同提取器带来的换行/空格差异，
    实质文字变化（修订、节选、缺章）必然导致指纹不同。
    """
    import re as _re

    return hashlib.md5(_re.sub(r"\s+", "", text or "").encode("utf-8")).hexdigest()


def _find_reusable_ruleset(
    *,
    source_files: list[dict[str, Any]],
    source_meta: list[dict[str, Any]],
    fingerprint: str,
    user_id: str | None,
) -> tuple[dict[str, Any] | None, str]:
    """查找可复用的已就绪解析结果（跳过重复解析）。

    匹配优先级：
    1. 来源文件+参数指纹完全一致（原有行为）；
    2. 法规文件 MD5 集合与已有解析记录相同（参数变化也复用）；
    3. 法规版本信息（法规名称+文号）匹配 **且逐文件通过正文内容指纹校验**。
    返回 (规则集记录, 复用依据)；无可复用时 (None, "")。

    第 3 级安全语义（2026-09-05 修复）：仅身份键重叠不再复用——同名法规的不同文本
    （修订稿、节选、不同来源提取版本）会被误复用。现在要求新上传的 **每个** 文件
    的身份键与正文指纹（_text_fp，去空白 md5）都在既有记录中得到确认，任一文件
    无法确认一致（指纹不同、或历史记录缺指纹且原文已不可取）即不复用，走正常解析。
    """
    with _lock:
        candidates = [
            json.loads(json.dumps(r))
            for r in _items.values()
            if r.get("status") == "ready" and _accessible(r, user_id)
        ]

    # 1) 精确指纹
    for r in candidates:
        if r.get("sources_fingerprint") == fingerprint:
            return r, "来源文件与解析参数完全一致"

    # 2) 文件 MD5 集合相同
    new_md5 = {str(f.get("md5") or "") for f in source_files} - {""}
    if new_md5:
        for r in candidates:
            old_md5 = {str(f.get("md5") or "") for f in r.get("source_files") or []} - {""}
            if old_md5 and (old_md5 == new_md5 or new_md5 <= old_md5):
                return r, "法规文件 MD5 与已有解析记录相同"

    # 3) 法规版本信息匹配 + 正文内容一致性逐文件确认
    new_fps: dict[tuple[str, str], str] = {}
    for m in source_meta or []:
        key = (str(m.get("law_name") or "").strip(), str(m.get("doc_number") or "").strip())
        fp = str(m.get("text_fp") or "")
        if any(key) and fp:
            new_fps[key] = fp
    # 每个新文件都必须有可确认的身份（缺元数据的文件无法做版本比对，直接放弃第 3 级）
    if new_fps and len(new_fps) == len(source_files):
        for r in candidates:
            old_fps: dict[tuple[str, str], str] = {}
            for m in r.get("source_meta") or []:
                key = (
                    str(m.get("law_name") or "").strip(),
                    str(m.get("doc_number") or "").strip(),
                )
                if not any(key):
                    continue
                fp = str(m.get("text_fp") or "")
                if not fp:
                    # 历史记录缺正文指纹（旧版本数据）：尝试用 file_store 现算；
                    # 原文已不可取则该键视为无法确认 → 不复用（宁可重解析）
                    old_rec = file_store.get(str(m.get("file_id") or ""))
                    if old_rec:
                        fp = _text_fp(old_rec.get("text") or "")
                if fp:
                    old_fps[key] = fp
            if new_fps and all(old_fps.get(k) == fp for k, fp in new_fps.items()):
                law_name = next(iter(new_fps))[0]
                return r, f"法规《{law_name}》身份匹配且正文内容一致性校验通过"
    return None, ""


def _sources_fingerprint(source_files: list[dict[str, Any]], params: dict[str, Any]) -> str:
    payload = json.dumps(
        {
            "files": sorted(
                (f.get("md5") or "", f.get("file_id") or "") for f in source_files
            ),
            "params": params,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


async def _extract_chunk(
    chunk: str,
    *,
    index: int,
    total: int,
    mode: str,
    source_file: str,
    prefix: str,
    start_seq: int,
    sem: asyncio.Semaphore,
    timeout: float,
) -> tuple[list[dict[str, Any]], str | None]:
    """抽取单个分块的候选规则；返回 (规则列表, 失败原因|None)。"""
    messages = [
        {"role": "system", "content": _mining_system()},
        {"role": "user", "content": _mining_user_message(chunk, mode=mode, index=index, total=total)},
    ]
    async with sem:
        try:
            use_stream = bool(config.get("legal_mining_stream", True))
            msg = await llm_client.chat(
                messages,
                temperature=0.0,  # 抽取任务要稳定可复现
                timeout=timeout,
                stream=use_stream,
            )
        except Exception as exc:  # noqa: BLE001 - 单块失败不影响整批
            logger.warning("法规分块 %d/%d 抽取失败: %s", index, total, exc)
            return [], f"第{index}块: {type(exc).__name__}"

    try:
        data = llm_client.parse_json(msg.get("content") or "")
    except Exception as exc:  # noqa: BLE001
        logger.warning("法规分块 %d/%d 输出解析失败: %s", index, total, exc)
        return [], f"第{index}块: 输出非 JSON"

    if isinstance(data, dict):
        raws = data.get("rules") or []
    elif isinstance(data, list):
        raws = data
    else:
        raws = []
    if not isinstance(raws, Iterable):
        raws = []

    out: list[dict[str, Any]] = []
    for seq, raw in enumerate(raws, start=start_seq + 1):
        rule = coerce_rule(raw, seq=seq, prefix=prefix, source_file=source_file)
        if rule:
            out.append(rule)
    return out, None


async def _run_mining(rid: str) -> None:
    """后台执行规则挖矿，把进度写入记录。"""
    t0 = time.monotonic()
    try:
        with _lock:
            rec = _items.get(rid)
            if not rec:
                return
            rec["status"] = "mining"
            rec["progress"] = 2.0
            rec["progress_message"] = "正在解析法规文件"
            rec["updated_at"] = _now()
            rec["logs"] = []
            _log(
                rec,
                f"开始解析 {len(rec.get('source_files') or [])} 个法规文件，"
                f"模型 {rec.get('params', {}).get('llm_model') or config.get('llm_model')}",
            )
            _save()

        # 1) 取源文本
        texts: list[tuple[str, str]] = []  # (filename, text)
        warnings: list[str] = []
        max_chars = int(config.get("legal_max_source_chars", 600000))
        total_chars = 0
        for f in rec.get("source_files", []):
            record = file_store.get(f.get("file_id") or "")
            if not record:
                warnings.append(f"文件已失效，已跳过：{f.get('filename')}")
                _log_rid(rid, f"文件已失效，已跳过：{f.get('filename')}", "warn")
                continue
            text = (record.get("text") or "").strip()
            if not text:
                warnings.append(f"未提取到可解析文本，已跳过：{f.get('filename')}")
                _log_rid(rid, f"未提取到可解析文本，已跳过：{f.get('filename')}", "warn")
                continue
            if len(text) > max_chars:
                text = text[:max_chars]
                warnings.append(
                    f"《{f.get('filename')}》超过处理上限 {max_chars} 字，"
                    f"已截断（超出部分未参与规则抽取）"
                )
                _log_rid(
                    rid,
                    f"《{f.get('filename')}》{len(text):,} 字，超过上限 {max_chars:,} 字已截断",
                    "warn",
                )
            else:
                _log_rid(rid, f"解析《{f.get('filename')}》：{len(text):,} 字", "ok")
            texts.append((str(f.get("filename") or record.get("filename") or ""), text))
            total_chars += len(text)

        if not texts:
            raise RuntimeError("没有可解析的法规文本内容")

        # 2) 切分打包
        budget = int(config.get("legal_chunk_chars", 12000))
        mode = str(rec.get("params", {}).get("mode") or "bid")
        jobs: list[tuple[str, str, int, int]] = []  # (filename, chunk, idx, total)
        seg_modes: set[str] = set()
        for fname, text in texts:
            segs, how = split_legal_text(text)
            seg_modes.add(how)
            if how == "paragraph":
                warnings.append(
                    f"《{fname}》未识别到「第X条/第X章」结构，已按段落切分，"
                    "条款边界可能不够精确"
                )
                _log_rid(
                    rid,
                    f"《{fname}》未识别到「第X条/第X章」结构，按段落切分（边界可能不够精确）",
                    "warn",
                )
            chunks = pack_chunks(segs, budget)
            _log_rid(rid, f"《{fname}》切分为 {len(chunks)} 个文本块（{how}）")
            for i, c in enumerate(chunks, 1):
                jobs.append((fname, c, i, len(chunks)))

        if not jobs:
            raise RuntimeError("切分后无可抽取的文本块")

        with _lock:
            rec = _items.get(rid)
            if not rec:
                return
            rec["stats"].update(
                {
                    "source_chars": total_chars,
                    "chunks": len(jobs),
                    "split_mode": "/".join(sorted(seg_modes)),
                }
            )
            rec["progress"] = 5.0
            rec["progress_message"] = f"已切分为 {len(jobs)} 个文本块，开始抽取规则"
            rec["updated_at"] = _now()
            _log(
                rec,
                f"共切分 {len(jobs)} 个文本块（每块约 {budget} 字，"
                f"并发 {max(1, int(config.get('legal_mining_concurrency', 3)))}），开始逐块抽取规则",
            )
            _save()

        # 3) 并发抽取
        sem = asyncio.Semaphore(max(1, int(config.get("legal_mining_concurrency", 3))))
        timeout = float(config.get("legal_mining_timeout", 180))
        short = rid.replace("lrs-", "")[:6]

        async def _one(job: tuple[str, str, int, int], slot: int) -> tuple[list[dict], str | None]:
            fname, chunk, idx, total = job
            with _lock:
                cur0 = _items.get(rid)
                if cur0:
                    cur0["stats"]["chunks_started"] = cur0["stats"].get("chunks_started", 0) + 1
                    _log(cur0, f"第 {idx}/{total} 块开始抽取（{len(chunk)} 字）…")
                    cur0["progress"] = _chunk_progress(cur0, total)
                    cur0["progress_message"] = f"正在抽取第 {idx}/{total} 个文本块"
                    cur0["updated_at"] = _now()
                    _save()

            t1 = time.monotonic()
            rules, err = await _extract_chunk(
                chunk,
                index=idx,
                total=total,
                mode=mode,
                source_file=fname,
                prefix=f"lr-{short}",
                start_seq=slot * 100,
                sem=sem,
                timeout=timeout,
            )
            dur = time.monotonic() - t1

            with _lock:
                cur = _items.get(rid)
                if cur:
                    cur["stats"]["chunks_done"] = cur["stats"].get("chunks_done", 0) + 1
                    done = cur["stats"]["chunks_done"]
                    if err:
                        _log(cur, f"第 {idx}/{total} 块抽取失败（{dur:.0f}s）：{err}", "warn")
                    elif rules:
                        _log(
                            cur,
                            f"第 {idx}/{total} 块完成：抽取 {len(rules)} 条规则（{dur:.0f}s）",
                            "ok",
                        )
                    else:
                        _log(cur, f"第 {idx}/{total} 块未抽取到规则（{dur:.0f}s）", "warn")
                    cur["progress"] = _chunk_progress(cur, total)
                    cur["progress_message"] = f"已抽取 {done}/{total} 个文本块"
                    cur["stats"]["elapsed_sec"] = round(time.monotonic() - t0, 1)
                    cur["updated_at"] = _now()
                    _save()
            return rules, err

        results = await asyncio.gather(*[_one(j, i) for i, j in enumerate(jobs)])

        raw_rules: list[dict[str, Any]] = []
        errors: list[str] = []
        for rules, err in results:
            raw_rules.extend(rules)
            if err:
                errors.append(err)

        # 4) 去重 / 打分 / 截断
        with _lock:
            rec = _items.get(rid)
            if not rec:
                return
            rec["progress"] = 90.0
            rec["progress_message"] = "正在合并去重"
            rec["updated_at"] = _now()
            _log(rec, f"抽取结束，共 {len(raw_rules)} 条原始规则，开始合并去重")
            _save()

        kept, removed = dedup_rules(raw_rules)
        kept.sort(key=_rule_score, reverse=True)
        max_rules = int(rec.get("params", {}).get("max_rules") or config.get("legal_max_rules", 30))
        truncated = 0
        _log_rid(
            rid,
            f"合并去重完成：{len(raw_rules)} 条 → {len(kept)} 条（移除 {removed} 条重复）",
        )
        if len(kept) > max_rules:
            truncated = len(kept) - max_rules
            kept = kept[:max_rules]
            warnings.append(
                f"抽取到 {len(kept) + truncated} 条规则，超过上限 {max_rules}，"
                f"已按「严重级别 + 可量化程度」保留前 {max_rules} 条"
            )
            _log_rid(
                rid,
                f"超过上限 {max_rules} 条，按「严重级别 + 可量化程度」保留前 {max_rules} 条"
                f"（丢弃 {truncated} 条）",
                "warn",
            )
        if errors:
            warnings.append(f"{len(errors)} 个文本块抽取失败，该部分条款未纳入规则集")
            _log_rid(rid, f"{len(errors)} 个文本块抽取失败，该部分条款未纳入规则集", "warn")
        if not kept:
            raise RuntimeError("未能从该文件中抽取到任何可核查的规则")

        # 重排序号：保证「同输入 → 同 id」，避免并发完成顺序影响规则 id，
        # 使同一份法规文件多次生成的规则集可复现、可跨任务比对。
        for i, r in enumerate(kept, 1):
            r["id"] = f"lr-{short}-{i:03d}"

        with _lock:
            rec = _items.get(rid)
            if not rec:
                return
            rec["rules"] = kept
            rec["status"] = "ready"
            rec["progress"] = 100.0
            rec["progress_message"] = f"已生成 {len(kept)} 条规则"
            rec["warnings"] = warnings
            _log(
                rec,
                f"生成完成：共 {len(kept)} 条规则，耗时 {time.monotonic() - t0:.0f}s",
                "ok",
            )
            rec["stats"].update(
                {
                    "raw_rules": len(raw_rules),
                    "dedup_removed": removed,
                    "kept": len(kept),
                    "truncated": truncated,
                    "failed_chunks": len(errors),
                    "elapsed_sec": round(time.monotonic() - t0, 1),
                }
            )
            rec["error"] = None
            rec["updated_at"] = _now()
            _save()
        logger.info(
            "法规规则集 %s 生成完成：%d 条（原始 %d，去重 %d，耗时 %.1fs）",
            rid, len(kept), len(raw_rules), removed, time.monotonic() - t0,
        )
    except Exception as exc:  # noqa: BLE001 - 保证进入终态
        logger.exception("法规规则集 %s 生成失败", rid)
        with _lock:
            rec = _items.get(rid)
            if rec:
                rec["status"] = "failed"
                rec["error"] = str(exc)
                rec["progress_message"] = f"生成失败：{exc}"
                rec["stats"]["elapsed_sec"] = round(time.monotonic() - t0, 1)
                _log(rec, f"生成失败：{exc}", "error")
                rec["updated_at"] = _now()
                _save()


async def create_ruleset(
    *,
    name: str,
    file_ids: list[str],
    mode: str = "bid",
    max_rules: int | None = None,
    description: str = "",
    user_id: str | None = None,
    is_shared: bool = False,
    reuse: bool = True,
) -> dict[str, Any]:
    """创建并（后台）启动一个临时规则集挖矿任务，立即返回记录。"""
    if not file_ids:
        raise ValueError("请至少选择一个法规文件")
    if mode not in ("bid", "tender", "general"):
        mode = "bid"

    source_files: list[dict[str, Any]] = []
    source_meta: list[dict[str, Any]] = []
    for fid in file_ids:
        rec = file_store.get(fid)
        if not rec:
            raise ValueError(f"文件不存在或已过期: {fid}")
        text = (rec.get("text") or "").strip()
        if not text:
            raise ValueError(f"文件未提取到可解析文本: {rec.get('filename')}")
        filename = rec.get("filename") or fid
        meta = _extract_doc_meta(text)
        if meta:
            source_meta.append(
                {
                    "file_id": fid,
                    "filename": filename,
                    "text_fp": _text_fp(text),
                    **meta,
                }
            )
        source_files.append(
            {
                "file_id": fid,
                "filename": filename,
                "md5": rec.get("md5") or "",
                "char_count": int(rec.get("char_count") or 0),
            }
        )

    params = {
        "mode": mode,
        "max_rules": int(max_rules or config.get("legal_max_rules", 30)),
        "chunk_chars": int(config.get("legal_chunk_chars", 12000)),
        "llm_model": config.get("llm_model"),
    }
    fp = _sources_fingerprint(source_files, params)

    # ---- 复用判定：命中已有解析结果 → 直接返回，跳过重复解析 ----
    # 匹配优先级：来源+参数指纹 > 文件 MD5 集合 > 法规版本信息（法规名+文号）。
    if reuse:
        reused, reuse_reason = _find_reusable_ruleset(
            source_files=source_files,
            source_meta=source_meta,
            fingerprint=fp,
            user_id=user_id,
        )
        if reused:
            # 在既有记录上留一条持久化复用痕迹（卡片日志面板可见）
            _log_rid(
                reused["id"],
                f"新上传法规命中本解析结果（{reuse_reason}），直接复用，跳过重复解析",
                "ok",
            )
            out = json.loads(json.dumps(reused))
            out["reused"] = True
            out["reuse_reason"] = reuse_reason
            out["logs"] = [
                {
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "text": f"命中已有解析结果（{reuse_reason}），直接复用，跳过重复解析",
                    "level": "ok",
                }
            ]
            return out

    rid = f"lrs-{uuid.uuid4().hex[:12]}"
    rec: dict[str, Any] = {
        "id": rid,
        "name": (name or "").strip() or f"法规临时规则集 {rid[-6:]}",
        "description": (description or "").strip(),
        "status": "pending",
        "progress": 0.0,
        "progress_message": "排队中",
        "error": None,
        "owner_id": user_id,
        "is_shared": bool(is_shared),
        "created_at": _now(),
        "updated_at": _now(),
        "version": 1,
        "source_files": source_files,
        "sources_fingerprint": fp,
        "source_meta": source_meta,
        "params": params,
        "rules": [],
        "logs": [],
        "warnings": [
            _stale_warning(m)
            for m in source_meta
            if _stale_warning(m)
        ],
        "stats": {
            "source_chars": 0,
            "chunks": 0,
            "chunks_started": 0,
            "chunks_done": 0,
            "raw_rules": 0,
            "dedup_removed": 0,
            "kept": 0,
            "truncated": 0,
            "failed_chunks": 0,
            "elapsed_sec": 0.0,
            "split_mode": "",
        },
    }
    _persist(rec)

    task = asyncio.create_task(_run_mining(rid))
    _background.add(task)
    task.add_done_callback(_background.discard)
    return json.loads(json.dumps(rec))


def update_ruleset(
    ruleset_id: str,
    *,
    user_id: str | None = None,
    name: str | None = None,
    description: str | None = None,
    rules: list[dict[str, Any]] | None = None,
    is_shared: bool | None = None,
) -> dict[str, Any] | None:
    """编辑临时规则集（改名/改说明/整体替换规则/改共享范围）。

    规则经人工编辑后 ``version`` 递增，用于报告溯源与可复现校验。
    """
    with _lock:
        rec = _items.get(ruleset_id)
        if not rec or not _accessible(rec, user_id):
            return None
        if user_id is not None and rec.get("owner_id") != user_id:
            return None
        if name is not None:
            rec["name"] = str(name).strip() or rec["name"]
        if description is not None:
            rec["description"] = str(description).strip()
        if is_shared is not None:
            rec["is_shared"] = bool(is_shared)
        if rules is not None:
            cleaned: list[dict[str, Any]] = []
            short = ruleset_id.replace("lrs-", "")[:6]
            for i, raw in enumerate(rules, 1):
                rule = coerce_rule(
                    raw,
                    seq=i,
                    prefix=f"lr-{short}",
                    source_file=str(raw.get("source_file") or ""),
                    # 用户编辑路径：允许显式提交 structured（半自动结构化的「确认」动作），
                    # 前端把 structured_hint 复制为 structured 后 PATCH 即转为确定性规则。
                    allow_structured=True,
                )
                if rule:
                    cleaned.append(rule)
            rec["rules"] = cleaned
            rec["stats"]["kept"] = len(cleaned)
            rec["version"] = int(rec.get("version") or 1) + 1
            _log(
                rec,
                f"人工编辑：保留 {len(cleaned)} 条规则，版本更新为 v{rec['version']}",
                "ok",
            )
        rec["status"] = "ready"
        rec["progress"] = 100.0
        return _persist(rec)


def resolve_rules(
    ruleset_ids: list[str], user_id: str | None = None
) -> tuple[list[dict[str, Any]], list[str]]:
    """把多个临时规则集解析成引擎可直接消费的规则列表（按 id 去重）。

    返回 (规则列表, 未找到/无权限的规则集 id 列表)。
    """
    rules: list[dict[str, Any]] = []
    missing: list[str] = []
    seen: set[str] = set()
    for rid in ruleset_ids or []:
        rec = get_set(rid, user_id)
        if not rec:
            missing.append(rid)
            continue
        if rec.get("status") != "ready":
            missing.append(rid)
            continue
        for r in rec.get("rules") or []:
            rid_ = str(r.get("id") or "")
            if not rid_ or rid_ in seen:
                continue
            # id 加规则集前缀，避免两个法规集之间 id 撞车
            rule = dict(r)
            rule["id"] = f"{rid}:{rid_}" if not rid_.startswith(f"{rid}:") else rid_
            rule["enabled"] = bool(r.get("enabled", True))
            seen.add(rule["id"])
            rules.append(rule)
    return rules, missing


def promote_to_ruleset(
    ruleset_id: str, *, user_id: str | None = None, name: str | None = None
) -> dict[str, Any] | None:
    """「转正」：把临时规则集固化为正式自定义规则集（rules_store）。

    依据条款会并入 description，保证正式规则集脱离临时集后仍可溯源。
    """
    from . import rules_store  # 延迟导入，避免循环依赖

    rec = get_set(ruleset_id, user_id)
    if not rec or rec.get("status") != "ready":
        return None
    rules: list[dict[str, Any]] = []
    for r in rec.get("rules") or []:
        basis = str(r.get("legal_basis") or "").strip()
        desc = str(r.get("description") or "").strip()
        if basis and basis not in desc:
            desc = f"{desc}（依据：{basis}）".strip()
        rules.append(
            {
                "id": str(r.get("id") or ""),
                "name": r.get("name"),
                "category": r.get("category") if r.get("category") in ALLOWED_CATEGORIES else "legal",
                "severity": r.get("severity") if r.get("severity") in ALLOWED_SEVERITIES else "major",
                "description": desc,
                "checkpoints": list(r.get("checkpoints") or []),
                "need_legal_basis": False,
                "enabled": bool(r.get("enabled", True)),
                "builtin": False,
                "doc_types": [],
                # 已激活的确定性条件随转正保留（save_ruleset 内部会再次白名单归一化）
                **({"structured": r["structured"]} if r.get("structured") else {}),
            }
        )
    if not rules:
        return None
    saved = rules_store.save_ruleset(
        {
            "id": f"rs-legal-{uuid.uuid4().hex[:8]}",
            "name": (name or rec.get("name") or "法规派生规则集").strip(),
            "description": rec.get("description") or f"由法规文件自动生成（来源 {rec['id']}）",
            "rules": rules,
            "builtin": False,
        },
        user_id=user_id,
        is_shared=bool(rec.get("is_shared")),
    )
    return saved
