"""在已解析文档中定位文本片段，返回带上下文的原文位置。

模型给出的 evidence 往往是摘录或轻微改写，因此采用三级匹配：
精确匹配 -> 归一化匹配（忽略空白与全角标点差异）-> 最长公共子串模糊匹配。
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

# doc_parser 在解析时插入的位置标记（PDF 分页 / Excel 表格 / DOCX 章节）
# 注意：章节分支整段用捕获组包住，否则 (\d+) 会被误当作第 4 个捕获组只返回数字。
_MARKER_RE = re.compile(
    r"\[(?:第(\d+)页|表格(\d+)|工作表:\s*([^\]]+)|(第\d+章[^\]]*))\]"
)

# 全角标点归一化为半角，消除模型改写引入的差异
_PUNCT_MAP = {
    "，": ",", "。": ".", "：": ":", "；": ";", "！": "!", "？": "?",
    "（": "(", "）": ")", "【": "[", "】": "]", "、": ",",
    "“": '"', "”": '"', "‘": "'", "’": "'", "％": "%", "－": "-",
}

# 模糊匹配时公共子串至少要覆盖片段的比例
_MIN_FUZZY_RATIO = 0.4

# 模糊匹配时评估的候选出现上限，避免重复短语（如「投标人应按…」）在长文档中
# 产生成千上万次命中导致线性膨胀。
_MAX_OCCURRENCES = 80

# 消歧打分时在每次出现周围取该宽度的上下文窗口（与用户可见上下文量级一致），
# 以确保能覆盖到区分各实例的唯一文本（如某条条款后紧邻的特殊说明）。
_SCORE_WIN = 240


def _build_normalized(text: str) -> tuple[str, list[int]]:
    """归一化文本并保留到原文的下标映射。

    Returns:
        (归一化串, index_map)，index_map[i] 为归一化串第 i 位在原文中的下标。
    """
    chars: list[str] = []
    index_map: list[int] = []
    for idx, ch in enumerate(text):
        if ch.isspace():
            continue
        chars.append(_PUNCT_MAP.get(ch, ch))
        index_map.append(idx)
    return "".join(chars), index_map


def _detect_location(text: str, pos: int) -> str:
    """回溯最近的位置标记，推断片段所在页/表格/工作表/章节。"""
    label = ""
    for match in _MARKER_RE.finditer(text, 0, pos + 1):
        page, table, sheet, chapter = match.groups()
        if page:
            label = f"第{page}页"
        elif table:
            label = f"表格{table}"
        elif sheet:
            label = f"工作表 {sheet}"
        elif chapter:
            label = chapter  # 形如「第1章: 投标函」
    return label


def _scope_region(text: str, location: str | None) -> tuple[int, int] | None:
    """根据模型标注的位置（如「第3页」「表格2」「第1章」）限定检索区间。

    返回 (start, end) 绝对下标；无法解析或文档中找不到对应标记时返回 None，
    此时调用方应回退到全文检索。
    """
    if not location:
        return None
    marker: str | None = None
    m = re.search(r"第\s*(\d+)\s*页", location)
    if m:
        marker = f"[第{m.group(1)}页]"
    if marker is None:
        m = re.search(r"表格\s*(\d+)", location)
        if m:
            marker = f"[表格{m.group(1)}]"
    if marker is None:
        m = re.search(r"第?\s*(\d+)\s*章", location)
        if m:
            # 章节标记内还带标题，按前缀匹配即可
            marker = f"[第{m.group(1)}章"
    if marker is None:
        return None
    start = text.find(marker)
    if start < 0:
        return None
    nxt = text.find("\n[", start + 1)
    end = nxt if nxt >= 0 else len(text)
    return (start, end)


def _match_in_region(
    text: str,
    snippet: str,
    rstart: int,
    rend: int,
    candidates: list[str] | None,
) -> tuple[int, int, str, float] | None:
    """在 text[rstart:rend] 内做三级匹配，返回绝对下标 (start, end, type, confidence)。"""
    seg = text[rstart:rend]
    found = _exact_match(seg, snippet)
    if found is not None:
        return (rstart + found[0], rstart + found[1], "exact", 1.0)
    found = _normalized_match(seg, snippet)
    if found is not None:
        return (rstart + found[0], rstart + found[1], "normalized", 0.9)
    fuzzy = _fuzzy_match(seg, snippet, candidates)
    if fuzzy is not None:
        s, e, ratio = fuzzy
        return (rstart + s, rstart + e, "fuzzy", ratio)
    return None


def _exact_match(text: str, snippet: str) -> tuple[int, int] | None:
    pos = text.find(snippet)
    return (pos, pos + len(snippet)) if pos >= 0 else None


def _normalized_match(text: str, snippet: str) -> tuple[int, int] | None:
    norm_text, index_map = _build_normalized(text)
    norm_snippet, _ = _build_normalized(snippet)
    if not norm_snippet:
        return None
    pos = norm_text.find(norm_snippet)
    if pos < 0:
        return None
    start = index_map[pos]
    end_idx = pos + len(norm_snippet) - 1
    end = index_map[end_idx] + 1
    return start, end


def _fuzzy_match(
    text: str, snippet: str, candidates: list[str] | None = None
) -> tuple[int, int, float] | None:
    """用最长公共子串定位，容忍模型改写。

    当公共子串在文档中多次出现（如重复条款「投标人应按…」）时，仅凭首个命中
    往往会高亮错误实例。这里枚举所有出现位置，用 candidates（finding 的
    evidence→detail→title 渐进上下文）对每个出现的上下文做相似度打分，挑选
    与原文最契合的实例返回。
    """
    norm_text, index_map = _build_normalized(text)
    norm_snippet, _ = _build_normalized(snippet)
    if not norm_snippet or not norm_text:
        return None

    matcher = SequenceMatcher(None, norm_text, norm_snippet, autojunk=False)
    block = matcher.find_longest_match(0, len(norm_text), 0, len(norm_snippet))
    if block.size == 0:
        return None

    ratio = block.size / len(norm_snippet)
    if ratio < _MIN_FUZZY_RATIO:
        return None

    common = norm_text[block.a : block.a + block.size]

    # 公共子串的所有出现位置（限制上限，避免中文高频短语线性膨胀）
    occurrences: list[int] = []
    for m in re.finditer(re.escape(common), norm_text):
        occurrences.append(m.start())
        if len(occurrences) >= _MAX_OCCURRENCES:
            break
    if not occurrences:
        occurrences = [block.a]

    # candidates 归一化后用于上下文相似度比对
    norm_cands = [
        _build_normalized(c)[0] for c in (candidates or []) if c and c.strip()
    ]

    snip_len = len(norm_snippet)
    best: tuple[float, int, int] | None = None
    for occ in occurrences:
        # 公共子串在不同出现处的上下文可能不同（模型改写导致前后缀有差异），
        # 需在本次出现附近做一次局部对齐，得到正确的扩张边界，而非复用首个对齐的
        # lead/tail（否则会把前一处的前缀误并入，切出乱码 span）。
        win_lo = max(0, occ - snip_len)
        win_hi = min(len(norm_text), occ + snip_len)
        local = SequenceMatcher(
            None, norm_text[win_lo:win_hi], norm_snippet, autojunk=False
        )
        lb = local.find_longest_match(0, win_hi - win_lo, 0, snip_len)
        abs_a = win_lo + lb.a
        lead_l = lb.b
        tail_l = snip_len - (lb.b + lb.size)
        start_norm = max(0, abs_a - lead_l)
        end_norm = min(len(norm_text), abs_a + lb.size + tail_l)

        # 消歧打分用更宽的上下文窗口（能覆盖区分各实例的唯一文本），
        # 与最终返回的紧凑 matched span 解耦，保证高亮边界精确的同时打分稳健。
        score_lo = max(0, occ - _SCORE_WIN)
        score_hi = min(len(norm_text), occ + len(common) + _SCORE_WIN)
        window = norm_text[score_lo:score_hi]
        if norm_cands:
            score = max(
                SequenceMatcher(None, window, c, autojunk=False).ratio()
                for c in norm_cands
            )
        else:
            # 无候选上下文时不消歧，沿用首个命中（与改造前行为一致）
            score = 1.0
        if best is None or score > best[0]:
            best = (score, start_norm, end_norm)

    _, start_norm, end_norm = best
    start = index_map[start_norm]
    end = index_map[end_norm - 1] + 1
    return start, end, round(ratio, 2)


def _try_match(
    text: str,
    snippet: str,
    region: tuple[int, int] | None,
    candidates: list[str] | None,
) -> tuple[int, int, str, float, bool] | None:
    """在 text 中按 region 约束做一次定位；region 内未命中则回退全文。

    返回 (start, end, match_type, confidence, constrained)，constrain 表示是否在
    模型标注区间内命中（用于 page_constrained 标记）。
    """
    if region:
        rstart, rend = region
        found = _match_in_region(text, snippet, rstart, rend, candidates)
        constrained = found is not None
        if not found:
            # 模型标注位置内未命中，回退全文检索
            found = _match_in_region(text, snippet, 0, len(text), candidates)
    else:
        found = _match_in_region(text, snippet, 0, len(text), candidates)
        constrained = False
    if found is None:
        return None
    start, end, mtype, conf = found
    return (start, end, mtype, conf, constrained)


def locate(
    text: str,
    snippet: str,
    context_chars: int = 240,
    location: str | None = None,
    candidates: list[str] | None = None,
) -> dict[str, Any] | None:
    """在文本中定位片段。

    Args:
        text: 文档全文
        snippet: 待定位的片段（模型给出的 evidence）
        context_chars: 命中位置前后各保留的上下文字符数
        location: 模型标注的位置（如「第3页」「第1章」），用于限定检索区间，
            命中失败后回退到全文检索
        candidates: finding 的渐进上下文（evidence→detail→title）。双重作用：
            ① 当主 snippet 未命中时，按顺序回退用其余候选作为 snippet 重试；
            ② 模糊匹配时作为上下文用于多个命中的消歧打分

    Returns:
        命中则返回定位结果，否则 None。
    """
    snippet = (snippet or "").strip()
    if not snippet or not text:
        return None

    model_location = location or None
    region = _scope_region(text, location)

    # 候选 snippet 顺序：主 snippet 优先，其后按前端传入的 candidates
    # （evidence→detail→title）中非空的、且与主 snippet 不同的条目作回退；
    # 任一命中即采用（fix2：evidence 若为模型改写/归纳导致整体检索失败时，
    # 回退 detail/title 往往能命中原文摘录）。
    tried = [snippet]
    seen = {snippet}
    for c in candidates or []:
        cs = (c or "").strip()
        if cs and cs not in seen:
            seen.add(cs)
            tried.append(cs)

    # 消歧上下文用全部候选（含主 snippet），保证模糊匹配时能借助 detail/title
    # 的独有信息挑选正确实例。
    ctx_candidates = [c for c in (candidates or []) if c and c.strip()]

    for snip in tried:
        found = _try_match(text, snip, region, ctx_candidates)
        if found is None:
            continue
        start, end, match_type, confidence, constrained = found

        # 仅当提供了模型位置且确实在该区间内命中时才标记「已限定」
        page_constrained = bool(region) and constrained

        ctx_start = max(0, start - context_chars)
        ctx_end = min(len(text), end + context_chars)

        return {
            "match_type": match_type,
            "confidence": confidence,
            "start": start,
            "end": end,
            "location": _detect_location(text, start),
            "model_location": model_location,
            "page_constrained": page_constrained,
            "context_before": text[ctx_start:start],
            "matched": text[start:end],
            "context_after": text[end:ctx_end],
            "truncated_before": ctx_start > 0,
            "truncated_after": ctx_end < len(text),
        }
    return None


def locate_in_docs(
    docs: list[dict[str, Any]],
    snippet: str,
    context_chars: int = 240,
    location: str | None = None,
    candidates: list[str] | None = None,
) -> list[dict[str, Any]]:
    """在多个文档中定位片段，按匹配质量排序。"""
    results: list[dict[str, Any]] = []
    for doc in docs:
        hit = locate(
            doc.get("text") or "",
            snippet,
            context_chars,
            location,
            candidates,
        )
        if hit is None:
            continue
        hit["file_id"] = doc.get("file_id")
        hit["filename"] = doc.get("filename")
        hit["role"] = doc.get("role")
        results.append(hit)

    order = {"exact": 0, "normalized": 1, "fuzzy": 2}
    results.sort(key=lambda r: (order.get(r["match_type"], 3), -r["confidence"]))
    return results
