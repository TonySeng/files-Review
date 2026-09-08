"""审核引擎：文档编排 -> 招标要求提取 -> 分批规则审核（含知识库自主检索）-> 一致性核查。

对外以异步事件流产出进度，供 SSE 推送前端。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, AsyncIterator

from .. import config
from . import consistency_cache, feedback_store, findings_cache, kb_client, llm_client, prompts, rules_store, web_search_client
from . import sections as sections_store
from . import text_locator
from . import versioning
from .doc_parser import truncate

logger = logging.getLogger(__name__)

# 每批送审的规则条数：过大易导致模型漏项，过小则调用次数多
RULES_PER_BATCH = 4

# 校对类规则：偏向"逐字/逐句"文字质量核查（错别字、语义、术语）。
# 这类规则需要模型集中注意力逐字扫描，若与标点/格式类规则同批竞用，
# 模型常只抓"低垂果实"（标点/空格噪声）而漏掉真正的字替换错别字。
# 因此单独成批调用，与常规规则隔离。实测表明单独调用时错别字命中率显著提升。
EDITING_RULE_IDS = {"gen-typo", "gen-semantics", "gen-terminology"}
SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2, "info": 3}
SEVERITY_WEIGHT = {"critical": 25, "major": 10, "minor": 3, "info": 0}

# ---- 持久化 KB 法规依据缓存（跨任务复用，规避 KB 单查 50-80s 的主瓶颈）----
# 查询文本由规则字段构造、与文档内容无关；同一套规则+同一知识库+同一数据快照的法规依据可安全复用。
# 落盘于挂载卷 /app/backend/data，容器重建不丢。只持久化「有答案的成功条目」，失败/无结果负缓存不落盘，
# 防止 KB 瞬时故障污染后续任务。
_KB_CACHE_FILE = rules_store.DATA_DIR / "kb_cache.json"
_kb_cache_lock = asyncio.Lock()


def _load_kb_cache() -> dict[str, tuple[str, list]]:
    """加载持久化 KB 缓存为 {命名空间键: (answer, sources)}。文件缺失/损坏时返回空缓存。"""
    try:
        if not _KB_CACHE_FILE.exists():
            return {}
        raw = json.loads(_KB_CACHE_FILE.read_text(encoding="utf-8"))
        out: dict[str, tuple[str, list]] = {}
        for k, v in raw.items():
            if isinstance(v, list) and len(v) == 2 and v[0]:
                out[str(k)] = (str(v[0]), list(v[1] or []))
        return out
    except Exception:  # noqa: BLE001 - 缓存损坏只影响命中率，不影响任务
        logger.warning("KB 缓存加载失败(忽略，走全量检索): %s", _KB_CACHE_FILE)
        return {}


async def _save_kb_cache(cache: dict[str, tuple[str, list]]) -> None:
    """把有答案的成功条目原子落盘（tmp+rename），并发任务最后写者胜，均不破坏文件。"""
    if not cache:
        return
    payload = {k: [v[0], v[1]] for k, v in cache.items() if v and v[0]}
    if not payload:
        return
    async with _kb_cache_lock:
        try:
            rules_store.DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = _KB_CACHE_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(_KB_CACHE_FILE)
            logger.info("KB 法规依据缓存已落盘: %d 条 -> %s", len(payload), _KB_CACHE_FILE)
        except Exception:  # noqa: BLE001
            logger.exception("KB 缓存写入失败(忽略)")


def _compose_docs(docs: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """把多文件拼成带类型标注的上下文。

    返回 (full_text, per_doc_blocks)：
    - full_text：拼接后的完整上下文（供确定性结构化规则全量匹配）；
    - per_doc_blocks：与 docs 等长对齐的列表，每项为该文档的带标注文本块
      （无文本则为空串），便于按规则关联文档类型过滤后重新拼接子集上下文。
    """
    limit = int(config.get("max_chars_per_doc", 60000))
    role_label = {"tender": "招标文件", "bid": "投标文件", "attachment": "附件"}
    blocks: list[str] = []
    per_doc_blocks: list[str] = []
    for doc in docs:
        # 优先使用用户手动指定的文件类型名称，否则回落到角色标签
        label = doc.get("file_type_name") or role_label.get(doc.get("role", "bid"), "文件")
        text = truncate(doc.get("text") or "", limit)
        if not text.strip():
            per_doc_blocks.append("")
            continue
        block = (
            f"===== 【{label}】{doc['filename']} "
            f"（{'OCR识别' if doc.get('used_ocr') else '文本提取'}）=====\n{text}"
        )
        blocks.append(block)
        per_doc_blocks.append(block)
    return ("\n\n".join(blocks) if blocks else "（无可用文本内容）"), per_doc_blocks


async def _build_docs_text(
    docs: list[dict[str, Any]], mode: str, emit: Any, temperature: float | None = None,
    rule_token: str | None = None, cache_enabled: bool | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """拼装送审文档上下文，并返回「每份文档的紧凑表征」供一致性要素提取复用。

    超大文档（超过 max_chars_per_doc）先按段切片、并行 LLM 摘要，用「分片摘要」作为
    整篇文档的紧凑表征送后续按规则审核——即"拆分文档→分片总结→再对整篇审核"，
    从根本上避免把整份大文档一次性塞进单个巨型 prompt 触发模型超时/卡死
    （两个超大 PDF 曾导致任务长时间挂起）。摘要走 consistency_cache 复用，二次审核秒回。

    返回值：(docs_text, per_doc)
      - docs_text：拼接后的完整送审上下文（含标签头），供按规则审核批次使用；
      - per_doc：与 docs 等长的列表，每项为该文档的"紧凑表征"文本（大文档=分片摘要，
        小文档=截断原文）。一致性要素提取直接复用它，而非把 60k 原始文本喂给模型，
        从而把一致性阶段最大的输入源从 60k 压到 5–20k，规避 DeepSeek-V4-Flash 在
        大上下文上的分钟级慢调用（Fix A）。

    cache_enabled: 任务级缓存开关（None=跟随全局各缓存开关）。
    """
    limit = int(config.get("max_chars_per_doc", 60000))
    max_seg = int(config.get("doc_summary_max_segments", 40))
    role_label = {"tender": "招标文件", "bid": "投标文件", "attachment": "附件"}
    blocks: list[str] = []
    per_doc: list[dict[str, Any]] = []
    per_doc_blocks: list[str] = []  # 与 docs 等长对齐，便于按规则过滤文档类型
    for doc in docs:
        label = doc.get("file_type_name") or role_label.get(doc.get("role", "bid"), "文件")
        filename = doc.get("filename", "未命名文件")
        text = doc.get("text") or ""
        used_ocr = doc.get("used_ocr")
        if not text.strip():
            per_doc.append({"filename": filename, "role": doc.get("role"), "text": ""})
            per_doc_blocks.append("")
            continue
        if len(text) <= limit:
            shown = truncate(text, limit)
            block = (
                f"===== 【{label}】{filename} "
                f"（{'OCR识别' if used_ocr else '文本提取'}）=====\n{shown}"
            )
            blocks.append(block)
            per_doc_blocks.append(block)
            per_doc.append({"filename": filename, "role": doc.get("role"), "text": shown})
        else:
            await emit(
                {
                    "type": "stage", "stage": "doc_summarize",
                    "message": f"文档《{filename}》超长（{len(text)} 字），分片摘要后送审",
                }
            )
            # max_chars=None → 摘要整篇（不截断）；max_seg 防止极端超大文档摘要成本失控
            summary = await _summarize_doc(
                doc, mode, label, elements=None, temperature=temperature,
                max_chars=None, max_segments=max_seg,
                doc_md5=doc.get("md5"), rule_token=rule_token,
                cache_enabled=cache_enabled,
            )
            if not summary.strip():
                summary = truncate(text, limit)  # 摘要兜底：退回截断原文
            block = (
                f"===== 【{label}】{filename}（分片摘要，原文 {len(text)} 字）=====\n{summary}"
            )
            blocks.append(block)
            per_doc_blocks.append(block)
            per_doc.append({"filename": filename, "role": doc.get("role"), "text": summary})
    return ("\n\n".join(blocks) if blocks else "（无可用文本内容）", per_doc, per_doc_blocks)


def _truncate_docs_text(text: str, max_chars: int) -> str:
    """将送审正文截断到 max_chars 以内，尽量保留所有文档块（每块按比例截断尾部）。

    仅用于上下文超长降级：优先按比例裁掉每块尾部，避免直接按整串头部截断
    导致后续文档整块丢失。delimiter 行以 ``=====`` 开头（见 _build_docs_text）。
    """
    if len(text) <= max_chars:
        return text
    parts = re.split(r"(?m)^(=====.*?=====)\n", text)
    head = parts[0] if parts else ""
    blocks: list[tuple[str, str]] = []
    i = 1
    while i + 1 < len(parts):
        blocks.append((parts[i], parts[i + 1]))
        i += 2
    if not blocks:
        return text[:max_chars]
    per = max(200, (max_chars - len(head)) // max(1, len(blocks)))
    out = [head]
    for delim, body in blocks:
        out.append(delim + "\n")
        out.append(body[:per])
    return "".join(out)


async def _run_rule_prompt_guarded(
    batch: list[dict[str, Any]],
    docs_text: str,
    tender_summary: str,
    extra_instruction: str | None,
    *,
    mode: str,
    file_manifest: str = "",
    kb_text: str,
    run_kb: bool,
    kb_id: str | None,
    web_search_enabled: bool,
    on_event: Any,
    rules: list[dict[str, Any]],
    kb_cache: dict,
    cache_prefix: str,
    temperature: float | None,
    kb_state: dict | None,
) -> tuple[Any, list[dict[str, Any]]]:
    """带「上下文超长多级降级」的单 prompt 审核调用（Fix：上线频繁上下文超长 400）。

    降级层级（每级均 emit 进度/警告，便于界面观察）：
      L0 原始：base_prompt + KB 依据
      L1 丢弃 KB 依据（最大单项裁剪，且关闭 KB 工具避免模型回捞依据再超长）
      L2 截断送审正文至预算 ~50%
      L3 截断送审正文至预算 ~20%
    任一层级真实命中 LLMContextOverflow 仍超长，或已达最大降级次数，则抛出交由上层告警跳过。

    发前 token 预算：拼好的 messages 估算超 ``llm_max_input_tokens`` 即主动降级，
    省一次必败的模型调用（真实 400 仍由 llm_client 识别并触发同款降级）。
    """
    budget = int(config.get("llm_max_input_tokens", 60000))
    max_degrade = int(config.get("llm_context_overflow_max_degrade", 3))
    base_prompt = prompts.build_rule_prompt(
        batch, docs_text, tender_summary, extra_instruction, mode=mode,
        file_manifest=file_manifest,
    )
    # 系统提示 + 工具 schema 的固定开销（近似），用于发前预算判断
    overhead_tokens = llm_client.estimate_tokens(prompts.system_prompt()) + 1000

    def _tok(prompt: str, kb: str) -> int:
        return (
            overhead_tokens
            + llm_client.estimate_tokens(prompt)
            + (llm_client.estimate_tokens(kb) if kb else 0)
        )

    use_kb = bool(kb_text)
    text = docs_text
    last_exc: Exception | None = None
    for level in range(0, max_degrade + 1):
        kb = kb_text if use_kb else ""
        prompt = base_prompt
        if level >= 1:
            # L1：丢弃 KB 依据（最大单项裁剪）
            kb = ""
            use_kb = False
        if level >= 2:
            # L2/L3：截断送审正文（1.5 token/char 反推字符预算）
            ratio = 0.5 if level == 2 else 0.2
            max_chars = max(2000, int(budget * ratio / 1.5))
            text = _truncate_docs_text(text, max_chars)
            prompt = prompts.build_rule_prompt(
                batch, text, tender_summary, extra_instruction, mode=mode,
                file_manifest=file_manifest,
            )
        # 发前预算检查：未触发真实 400 即主动降级（省一次必败调用）
        if _tok(prompt, kb) > budget and level < max_degrade:
            if level == 0:
                await on_event(
                    {
                        "type": "stage", "stage": "rules",
                        "message": (
                            f"批次预估 token≈{_tok(prompt, kb)} 超预算 {budget}，"
                            f"自动降级（丢弃知识库依据）"
                        ),
                    }
                )
            continue
        full = prompt
        if kb:
            full = (
                f"{prompt}\n\n# 法规依据（来自知识库预检索，供本批次核查参考）\n{kb}"
            )
        try:
            return await _run_with_kb(
                full,
                kb_enabled=run_kb and bool(kb),
                kb_id=kb_id,
                web_search_enabled=web_search_enabled,
                on_event=on_event,
                rules=rules,
                kb_cache=kb_cache,
                cache_prefix=cache_prefix,
                temperature=temperature,
                kb_state=kb_state,
            )
        except llm_client.LLMContextOverflow as exc:
            last_exc = exc
            if level < max_degrade:
                await on_event(
                    {
                        "type": "warning",
                        "message": (
                            f"批次上下文超长，自动降级（层级 {level + 1}）："
                            f"{'丢弃知识库依据' if level == 0 else '截断送审正文'}"
                        ),
                    }
                )
                continue
            raise
    if last_exc:
        raise last_exc
    return None, []


def _rule_doc_type_filter(batch: list[dict[str, Any]]) -> set[str] | None:
    """返回批次关联文档类型集合；若批次内所有规则均未限定文档类型，返回 None（适用于全部文件）。

    一批次可能含多条规则（如校对类合并批），取各规则 doc_types 的并集作为该批的适用范围。
    """
    types: set[str] = set()
    for r in batch:
        for dt in (r.get("doc_types") or []):
            if dt:
                types.add(str(dt))
    return types or None


def _applicable_docs(
    batch: list[dict[str, Any]], docs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """按规则关联的文档类型过滤适用文档；未限定则适用全部文档。"""
    types = _rule_doc_type_filter(batch)
    if types is None:
        return list(docs)
    return [d for d in docs if (d.get("file_type") or None) in types]


def _batch_section_ids(batch: list[dict[str, Any]]) -> list[str]:
    """批次关联章节 id 并集；批次内所有规则均未关联章节时返回空列表（=全量审核）。

    常规批次为「一条规则一批」；仅校对类规则合并成批——这类规则通常不配置章节，
    因此取并集不会造成范围放大。
    """
    ids: list[str] = []
    for r in batch or []:
        for sid in r.get("section_ids") or []:
            sid = str(sid).strip()
            if sid and sid not in ids:
                ids.append(sid)
    return ids


def _doc_label(doc: dict[str, Any]) -> str:
    role_label = {"tender": "招标文件", "bid": "投标文件", "attachment": "附件"}
    return doc.get("file_type_name") or role_label.get(doc.get("role", "bid"), "文件")


def _scoped_text(
    doc: dict[str, Any], defs: list[dict[str, Any]]
) -> tuple[str, list[str]]:
    """按章节定义取文档的送审正文。

    优先消费解析阶段预拆分的章节结构（doc_splitter，解析时一次完成），
    旧文件/拆分失败时回退实时 scope_text 全文重切——两者匹配语义一致。
    """
    prepared = doc.get("sections")
    if isinstance(prepared, list) and prepared:
        body, names = sections_store.scope_from_prepared(prepared, defs)
        if body.strip():
            return body, names
        # 预拆分结构未命中时再回退实时切分（防拆分产物异常导致漏审）
    return sections_store.scope_text(doc.get("text") or "", defs)


def _section_scoped(
    batch_docs: list[dict[str, Any]], section_ids: list[str]
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """按关联章节裁剪送审文档。

    章节以文件类型为维度：只取「文档自身 file_type 下、且 id 在 section_ids 中」的章节；
    命中则仅把命中章节的正文作为该文档的送审内容，未命中任何章节的文档直接剔除。

    返回 (scoped_docs, blocks, hit_names)：
      - scoped_docs：命中文档副本，parsed_hash 替换为「裁剪后正文」的指纹，
        使结论缓存随章节范围变化而失效（避免复用旧范围的结论）；
      - blocks：带标签头的送审文本块，与全量路径格式保持一致；
      - hit_names：命中的章节名（用于进度提示）。
    """
    limit = int(config.get("max_chars_per_doc", 60000))
    scoped_docs: list[dict[str, Any]] = []
    blocks: list[str] = []
    hit_names: list[str] = []
    for doc in batch_docs:
        defs = sections_store.for_file_type(section_ids, doc.get("file_type"))
        if not defs:
            continue  # 该文件类型下没有关联章节 → 本文档不参与本规则审核
        body, names = _scoped_text(doc, defs)
        if not body.strip():
            continue  # 文档里找不到这些章节 → 本文档不参与
        shown = truncate(body, limit)
        label = _doc_label(doc)
        filename = doc.get("filename", "未命名文件")
        blocks.append(
            f"===== 【{label}】{filename} （章节范围：{'、'.join(names)}）=====\n{shown}"
        )
        scoped_docs.append(
            dict(doc, parsed_hash=versioning.parsed_content_hash(shown))
        )
        for n in names:
            if n not in hit_names:
                hit_names.append(n)
    return scoped_docs, blocks, hit_names


def _split_text(text: str, size: int) -> list[str]:
    """按长度切分文本，优先在换行处断句，避免跨段落硬切。"""
    if len(text) <= size:
        return [text]
    segs: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + size, n)
        if end >= n:
            segs.append(text[start:])
            break
        nl = text.rfind("\n", start, end)
        if nl > start:
            end = nl + 1
        segs.append(text[start:end])
        start = end
    return segs


def _cache_on(explicit: bool | None, key: str, default: bool = True) -> bool:
    """缓存开关统一判定：任务级显式指定（cache_enabled）优先，否则回落全局配置项。

    任务级三态语义：None=跟随全局（默认，向后兼容）；True=本任务强制启用；
    False=本任务强制禁用（不受全局开关影响）。
    """
    return bool(config.get(key, default)) if explicit is None else bool(explicit)


async def _summarize_doc(
    doc: dict[str, Any], mode: str, label: str, elements: list[Any] | None = None,
    temperature: float | None = None,
    max_chars: int | None = None, max_segments: int | None = None,
    doc_md5: str | None = None, rule_token: str | None = None,
    cache_enabled: bool | None = None,
) -> str:
    """对单篇文档分段摘要提取，返回一致性比对用的关键事实文本。

    单文档内先分段，逐段调用 LLM 提取一致性关键事实（名称/金额/日期/编号等），
    再拼接为带来源标注的摘要，供后续跨文件/文档内一致性核查使用。
    elements: 本任务一致性规则声明的重点核查要素（str 或 {name, synonyms, note}），
    注入分段摘要提示词优先提取。
    temperature: 确定性模式下传入 0.0，关闭采样以保证可复现；否则由引擎默认。

    一致性切片摘要缓存（提效①）：相同「文档文本 + 模式 + 要素 + 温度」直接复用历史摘要，
    跳过全部分段 LLM 调用。开关由 config.consistency_cache_enabled 控制（默认开）；
    命中/未命中返回内容完全一致，不改变最终结论语义。
    """
    limit = int(config.get("max_chars_per_doc", 60000))
    raw = doc.get("text") or ""
    # max_chars=None 表示不截断（用于大文档"先摘要整篇再送审"），其余场景回落到
    # max_chars_per_doc 截断以控制摘要成本，保持历史一致性核查行为不变。
    text = raw if max_chars is None else truncate(raw, max_chars)
    if not text.strip():
        return ""

    cache_enabled = _cache_on(cache_enabled, "consistency_cache_enabled")
    ckey: str | None = None
    if cache_enabled:
        try:
            ckey = consistency_cache.make_key(
                text, mode=mode, elements=elements or [], temperature=temperature,
                doc_md5=doc_md5, rule_token=rule_token,
            )
            cached = consistency_cache.get(ckey)
            if cached is not None:
                logger.info("一致性摘要命中缓存，跳过 LLM 调用: %s", label)
                return cached
        except Exception as exc:  # noqa: BLE001 - 缓存异常降级为重新生成
            logger.warning("一致性摘要缓存读取异常(降级为重新生成): %s", exc)
            ckey = None

    seg_size = int(config.get("consistency_seg_chars", 8000))
    segs = _split_text(text, seg_size)
    if max_segments is not None and len(segs) > max_segments:
        logger.info("文档 %s 段落数 %d 超过上限 %d，仅摘要前 %d 段", label, len(segs), max_segments, max_segments)
        segs = segs[:max_segments]
    parts: list[str] = []
    # 单文档内分段摘要并发化（提效 2026-08-30）：原串行遍历在超大文档（如 20 万字符→25 段）
    # 会触发 25 次串行 LLM 调用，单文档即耗时 12-25 分钟，远超规则批次 600s 超时 → 整批被
    # _safe_batch 跳过；首个规则批次超时后摘要缓存尚未落盘，后续批次重复超时 → 出现"12 个批次超时"。
    # 改为受 doc_summary_concurrent 信号量约束的并发摘要，超大文档也能在超时窗内完成并写入缓存复用。
    seg_sem = asyncio.Semaphore(max(1, int(config.get("doc_summary_concurrent", 4))))

    async def _sum_seg(i: int, seg: str) -> str:
        async with seg_sem:
            try:
                message, _ = await _run_with_kb(
                    prompts.build_segment_summary_prompt(seg, mode=mode, elements=elements),
                    kb_enabled=False,
                    kb_id=None,
                    web_search_enabled=False,
                    rules=None,
                    temperature=temperature,
                )
                summary_raw = (message.get("summary") or "") if isinstance(message, dict) else ""
                # 模型可能把 summary 返回成数组/对象而非字符串，统一归一化为文本，
                # 否则下方 .strip() 会抛 AttributeError 导致整个审核任务在一致性阶段失败。
                if isinstance(summary_raw, list):
                    summary = "\n".join(str(x) for x in summary_raw)
                elif isinstance(summary_raw, dict):
                    summary = json.dumps(summary_raw, ensure_ascii=False)
                else:
                    summary = str(summary_raw)
            except llm_client.LLMError as exc:
                logger.warning("文档 %s 第%d段摘要失败(兜底用原文前段): %s", label, i + 1, exc)
                summary = seg[:800]
            return f"【{label} 第{i + 1}段】\n{summary}" if summary.strip() else ""

    parts = await asyncio.gather(*(_sum_seg(i, seg) for i, seg in enumerate(segs)))
    result = "\n\n".join(p for p in parts if p)

    if cache_enabled and ckey is not None and result.strip():
        try:
            consistency_cache.put(ckey, result)
        except Exception as exc:  # noqa: BLE001
            logger.warning("一致性摘要缓存写入异常(忽略): %s", exc)
    return result


async def _extract_file_elements(
    doc: dict[str, Any],
    elements: list[Any] | None,
    mode: str,
    temperature: float | None,
    summary_text: str | None = None,
    doc_md5: str | None = None,
    rule_token: str | None = None,
    cache_enabled: bool | None = None,
) -> dict[str, Any]:
    """从单个文件一次性提取一致性要素取值（紧凑结构化），供一致性阶段并行 Phase1 + 轻量 Phase2 比对。

    与 _summarize_doc（逐段多次调用、串行、产出冗长自由文本）不同，本函数用【单次 LLM 调用】
    通读整篇文件直接产出结构化要素取值，多文件可安全并行、单文件成本极低，避免逐段串行摘要的慢/超时问题。
    结果经 consistency_cache 复用（kind="elements"，与分段摘要缓存隔离）。

    输入优先级（Fix A）：优先使用调用方传入的「紧凑表征」summary_text（来自 _build_docs_text，
    大文档=分片摘要、小文档=截断原文，均已远小于原始全文），仅当缺失时才退回截断原始全文。
    这把一致性要素提取最大的输入源从 60k 原始文本压到 5–20k，规避 DeepSeek-V4-Flash
    在大上下文上的分钟级慢调用。
    """
    label = doc.get("file_type_name") or {
        "tender": "招标文件", "bid": "投标文件", "attachment": "附件"
    }.get(doc.get("role", "bid"), "文件")
    filename = doc.get("filename", "未命名文件")
    raw = doc.get("text") or ""
    limit = int(config.get("max_chars_per_doc", 60000))
    # 优先用紧凑表征（已显著更小）；对其再做一次安全截断，避免极端大摘要仍撑爆上下文。
    if summary_text and summary_text.strip():
        text = truncate(summary_text, int(config.get("consistency_element_max_chars", 20000)))
    else:
        text = truncate(raw, limit)
    if not text.strip():
        return {}

    cache_enabled = _cache_on(cache_enabled, "consistency_cache_enabled")
    ckey: str | None = None
    if cache_enabled:
        try:
            ckey = consistency_cache.make_key(
                text, mode=mode, elements=elements or [], temperature=temperature,
                kind="elements", doc_md5=doc_md5, rule_token=rule_token,
            )
            cached = consistency_cache.get(ckey)
            if cached is not None:
                logger.info("一致性要素提取命中缓存，跳过 LLM: %s", filename)
                try:
                    return json.loads(cached)
                except (json.JSONDecodeError, TypeError):
                    pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("一致性要素缓存读取异常(降级为重新提取): %s", exc)
            ckey = None

    result: dict[str, Any] = {}
    try:
        message, _ = await _run_with_kb(
            prompts.build_file_elements_prompt(text, mode=mode, elements=elements),
            kb_enabled=False,
            kb_id=None,
            web_search_enabled=False,
            rules=None,
            temperature=temperature,
        )
        if isinstance(message, dict):
            result = {str(k): v for k, v in message.items()}
    except llm_client.LLMError as exc:
        logger.warning("文件 %s 要素提取失败(返回空): %s", filename, exc)
        result = {}

    if cache_enabled and ckey is not None and result:
        try:
            consistency_cache.put(ckey, json.dumps(result, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            logger.warning("一致性要素缓存写入异常(忽略): %s", exc)
    return result


def _normalize_consistency_value(value: Any) -> str:
    """把要素取值归一化为可比较字符串。

    仅做「首尾去空白 + 内部空白压缩为单空格」，不过度归一化（不强行统一大小写/全半角/
    数字写法），以保留真实差异；疑似字面不同但语义等价的情形（如「壹佰万」vs「1000000」）
    交由后续 LLM 判定，避免代码误判漏报。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        s = value
    else:
        try:
            s = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            s = str(value)
    return re.sub(r"\s+", " ", s).strip()


# 角色约束型要素：同一主体在不同文档角色（招标/投标/评标等）中天然指向不同法律主体，
# 仅在「同角色文档」之间比对才有意义；跨角色比对属于无意义误报（典型如：投标文档的
# "投标人名称" 与招标文件里被抽取为 "投标人名称" 的招标人名称，被归到同一要素槽后
# 误判为「投标人与招标人身份错置」）。故对此类要素仅在同一文档角色内部比对，跨角色跳过。
_PARTY_ELEMENT_KEYWORDS = (
    "投标人", "招标人", "中标人", "供应商", "采购人", "成交人",
    "承包人", "发包人", "买方", "卖方", "发包方", "承包方",
    "代理商", "联合体", "联合体牵头人",
)


def _is_role_bound_element(name: str) -> bool:
    n = str(name or "")
    return any(k in n for k in _PARTY_ELEMENT_KEYWORDS)


def _consistency_precheck(
    file_elements: list[tuple[str, dict[str, Any]]],
    role_by_filename: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], int, int]:
    """代码级一致性预筛：按要素名跨文件比对取值。

    在送入 LLM 之前先用代码判定哪些要素「取值一致」、哪些「取值不一致」：
    - 仅出现在单个文件的要素无跨文件可比性，不参与比对也不送 LLM（避免误报）；
    - 出现在 ≥2 个文件且取值（归一化后）完全相同的要素 → 一致，跳过 LLM；
    - 取值不同的要素 → 候选不一致组，需送 LLM 做语义级判定（等价甄别 / 缺失是否真问题）。

    角色约束（2026-09-03 修复跨文件主体名称误报）：投标人/招标人/中标人等「主体名称」
    类要素在不同文档角色中指向不同法律主体，跨角色比对无意义且易误报，故仅在同一文档
    角色内部比对；跨角色直接跳过（不送 LLM）。

    返回 (candidate_groups, consistent_count, compared_count)：
    - candidate_groups：不一致要素组 [{element, per_file:{fname:value}}]；
    - consistent_count：跨文件一致（≥2 文件且取值相同）的要素组数；
    - compared_count：实际参与跨文件比对的要素组数（出现在 ≥2 文件）。
    """
    role_by_filename = role_by_filename or {}
    by_name: dict[str, dict[str, Any]] = {}
    for fname, elems in file_elements:
        if not elems:
            continue
        for k, v in elems.items():
            by_name.setdefault(str(k), {})[fname] = v

    candidate_groups: list[dict[str, Any]] = []
    consistent_count = 0
    compared_count = 0
    for name, per_file in by_name.items():
        if len(per_file) < 2:
            # 仅出现在单个文件：无跨文件可比性，跳过（不送 LLM）
            continue
        if _is_role_bound_element(name):
            # 仅在同一文档角色内比对；跨角色主体天然不同，不比较、不送 LLM
            role_groups: dict[str, dict[str, Any]] = {}
            for fname, v in per_file.items():
                r = role_by_filename.get(fname, "")
                role_groups.setdefault(r, {})[fname] = v
            for _role, grp in role_groups.items():
                if len(grp) < 2:
                    continue
                compared_count += 1
                normed = {f: _normalize_consistency_value(v) for f, v in grp.items()}
                if len(set(normed.values())) == 1:
                    consistent_count += 1
                else:
                    candidate_groups.append({"element": name, "per_file": grp})
            continue
        # 中性要素（项目名称/编号/金额/日期等）：跨所有文件比对
        compared_count += 1
        normed = {f: _normalize_consistency_value(v) for f, v in per_file.items()}
        if len(set(normed.values())) == 1:
            consistent_count += 1
        else:
            candidate_groups.append({"element": name, "per_file": per_file})
    return candidate_groups, consistent_count, compared_count


def _build_mismatch_material(
    candidate_groups: list[dict[str, Any]],
    role_by_filename: dict[str, str] | None = None,
) -> str:
    """把不一致要素组拼成送 LLM 的小 prompt 文本。

    仅含候选不一致组（按要素聚合各文件取值），体量通常数百~数千字，
    远小于原先把全部文件要素拼接成的 40k 巨 prompt，从根本上消除慢调用与超时。
    每个文件附带其文档角色标签（招标/投标/评标等），帮助模型区分不同主体、避免误报。
    """
    role_by_filename = role_by_filename or {}
    blocks: list[str] = []
    for g in candidate_groups:
        name = g.get("element") or "未知要素"
        lines = [f"===== 不一致要素【{name}】的跨文件取值 ====="]
        for fname, val in (g.get("per_file") or {}).items():
            role = role_by_filename.get(fname, "")
            tag = f"（{role}）" if role else ""
            if isinstance(val, str):
                text = val
            else:
                try:
                    text = json.dumps(val, ensure_ascii=False)
                except (TypeError, ValueError):
                    text = str(val)
            lines.append(f"- {fname}{tag}：{text}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


# 一致性核查的「核心要素」已下沉为规则数据：要素库见
# rules_store.CONSISTENCY_ELEMENT_LIBRARY，规则可通过
# structured.consistency_elements 显式声明本规则比对的要素；
# 规格聚合统一走 rules_store.collect_consistency_spec(rules)，
# 从而使一致性核查随规则集 / 规则组复用，不再是本模块的硬编码常量。


# 服务端是否支持原生 function calling，首次失败后置 False 并全局降级
_TOOLS_SUPPORTED: dict[str, bool] = {"value": True}
# 工具调用支持仅探测一次，避免首个批次先失败再降级（见 _probe_tool_support）
_TOOLS_PROBED: dict[str, bool] = {"done": False}


def _rule_to_kb_query(rule: dict[str, Any]) -> str:
    """确定性构造法规依据检索问题（不依赖 LLM，保证跨任务/跨批次文本稳定，持久化缓存才能命中）。

    法规依据查询与文档内容无关、只与规则+知识库有关，因此用规则自身字段（法条关键词 + 规则名 +
    描述）拼接即可作为稳定检索键。优先法条关键词，保证 Milvus 稠密+稀疏混合检索能命中原文。
    """
    parts: list[str] = []
    for k in ("legal_basis", "legal_basis_hint", "law_ref", "law_reference"):
        v = str(rule.get(k) or "").strip()
        if v:
            parts.append(v)
    name = str(rule.get("name") or "").strip()
    if name:
        parts.append(name)
    desc = str(rule.get("description") or "").strip()
    if desc:
        parts.append(desc)
    query = "；".join(dict.fromkeys(p for p in parts if p))  # 去重保序
    return query[:300]


async def _plan_kb_queries(
    rules: list[dict[str, Any]], on_event: Any = None
) -> list[dict[str, str]]:
    """为 need_legal_basis 规则确定性生成法规依据检索问题（不走 LLM）。

    原实现用 LLM 生成查询文本，带随机性导致持久化缓存键不稳定（冷热跑全 miss）；
    改为规则字段确定性拼接后：① 查询文本稳定 → kb_cache 跨任务命中；② 每条规则
    省 1 次 LLM 规划调用。仅对 need_legal_basis 规则生成，其余规则不检索。
    """
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for r in rules:
        if not isinstance(r, dict) or not r.get("need_legal_basis"):
            continue
        query = _rule_to_kb_query(r)
        if not query or query in seen:
            continue
        seen.add(query)
        out.append({"query": query, "reason": f"规则「{r.get('name') or r.get('id') or ''}」需法规依据"})
    if on_event and not out:
        await on_event({"type": "kb_skip", "message": "本批规则无需检索法规知识库"})
    return out


async def _fetch_kb_context(
    queries: list[dict[str, str]],
    kb_id: str | None,
    on_event: Any = None,
    cache: dict[str, tuple[str, list]] | None = None,
    cache_prefix: str = "",
) -> tuple[str, list[dict[str, Any]]]:
    """执行检索并拼装为法规依据上下文。

    cache: 跨批次共享的查询缓存（query -> (answer, sources)）。命中缓存时跳过网络调用，
    既避免对相同法规问题的重复检索，也将"不可达/无结果"的查询做负缓存，防止每批重试。
    cache_prefix: 缓存键命名空间（kb_id+数据快照版本），避免不同知识库/不同版本的法规答案串用。
    持久化缓存（跨任务复用法规依据）见 _load_kb_cache / _save_kb_cache。
    """
    traces: list[dict[str, Any]] = []
    blocks: list[str] = []

    async def _one(item: dict[str, str]) -> dict[str, Any] | None:
        """检索单条查询，返回要追加的 trace(含拼接块)；命中负缓存/无结果返回 None。"""
        query = item["query"]
        ckey = cache_prefix + query
        cached = cache.get(ckey) if cache is not None else None
        if cached is not None:
            answer, sources = cached
            if not answer:
                return None
            return {
                "query": query,
                "reason": item.get("reason", ""),
                "answer": answer,
                "sources": sources,
                "block": f"问：{query}\n答：{answer}",
            }
        if on_event:
            await on_event({"type": "kb_query", "query": query, "reason": item.get("reason", "")})
        try:
            result = await kb_client.ask(query, kb_id=kb_id)
        except kb_client.KBError as exc:
            logger.warning("知识库检索失败: %s", exc)
            if cache is not None:
                cache[ckey] = ("", [])
            if on_event:
                await on_event({"type": "kb_error", "query": query, "message": str(exc)})
            return None
        answer = (result.get("answer") or "").strip()
        if not answer:
            if cache is not None:
                cache[ckey] = ("", [])
            return None
        if cache is not None:
            cache[ckey] = (answer, result.get("sources", []))
        return {
            "query": query,
            "reason": item.get("reason", ""),
            "answer": answer,
            "sources": result.get("sources", []),
            "block": f"问：{query}\n答：{answer}",
        }

    # 多条查询相互独立，并行拉取以消除串行等待（外部知识库服务是主要延迟来源）。
    results = await asyncio.gather(*(_one(item) for item in queries))
    for r in results:
        if not r:
            continue
        traces.append(
            {
                "query": r["query"],
                "reason": r["reason"],
                "answer": r["answer"],
                "sources": r["sources"],
            }
        )
        blocks.append(r["block"])
        if on_event:
            await on_event(
                {
                    "type": "kb_result",
                    "query": r["query"],
                    "answer": r["answer"][:400],
                    "sources": r["sources"],
                }
            )
    context = (
        "## 法规依据（来自本地招采法律法规知识库，请优先采信）\n" + "\n\n".join(blocks)
        if blocks
        else ""
    )
    return context, traces


def _is_transient_llm_error(exc: Exception) -> bool:
    """判断 LLMError 是否由瞬时限流/过载/网络抖动引起（用以决定是否触发双倍无工具重试）。"""
    s = str(exc)
    keys = ("429", "503", "system is too busy", "readtimeout", "connecterror",
            "connecttimeout", "timed out", "remoteprotocolerror", "too many request",
            # 读超时撞硬上限（"llm call exceeded hard ceiling 540s"）：提供方慢/不可达，
            # 去掉工具也不会变快，绝不触发「无工具整批重试」否则单条规则再烧一轮 540s。
            "hard ceiling", "exceeded hard", "deadline exceeded")
    return any(k in s.lower() for k in keys)


async def _run_with_kb(
    user_prompt: str,
    *,
    kb_enabled: bool,
    kb_id: str | None,
    web_search_enabled: bool = False,
    on_event: Any = None,
    rules: list[dict[str, Any]] | None = None,
    kb_cache: dict[str, tuple[str, list]] | None = None,
    cache_prefix: str = "",
    timeout: float | None = None,
    temperature: float | None = None,
    kb_state: dict | None = None,
) -> tuple[Any, list[dict[str, Any]]]:
    """执行一次审核请求，允许模型多轮调用知识库检索和联网搜索工具后再产出结论。"""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": prompts.system_prompt()},
        {"role": "user", "content": user_prompt},
    ]
    kb_traces: list[dict[str, Any]] = []
    max_queries = int(config.get("kb_max_queries", 6))

    # 知识库健康短路：kb_state["enabled"] 为 False 时本任务剩余批次整体跳过 KB 工具调用，
    # 直接基于模型通用知识审核，避免对持续超时的本地知识库服务反复重试（曾出现单任务 119 次 KB 失败）。
    kb_enabled_eff = kb_enabled and (kb_state is None or kb_state.get("enabled", True))
    kb_fail_streak = 0

    # 根据配置决定可用工具
    tools = None
    if kb_enabled_eff or web_search_enabled:
        tools = []
        if kb_enabled_eff:
            tools.append(prompts.TOOLS[0])  # search_knowledge_base
        if web_search_enabled:
            tools.append(prompts.TOOLS[1])  # web_search

    # 部分 vLLM 部署未开启 --enable-auto-tool-choice，此时降级为提示词规划模式
    if tools and not _TOOLS_SUPPORTED.get("value", True):
        tools = None

    # 降级模式：先规划检索，再把法规依据注入正式审核请求
    if kb_enabled_eff and tools is None and rules:
        planned = await _plan_kb_queries(rules, on_event)
        if planned:
            context, traces = await _fetch_kb_context(planned, kb_id, on_event, cache=kb_cache, cache_prefix=cache_prefix)
            kb_traces.extend(traces)
            if context:
                messages[1]["content"] = context + "\n\n" + user_prompt

    for _ in range(max_queries + 1):
        try:
            message = await llm_client.chat(messages, tools=tools, timeout=timeout, temperature=temperature)
        except llm_client.LLMError as exc:
            if tools and "tool choice" in str(exc).lower():
                logger.warning("服务端未启用原生工具调用，降级为提示词规划模式")
                _TOOLS_SUPPORTED["value"] = False
                tools = None
                # 本批次补做检索规划，避免降级首批丢失法规依据
                if kb_enabled and rules:
                    planned = await _plan_kb_queries(rules, on_event)
                    if planned:
                        context, traces = await _fetch_kb_context(planned, kb_id, on_event, cache=kb_cache, cache_prefix=cache_prefix)
                        kb_traces.extend(traces)
                        if context:
                            messages[1]["content"] = context + "\n\n" + user_prompt
                message = await llm_client.chat(messages, tools=None, timeout=timeout, temperature=temperature)
            else:
                # 「工具不支持」之外的异常：若是瞬时限流/过载/网络抖动，由 llm_client 内部退避重试处理，
                # 此处不再额外发起一次「无工具整批重试」，避免对过载的提供方加倍施压（曾出现 17 次双倍重试）。
                # 仅当属非瞬时错误（如模型输出解析问题）时才降级无工具模式补全本批。
                # 上下文超长须立即上抛：用同一份超长 prompt 再发一次毫无意义，
                # 且下方「补做检索规划」会重新注入 KB 依据令其更长——必须由调用方降级（丢 KB/截正文）后重试。
                if isinstance(exc, llm_client.LLMContextOverflow):
                    raise
                if _is_transient_llm_error(exc):
                    logger.warning("审核主调用瞬时故障(限流/过载/抖动)，交由上层退避重试: %s", exc)
                    raise
                logger.warning("审核主调用异常(%s)，降级无工具模式重试以保全规则审核", exc)
                tools = None
                if kb_enabled_eff and rules and not kb_traces:
                    planned = await _plan_kb_queries(rules, on_event)
                    if planned:
                        context, traces = await _fetch_kb_context(planned, kb_id, on_event, cache=kb_cache, cache_prefix=cache_prefix)
                        kb_traces.extend(traces)
                        if context:
                            messages[1]["content"] = context + "\n\n" + user_prompt
                try:
                    message = await llm_client.chat(messages, tools=None, timeout=timeout, temperature=temperature)
                except llm_client.LLMError:
                    raise
        tool_calls = message.get("tool_calls") or []

        if not tool_calls:
            return llm_client.parse_json(message.get("content") or ""), kb_traces

        messages.append(
            {
                "role": "assistant",
                "content": message.get("content") or "",
                "tool_calls": tool_calls,
            }
        )

        for call in tool_calls:
            fn = call.get("function") or {}
            args: dict[str, Any] = {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                pass
            query = (args.get("query") or "").strip()
            reason = args.get("reason") or ""
            fn_name = fn.get("name")

            if fn_name == "search_knowledge_base":
                if not query:
                    result_text = "工具调用参数无效：缺少查询内容"
                elif kb_state is not None and not kb_state.get("enabled", True):
                    # 知识库已判定不可用（连续失败达阈值），本轮直接跳过调用，避免继续超时重试
                    result_text = "知识库服务当前不可用，请基于通用法规常识审慎判断。"
                    logger.warning("知识库已短路：跳过本次 KB 检索(%s)", query)
                else:
                    if on_event:
                        await on_event(
                            {"type": "kb_query", "query": query, "reason": reason}
                        )
                    try:
                        kb_result = await kb_client.ask(query, kb_id=kb_id)
                        result_text = kb_result.get("answer") or "知识库未返回内容"
                        kb_traces.append(
                            {
                                "query": query,
                                "reason": reason,
                                "answer": result_text,
                                "sources": kb_result.get("sources", []),
                            }
                        )
                        if on_event:
                            await on_event(
                                {
                                    "type": "kb_result",
                                    "query": query,
                                    "answer": result_text[:400],
                                    "sources": kb_result.get("sources", []),
                                }
                            )
                    except kb_client.KBError as exc:
                        result_text = f"知识库检索失败：{exc}。请基于通用法规常识审慎判断。"
                        logger.warning("知识库检索失败: %s", exc)
                        if on_event:
                            await on_event({"type": "kb_error", "query": query, "message": str(exc)})
                        # 连续失败达阈值（默认 3）→ 任务级短路，剩余批次不再调用 KB，避免雪崩重试
                        kb_fail_streak += 1
                        if kb_state is not None and kb_fail_streak >= int(config.get("kb_fail_threshold", 3)):
                            kb_state["enabled"] = False
                            logger.warning("知识库连续失败 %d 次，已对本任务短路禁用 KB", kb_fail_streak)

            elif fn_name == "web_search":
                if not query:
                    result_text = "工具调用参数无效：缺少查询内容"
                else:
                    if on_event:
                        await on_event(
                            {"type": "web_search", "query": query, "reason": reason}
                        )
                    try:
                        search_result = await web_search_client.search(query)
                        results = search_result.get("results", [])
                        if results:
                            result_parts = [f"找到 {len(results)} 条搜索结果：\n"]
                            for i, item in enumerate(results[:5], 1):
                                result_parts.append(
                                    f"{i}. {item.get('title', '无标题')}\n"
                                    f"   {item.get('snippet', '无摘要')}\n"
                                    f"   来源：{item.get('url', '')}\n"
                                )
                            result_text = "\n".join(result_parts)
                        else:
                            result_text = "未找到相关搜索结果"

                        if on_event:
                            await on_event(
                                {
                                    "type": "web_search_result",
                                    "query": query,
                                    "result_count": len(results),
                                }
                            )
                    except web_search_client.WebSearchError as exc:
                        result_text = f"联网搜索失败：{exc}。请基于现有信息判断。"
                        logger.warning("联网搜索失败: %s", exc)
                        if on_event:
                            await on_event({"type": "web_search_error", "query": query, "message": str(exc)})

            else:
                result_text = f"不支持的工具：{fn_name}"

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id") or "",
                    "content": result_text[:4000],
                }
            )

    # 工具调用次数用尽，强制收口
    messages.append({"role": "user", "content": "请立即基于已有信息输出最终 JSON 结论，不要再调用工具。"})
    message = await llm_client.chat(messages, tools=None)
    return llm_client.parse_json(message.get("content") or ""), kb_traces


_PROBE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "ping_probe",
        "description": "探测模型是否支持原生工具调用（无害占位工具）",
        "parameters": {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
        },
    },
}


async def _probe_tool_support() -> None:
    """懒探测一次模型是否支持原生工具调用，避免首个批次先失败再降级。

    非致命：任何异常（网络/超时/密钥缺失）都视为「支持=True」，保持历史默认行为，
    绝不阻塞或破坏审核。探测结果与运行期失败降级路径共用 _TOOLS_SUPPORTED。
    """
    if _TOOLS_PROBED.get("done"):
        return
    _TOOLS_PROBED["done"] = True
    try:
        msg = await llm_client.chat(
            [
                {"role": "system", "content": "你是探测助手，请按要求调用工具。"},
                {"role": "user", "content": "请调用 ping_probe 工具，参数 ok=true。"},
            ],
            tools=[_PROBE_TOOL],
            temperature=0.0,
            max_tokens=64,
        )
        supported = bool(msg.get("tool_calls"))
        _TOOLS_SUPPORTED["value"] = supported
        logger.info(
            "工具调用支持探测完成: %s", "支持原生工具调用" if supported else "不支持(将走提示词规划降级)"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("工具调用探测失败(假设支持=True): %s", exc)
        _TOOLS_SUPPORTED["value"] = True


async def _extract_tender_summary(
    docs: list[dict[str, Any]], on_event: Any = None, temperature: float | None = None
) -> tuple[str, dict[str, Any] | None]:
    """从招标文件提取关键约束；无招标文件时返回空。"""
    tenders = [
        d
        for d in docs
        if d.get("role") == "tender" or "招标" in (d.get("file_type_name") or d.get("file_type") or "")
    ]
    if not tenders:
        return "", None

    limit = int(config.get("max_chars_per_doc", 60000))
    text = "\n\n".join(
        f"【{d['filename']}】\n{truncate(d.get('text') or '', limit)}" for d in tenders
    )
    if on_event:
        await on_event({"type": "stage", "stage": "tender_summary", "message": "提取招标文件关键要求"})
    try:
        message = await llm_client.chat(
            [
                {"role": "system", "content": prompts.system_prompt()},
                {"role": "user", "content": prompts.build_tender_summary_prompt(text)},
            ],
            temperature=temperature,
        )
        data = llm_client.parse_json(message.get("content") or "")
    except (llm_client.LLMError, ValueError) as exc:
        logger.warning("招标要求提取失败: %s", exc)
        return "", None

    lines: list[str] = []
    labels = {
        "project_name": "项目名称", "project_no": "项目编号", "budget": "最高限价/预算",
        "bid_bond": "投标保证金", "bid_validity": "投标有效期", "duration": "工期要求",
        "qualifications": "资格要求", "personnel": "人员要求", "performance": "业绩要求",
        "star_items": "实质性(★)条款", "doc_composition": "投标文件组成",
        "seal_requirements": "签章要求", "reject_clauses": "否决投标情形",
        "deadlines": "关键时间节点",
    }
    for key, label in labels.items():
        value = data.get(key)
        if not value:
            continue
        if isinstance(value, list):
            joined = "\n".join(f"  - {v}" for v in value if v)
            if joined:
                lines.append(f"{label}:\n{joined}")
        else:
            lines.append(f"{label}: {value}")
    return "\n".join(lines), data


def _collect_typos(item: dict[str, Any], rule_id: str) -> list[dict[str, Any]]:
    """从一条模型结论收集全部错别字（支持单对象 / 对象数组 / 纯文本多处匹配）。

    返回去重后的 [{wrong, correct, context}, ...]。仅错别字类规则(gen-typo)才会
    从 detail/suggestion 文本中兜底挖掘「X 改为 Y」模式，避免把建议性表述误判为错字。
    """
    t = item.get("typo")
    out: list[dict[str, Any]] = []

    def _add(wrong: str, correct: str, context: str) -> None:
        wrong = (wrong or "").strip()
        correct = (correct or "").strip()
        context = (context or "").strip()
        if wrong and feedback_store._is_clean_typo_pair(wrong, correct):
            out.append({"wrong": wrong, "correct": correct, "context": context})

    if isinstance(t, list):
        for x in t:
            if isinstance(x, dict):
                _add(x.get("wrong"), x.get("correct"), x.get("context"))
    elif isinstance(t, dict):
        _add(t.get("wrong"), t.get("correct"), t.get("context"))
    else:
        # typo 缺失：仅错别字规则尝试从正文挖掘多处「X 改为 Y」
        if str(rule_id) in feedback_store.TYPO_RULE_IDS:
            blob = f"{item.get('detail') or ''} {item.get('suggestion') or ''}"
            for m in re.finditer(
                r"['\"「]?\s*(.{1,12}?)\s*['\"」]?\s*"
                r"(?:改为|应为|纠正为|应写作|修正为|建议为)\s*"
                r"['\"「]?\s*(.{1,12}?)\s*['\"」]?",
                blob,
            ):
                _add(m.group(1), m.group(2), "")

    # 按 (wrong, correct) 去重，保持首次出现顺序
    seen: set[tuple[str, str]] = set()
    dedup: list[dict[str, Any]] = []
    for x in out:
        key = (x["wrong"], x["correct"])
        if key not in seen:
            seen.add(key)
            dedup.append(x)
    return dedup


# 招标文件摘要里常见的「关注点标签」。若模型把摘要中的这些标签直接当作某条规则的
# 结论标题/核心内容，而当前规则说明里根本不涉及该标签，即可判定为「被招标文件摘要
# 带偏/结论挂错规则」，降级为 unknown 并提示人工复核。
_TENDER_LABELS = (
    "人员要求", "资格要求", "业绩要求", "否决投标情形", "实质性",
    "投标保证金", "投标有效期", "工期要求", "签章要求", "投标文件组成",
    "关键时间节点", "最高限价", "预算", "项目名称", "项目编号",
)


def _tender_label_mismatch(item: dict[str, Any], rule: dict[str, Any]) -> str | None:
    """检查 finding 内容是否被招标文件摘要标签带偏。

    若 title/detail 中出现了 tender_summary 常见的关注点标签，但当前规则的
    name/description/checkpoints 中完全没有该标签，说明模型把其他关注点的结论
    写到了本规则下。返回命中的标签名，否则返回 None。
    """
    blob = f"{item.get('title') or ''} {item.get('detail') or ''}"
    rule_text = " ".join(
        [
            rule.get("name") or "",
            rule.get("description") or "",
            " ".join(rule.get("checkpoints") or []),
        ]
    )
    for label in _TENDER_LABELS:
        if label in blob and label not in rule_text:
            return label
    return None


def _normalize_findings(
    raw: Any,
    batch: list[dict[str, Any]],
    doc_names: list[str] | None = None,
) -> list[dict[str, Any]]:
    """把模型输出对齐到规则定义，补齐缺失项。

    错别字规则若一次命中多处错字，模型以 typo 数组返回，本函数将其拆分为多条
    独立结论（每条一个 typo），使审核结果、反馈与训练数据均能「逐条」记录。
    """
    items = raw.get("findings") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        items = []
    by_id = {r["id"]: r for r in batch}
    seen: set[str] = set()
    findings: list[dict[str, Any]] = []

    for item in items:
        if not isinstance(item, dict):
            continue
        rule_id = str(item.get("rule_id") or "").strip()
        rule = by_id.get(rule_id)
        if rule is None:
            continue
        seen.add(rule_id)
        status = str(item.get("status") or "unknown").lower()
        if status not in ("pass", "fail", "warn", "unknown"):
            status = "unknown"
        # 方案 C：结构化判定与自由文本矛盾 → 先纠偏、纠偏不了再降级 unknown。
        # 仅对 LLM 结论生效（确定性结论走 _normalize_finding_deterministic，不会被调用到此）。
        contradiction = False
        corrected_pass = False
        evidence_missing = False
        duplicate_unverified = False
        file_unresolved = False
        typo_entity = False
        tender_mismatch = _tender_label_mismatch(item, rule)
        if tender_mismatch and status in ("fail", "warn"):
            # 模型把招标文件摘要中的关注点直接当成了本规则的结论：
            # 例如 dec-03 打分规则下出现「人员要求不符合招标文件要求」「否决投标情形」。
            # 这类结论与当前规则说明完全脱节，直接降级为 unknown 并提示人工复核。
            status = "unknown"
        if status == "fail" and _explicitly_reports_no_violation(item):
            # 结语句明确「未发现/不存在 + 违规对象」却填 fail → 直接纠正为通过，
            # 并清空定位类字段（通过结论不携带原文定位，证据「无」无定位意义）。
            status = "pass"
            corrected_pass = True
        elif status == "fail" and _finding_text_contradicts_fail(item):
            status = "unknown"
            contradiction = True
        elif (
            status in ("fail", "warn")
            and _evidence_is_placeholder(item.get("evidence"))
            and not _finding_has_alternative_basis(item)
        ):
            # fail/warn 却给不出原文依据（evidence 为「无」类占位词/空，
            # detail 也没有引文或缺失类表述）→ 结论纯属模型断言，降级为
            # 待人工复核，杜绝「没有相关依据也判不合规」。
            status = "unknown"
            evidence_missing = True
        elif status in ("fail", "warn") and _duplicate_claim_unsubstantiated(item):
            # 断言「重复/雷同」但引用的原文中比对不出任何重复主体 → 降级待人工复核。
            # 引文保留（真实摘录，供人工核对），仅翻转状态并标注原因。
            status = "unknown"
            duplicate_unverified = True
        elif (
            status in ("fail", "warn")
            and doc_names
            and _unresolved_file_reference(item, doc_names)
        ):
            # fail/warn 引用的文件全部解析不到真实送审文件 → 疑似臆造文件/无来源引用，
            # 降级待人工复核，杜绝「引用不存在的投标文件」这类幻觉结论。
            status = "unknown"
            file_unresolved = True
        elif (
            status in ("fail", "warn")
            and str(rule.get("id") or "") in _TYPO_RULE_SCOPE
            and _typo_claim_is_entity_equivalence(item)
        ):
            # 错别字规则把「两个不同的主体名称互为正误」误判为错别字：专有名词不是
            # 行文错别字，应判定为无错别字(pass)，避免把合法公司名标成错别字。
            status = "pass"
            typo_entity = True
        try:
            confidence = float(item.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        base = {
            "rule_id": rule_id,
            "rule_name": rule["name"],
            "category": rule.get("category", ""),
            "severity": rule.get("severity", "major"),
            "status": status,
            "title": str(item.get("title") or "")[:300],
            "detail": str(item.get("detail") or ""),
            "evidence": str(item.get("evidence") or "")[:1200],
            "location": str(item.get("location") or ""),
            "suggestion": str(item.get("suggestion") or ""),
            "legal_basis": str(item.get("legal_basis") or ""),
            "involved_files": [str(x) for x in (item.get("involved_files") or [])],
            "confidence": max(0.0, min(1.0, confidence)),
            # 错别字类识别的结构化信息（用于校验集去噪）；多错字时下方逐条拆分
            "typo": None,
        }
        if corrected_pass:
            # 纠偏为通过：清空定位类字段——「未发现违规」的结论没有原文位置可言，
            # 保留 evidence（如「无」）只会诱导定位匹配到无关单字（如正文中的「无」）。
            base["status"] = "pass"
            base["evidence"] = ""
            base["location"] = ""
            base["involved_files"] = []
            base["status_corrected"] = True
            base["detail"] = (
                base["detail"]
                + "\n\n（系统纠偏：结论文本明确为「未发现违规」，"
                "模型误标的 fail 已自动纠正为「通过」。）"
            ).strip()
        if contradiction:
            # 模型结构化字段标 fail，但结论文本自相矛盾（文本倾向 pass/不构成违规）。
            # 降级为「待人工复核」，并在结论中标注，避免把本应通过的结论误判为不合规。
            base["detail"] = (
                base["detail"]
                + "\n\n⚠️ 模型在结构化字段判定为 fail，但结论文本自相矛盾"
                "（文本倾向 pass/不构成违规），已自动降级为「待人工复核」，避免误判不合规。"
            ).strip()
            base["contradiction_detected"] = True
        if evidence_missing:
            # fail/warn 无原文依据：清掉占位词 evidence 与定位类字段，交人工复核。
            base["evidence"] = ""
            base["location"] = ""
            base["involved_files"] = []
            base["evidence_missing"] = True
            base["detail"] = (
                base["detail"]
                + "\n\n⚠️ 模型判定为不合规/存疑，但未提供可核验的原文依据"
                "（evidence 为「无」类占位词），已自动降级为「待人工复核」，"
                "请人工核实原文后再判定。"
            ).strip()
        if duplicate_unverified:
            # 重复类断言无实证：引文里比对不出重复主体，状态降级、引文保留。
            base["duplicate_unverified"] = True
            base["detail"] = (
                base["detail"]
                + "\n\n⚠️ 结论断言存在「重复/雷同」，但对结论引用的原文逐一比对后"
                "未发现任何实际重复的主体名称，断言缺乏依据，已自动降级为"
                "「待人工复核」，请人工核实。"
            ).strip()
        if file_unresolved:
            # 引用了本次未送审的文件：结论依据的文件不存在，状态降级、清空定位类字段。
            base["evidence"] = ""
            base["location"] = ""
            base["involved_files"] = []
            base["file_unresolved"] = True
            cited = "、".join(str(x) for x in (item.get("involved_files") or []))
            base["detail"] = (
                base["detail"]
                + f"\n\n⚠️ 结论引用的文件「{cited}」不在本次送审文件范围内，"
                "属于无来源引用，已自动降级为「待人工复核」，请人工核实原文。"
            ).strip()
        if typo_entity:
            # 错别字结论把两个主体名称(公司名)当成正误关系：专有名词不是行文错别字，
            # 纠正为「通过」(无错别字)，避免把合法公司名称标成错别字。
            base["status"] = "pass"
            base["evidence"] = ""
            base["location"] = ""
            base["involved_files"] = []
            base["typo"] = None
            base["typo_entity_equivalence"] = True
            base["detail"] = (
                "（系统纠偏：本条为错别字审查，但结论把两个不同的主体名称"
                "（如公司名）当作正误关系，专有名词不是行文错别字，已自动判定为"
                "「通过」。若确属名称笔误请人工在对应业务规则下复核。）"
            ).strip()
        if tender_mismatch:
            # 模型被招标文件摘要带偏，把「人员要求/资格要求/否决投标情形」等
            # 其他关注点的结论写到了当前规则下，与当前规则说明完全脱节。
            # 降级为待人工复核，并保留原结论供人工核对。
            base["status"] = "unknown"
            base["tender_mismatch"] = True
            base["detail"] = (
                base["detail"]
                + f"\n\n⚠️ 模型结论内容与当前规则「{rule.get('name','')}」的审核范围不一致："
                f"出现了「{tender_mismatch}」等招标文件摘要中的关注点，而当前规则并不负责审查该项。"
                "已自动降级为「待人工复核」，请人工核对是否应归入其他规则。"
            ).strip()
        typos = _collect_typos(item, rule_id)
        if not typos:
            findings.append(base)
        elif len(typos) == 1:
            base["typo"] = typos[0]
            findings.append(base)
        else:
            # 同一条规则命中多处错别字 → 拆为多条独立结论，逐条列出/记录
            n = len(typos)
            for i, tp in enumerate(typos):
                fc = dict(base)
                fc["typo"] = tp
                wrong = tp.get("wrong") or ""
                correct = tp.get("correct") or ""
                ctx = tp.get("context") or ""
                fc["title"] = f"错别字/错用词：『{wrong}』应改为『{correct}』"
                ctx_part = f"：{ctx}" if ctx else ""
                fc["detail"] = (
                    f"{base['detail']}\n\n【错别字 {i + 1}/{n}】『{wrong}』"
                    f"应改为『{correct}』{ctx_part}"
                ).strip()
                fc["suggestion"] = f"将『{wrong}』修正为『{correct}』"
                fc["evidence"] = ctx or base["evidence"]
                findings.append(fc)

    # 模型漏答的规则显式标记，避免静默丢项
    for rule in batch:
        if rule["id"] not in seen:
            findings.append(
                {
                    "rule_id": rule["id"], "rule_name": rule["name"],
                    "category": rule.get("category", ""),
                    "severity": rule.get("severity", "major"),
                    "status": "unknown", "title": "模型未返回该规则结论",
                    "detail": "本条规则未获得有效审核结论，建议重试或人工复核。",
                    "evidence": "", "location": "", "suggestion": "人工复核",
                    "legal_basis": "", "involved_files": [], "confidence": 0.0,
                }
            )
    # 分段校对的去重收尾：同一错别字可能被多个段落重复报告（尤其段边界处），
    # 「规则 + 错字 + 正字 + 上下文」四项完全相同者只保留一条；
    # 而同一错字在文档不同位置出现时 context 不同，属彼此独立的实例，必须逐条保留。
    # 仅对含 typo 的结论做指纹去重 —— 无 typo 的结论若也按此键去重，
    # 会因 wrong/correct/context 皆为空而被误判重复，导致同一规则的多条问题被吞掉。
    deduped: list[dict[str, Any]] = []
    seen_fp: set[tuple[str, str, str, str]] = set()
    for f in findings:
        tp = f.get("typo") or {}
        wrong = str(tp.get("wrong") or "")
        if wrong:
            fp = (
                str(f.get("rule_id") or ""),
                wrong,
                str(tp.get("correct") or ""),
                str(tp.get("context") or ""),
            )
            if fp in seen_fp:
                continue
            seen_fp.add(fp)
        deduped.append(f)
    return deduped


def _normalize_finding_deterministic(f: dict[str, Any]) -> dict[str, Any]:
    """对单条确定性结论做字段归一（status/severity 小写化、补 deterministic 标记）。

    仅在 det_mode=True 时被调用；不修改 status 的语义判定，仅归一形态，
    使确定性锁定结论与 LLM 辅助结论在落库/聚合时形态一致。
    （此前该引用缺失，会在 deterministic_mode=True 时 NameError；此处补齐。）
    """
    if not isinstance(f, dict):
        return f
    out = dict(f)
    st = str(out.get("status") or "unknown").lower()
    if st not in ("pass", "fail", "warn", "unknown"):
        st = "unknown"
    out["status"] = st
    sev = str(out.get("severity") or "major").lower()
    if sev not in ("critical", "major", "minor", "info"):
        sev = "major"
    out["severity"] = sev
    out["deterministic"] = True
    return out


_LOCATION_MAX_PER_FINDING = 10


def _norm_filename(name: str, strip_copy_suffix: bool = False) -> str:
    """文件名归一化：去扩展名、去空白与全半角差异，用于模糊归属匹配。

    strip_copy_suffix=True 时额外剥去尾部「(1)/(2)」类副本后缀（LLM 写
    involved_files 时常带下载副本名），作为二级匹配键。
    """
    import re as _re
    import unicodedata as _ud

    s = str(name or "").strip()
    s = _re.sub(r"\.[A-Za-z0-9]{1,5}$", "", s)
    if strip_copy_suffix:
        s = _re.sub(r"[\s(（]*[(（]\d+[)）][\s)）]*$", "", s)
    s = _ud.normalize("NFKC", s)
    return _re.sub(r"\s+", "", s).lower()


def _pdf_locate_rects(
    path: str | None, page_no: int | None, snippet: str | None
) -> list[dict[str, Any]]:
    """用 PyMuPDF 在 PDF 原始页面上检索锚点，返回精确矩形坐标。

    坐标口径：PDF 坐标系（原点左下、y 向上），与 PDF.js viewport 的
    convertToViewportRectangle 直接兼容。检索失败（文件缺失/未命中）返回 []，
    不伪造坐标。
    """
    if not path or not snippet or not page_no:
        return []
    try:
        import fitz

        with fitz.open(path) as pdf:
            if page_no < 1 or page_no > pdf.page_count:
                return []
            page = pdf[page_no - 1]
            # search_for 对空白宽松；依次尝试原文片段 → 压缩空白 → 前缀
            candidates = [snippet]
            squeezed = "".join(snippet.split())
            if squeezed and squeezed != snippet:
                candidates.append(squeezed)
            prefix = snippet.strip()[:30]
            if len(prefix) >= 6 and prefix not in candidates:
                candidates.append(prefix)
            rects: list = []
            for cand in candidates:
                rects = page.search_for(cand)
                if rects:
                    break
            page_h = page.rect.height
            return [
                {
                    "page": page_no,
                    "x0": round(r.x0, 2),
                    "y0": round(page_h - r.y1, 2),  # fitz 原点左下→PDF 原点左下
                    "x1": round(r.x1, 2),
                    "y1": round(page_h - r.y0, 2),
                }
                for r in rects[:8]
            ]
    except Exception:  # 坐标属增强信息，任何失败都不影响定位主流程
        return []


def _attach_locations(
    findings: list[dict[str, Any]], docs: list[dict[str, Any]]
) -> None:
    """为每条结论就地附加结构化原文定位信息 ``locations``。

    定位口径（与 /api/files/locate、/api/files/{id}/preview 完全同源）：
    - involved_files（文件名）→ 本次审核上传文档映射出 file_id / ext（精确匹配
      后回退归一化匹配）；filename 一律以文件记录的真实名为准，LLM 写的
      involved_files 名称可能不精确，不做展示口径；
    - 锚点文本按 evidence → detail → title 渐进回退，用 text_locator 三级匹配
      （精确 → 归一化 → 模糊）在提取全文中定位，命中后换算绝对字符下标与页码
      （PDF 提取文本自带 [第N页] 标记，页码与文档真实页一致）；
    - PDF 额外用 PyMuPDF 在原始页面检索锚点，给出 rects 精确坐标（PDF 坐标系），
      供前端/第三方在原始页面上绘制高亮。

    每项结构：{file_id, filename, ext, page, page_label, page_count,
    char_start, char_end, snippet, matched, match_type, rects}。

    ⚠️ 只附加「真正命中」的定位（matched=True）：完整性检查等不涉及原文定位的
    结论锚点在正文中无命中，此时**不设置 locations 字段**（而非返回
    matched=False 的空壳条目），接口消费方可直接用「有无 locations」判断可定位性。
    """
    by_name: dict[str, dict[str, Any]] = {}
    norm_index: dict[str, dict[str, Any]] = {}
    norm_index2: dict[str, dict[str, Any]] = {}  # 二级键：剥副本后缀
    for d in docs:
        name = str(d.get("filename") or "").strip()
        if not name:
            continue
        by_name.setdefault(name, d)  # 同名文件取第一个（上传侧已禁重名）
        nk = _norm_filename(name)
        if nk:
            norm_index.setdefault(nk, d)
        nk2 = _norm_filename(name, strip_copy_suffix=True)
        if nk2:
            norm_index2.setdefault(nk2, d)

    def _resolve(name: str) -> dict[str, Any] | None:
        doc = by_name.get(name)
        if doc is not None:
            return doc
        return (
            norm_index.get(_norm_filename(name))
            or norm_index2.get(_norm_filename(name, strip_copy_suffix=True))
            or None
        )

    for f in findings:
        # involved_files 清洗：模型会编造「项目名称」等表头词/泛称，凡解析不到
        # 本次审核文档的一律剔除，并以真实文件名回写，避免结果页出现幻觉文件分组。
        names_all = [str(x) for x in (f.get("involved_files") or []) if str(x).strip()]
        names: list[str] = []
        resolved_docs: list[dict[str, Any]] = []
        for name in names_all[:_LOCATION_MAX_PER_FINDING]:
            doc = _resolve(name)
            if doc is None:
                continue  # 文件已不在本次审核范围 → 不伪造归属
            real = str(doc.get("filename") or name)
            if real not in names:
                names.append(real)
                resolved_docs.append(doc)
        f["involved_files"] = names
        # 通过结论不携带原文定位：「未发现违规」没有位置可言，且其 evidence
        # 常为「无」之类的占位词，参与定位只会匹配到正文无关单字。已有 locations
        # 的旧缓存也一并剥离，保证「status=pass ⇒ locations 为空」这一接口不变量。
        if str(f.get("status") or "") == "pass":
            f.pop("locations", None)
            f["location"] = ""  # 通过结论同样不保留臆造的位置文本
            continue
        if f.get("locations"):
            continue  # 已带定位（如缓存回放的旧结构）不重复计算
        # 锚点候选过滤：过短的候选（如「无」「是」）会在全文里随机命中单字，
        # 产生错误定位；少于 4 字的候选不参与三级匹配。
        candidates = [
            c
            for c in (f.get("evidence"), f.get("detail"), f.get("title"))
            if isinstance(c, str) and len(c.strip()) >= 4
        ]
        locs: list[dict[str, Any]] = []
        for doc in resolved_docs:
            name = str(doc.get("filename") or "")
            text = str(doc.get("text") or "")
            if not text.strip():
                continue
            if not candidates:
                continue  # 锚点全是「无」这类占位词 → 无有效定位依据，不产定位
            hit = text_locator.locate(
                text,
                candidates[0],
                context_chars=0,
                location=str(f.get("location") or "") or None,
                candidates=candidates,
            )
            if not hit:
                continue  # 锚点未命中正文（如完整性结论）→ 不产定位
            pages = text_locator.split_pages(text)
            pg = text_locator.page_for_offset(pages, hit["start"])
            loc: dict[str, Any] = {
                "file_id": doc.get("file_id"),
                "filename": doc.get("filename") or name,
                "ext": doc.get("ext"),
                "matched": True,
                "match_type": hit.get("match_type"),
                "char_start": int(hit["start"]),
                "char_end": int(hit["end"]),
                "snippet": text[hit["start"] : hit["end"]],
                "page": int(pg["page"]) if pg else None,
                "page_label": pg["label"] if pg else None,
                "page_count": int(pages[-1]["page"]) if pages else None,
                "rects": [],
            }
            # PDF 增强：在原始文件页面上检索精确坐标（失败仅少坐标，不影响主定位）
            if str(doc.get("ext") or "").lower().lstrip(".") == "pdf":
                loc["rects"] = _pdf_locate_rects(
                    doc.get("path"), loc["page"], loc["snippet"]
                )
            locs.append(loc)
        if locs:
            f["locations"] = locs
        # 规范化 location 自由文本：模型常写「附件1 和附件2 第1页」这类泛称/臆造位置。
        # 命中 → 以真实文件名+页码重写；未命中/pass/完整性类 → 清空，不让臆造文本外漏。
        if locs:
            first = locs[0]
            fname = str(first.get("filename") or "")
            pg = first.get("page")
            f["location"] = f"{fname} 第{pg}页" if pg else fname
        else:
            f["location"] = ""


def _aggregate_rule_results(
    findings: list[dict[str, Any]], rules: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """按 rule_id 把（分段/多文档并行产生的）N 条结论聚合为「每规则一条」最终结果。

    保证 20 个规则最终仅输出 20 条审核结果：同一条规则的若干问题合并到一条规则结果内，
    状态取 fail>warn>unknown>pass 的最严重项，并保留问题计数与样例，供前端「审核结果」
    按规则展示，避免长文档被拆分并行审核后同一规则出现多条散落的并行结果。
    """
    by_rule: dict[str, list[dict[str, Any]]] = {}
    for f in findings:
        rid = str(f.get("rule_id") or "")
        by_rule.setdefault(rid, []).append(f)
    order = {"fail": 0, "warn": 1, "unknown": 2, "pass": 3}
    name_of = {v: k for k, v in order.items()}

    def _file_results(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把一条规则下的若干结论按「文件」维度归组，供前端从规则下钻查看逐条结论。

        归组口径：按 finding.involved_files 归属——跨文件结论（involved_files 列多个文件）
        归入其列出的每个文件（该结论确实同时涉及这些文件）；未关联文件的结论（如确定性
        规则结论）归入「（未关联文件）」桶，保证下钻不丢条目。文件组的 status 取组内
        最严重结论，issue_count 统计组内 fail/warn/unknown 条数。
        """

        def _status_of(it: dict[str, Any]) -> str:
            return str(it.get("status") or "pass")

        groups: dict[str, list[dict[str, Any]]] = {}
        for it in items:
            files = [str(x) for x in (it.get("involved_files") or []) if str(x).strip()]
            for fname in files or ["（未关联文件）"]:
                groups.setdefault(fname, []).append(it)
        out_files: list[dict[str, Any]] = []
        for fname, its in groups.items():
            worst = min((order.get(_status_of(it), 3) for it in its), default=3)
            out_files.append(
                {
                    "file": fname,
                    "status": name_of[worst],
                    "issue_count": sum(
                        1 for it in its if _status_of(it) in ("fail", "warn", "unknown")
                    ),
                    "findings": [
                        {
                            "status": _status_of(it),
                            "title": str(it.get("title") or ""),
                            "detail": str(it.get("detail") or ""),
                            "evidence": str(it.get("evidence") or ""),
                            "location": str(it.get("location") or ""),
                            "suggestion": str(it.get("suggestion") or ""),
                            "confidence": it.get("confidence"),
                            # 结构化原文定位（_attach_locations 已在聚合前附加）
                            "locations": it.get("locations") or [],
                        }
                        for it in its
                    ],
                }
            )
        # 文件名排序保证多次查询返回顺序稳定
        out_files.sort(key=lambda x: x["file"])
        return out_files

    out: list[dict[str, Any]] = []
    for r in rules:
        rid = str(r.get("id") or "")
        items = by_rule.get(rid, [])
        if items:
            worst = min(
                (order.get(str(it.get("status") or "pass"), 3) for it in items),
                default=3,
            )
            status = name_of[worst]
            issue_count = sum(
                1 for it in items if str(it.get("status") or "pass") in ("fail", "warn", "unknown")
            )
            samples = [
                it.get("title", "")
                for it in items
                if str(it.get("status") or "pass") in ("fail", "warn")
            ][:5]
        else:
            status, issue_count, samples = "pass", 0, []
        out.append(
            {
                "rule_id": rid,
                "rule_name": r.get("name", ""),
                "category": r.get("category", ""),
                "severity": r.get("severity", "major"),
                "status": status,
                "issue_count": issue_count,
                "samples": samples,
                # 文件级结论明细：前端可从规则行下钻，查看该规则针对每个文件的逐条结论
                "file_results": _file_results(items),
            }
        )
    return out
    """确定性归一化：消除 LLM 输出在离散字段上的微小漂移，使多次审核结果稳定可比。

    仅对「结构化离散字段」做归一（status/severity 统一小写、空白折叠），不改变结论语义。
    正文类字段（title/detail/evidence）保持原样，避免破坏证据可读性。
    """
    f = dict(f)
    status = str(f.get("status") or "unknown").strip().lower()
    if status not in ("pass", "fail", "warn", "unknown"):
        status = "unknown"
    f["status"] = status
    sev = str(f.get("severity") or "major").strip().lower()
    if sev not in ("critical", "major", "minor", "info"):
        sev = "major"
    f["severity"] = sev
    # confidence 归一为两位小数，消除浮点精度噪声
    try:
        f["confidence"] = round(float(f.get("confidence") or 0.0), 2)
    except (TypeError, ValueError):
        f["confidence"] = 0.0
    return f


# --------------------------------------------------------------------------- #
# 确定性规则引擎：结构化可执行条件预检（需求 1.2 / 3.3）
# --------------------------------------------------------------------------- #
import re as _re


def _as_field_list(field) -> list[str]:
    """将 structured 字段的 a_field/b_field 归一为候选名列表（兼容字符串与列表）。"""
    if field is None:
        return []
    if isinstance(field, (list, tuple)):
        return [str(x).strip() for x in field if str(x).strip()]
    return [str(field).strip()] if str(field).strip() else []


def _extract_amount_near(text: str, field) -> float | None:
    """在文本中抽取 field 字段名附近的金额（兼容 万/千/亿 单位），统一换算为「元」。

    field 可为字符串或候选名列表（按顺序尝试）。用于金额对（如暂估价 vs 中标价）的
    确定性抽取与差值比较，避免依赖 LLM 的数值解析（LLM 易把「差额65400元」误读为 fail）。

    抽取策略：优先在字段名「之后」搜索（字段名在前、金额在后的主流语序，如「暂估价44万元」），
    向后窗口不会误吞前序其他字段的金额；仅当向后搜索失败时，才向前兜底（覆盖「44万元暂估价」）。
    后置单位缺失时还会识别「单位前置」写法（如「暂估价(万元) 172.7」→ 1,727,000 元）。
    """
    if not text:
        return None
    candidates = _as_field_list(field)
    if not candidates:
        return None

    def _apply_unit(val: float, unit: str | None) -> float:
        mult = 1.0
        if unit == "万":
            mult = 10000.0
        elif unit == "亿":
            mult = 100000000.0
        elif unit == "千":
            mult = 1000.0
        return val * mult

    def _leading_unit(prefix: str) -> str | None:
        """识别「单位前置」写法（如「暂估价(万元) 172.7」→ 万）。

        表头/分项表里单位常写在数字**前面**，此时数字后面没有单位，只按后置单位解析
        会把 172.7 万元读成 172.7 元，与「中标价 152万元」(1,520,000 元) 相差百万倍，
        差值比较必然失真。故后置单位缺失时，回看数字前的短前缀是否就是单位声明。
        前缀限长 4 字——过长说明中间夹了别的词（如「(万元)：本次招标控制价 5000」），
        单位与数字已非直接修饰关系，此时宁可不套用，避免错乘。
        """
        s = _re.sub(r"[\s:：为（(）)\[\]【】,，]", "", prefix or "")
        if not s or len(s) > 4:
            return None
        for unit in ("万元", "亿元", "千元"):
            if s.endswith(unit):
                return unit[0]
        return None

    for cand in candidates:
        idx = text.lower().find(cand.lower())
        if idx < 0:
            continue
        start = idx + len(cand)
        # 1) 优先向后：字段名之后 60 字内首个金额
        fwd = text[start: start + 60]
        m = _re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(万|千|亿)?", fwd)
        if m:
            try:
                unit = m.group(2) or _leading_unit(fwd[: m.start()])
                return _apply_unit(float(m.group(1)), unit)
            except (TypeError, ValueError):
                pass
        # 2) 兜底向前：字段名之前 30 字内最后一个金额（如「44万元暂估价」）
        bwd = text[max(0, idx - 30): idx]
        nums = list(_re.finditer(r"([0-9]+(?:\.[0-9]+)?)\s*(万|千|亿)?", bwd))
        if nums:
            m = nums[-1]
            try:
                return _apply_unit(float(m.group(1)), m.group(2))
            except (TypeError, ValueError):
                pass
    return None


def _opt_float(val: Any) -> float | None:
    """宽松转 float：None/空串/非法值 → None（区分「未配置」与「阈值就是 0」）。"""
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _pair_is_violation(diff: float, threshold: float, fail_when: str) -> bool:
    """两金额差值 diff 是否构成「违规（fail）」。

    fail_when 语义：le=差值≤阈值即违规（如「高度相似」），lt=差值<阈值，
    ge=差值≥阈值，gt=差值>阈值。默认 le。
    """
    if fail_when == "lt":
        return diff < threshold
    if fail_when == "ge":
        return diff >= threshold
    if fail_when == "gt":
        return diff > threshold
    return diff <= threshold


_NO_VIOLATION_MARKS = (
    "未发现", "未存在", "不存在", "未见", "未出现", "未涉及",
    "均符合", "均满足", "符合要求", "满足要求", "不构成",
)
# 与违规语义搭配的名词：单独「未发现」不够（可能后面接真实问题），需「未发现+违规对象」
_VIOLATION_NOUNS = (
    "违规", "违法", "否决", "不合规", "异常", "问题", "错误", "差错",
    "缺失", "缺少", "隐瞒", "造假", "弄虚作假", "矛盾", "不一致",
    "出入", "抵触", "违背", "串标", "围标", "废标", "无效投标",
)
# 结语句中出现这些信号说明后半句在报真实问题，不能按「未发现违规」翻转为 pass
_FAIL_TAIL_MARKS = (
    "但", "然而", "不过", "已发现", "发现存在", "发现有", "存在", "不足",
    "超出", "低于", "高于", "遗漏", "未按", "缺少", "逾期", "低于",
)

# —— 无依据结论防护：fail/warn 但 evidence 是占位词 → 结论没有可核验的原文支撑 ——
_PLACEHOLDER_EVIDENCE = {
    "无", "暂无", "没有", "没有发现", "未找到", "未提供", "未见", "未提供原文",
    "无原文", "无原文依据", "无依据", "无相关依据", "无相关原文", "不适用",
    "—", "-", "/", "／", "n/a", "na", "none", "null",
}
# 缺失/漏附类违规天然没有可摘录的原文（违规内容本身就是「不存在」），
# detail/title 命中这些标记时不视为「无依据」，保留原判定。
_ABSENCE_VIOLATION_MARKS = (
    "缺少", "缺失", "未提供", "未附", "未提交", "未出具", "未签署", "未盖章",
    "未签字", "遗漏", "未列明", "不齐全", "不完整", "未载明", "未注明", "未标注",
)


def _evidence_is_placeholder(evidence: Any) -> bool:
    """evidence 是否为「无」类占位词或空串（规范化后比对，忽略标点与大小写）。"""
    s = str(evidence or "").strip().strip("。．：:，, ；;、").lower()
    return (not s) or s in _PLACEHOLDER_EVIDENCE


def _finding_has_alternative_basis(item: dict[str, Any]) -> bool:
    """evidence 为占位词时，判断 detail/title 是否自带可核验的判定依据。

    满足任一条即视为「有依据」，不触发降级：
    - detail/title 报的是缺失/漏附类违规（天然无原文可摘录）；
    - detail 里带引号原文摘录（「」『』“” 引号内 ≥6 字）。
    """
    blob = " ".join([str(item.get("detail") or ""), str(item.get("title") or "")])
    if any(m in blob for m in _ABSENCE_VIOLATION_MARKS):
        return True
    if re.search(r"[「『“\"][^」』”\"]{6,}[」』”\"]", blob):
        return True
    return False


def _conclusion_sentence(item: dict[str, Any]) -> str:
    """取结论的「结语句」：detail+title 按句切分后的最后一个非空句。

    模型被要求在 detail 末尾写结论，矛盾纠偏只看结语句，
    避免被前文「摘录原文」里的字样误触发。
    """
    text = " ".join([str(item.get("detail") or ""), str(item.get("title") or "")])
    parts = [
        s.strip()
        for s in re.split(r"[。；;！!？?\n]", text)
        if s.strip()
    ]
    return parts[-1] if parts else text.strip()


def _has_non_negated(seg: str, marks: tuple[str, ...]) -> bool:
    """seg 中是否存在未被「不/未/无」否定的标记词。

    修复子串误伤：「不符合要求」包含「符合要求」、「不存在问题」包含「存在」，
    若直接用 ``in`` 判断会把真实问题句误判成「未发现违规」而纠偏为 pass。
    标记词前一字为否定前缀时该次出现不计。
    """
    for m in marks:
        start = 0
        while True:
            i = seg.find(m, start)
            if i < 0:
                break
            prev = seg[i - 1] if i > 0 else ""
            if prev not in ("不", "未", "无"):
                return True
            start = i + 1
    return False


# —— 重复类断言无实证防护：fail/warn 断言「重复/雷同」但引文里比对不出重复主体 ——
# 案例：四个完全不同的公司名称被模型断言「所有投标公司名称均重复」。
_DUPLICATE_CLAIM_MARKS = (
    "均重复", "名称重复", "重复投标", "重复的投标", "投标人重复", "重复出现",
    "相互重复", "存在重复", "雷同", "完全相同", "高度雷同",
)
# 主体名称 token（公司/机构等），非贪婪前缀 + 常见组织后缀
_ENTITY_TOKEN_RE = re.compile(
    r"[\u4e00-\u9fa5A-Za-z0-9（）()·]{3,40}?"
    r"(?:股份有限公司|有限责任公司|有限公司|集团公司|集团|事务所|研究院|研究中心|分公司|公司)"
)


def _duplicate_claim_unsubstantiated(item: dict[str, Any]) -> bool:
    """结论断言「重复/雷同/完全相同」，但引用的原文中不存在实际重复的主体名称。

    触发前提：detail/title 含重复类断言词，且能从 detail/evidence 中提取到
    ≥2 个主体名称 token（提取不到说明无法证伪，不触发本防护，交人工/原判定）。
    认定口径：全名归一化（去空白、忽略大小写）后完全一致才算重复；
    简称/别名/分公司不与总公司视为重复（与 dec-09 口径一致，宁漏勿误）。
    """
    blob = " ".join(
        [
            str(item.get("detail") or ""),
            str(item.get("title") or ""),
            str(item.get("evidence") or ""),
        ]
    )
    if not any(m in blob for m in _DUPLICATE_CLAIM_MARKS):
        return False
    # 在单个来源（evidence 或 detail）内提取主体并比对重复。
    # 不做跨字段合并——detail 转述与 evidence 摘录同批名称属正常重述，
    # 合并计数会把「同名重述」误判为「存在重复」而漏防。
    for text in (str(item.get("evidence") or ""), str(item.get("detail") or "")):
        # 先去空白再提取：名称内的空格/换行不改变主体同一性（「南京 ABC 公司」≡「南京ABC公司」）
        compact = re.sub(r"\s+", "", text)
        tokens = [t.lower() for t in _ENTITY_TOKEN_RE.findall(compact)]
        if len(tokens) >= 2 and len(tokens) == len(set(tokens)):
            return True  # 单一来源内 ≥2 个主体且互不相同 → 「重复」断言无实证
    return False


def _doc_names_from(docs: Any) -> list[str]:
    """从送审文档列表提取真实文件名集合（用于反幻觉：校验结论引用的文件是否真实存在）。"""
    names: list[str] = []
    if not isinstance(docs, (list, tuple)):
        return names
    for d in docs:
        if isinstance(d, dict):
            n = str(d.get("filename") or d.get("file_name") or "").strip()
            if n:
                names.append(n)
    return names


def _unresolved_file_reference(item: dict[str, Any], doc_names: list[str]) -> bool:
    """fail/warn 结论引用的文件全部解析不到真实送审文件 → 疑似臆造文件。

    触发前提：involved_files 非空且其中没有任何一个能匹配到本次送审的真实文件名
    （精确相等 / 互为子串）。命中说明模型把「未上传的文件」写进了结论，
    属于无来源的幻觉引用（如把文档里没有的「XX公司投标文件」当依据）。
    仅做精确的 involved_files 匹配，不做正文语义猜测，避免误伤合法泛称引用。
    """
    if str(item.get("status") or "").lower() not in ("fail", "warn"):
        return False
    raw = [str(x).strip() for x in (item.get("involved_files") or []) if str(x).strip()]
    if not raw:
        return False
    names = set(doc_names or [])

    def resolves(n: str) -> bool:
        return any(d == n or d in n or n in d for d in names)

    # 全部引用的文件都解析不到真实送审文件 → 疑似臆造
    return all(not resolves(n) for n in raw)


# --------------------------------------------------------------------------- #
# 跨文件审查前置校验（缺件预筛）
# --------------------------------------------------------------------------- #
# 文档角色关键词 → 规范角色名。用于「对送审文件清单名称做预处理筛选」，确认跨文件
# 审查所需的各文档角色均已送审，避免因缺少指定文件而臆造或做单边不完整比对。
_DOC_ROLE_KEYWORDS: list[tuple[str, str]] = [
    ("评审报告", "评审报告"),
    ("评标报告", "评审报告"),
    ("定标结果", "定标结果"),
    ("定标审批", "定标结果"),
    ("定标批复", "定标结果"),
    ("中标通知书", "中标通知书"),
    ("中标结果通知书", "中标通知书"),
    ("招标文件", "招标文件"),
    ("投标文件", "投标文件"),
    ("资格证明", "资格证明"),
    ("资质证书", "资格证明"),
    ("营业执照", "资格证明"),
    ("承诺书", "承诺书"),
]
# 规则文本中以这些措辞声明的角色视为「可选」：缺失不阻断审查（如「（如有）」）。
_OPTIONAL_ROLE_MARKERS = ("如有", "（如有）", "(如有)", "可无", "如提供", "若提供", "可提供")


def _infer_doc_roles(doc: dict[str, Any]) -> list[str]:
    """从文件名/类型推断该送审文件可能对应的文档角色（用于跨文件规则缺件预筛）。"""
    name = str(doc.get("filename") or doc.get("file_name") or "")
    ftype = str(doc.get("file_type_name") or doc.get("file_type") or "")
    hay = f"{name} {ftype}"
    roles: list[str] = []
    for kw, canon in _DOC_ROLE_KEYWORDS:
        if kw in hay and canon not in roles:
            roles.append(canon)
    return roles


def _rule_required_roles(rule: dict[str, Any]) -> tuple[list[str], list[str]]:
    """从规则名称/说明抽取其跨文件审查所需的文档角色。

    返回 (required, optional)：required 为缺失即阻断审查的必需角色，
    optional 为规则中以「（如有）」等措辞声明、缺失不阻断的角色。
    """
    text = f"{rule.get('name') or ''} {rule.get('description') or ''}"
    found: list[str] = []
    for kw, canon in _DOC_ROLE_KEYWORDS:
        idx = text.find(kw)
        if idx >= 0 and canon not in found:
            found.append(canon)
    optional: list[str] = []
    for kw, canon in _DOC_ROLE_KEYWORDS:
        idx = text.find(kw)
        if idx < 0:
            continue
        # 仅取角色关键词紧邻后的少量字符判断「（如有）」等可选标记，
        # 窗口过大会把同句中其他角色的标记误判到本角色（如「中标通知书（如有）」
        # 的「（如有）」距前文「评审报告」仅十余字，过长窗口会误伤）。
        window = text[idx : idx + len(kw) + 8]
        if any(m in window for m in _OPTIONAL_ROLE_MARKERS):
            if canon not in optional:
                optional.append(canon)
    required = [r for r in found if r not in optional]
    return required, optional


def _is_cross_file_rule(rule: dict[str, Any]) -> bool:
    """判定规则是否涉及跨文件内容审查。"""
    if str(rule.get("category") or "") == "consistency":
        return True
    text = f"{rule.get('name') or ''} {rule.get('description') or ''}"
    return "跨文档" in text or "跨文件" in text


def _cross_file_precheck(
    rule: dict[str, Any], docs: list[dict[str, Any]]
) -> dict[str, Any]:
    """跨文件规则缺件前置校验。

    对送审文件清单名称做预处理筛选：确认规则所需的各文档角色均已送审。
    返回 dict：
      is_cross_file: 是否跨文件规则
      required/optional: 必需/可选角色
      present: 送审文件中实际命中的角色集合
      missing: 必需但送审文件中未识别到的角色（空=齐全）
      present_files: 命中角色的送审文件名
      confident_missing: 是否「有把握判定缺失」（已识别到至少一个角色文件、却缺另一必需角色）
                         ——仅此情形由后端硬性拦截，避免文件名过于泛化时误拦截。
    """
    is_cf = _is_cross_file_rule(rule)
    required, optional = _rule_required_roles(rule)
    present_roles: set[str] = set()
    present_files: list[str] = []
    for d in docs:
        for role in _infer_doc_roles(d):
            if role in required or role in optional:
                if role not in present_roles:
                    present_roles.add(role)
                    present_files.append(
                        str(d.get("filename") or d.get("file_name") or "?")
                    )
    required_present = [r for r in required if r in present_roles]
    missing = [r for r in required if r not in present_roles]
    # 是否「有把握判定缺失」并硬性拦截（避免文件名泛化时误拦截）：
    # - 规则未声明任何必需角色（如 dec-11 仅写「各文档」）无法判定，不拦截；
    # - 单文档内部一致性规则(必需角色仅 1 个)：仅当该角色缺失、且能识别到其他角色文件时才拦截；
    # - 多文档跨文件规则(必需角色≥2)：仅当可比对文档数 < 2（不足以做跨文件比对）时拦截，
    #   避免把「仅缺可选/额外来源文件」误判为无法审查（如 dec-09 缺中标通知书仍可比对报告vs定标）。
    if not required:
        confident_missing = False
    elif len(required) == 1:
        confident_missing = bool(present_roles) and not required_present
    else:
        confident_missing = bool(present_roles) and len(required_present) < 2
    return {
        "is_cross_file": is_cf,
        "required": required,
        "optional": optional,
        "present": sorted(present_roles),
        "missing": missing,
        "present_files": present_files,
        "confident_missing": confident_missing,
    }


def _make_cross_file_missing_finding(
    rule: dict[str, Any], pc: dict[str, Any], docs: list[dict[str, Any]]
) -> dict[str, Any]:
    """构造「因缺少指定文件而无法完成跨文件审查」的受控 unknown 结论。"""
    missing = "、".join(pc["missing"])
    present = "、".join(pc["present_files"]) or "（无匹配文件）"
    all_files = (
        "、".join(
            str(d.get("filename") or d.get("file_name") or "?") for d in docs
        )
        or "（无）"
    )
    detail = (
        f"本规则为跨文件审查，需比对以下文档角色：{ '、'.join(pc['required']) }"
        f"（可选：{ '、'.join(pc['optional']) or '无' }）。"
        f"经对送审文件清单名称预处理筛选，本次仅识别到：{ present }；"
        f"缺失必需文件角色：{ missing }。因缺少指定文件，无法完成跨文件比对，"
        f"为避免误审或产生不完整的审核结果，本规则标记为待人工复核。"
        f"本次共送审 { len(docs) } 个文件：{ all_files }。"
    )
    return {
        "rule_id": str(rule.get("id")),
        "rule_name": rule.get("name", ""),
        "category": rule.get("category", ""),
        "severity": rule.get("severity", "major"),
        "status": "unknown",
        "title": f"无法完成跨文件审查：缺少指定文件（{missing}）",
        "detail": detail,
        "evidence": "",
        "location": "",
        "suggestion": f"请补充送审缺失文件（{ missing }）后重新审核本规则。",
        "legal_basis": "",
        "involved_files": [],
        "confidence": 0.0,
        "typo": None,
        "cross_file_missing": True,
    }


def _build_file_manifest(docs: list[dict[str, Any]]) -> str:
    """构造送审文件清单（含推断角色），供提示词注入与跨文件预筛展示。"""
    if not docs:
        return "（本次未送审任何文件）"
    lines = []
    for i, d in enumerate(docs, 1):
        name = str(d.get("filename") or d.get("file_name") or f"文档{i}")
        roles = _infer_doc_roles(d)
        role_hint = (
            f"（推断角色：{ '、'.join(roles) }）" if roles else "（未识别到明确文档角色）"
        )
        lines.append(f"{i}. {name} {role_hint}")
    return "\n".join(lines)


_ENTITY_SUFFIXES = (
    "股份有限公司", "有限责任公司", "有限公司", "集团公司", "集团", "分公司",
    "事务所", "研究院", "研究中心", "学校", "医院", "公司",
)

# 错别字类规则范围：其结论若把专有名词当错别字，按「无错别字」纠正。
_TYPO_RULE_SCOPE = {"dec-17", "gen-typo"}


def _typo_claim_is_entity_equivalence(item: dict[str, Any]) -> bool:
    """错别字结论把「专有名词(公司/机构名)互为错别字」误判 → 应视为无错别字。

    判定口径(稳健，不依赖脆弱的正则配对)：结论(detail/title)同时满足——
      1) 声明了错别字类问题(错别字/错字/形近/音近)；
      2) 出现等价断言词(应为/而非/改为/应写作/纠正为/修正为)；
      3) 文本中能抽取到 ≥2 个不同的主体名称(以组织后缀结尾,如 有限公司/公司)。
    三者同时成立 → 模型是在把「两个不同的主体名称」当成正误关系，专有名词不是
    行文错别字，判定为无错别字(pass)。仅报单个主体名或仅普通行文错别字时不命中。
    """
    if str(item.get("status") or "").lower() not in ("fail", "warn"):
        return False
    blob = " ".join(
        [
            str(item.get("title") or ""),
            str(item.get("detail") or ""),
            str(item.get("evidence") or ""),
        ]
    )
    if not any(k in blob for k in ("错别字", "错字", "形近", "音近")):
        return False
    if not any(kw in blob for kw in ("应为", "而非", "改为", "应写作", "纠正为", "修正为")):
        return False
    # 抽取主体名称(以组织后缀结尾的 span)，归一化(去空白、忽略大小写)后去重。
    names = [re.sub(r"\s+", "", t).lower() for t in _ENTITY_TOKEN_RE.findall(blob)]
    distinct = {n for n in names if n}
    return len(distinct) >= 2


def _explicitly_reports_no_violation(item: dict[str, Any]) -> bool:
    """结语句明确写出「未发现/不存在 + 违规对象」等无违规结论。

    与 _finding_text_contradicts_fail（相似度规则的窄口径兜底）互补：
    本函数覆盖通用否定式结论（如「未发现否决投标情形」「不存在违法违规情形」），
    命中且句内无转折/真实问题信号时，模型却把 status 填成 fail —— 直接纠正为 pass。
    detail 的结语句与 title 任一命中即触发（title 常是一句话结论）。
    """
    for seg in (_conclusion_sentence({"detail": item.get("detail") or ""}),
                str(item.get("title") or "")):
        if not seg:
            continue
        if not _has_non_negated(seg, _NO_VIOLATION_MARKS):
            continue
        if not any(n in seg for n in _VIOLATION_NOUNS):
            continue
        # 先剥掉否定标记本身，再查失败信号（「不存在」含「存在」、「未发现」含「发现」的子串干扰）
        stripped = seg
        for m in _NO_VIOLATION_MARKS:
            stripped = stripped.replace(m, "")
        if not any(m in stripped for m in _FAIL_TAIL_MARKS):
            return True
    return False


def _finding_text_contradicts_fail(item: dict[str, Any]) -> bool:
    """检测模型自相矛盾的判定：结构化字段标 fail，但结论文本明确倾向 pass/不构成违规。

    用于兜底（方案 C）：当 LLM 被规则名「锚定」而误填 fail、又在其 detail 中自我否定时，
    降级为 unknown 交人工复核，避免把本应 pass 的结论错判为「不合规」。

    判定严格基于「文本是否给出可通过结论」或「显式否定相似性」：
    - 强信号：文本明确写出「应为pass/应为通过」等；
    - 显式否定相似性：文本写明「不构成/不构/不满足/并非 高度相似」等。
    注意：文本「构成高度相似，不满足独立报价要求」属真正 fail，不触发（否定的是别的要求）。
    """
    detail = " ".join(
        [str(item.get("detail") or ""), str(item.get("title") or "")]
    )
    if not detail.strip():
        return False
    # 强信号：文本明确给出「应通过」结论，却标 fail
    strong = [
        "应为pass", "应判为pass", "应判定为pass",
        "应为通过", "应判为通过", "应判定为通过",
    ]
    for s in strong:
        if s in detail:
            return True
    # 显式否定「相似性」本身：文本自认「不相似」即应判通过
    neg_similarity = [
        "不构成高度相似", "不构高度相似", "不构成相似", "不构相似",
        "不满足高度相似", "未达高度相似", "并非高度相似", "不属于高度相似",
        "不满足相似", "不构成相似判定",
    ]
    for s in neg_similarity:
        if s in detail:
            return True
    return False


def _apply_structured_rules(
    rules: list[dict[str, Any]], docs: list[dict[str, Any]], per_doc_full: list[str]
) -> dict[str, dict[str, Any]]:
    """对带 `structured` 可执行条件的规则做确定性预检，返回 {rule_id: finding}。

    这些规则的结论由「可计算条件」直接锁定（禁止关键词命中 / 必含要素缺失 / 正则 /
    金额阈值），不依赖 LLM 的随机判断——即需求 3.3「关键合规结论由确定性规则引擎给出」。
    LLM 仅对未结构化覆盖的规则做辅助判断，且不得推翻本层的 fail 结论。

    文本匹配按「规则关联文档类型」过滤后的子集：规则限定了 doc_types 时，仅对其匹配
    文件类型的文档做可计算判定；未限定则对全部文档判定。无匹配文档的规则直接跳过
    （不产生结论），与按规则审核路径的"按文档类型过滤"行为保持一致。
    """
    results: dict[str, dict[str, Any]] = {}
    for rule in rules:
        st = rule.get("structured")
        if not isinstance(st, dict):
            continue
        rid = str(rule.get("id") or "")
        # 按规则关联文档类型过滤适用文档，仅用其文本做判定
        types = _rule_doc_type_filter([rule])
        # 章节关联：规则指定了章节时，仅用命中章节的正文做可计算判定。
        # 注意此处必须用文档原文（per_doc_full 可能是超大文档的"分片摘要"，
        # 摘要里没有章节结构，无法按章节裁剪）。
        sec_ids = [
            str(x).strip() for x in (rule.get("section_ids") or []) if str(x).strip()
        ]
        if sec_ids:
            parts: list[str] = []
            for d in docs:
                if types is not None and (d.get("file_type") or None) not in types:
                    continue
                defs = sections_store.for_file_type(sec_ids, d.get("file_type"))
                if not defs:
                    continue
                body, _names = _scoped_text(d, defs)
                if body.strip():
                    parts.append(body)
            rule_text = "\n\n".join(parts) or ""
        elif types is None:
            rule_text = "\n\n".join(b for b in per_doc_full if b) or ""
        else:
            rule_idx = [i for i, d in enumerate(docs) if (d.get("file_type") or None) in types]
            rule_text = (
                "\n\n".join(per_doc_full[i] for i in rule_idx if i < len(per_doc_full)) or ""
            )
        if not rule_text.strip():
            continue  # 无匹配文档 → 跳过该规则，不执行审核
        text_lower = rule_text.lower()
        problems: list[str] = []

        # 1) 禁止关键词命中 → fail
        for kw in st.get("forbid_keywords", []) or []:
            if str(kw).strip() and str(kw).strip().lower() in text_lower:
                # 截取证据上下文（原文本，取首个命中位置前后 40 字）
                idx = text_lower.find(str(kw).strip().lower())
                ctx = rule_text[max(0, idx - 20): idx + len(kw) + 20]
                problems.append(f"命中禁止性关键词「{kw}」：…{ctx}…")

        # 2) 必含要素缺失 → fail
        for elem in st.get("require_elements", []) or []:
            if str(elem).strip() and str(elem).strip().lower() not in text_lower:
                problems.append(f"缺失必含要素「{elem}」")

        # 3) 正则模式
        for pat in st.get("regex_patterns", []) or []:
            pattern = pat.get("pattern")
            must_match = bool(pat.get("must_match", True))
            if not pattern:
                continue
            try:
                hit = bool(_re.search(pattern, rule_text or "", _re.IGNORECASE))
            except _re.error:
                continue
            if must_match and not hit:
                problems.append(f"未匹配必备正则模式：{pattern}")
            elif (not must_match) and hit:
                problems.append(f"命中应排除的正则模式：{pattern}")

        # 4) 金额阈值
        for amt in st.get("amount_thresholds", []) or []:
            field = amt.get("field")
            cap = amt.get("max")
            if not field or cap is None:
                continue
            # 单位感知抽取：兼容 万/千/亿（如「投标保证金：100万元」→ 1,000,000 元），
            # 与法规结构化提示（max 一律换算为元）保持同一量纲，避免「100万 > 80万上限」
            # 因裸数字 100 < 800000 而漏判。字段未出现或抽不出数字 → 跳过（回落 LLM）。
            val = _extract_amount_near(rule_text, field)
            if val is not None:
                try:
                    if val > float(cap):
                        problems.append(
                            f"「{field}」金额 {val:,.0f}元 超过上限 {float(cap):,.0f}元"
                        )
                except (TypeError, ValueError):
                    pass

        # 5) 两金额差值比较（如：暂估价 vs 中标价 是否高度相似）
        #    与 1~4 不同，本类条件必然产出确定性结论（fail 或 pass），故始终锁定该规则，
        #    不回落 LLM（避免模型把「差额65400元」误判为 fail，见方案 A/一致性预筛同类教训）。
        pair_pass_notes: list[str] = []
        pair_missing = False
        for pd in st.get("amount_pair_diff", []) or []:
            a_fields = _as_field_list(pd.get("a_field") or pd.get("a"))
            b_fields = _as_field_list(pd.get("b_field") or pd.get("b"))
            if not a_fields or not b_fields:
                continue
            fail_when = str(pd.get("fail_when", "le")).lower()
            abs_thr = _opt_float(pd.get("max_abs_diff"))
            ratio = _opt_float(pd.get("max_ratio"))
            if abs_thr is None and ratio is None:
                continue
            ratio_base = str(pd.get("ratio_base", "a") or "a").strip().lower()
            if ratio_base not in ("a", "b"):
                ratio_base = "a"
            va = _extract_amount_near(rule_text, a_fields)
            vb = _extract_amount_near(rule_text, b_fields)
            if va is None or vb is None:
                # 任一金额抽取失败 → 无法判定，标记缺失（不伪造 pass，回落 LLM）
                pair_missing = True
                continue
            diff = abs(va - vb)
            # 阈值取用优先级：相对阈值（基准值 × 比例）> 绝对阈值（元）。
            # 相对阈值用于「差值小于暂估价的 10%」这类随基准浮动的规则——此前只能用
            # 固定金额近似或直接回落 LLM，导致同一文件反复出现「文本算对、结构化
            # 字段填错」的自相矛盾结论（被降级为待人工复核）。
            ratio_note = ""
            if ratio is not None:
                base_val = va if ratio_base == "a" else vb
                if base_val <= 0:
                    pair_missing = True
                    continue
                threshold = base_val * ratio
                ratio_note = (
                    f"，差异率 {diff / base_val * 100:.2f}%"
                    f"（阈值 {ratio * 100:.2f}%，以 {ratio_base.upper()} 为分母）"
                )
            else:
                threshold = float(abs_thr or 0.0)
            op = {"le": "≤", "lt": "<", "ge": "≥", "gt": ">"}.get(fail_when, "≤")
            if _pair_is_violation(diff, threshold, fail_when):
                problems.append(
                    f"「{a_fields[0]}」{va:,.2f}元 与 「{b_fields[0]}」{vb:,.2f}元 "
                    f"相差 {diff:,.2f}元（{op}{threshold:,.2f}元{ratio_note}），构成高度相似"
                )
            else:
                pair_pass_notes.append(
                    f"「{a_fields[0]}」{va:,.2f}元 与 「{b_fields[0]}」{vb:,.2f}元 "
                    f"相差 {diff:,.2f}元（不满足「{op}{threshold:,.2f}元」的触发条件"
                    f"{ratio_note}），不构成高度相似，判定通过"
                )

        if problems:
            results[rid] = {
                "rule_id": rid,
                "rule_name": rule.get("name", ""),
                "category": rule.get("category", ""),
                "severity": rule.get("severity", "major"),
                "status": "fail",
                "title": f"结构化规则命中：{rule.get('name', '')}",
                "detail": "；".join(problems),
                "evidence": "；".join(problems)[:1200],
                "location": "",
                "suggestion": "请核对并修正上述确定性校验项。",
                "legal_basis": "",
                "involved_files": [],
                "confidence": 1.0,
                "deterministic": True,  # 标记由确定性引擎锁定，LLM 不可推翻
            }
        elif pair_pass_notes and not pair_missing:
            # 金额对比较未触发违规 → 确定性判定为通过（锁定，避免回落 LLM 误判）
            results[rid] = {
                "rule_id": rid,
                "rule_name": rule.get("name", ""),
                "category": rule.get("category", ""),
                "severity": rule.get("severity", "major"),
                "status": "pass",
                "title": f"确定性判定通过：{rule.get('name', '')}",
                "detail": "；".join(pair_pass_notes),
                "evidence": "；".join(pair_pass_notes)[:1200],
                "location": "",
                "suggestion": "金额对差值满足规则要求，无需进一步处理。",
                "legal_basis": "",
                "involved_files": [],
                "confidence": 1.0,
                "deterministic": True,  # 标记由确定性引擎锁定，LLM 不可推翻
            }
    return results


def _merge_structured_into_batch(
    batch: list[dict[str, Any]],
    structured_findings: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """拆分批次：已被确定性引擎锁定的规则从 LLM 批次中移除（避免重复判定与随机覆盖），
    其结论直接作为该批次的确定性 findings 返回；其余规则继续走 LLM。"""
    llm_rules: list[dict[str, Any]] = []
    locked: list[dict[str, Any]] = []
    for r in batch:
        rid = str(r.get("id") or "")
        if rid in structured_findings:
            locked.append(structured_findings[rid])
        else:
            llm_rules.append(r)
    return llm_rules, locked


def _missing_rule_ids(raw: Any, batch: list[dict[str, Any]]) -> set[str]:
    """模型 JSON 中缺失/漏答的规则 id（含整批返回 None 的情况）。"""
    items = (
        raw.get("findings")
        if isinstance(raw, dict)
        else (raw if isinstance(raw, list) else None)
    )
    answered: set[str] = set()
    if isinstance(items, list):
        for it in items:
            if isinstance(it, dict):
                rid = str(it.get("rule_id") or "").strip()
                if rid:
                    answered.add(rid)
    return {r["id"] for r in batch if r["id"] not in answered}


def _merge_raw(base: Any, extra: Any) -> dict[str, Any]:
    """把补答结果合并进原 raw：同 rule_id 以补答为准，补答独有的追加。"""
    base_items = (
        list((base or {}).get("findings") or [])
        if isinstance(base, dict)
        else (list(base) if isinstance(base, list) else [])
    )
    extra_items = (
        list((extra or {}).get("findings") or [])
        if isinstance(extra, dict)
        else (list(extra) if isinstance(extra, list) else [])
    )
    extra_by_id = {
        str(i.get("rule_id")): i for i in extra_items if isinstance(i, dict)
    }
    out: list[dict[str, Any]] = []
    replaced: set[str] = set()
    for it in base_items:
        rid = str((it or {}).get("rule_id") or "")
        if rid in extra_by_id:
            out.append(extra_by_id[rid])
            replaced.add(rid)
        else:
            out.append(it)
    for rid, it in extra_by_id.items():
        if rid not in replaced:
            out.append(it)
    return {"findings": out}


def _is_editing_batch(batch: list[dict[str, Any]]) -> bool:
    """本批次是否含校对类规则（错别字 / 语义 / 术语）。

    这类规则要求模型逐字逐句扫描全文，大文档下必须分段处理，否则必然超时。
    """
    return any(str(r.get("id") or "") in EDITING_RULE_IDS for r in batch or [])


def _merge_segment_raws(raws: list[Any]) -> dict[str, Any]:
    """累加合并「分段校对」各段结论。

    分段后同一条规则在每一段都会产出结论，语义与补答合并（_merge_raw）完全不同：
      - 问题类（fail/warn/unknown）：不同段发现的是彼此独立的问题，需全部保留，
        仅按 (rule_id, title) 去重，避免段边界处重复报告同一问题；
      - pass 类：多段的「通过」结论语义重复，同 rule_id 仅保留一条。
        这样既避免结果被大量 pass 噪声淹没，又保留「该规则已核查通过」的信息，
        同时确保 rule_id 出现在结果中 —— 否则 _missing_rule_ids 会判定漏答并触发
        补答重试，而补答用的是全文档，必然再次超时（这是分段方案必要的配套）。
    """
    out: list[dict[str, Any]] = []
    seen_problem: set[tuple[str, str]] = set()
    seen_pass: set[str] = set()
    for raw in raws or []:
        items = (
            raw.get("findings")
            if isinstance(raw, dict)
            else (raw if isinstance(raw, list) else [])
        )
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            rid = str(it.get("rule_id") or "")
            status = str(it.get("status") or "").lower()
            if status == "pass":
                if rid in seen_pass:
                    continue
                seen_pass.add(rid)
            else:
                key = (rid, str(it.get("title") or ""))
                if key in seen_problem:
                    continue
                seen_problem.add(key)
            out.append(it)
    return {"findings": out}


async def _run_rule_per_file(
    batch: list[dict[str, Any]],
    docs: list[dict[str, Any]],
    tender_summary: str,
    extra_instruction: str,
    mode: str,
    *,
    file_manifest: str = "",
    kb_enabled: bool,
    kb_id: str | None,
    web_search_enabled: bool,
    on_event: Any,
    rules: list[dict[str, Any]] | None,
    kb_cache: Any,
    cache_prefix: str,
    temperature: float | None,
    kb_state: dict | None,
    batch_kb_context: str = "",
    timeout: float | None = None,
) -> tuple[Any, list[dict[str, Any]]]:
    """多文件非编辑类规则的「按文件切片并行」审核（根治多文件巨型 prompt 撞 540s 硬顶）。

    与 _proofread_by_segments（按字符切片）同源思路：把切片维度从"字符段"换成"文件"。
    每份文档独立成一次调用（单文件 prompt ≲ max_chars_per_doc，响应秒级~数十秒，
    远离 llm_timeout），全部完成后用 _merge_segment_raws 按 (rule_id,title) 累加问题、
    同 rule 仅留一条 pass。单文件调用失败仅跳过该文件；全失败返回 (None, traces) 让上层跳过补答。

    为什么不直接用全文档拼接：N 份文档各截断到 60k 后拼接可达 N×60k 字符，SiliconFlow 后端的
    慢速模型（实际为千问 80B）对超大 prompt 吞吐极低，单次 prefill 逼近 llm_timeout(180s)，
    3 次重试 = 540s 即撞 hard ceiling（任务 8b562b448012 第 19 批即此命门）。
    """
    limit = int(config.get("max_chars_per_doc", 60000))
    conc = max(1, int(config.get("rule_per_file_concurrent", 6)))
    sem = asyncio.Semaphore(conc)

    async def run_one(d: dict[str, Any]):
        async with sem:
            fname = d.get("filename") or "未命名文件"
            text = truncate(d.get("text") or "", limit)
            file_text = f"【文件：{fname}】\n{text}"
            prompt = prompts.build_rule_prompt(
                batch, file_text, tender_summary, extra_instruction, mode=mode,
                file_manifest=file_manifest,
            )
            if batch_kb_context:
                prompt = (
                    f"{prompt}\n\n# 法规依据（来自知识库预检索，供本批次核查参考）\n"
                    f"{batch_kb_context}"
                )
            return await _run_with_kb(
                prompt,
                kb_enabled=kb_enabled,
                kb_id=kb_id,
                web_search_enabled=web_search_enabled,
                on_event=on_event,
                rules=batch,
                kb_cache=kb_cache,
                cache_prefix=cache_prefix,
                timeout=timeout,
                temperature=temperature,
                kb_state=kb_state,
            )

    results = await asyncio.gather(*[run_one(d) for d in docs], return_exceptions=True)
    raws: list[Any] = []
    all_traces: list[dict[str, Any]] = []
    overflow: Exception | None = None
    for r in results:
        if isinstance(r, Exception):
            if isinstance(r, llm_client.LLMContextOverflow) and overflow is None:
                overflow = r
            logger.warning("按文件审核单文件失败(跳过该文件): %s", r)
            continue
        if isinstance(r, tuple) and len(r) == 2:
            raws.append(r[0])
            all_traces.extend(r[1] or [])
    # 关键：全部文件均因「上下文超长」失败时必须上抛，交由调用方降级（丢弃 KB 依据后整体重试）。
    # 否则该异常会被上面的 per-file 容错吞掉，静默变成「整批无结论」且无任何补救——
    # 这正是上线后「频繁超长却看不到降级」的隐蔽路径之一。
    # 仅当有文件已成功时才保留部分结论，避免为了降级而丢弃已拿到的有效结果。
    if not raws and overflow is not None:
        raise overflow
    if not raws:
        return None, all_traces
    return _merge_segment_raws(raws), all_traces


async def _proofread_by_segments(
    batch: list[dict[str, Any]],
    docs_text: str,
    tender_summary: str,
    extra_instruction: str,
    mode: str,
    emit: Any,
    file_manifest: str = "",
    batch_kb_context: str = "",
    batch_index: int = 0,
    total_batches: int = 1,
    total: int = 1,
    **run_kwargs: Any,
) -> tuple[Any, list[dict[str, Any]]]:
    """校对类规则的分段并行审核（根治大文档下「hard ceiling 540s」超时）。

    根因：校对类规则要求模型"逐字逐句"扫描全文，单次调用要把数万字符原文
    （实测 59,457 字 ≈ 4.2 万 token）一次性 prefill 后再长输出，必然超过
    llm_timeout(180s)；三次重试全部耗尽即撞 llm_call_hard_ceil(540s)，
    报「批次审核失败: llm call exceeded hard ceiling 540s」。
    该现象与提供方是否抖动无关 —— 只要文档足够大就必然复现。

    方案：按 editing_seg_chars 切段，每段独立调用（单段 prompt ≈8k 字符，
    响应降到秒级~数十秒），并行执行后累加合并各段结论。单段失败仅跳过该段，
    不拖垮整条规则；全部段失败时返回 None，让上层跳过"全文档补答重试"
    （补答会再次必然超时）。并发由 llm_client 全局闸 llm_max_concurrent 收口。
    """
    seg_chars = max(1000, int(config.get("editing_seg_chars", 8000)))
    max_segs = max(1, int(config.get("editing_max_segments", 40)))
    segs = _split_text(docs_text or "", seg_chars)
    if len(segs) > max_segs:
        # 极端超大文档：合并尾部所有段，避免段数/请求数失控
        segs = segs[: max_segs - 1] + ["".join(segs[max_segs - 1 :])]
    total_seg = len(segs)
    rule_names = ", ".join(str(r.get("name") or r.get("id")) for r in batch)
    await emit(
        {
            "type": "stage",
            "stage": "rules",
            "message": (
                f"规则 {rule_names} 需逐字校对，文档较长（{len(docs_text or '')} 字），"
                f"已分为 {total_seg} 段并行审核"
            ),
        }
    )

    async def run_seg(idx: int, seg: str):
        seg_prompt = prompts.build_rule_prompt(
            batch, seg, tender_summary, extra_instruction, mode=mode,
            file_manifest=file_manifest,
        )
        seg_prompt = (
            f"【分段校对 {idx}/{total_seg}】以下为待审文档的第 {idx}/{total_seg} 段原文，"
            f"请仅针对本段内容逐字逐句核查，不要臆测本段之外的缺失内容。\n\n{seg_prompt}"
        )
        if batch_kb_context:
            seg_prompt = (
                f"{seg_prompt}\n\n# 法规依据（来自知识库预检索，供本批次核查参考）\n"
                f"{batch_kb_context}"
            )
        return await _run_with_kb(seg_prompt, **run_kwargs)

    results = await asyncio.gather(
        *[run_seg(i, s) for i, s in enumerate(segs, 1)], return_exceptions=True
    )
    raws: list[Any] = []
    traces: list[dict[str, Any]] = []
    for i, r in enumerate(results, 1):
        if isinstance(r, BaseException):
            logger.warning("校对分段 %d/%d 失败(跳过该段): %s", i, total_seg, r)
            continue
        raw, tr = r
        if raw:
            raws.append(raw)
        traces.extend(tr or [])
        # 逐段实时进度：把本段完成折算进「该规则批次」在整体进度条中的占比，
        # 避免数分钟的分段校对期间进度数值与消息长时间静止（用户观感为「卡 0%」）。
        # 与「每条规则完成推进」共用 total 分母，保证进度单调、不回跳。
        if total > 0:
            frac = ((batch_index - 1) + i / total_seg) / total
            await emit(
                {
                    "type": "stage",
                    "stage": "rules",
                    "message": (
                        f"规则 {rule_names} 分段校对进度 {i}/{total_seg}"
                    ),
                    "progress": round(min(99, max(0, frac * 100))),
                }
            )
    if not raws:
        logger.error("校对分段全部失败，放弃该批次（不再全文档补答，避免必然超时）")
        return None, traces
    return _merge_segment_raws(raws), traces


def _summarize(
    findings: list[dict[str, Any]], issues: list[dict[str, Any]]
) -> dict[str, Any]:
    counts = {"pass": 0, "fail": 0, "warn": 0, "unknown": 0}
    severity_counts = {"critical": 0, "major": 0, "minor": 0, "info": 0}
    deduction = 0

    for f in findings:
        counts[f["status"]] = counts.get(f["status"], 0) + 1
        if f["status"] in ("fail", "warn"):
            sev = f.get("severity", "major")
            severity_counts[sev] = severity_counts.get(sev, 0) + 1
            weight = SEVERITY_WEIGHT.get(sev, 10)
            deduction += weight if f["status"] == "fail" else weight * 0.4

    for issue in issues:
        sev = issue.get("severity", "major")
        severity_counts[sev] = severity_counts.get(sev, 0) + 1
        deduction += SEVERITY_WEIGHT.get(sev, 10) * 0.6

    score = max(0, min(100, round(100 - deduction)))
    critical_fails = [
        f for f in findings if f["status"] == "fail" and f.get("severity") == "critical"
    ]
    if critical_fails:
        conclusion = "存在否决项风险"
    elif counts["fail"]:
        conclusion = "存在合规缺陷需整改"
    elif counts["warn"] or issues:
        conclusion = "基本合规但有瑕疵"
    elif counts["unknown"] == len(findings) and findings:
        conclusion = "材料不足无法判定"
    else:
        conclusion = "合规"

    return {
        "score": score,
        "conclusion": conclusion,
        "total_rules": len(findings),
        "status_counts": counts,
        "severity_counts": severity_counts,
        "consistency_issue_count": len(issues),
        "critical_items": [f["rule_name"] for f in critical_fails],
    }


async def run_review(
    docs: list[dict[str, Any]],
    rules: list[dict[str, Any]],
    *,
    mode: str = "bid",
    kb_enabled: bool,
    kb_id: str | None,
    web_search_enabled: bool = False,
    extra_instruction: str = "",
    task_id: str | None = None,
    user_id: str | None = None,
    deterministic: bool | None = None,
    ruleset_id: str | None = None,
    rule_group_ids: list[str] | None = None,
    auto_match: bool = False,
    legal_rulesets: list[dict[str, Any]] | None = None,
    cache_enabled: bool | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """执行完整审核，产出事件流。

    Args:
        mode: "bid" | "tender" | "general" — 审核模式，影响招标要求提取与一致性核查策略
        web_search_enabled: 是否启用联网搜索
        deterministic: 是否确定性执行。None=跟随配置(deterministic_mode)；True=强制开启；
            False=强制关闭。开启时固定规则执行顺序、temperature=0、串行执行、不依赖系统时间，
            并对 LLM 输出做结构化约束与确定性归一化，保证可重复审核结果一致、可追溯。
        cache_enabled: 任务级缓存开关。None=各缓存跟随全局配置（默认，向后兼容）；
            True=本任务强制启用结论缓存与一致性摘要/要素缓存；False=本任务强制禁用，
            不受全局 findings_cache_enabled / consistency_cache_enabled 影响。
    """
    # 确定性模式判定：显式参数 > 配置项。默认开启（可重复审核一致）。
    det_mode = (
        bool(deterministic)
        if deterministic is not None
        else bool(config.get("deterministic_mode", True))
    )

    # 规则确定性排序：按规则 id 升序固定执行顺序，消除任何顺序不确定性
    # （并发竞态、字典遍历顺序差异等导致的输出波动）。
    ordered_rules = sorted(rules, key=lambda r: str(r.get("id") or ""))

    # 为每篇文档计算解析结果快照指纹，随审计存证，支撑按历史版本重跑校验。
    for d in docs:
        if "parsed_hash" not in d:
            d["parsed_hash"] = versioning.parsed_content_hash(d.get("text") or "")

    # 确定性模式下使用的采样温度：0=关闭采样，输出尽可能确定。
    det_temperature = float(config.get("deterministic_temperature", 0.0)) if det_mode else None

    queue: asyncio.Queue = asyncio.Queue()

    async def emit(evt: dict[str, Any]) -> None:
        await queue.put(evt)

    async def worker() -> None:
        findings: list[dict[str, Any]] = []
        kb_traces: list[dict[str, Any]] = []
        issues: list[dict[str, Any]] = []
        # 捕获请求级参数，供版本清单解析规则集版本使用
        req_ruleset_id = ruleset_id
        req_rule_group_ids = rule_group_ids
        req_auto_match = auto_match
        try:
            # 文档上下文：超大文档先做分片摘要（compact 表征），再送后续按规则审核，
            # 避免把整份大文档一次性塞进单个巨型 prompt 触发模型超时/卡死。
            docs_text_full, per_doc_full = _compose_docs(docs)  # 供确定性结构化规则全量匹配
            # 提前聚合一致性规格与规则内容指纹，供文档摘要/要素提取缓存键使用，
            # 避免「一致性规则定义已改」仍命中旧缓存（一致性缓存失效正确性保障）。
            consistency_spec = rules_store.collect_consistency_spec(rules)
            consistency_rule_token = versioning.consistency_rule_token(rules)
            # 返回 (拼接上下文, 每份文档紧凑表征, 按文档对齐的文本块)；
            # 后者供一致性要素提取复用，且用于按规则关联文档类型过滤后重建子集上下文。
            docs_text, doc_summaries, per_doc_blocks = await _build_docs_text(
                docs, mode, emit, det_temperature, rule_token=consistency_rule_token,
                cache_enabled=cache_enabled,
            )

            # 确定性规则引擎预检：对带 structured 可执行条件的规则，先跑可计算判定，
            # 结论锁定（deterministic=True），LLM 不得推翻。本层不依赖 LLM，结果可复现。
            # per_doc_full 与 docs 等长对齐，供按规则关联文档类型过滤（仅核查匹配类型的文件）。
            structured_findings = _apply_structured_rules(ordered_rules, docs, per_doc_full)
            if structured_findings:
                await emit(
                    {
                        "type": "stage", "stage": "structured",
                        "message": f"确定性规则引擎预检命中 {len(structured_findings)} 项",
                        "progress": 2,
                    }
                )

            # 持久化 KB 法规依据缓存（跨任务复用）：
            # 查询文本由规则字段构造、与文档内容无关，同一套规则+同一知识库的法规依据可安全复用。
            # 键命名空间 = kb_id + 数据快照版本，避免知识库更新/换库后串用旧答案。
            # 仅在任务正常结束时落盘（finally），负缓存（失败/无结果）不持久化，防瞬时故障污染后续任务。
            kb_cache: dict[str, tuple[str, list]] = {}
            cache_prefix = ""
            if kb_enabled:
                cache_prefix = (
                    f"kb={kb_id or config.get('kb_id', '')}|"
                    f"snap={config.get('data_snapshot_version', 'KB_BASELINE')}|"
                )
                kb_cache = _load_kb_cache()
            # 知识库健康状态（任务级共享）：连续失败达阈值即短路禁用，避免对超时服务雪崩重试
            kb_state: dict[str, bool] = {"enabled": True}
            # 改为「按规则」按需检索：每条规则仅在 need_legal_basis=True 时，于本规则送审前
            # 单独规划并拉取该规则的法规依据（共享 kb_cache 去重跨规则重复查询），
            # 仅注入该规则的 prompt——不再把全部规则的 KB 依据预检索后灌入每一个批次，
            # 从根上压低单条请求的体量（大文档 + 全量 KB 灌入曾是巨型 prompt 卡死的主因）。

            # 招标要求提取：仅在投标审核模式且有招标文件时执行
            tender_summary = ""
            tender_data = None
            if mode == "bid":
                tender_summary, tender_data = await _extract_tender_summary(docs, emit, temperature=det_temperature)
                if tender_data:
                    await emit({"type": "tender_summary", "data": tender_data})

            # 按「单条规则」拆批：每条规则独立一个 LLM 调用（并行受 semaphore 控制），
            # 即"按规则审核、并行处理"，而非把多条规则打包一次性送审。
            # 失败隔离（单条异常不影响其余）、单请求体量更小、结论缓存命中率更高。
            # 校对类规则本就是单条调用，无需再单独成批。
            editing_rules = [r for r in ordered_rules if r["id"] in EDITING_RULE_IDS]
            normal_rules = [r for r in ordered_rules if r["id"] not in EDITING_RULE_IDS]
            batches = [[r] for r in normal_rules]
            if editing_rules:
                # 校对类规则合并为单个批次：错别字/语义/术语需对同一长文档「逐字扫描」，
                # 若各自成批会分别对整文档做分段并行校对（N_rules×N_seg 次 LLM 调用，
                # 且受 llm_max_concurrent 限制需多轮排队），这是 20 规则任务>15 分钟的主因。
                # 合并后一次分段并行即可同时覆盖全部校对规则，调用量降到 N_seg（≈3× 提速），
                # _proofread_by_segments 会按 rule_id 累加合并各段结论，质量不受影响。
                batches.append(editing_rules)
            # 确定性模式：强制按规则 id 升序固定批次序列，消除任何顺序/并发不确定。
            if det_mode:
                batches.sort(key=lambda b: str(b[0].get("id") if b else ""))
            # 一致性核查门控（2026-09-01 起规则驱动）：
            # 核查要点与核心要素全部取自任务里的「一致性」类规则，因此同一套一致性配置
            # 可随规则集 / 规则组复用——规则集里不含一致性规则，就不会执行一致性核查。
            #   consistency_enabled=auto（默认）：存在一致性类规则才执行
            #   on：强制执行（旧行为：mode != "general"）
            #   off：关闭
            # 单文件=文档内一致性、多文件=跨文件一致性，二者走同一套处理流程；
            # 多文件超长时将在一致性块内逐文档分段摘要后跨文件比对，避免整份大文档
            # 一次性大请求触发 ReadTimeout。
            multi_doc = len(docs) >= 2
            _cs_switch = str(config.get("consistency_enabled", "auto") or "auto").strip().lower()
            if _cs_switch == "off":
                run_consistency = False
            elif _cs_switch == "on":
                run_consistency = (mode != "general")
            else:
                run_consistency = bool(consistency_spec["rule_ids"])
            if run_consistency and not consistency_spec["rule_ids"]:
                logger.info("一致性核查已启用但任务未包含一致性类规则，回退为内置默认关注点")
            total_batches = len(batches)
            total = total_batches + (1 if run_consistency else 0)

            # 启动/懒探测模型工具调用支持（仅一次，非致命），避免首个批次先失败再降级，
            # 消除降级模式额外规划调用带来的耗时（方案 C）。
            await _probe_tool_support()

            # 并发审核各规则批次：受 concurrency 限制（默认 3 → 6），缩短整体耗时。
            # 历史实现为严格串行，单批内的多轮 LLM/KB 调用会叠加放大到整任务。
            # 确定性模式：强制串行执行（sem=1），消除并发竞态导致的结果波动；
            # 此时 throughput 让位于「可重复的一致输出」。
            sem = asyncio.Semaphore(
                1 if det_mode else max(1, int(config.get("concurrency", 3)))
            )

            async def process_batch(
                idx: int, batch: list[dict[str, Any]]
            ) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]]]:
                # 注意：并发信号量 sem 在外层 _safe_batch 中获取（wait_for 计时之前），
                # 此处不再获取——否则排队等待时间会被计入 batch_timeout，
                # 导致未轮到执行的批次被「排队超时」误杀（曾出现 117 规则 94 批同时超时跳过）。
                if True:
                    # 调用审计上下文：把本批次的 AI(LLM/KB) 调用归因到具体 task / rule，
                    # 便于事后回放定位「哪条规则、哪次调用超时/429/失败」。
                    from . import call_audit

                    call_audit.set_context(
                        task_id=task_id,
                        rule_id=",".join(
                            str(r.get("id")) for r in batch if r.get("id")
                        ),
                    )
                    # 拆分：已被确定性引擎锁定的规则移出 LLM 批次（避免随机覆盖），
                    # 其结论直接计入本批次最终 findings；其余走 LLM 辅助判定。
                    llm_rules, locked_findings = _merge_structured_into_batch(
                        batch, structured_findings
                    )

                    # 跨文件规则缺件前置校验：对送审文件清单名称做预处理筛选，
                    # 必需文档角色缺失且可判定时，直接给出受控 unknown 并排除出 LLM 批次，
                    # 避免模型臆造或做单边不完整比对（如 dec-09 缺定标结果时误判名称一致）。
                    file_manifest = _build_file_manifest(docs)
                    _cf_intercepted: list[tuple[dict[str, Any], dict[str, Any]]] = []
                    _cf_keep: list[dict[str, Any]] = []
                    for _r in llm_rules:
                        _pc = _cross_file_precheck(_r, docs)
                        if _pc["is_cross_file"] and _pc["confident_missing"]:
                            _cf_intercepted.append((_r, _pc))
                        else:
                            _cf_keep.append(_r)
                    if _cf_intercepted:
                        for _r, _pc in _cf_intercepted:
                            locked_findings.append(
                                _make_cross_file_missing_finding(_r, _pc, docs)
                            )
                        llm_rules = _cf_keep
                        _intercept_ids = {r[0].get("id") for r in _cf_intercepted}
                        batch = [r for r in batch if r.get("id") not in _intercept_ids]

                    # 按规则关联文档类型过滤适用文档：规则限定了 doc_types 时，
                    # 仅对匹配文件类型的文件执行本规则审核（其他文件不执行该规则）。
                    # 一批次含多规则时取 doc_types 并集；若无任何文件匹配，则跳过该批次
                    # （不产生结论，避免误报 unknown/失败）。
                    batch_docs = _applicable_docs(batch, docs)
                    if not batch_docs:
                        names = ", ".join(
                            str(r.get("name") or r.get("id")) for r in batch
                        )
                        await emit(
                            {
                                "type": "stage", "stage": "rules",
                                "message": f"规则（{names}）未关联适用的文件类型，跳过（无匹配文件）",
                                "progress": round((idx - 1) / total * 100),
                            }
                        )
                        return idx, [], []
                    # 章节关联：规则指定了关联章节时，仅对「命中章节」的正文执行审核，
                    # 其余章节直接跳过；未指定章节时沿用原有全量审核逻辑。
                    section_ids = _batch_section_ids(batch)
                    if section_ids:
                        scoped_docs, scoped_blocks, hit_names = _section_scoped(
                            batch_docs, section_ids
                        )
                        if not scoped_docs:
                            names = ", ".join(
                                str(r.get("name") or r.get("id")) for r in batch
                            )
                            await emit(
                                {
                                    "type": "stage", "stage": "rules",
                                    "message": f"规则（{names}）关联的章节在文档中未找到，跳过（不产生结论）",
                                    "progress": round((idx - 1) / total * 100),
                                }
                            )
                            return idx, [], []
                        batch_docs = scoped_docs
                        batch_docs_text = "\n\n".join(scoped_blocks) or "（无可用文本内容）"
                    else:
                        batch_doc_ids = {id(d) for d in batch_docs}
                        applicable_idx = [
                            i for i, d in enumerate(docs) if id(d) in batch_doc_ids
                        ]
                        batch_docs_text = (
                            "\n\n".join(per_doc_blocks[i] for i in applicable_idx)
                            or "（无可用文本内容）"
                        )

                    # 结果缓存（方案 C）：相同输入复用历史批次结论，跳过 LLM 调用。
                    # 仅当存在需 LLM 判定的规则时适用；整批锁定的分支维持原逻辑。
                    batch_cache_key = None
                    if _cache_on(cache_enabled, "findings_cache_enabled") and llm_rules:
                        batch_cache_key = findings_cache.make_key(
                            doc_hashes=[
                                d.get("parsed_hash") or versioning.parsed_content_hash(d.get("text") or "")
                                for d in batch_docs
                            ],
                            rule_ids=[str(r.get("id")) for r in llm_rules],
                            mode=mode,
                            kb_enabled=kb_enabled,
                            web_search_enabled=web_search_enabled,
                            det_mode=det_mode,
                            tender_summary=tender_summary,
                            global_kb_context="",
                            extra_instruction=extra_instruction,
                            doc_md5s=[str(d.get("md5")) for d in batch_docs if d.get("md5")],
                            rule_fps=[versioning.rule_content_hash(r) for r in llm_rules],
                        )
                        cached = findings_cache.get(batch_cache_key)
                        if cached is not None:
                            # 命中：复用历史结论，重新下发 finding 事件保证 SSE/前端体验一致
                            if det_mode:
                                cached = [_normalize_finding_deterministic(f) for f in cached]
                            for f in cached:
                                await emit({"type": "finding", "finding": f})
                            await emit(
                                {
                                    "type": "stage", "stage": "rules",
                                    "message": f"命中结论缓存，跳过 LLM 调用（规则 {', '.join(r['name'] for r in batch)}）",
                                    "progress": round((idx - 1) / total * 100),
                                }
                            )
                            return idx, locked_findings + cached, []

                    await emit(
                        {
                            "type": "stage", "stage": "rules",
                            "message": f"审核规则 {', '.join(r['name'] for r in batch)}",
                            "progress": round((idx - 1) / total * 100),
                        }
                    )
                    # 整批已被确定性锁定（无 LLM 规则）时，直接返回锁定结论，跳过 LLM 调用。
                    if not llm_rules:
                        if det_mode:
                            locked_findings = [
                                _normalize_finding_deterministic(f) for f in locked_findings
                            ]
                        return idx, locked_findings, []
                    # 按需检索本批次(单条规则)的法规依据：仅 need_legal_basis 的规则检索，
                    # 结果仅注入该批次 prompt，不再把全部规则 KB 灌入每个请求（巨型 prompt 卡死主因）。
                    batch_kb_context = ""
                    if (
                        kb_enabled
                        and config.get("kb_prefetch_global", True)
                        and any(r.get("need_legal_basis") for r in batch)
                        and kb_state.get("enabled", True)
                    ):
                        try:
                            planned = await _plan_kb_queries(batch, emit)
                            if planned:
                                ctx, traces = await _fetch_kb_context(planned, kb_id, emit, cache=kb_cache, cache_prefix=cache_prefix)
                                kb_traces.extend(traces)
                                batch_kb_context = ctx
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("批次 %s 知识库检索失败(降级无依据): %s", [r.get("id") for r in batch], exc)
                            batch_kb_context = ""
                    # 默认「仅预检索」模式：法规依据由上方 kb_prefetch_global 一次性注入 prompt，
                    # 模型不再实时调用 KB 工具（kb_tool_calls_enabled=False）。原因：KB 单次查询 50~80s，
                    # 模型多轮实时检索会让单批累计耗时突破 batch_timeout 导致整批超时跳过。
                    # 仅当显式开启 kb_tool_calls_enabled 且本批预检索未拿到依据时，才允许模型兜底实时检索。
                    kb_tools_enabled = bool(config.get("kb_tool_calls_enabled", True))
                    run_kb = bool(kb_enabled and not batch_kb_context and kb_tools_enabled)
                    # 校对类规则(错别字/语义/术语)需逐字扫描全文，大文档下单次调用必然超时：
                    # 数万字符一次性 prefill → 超 llm_timeout(180s) → 撞 hard ceiling 540s。
                    # 故改为分段并行校对：每段 ≈8k 字符，单次响应降到秒级，最后累加合并各段结论。
                    seg_chars = max(1000, int(config.get("editing_seg_chars", 8000)))
                    segmented = _is_editing_batch(batch) and len(batch_docs_text or "") > seg_chars
                    # 多文件非编辑类规则：按"文件"切片并行（同源分段思路，切片维度=文件），
                    # 避免把 N 份文档拼成巨型 prompt 单次 prefill 撞 540s 硬顶（见任务 8b562b448012）。
                    # 单文件任务保持原单 prompt 路径（避免改变其结论缓存键/行为）。
                    per_file = (
                        bool(config.get("rule_per_file_enabled", True))
                        and not segmented
                        and len(batch_docs) > 1
                    )
                    if segmented:
                        try:
                                raw, traces = await _proofread_by_segments(
                                    batch,
                                    batch_docs_text,
                                tender_summary,
                                extra_instruction,
                                mode=mode,
                                emit=emit,
                                batch_index=idx,
                                total_batches=total_batches,
                                total=total,
                                batch_kb_context=batch_kb_context,
                                kb_enabled=run_kb,
                                kb_id=kb_id,
                                web_search_enabled=web_search_enabled,
                                on_event=emit,
                                rules=batch,
                                kb_cache=kb_cache,
                                cache_prefix=cache_prefix,
                                temperature=det_temperature,
                                kb_state=kb_state,
                                file_manifest=file_manifest,
                            )
                        except llm_client.LLMError as exc:
                            logger.error("规则批次审核失败: %s", exc)
                            await emit({"type": "warning", "message": f"批次审核失败: {exc}"})
                            raw, traces = None, []
                    else:
                        if per_file:
                            # 按文件切片并行：每份文档一次有界调用，合并各文件结论。
                            # 见 _run_rule_per_file 文档——根治多文件巨型 prompt 撞 540s 硬顶。
                            try:
                                raw, traces = await _run_rule_per_file(
                                    batch, batch_docs, tender_summary, extra_instruction,
                                    file_manifest=file_manifest,
                                    mode=mode,
                                    kb_enabled=run_kb, kb_id=kb_id,
                                    web_search_enabled=web_search_enabled,
                                    on_event=emit, rules=batch,
                                    kb_cache=kb_cache, cache_prefix=cache_prefix,
                                    temperature=det_temperature, kb_state=kb_state,
                                    batch_kb_context=batch_kb_context,
                                )
                            except llm_client.LLMContextOverflow as exc:
                                # 按文件切片仍超长（单文件过大 + KB 依据）：丢弃 KB 依据重试一次，
                                # 关闭 KB 工具避免模型回捞依据再次撑爆上下文。
                                logger.warning("规则批次(按文件)上下文超长，丢弃 KB 依据重试: %s", exc)
                                await emit({"type": "warning", "message": "批次上下文超长，已丢弃知识库依据重试"})
                                try:
                                    raw, traces = await _run_rule_per_file(
                                        batch, batch_docs, tender_summary, extra_instruction,
                                        mode=mode,
                                        kb_enabled=False, kb_id=kb_id,
                                        web_search_enabled=web_search_enabled,
                                        on_event=emit, rules=batch,
                                        kb_cache=kb_cache, cache_prefix=cache_prefix,
                                        temperature=det_temperature, kb_state=kb_state,
                                        batch_kb_context="",
                                    )
                                except llm_client.LLMError as exc2:
                                    logger.error("规则批次(按文件)审核失败: %s", exc2)
                                    await emit({"type": "warning", "message": f"批次审核失败: {exc2}"})
                                    raw, traces = None, []
                            except llm_client.LLMError as exc:
                                logger.error("规则批次(按文件)审核失败: %s", exc)
                                await emit({"type": "warning", "message": f"批次审核失败: {exc}"})
                                raw, traces = None, []
                        else:
                            # 单 prompt 路径（非分段、非按文件）：可能因注入 KB 依据 + 多文档全文
                            # 而超过模型上下文窗口。接入「发前 token 预算 + 上下文超长多级降级」，
                            # 确保大文档审核不静默失败（降级细节见 _run_rule_prompt_guarded）。
                            try:
                                raw, traces = await _run_rule_prompt_guarded(
                                    batch,
                                    batch_docs_text,
                                    tender_summary,
                                    extra_instruction,
                                    file_manifest=file_manifest,
                                    mode=mode,
                                    kb_text=batch_kb_context,
                                    run_kb=run_kb,
                                    kb_id=kb_id,
                                    web_search_enabled=web_search_enabled,
                                    on_event=emit,
                                    rules=batch,
                                    kb_cache=kb_cache,
                                    cache_prefix=cache_prefix,
                                    temperature=det_temperature,
                                    kb_state=kb_state,
                                )
                            except llm_client.LLMError as exc:
                                logger.error("规则批次审核失败: %s", exc)
                                await emit({"type": "warning", "message": f"批次审核失败: {exc}"})
                                raw, traces = None, []
                    # 补答重试：模型漏答或整批失败时，对缺失规则做针对性重审，
                    # 避免直接把整批规则标为“模型未返回该规则结论”(unknown)。
                    missing = _missing_rule_ids(raw, llm_rules)
                    # 分段校对模式下 raw 为 None = 全部分段均失败；此时若再走补答会用全文档
                    # 重发请求，必然再次超时（这正是分段要根治的问题）→ 直接跳过补答。
                    if segmented and raw is None:
                        missing = set()
                    retry_n = int(config.get("rule_retry_on_missing", 1))
                    for _r in range(retry_n):
                        if not missing:
                            break
                        miss_rules = [r for r in llm_rules if r["id"] in missing]
                        if per_file:
                            # 按文件补答同样有界：仅对缺失规则重跑各文件，不会用全文档巨型 prompt 重试。
                            try:
                                r2, t2 = await _run_rule_per_file(
                                    miss_rules, batch_docs, tender_summary, extra_instruction,
                                    mode=mode,
                                    kb_enabled=run_kb, kb_id=kb_id,
                                    web_search_enabled=web_search_enabled,
                                    on_event=emit, rules=miss_rules,
                                    kb_cache=kb_cache, cache_prefix=cache_prefix,
                                    temperature=det_temperature, kb_state=kb_state,
                                    batch_kb_context=batch_kb_context,
                                )
                                traces.extend(t2 or [])
                            except llm_client.LLMError as exc:
                                logger.warning("缺失规则(按文件)补答失败(放弃本轮): %s", exc)
                                break
                            raw = _merge_raw(raw, r2)
                            missing = _missing_rule_ids(raw, llm_rules)
                            continue
                        # 缺失规则补答：单 prompt 路径同样接入上下文超长降级，
                        # 复用 _run_rule_prompt_guarded（与主批一致），避免重试时再次超长静默失败。
                        try:
                            r2, t2 = await _run_rule_prompt_guarded(
                                miss_rules, batch_docs_text, tender_summary, extra_instruction,
                                mode=mode,
                                kb_text=batch_kb_context,
                                run_kb=run_kb,
                                kb_id=kb_id,
                                web_search_enabled=web_search_enabled,
                                on_event=emit,
                                rules=miss_rules,
                                kb_cache=kb_cache,
                                cache_prefix=cache_prefix,
                                temperature=det_temperature,
                                kb_state=kb_state,
                            )
                            traces.extend(t2 or [])
                        except llm_client.LLMError as exc:
                            logger.warning("缺失规则补答失败(放弃本轮): %s", exc)
                            break
                        raw = _merge_raw(raw, r2)
                        missing = _missing_rule_ids(raw, llm_rules)
                    # 兜底：整批（含补答）仍全失败 → 不再静默丢弃该规则，合成 unknown 结论，
                    # 让前端/报告能反映"该规则本次未成功核查"，而非结果里凭空消失（见 8b562b448012）。
                    if raw is None and llm_rules:
                        raw = {
                            "findings": [
                                {
                                    "rule_id": str(r.get("id")),
                                    "rule_name": r.get("name", ""),
                                    "category": r.get("category", ""),
                                    "severity": r.get("severity", "major"),
                                    "status": "unknown",
                                    "title": "规则未成功核查（调用超时/失败）",
                                    "detail": "本批次大模型调用在硬上限内仍失败，结论缺失，建议重跑该规则。",
                                    "evidence": "",
                                    "location": "",
                                    "suggestion": "可在任务完成后单独重跑，或减小单次送审文件规模。",
                                    "legal_basis": "",
                                    "involved_files": [],
                                    "confidence": 0.0,
                                    "typo": None,
                                }
                                for r in llm_rules
                            ]
                        }
                    # 错别字校验集过滤：命中用户「不采纳」过滤规则的自动跳过（去噪）
                    try:
                        batch_findings = _normalize_findings(
                            raw, batch, doc_names=_doc_names_from(batch_docs)
                        )
                        batch_findings, _skipped = feedback_store.ingest_typo_findings(
                            batch_findings, task_id, user_id=user_id
                        )
                        for sf in _skipped:
                            await emit(
                                {
                                    "type": "warning",
                                    "message": f"已按校验集过滤规则跳过错别字项：{sf.get('title', '')}",
                                }
                            )
                    except Exception as exc:  # noqa: BLE001 - 过滤失败不应中断审核
                        logger.warning("错别字校验集过滤失败（已忽略）: %s", exc)
                        batch_findings = _normalize_findings(
                            raw, batch, doc_names=_doc_names_from(batch_docs)
                        )
                    # 确定性归一化：对单条 finding 的离散字段做归一，消除大小写/空白漂移，
                    # 使同类结论在多次审核间稳定可比（如 status 统一小写、severity 归一）。
                    if det_mode:
                        batch_findings = [_normalize_finding_deterministic(f) for f in batch_findings]
                        locked_findings = [
                            _normalize_finding_deterministic(f) for f in locked_findings
                        ]
                    # 结果缓存落盘（方案 C）：仅当本批走了 LLM 且命中键有效时写入
                    if batch_cache_key is not None:
                        try:
                            findings_cache.put(batch_cache_key, batch_findings)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("结论缓存写入失败(已忽略): %s", exc)
                    # 合并确定性锁定结论（structured 规则）与 LLM 辅助结论
                    return idx, locked_findings + batch_findings, traces

            # 单批次硬超时 + 失败隔离：任一批次（规则）因 LLM/KB 调用挂起或异常，
            # 均不会拖垮整任务——超时/异常批次被跳过并记录告警，其余批次照常出结论，
            # 整任务始终能推进到 done（不再出现「running 永久卡死」或「单条异常整任务失败」）。
            batch_timeout = float(config.get("batch_timeout", 600))

            async def _safe_batch(idx: int, b):
                # 信号量必须在 wait_for 计时开始之前获取：
                # 否则排队等待并发槽位的时间也会消耗 batch_timeout 预算，
                # 排队靠后的批次即使从未真正执行也会被整体超时跳过。
                async with sem:
                    try:
                        return await asyncio.wait_for(process_batch(idx, b), timeout=batch_timeout)
                    except asyncio.TimeoutError:
                        logger.error(
                            "规则批次 %s 审核超时(>%ss)，跳过往下走",
                            [r.get("id") for r in b], batch_timeout,
                        )
                        await emit(
                            {
                                "type": "warning",
                                "message": f"批次审核超时跳过（规则：{', '.join(str(r.get('name', '')) for r in b)}）",
                            }
                        )
                        return idx, [], []
                    except Exception as exc:  # noqa: BLE001 - 单批次失败不应中断整任务
                        logger.exception("规则批次 %s 审核异常，跳过: %s", [r.get("id") for r in b], exc)
                        await emit(
                            {
                                "type": "warning",
                                "message": f"批次审核异常跳过（规则：{', '.join(str(r.get('name', '')) for r in b)}）：{exc}",
                            }
                        )
                        return idx, [], []

            batch_results: list[tuple[int, list[dict[str, Any]], list[dict[str, Any]]]] = []
            done_batches = 0

            def _rule_progress_event() -> dict[str, Any]:
                return {
                    "type": "stage", "stage": "rules",
                    "message": f"已完成 {done_batches}/{total_batches} 条规则审核",
                    # 用 total（含一致性）作分母，使进度在「规则→一致性→完成」间单调推进，
                    # 不会在最后一条规则完成时直接跳到 100 再被一致性阶段拉回（观感回跳）。
                    "progress": round(done_batches / total * 100) if total else 0,
                }

            if det_mode:
                # 串行执行：严格按批次顺序，杜绝并发竞态导致的结果不确定性。
                for i, b in enumerate(batches):
                    batch_results.append(await _safe_batch(i + 1, b))
                    done_batches += 1
                    await emit(_rule_progress_event())
            else:
                # 并发执行，但用 as_completed 按「实际完成」顺序推进进度：
                # 进度 = 已完成批次数 / 总批次数（单调、准确），不再依赖批次起始序号，
                # 避免并发下进度回跳、或长耗时批次（如分段校对）处理中进度长期停滞在 0%。
                pending = [_safe_batch(i + 1, b) for i, b in enumerate(batches)]
                for coro in asyncio.as_completed(pending):
                    item = await coro
                    if isinstance(item, Exception):
                        logger.error("批次执行返回异常(已忽略): %s", item)
                        continue
                    batch_results.append(item)
                    done_batches += 1
                    await emit(_rule_progress_event())
            # 按批次序号归并，保证 findings 顺序与规则顺序一致
            batch_results.sort(key=lambda x: x[0])
            for _idx, batch_findings, traces in batch_results:
                kb_traces.extend(traces)
                findings.extend(batch_findings)

            # 结构化原文定位：为每条结论附加 locations（file_id/页码/字符下标），
            # 供前端与第三方应用直接加载原始文件并跳转到对应页与内容位置。
            # 在 finding 事件下发前完成，保证 SSE 增量、任务落库与 done 事件
            # 三条路径返回的结论均带定位信息（口径一致）。
            _attach_locations(findings, docs)

            for f in findings:
                await emit({"type": "finding", "finding": f})

            # 每规则一条的最终聚合结果：把分段/多文档并行产生的 N 条并行结论合并去重为
            # 一条规则结果（20 规则 → 20 条），供前端「审核结果」按规则维度展示，
            # 杜绝长文档拆分并行审核后同一规则散落多条结果。
            rule_results = _aggregate_rule_results(findings, ordered_rules)
            await emit({"type": "rule_results", "results": rule_results})

            # 一致性核查：统一采用「分段摘要 → 要素提取 → 一致性校验」逻辑，
            # 单文件=文档内一致性、多文件=跨文件一致性，二者走同一套处理流程。
            # 单篇文档先按段落切片，逐段用 LLM 提取关键要素与取值（重点贴合本任务
            # 一致性规则中声明的要素），再对各段摘要做跨位置/跨文件一致性比对，
            # 从根本上避免把整份大文档一次性塞进单个大请求触发 ReadTimeout。
            if run_consistency:
                # 按一致性类规则的关联文档类型过滤参与核查的文件：规则限定了
                # doc_types 时仅对这些文件类型做一致性比对；均未限定时沿用全部文件。
                _cons_rules = [r for r in rules if rules_store.is_consistency_rule(r)]
                # 跨文件一致性规则缺件前置校验：必需文档角色缺失且可判定时，
                # 跳过该规则的一致性比对，直接给出受控 unknown，避免单边不完整比对。
                _cons_intercepted: list[tuple[dict[str, Any], dict[str, Any]]] = []
                _cons_active: list[dict[str, Any]] = []
                for _cr in _cons_rules:
                    _pc = _cross_file_precheck(_cr, docs)
                    if _pc["is_cross_file"] and _pc["confident_missing"]:
                        _cons_intercepted.append((_cr, _pc))
                    else:
                        _cons_active.append(_cr)
                for _cr, _pc in _cons_intercepted:
                    findings.append(_make_cross_file_missing_finding(_cr, _pc, docs))
                _cons_rules = _cons_active
                _cons_types: set[str] = set()
                for _cr in _cons_rules:
                    for _dt in (_cr.get("doc_types") or []):
                        if _dt:
                            _cons_types.add(str(_dt))
                if _cons_types:
                    consistency_docs = [
                        (i, d) for i, d in enumerate(docs)
                        if (d.get("file_type") or None) in _cons_types
                    ]
                else:
                    consistency_docs = list(enumerate(docs))
                # 要素与核查要点全部来自「一致性」类规则（门控处已聚合）：
                # ① 要素优先取规则 structured.consistency_elements 的显式声明，
                #    未声明时按规则文本回查要素库推断；两者皆无则回退内置默认关注点。
                # ② 核查要点取规则的 checkpoints，注入提示词取代内置默认关注点。
                elements = consistency_spec["elements"]
                focus_points = consistency_spec["focus_points"]
                role_label = {"tender": "招标文件", "bid": "投标文件", "attachment": "附件"}
                # 并行 Phase1（2026-08-30 重构）：每个文件【一次性】提取结构化一致性要素
                # （单 LLM 调用，截断到 max_chars_per_doc 即可承载），多文件并行受
                # consistency_extract_concurrent 约束。相比原「逐段串行摘要 + 拼接 40k 巨 prompt」，
                # 调用量从 O(段数×文件数) 降到 O(文件数)，且 Phase2 比对 prompt 体量小、不触发截断/超时。
                ext_cc = max(1, int(config.get("consistency_extract_concurrent", 6)))
                _ext_sem = asyncio.Semaphore(ext_cc)

                async def _extract_one(di: int, d: dict[str, Any]) -> tuple[str, dict[str, Any]]:
                    async with _ext_sem:
                        # 复用 _build_docs_text 已算好的紧凑表征（大文档=分片摘要），而非原始 60k 全文
                        sum_text = doc_summaries[di]["text"] if di < len(doc_summaries) else None
                        elems = await _extract_file_elements(
                            d, elements, mode, det_temperature, summary_text=sum_text,
                            doc_md5=d.get("md5"), rule_token=consistency_rule_token,
                            cache_enabled=cache_enabled,
                        )
                        return d.get("filename", f"文档{di + 1}"), elems

                file_elements: list[tuple[str, dict[str, Any]]] = []
                _total_files = len(consistency_docs)
                _done_files = 0
                # 心跳续命（修复 A）：Phase1 每完成一个文件的要素提取就 emit 一次 progress 事件，
                # 刷新 last_progress_ts，避免多文件并行提取（或慢模型下单次提取偏长）耗时超过
                # 孤儿回收阈值时被误杀；同时让前端实时显示要素提取进度。
                for coro in asyncio.as_completed(
                    [_extract_one(di, d) for di, d in consistency_docs]
                ):
                    fname, elems = await coro
                    file_elements.append((fname, elems))
                    _done_files += 1
                    await emit(
                        {
                            "type": "stage",
                            "stage": "consistency",
                            "message": f"已完成 {_done_files}/{_total_files} 个文件的一致性要素提取",
                            "progress": round(
                                (len(batches) + 0.4 * (_done_files / max(1, _total_files)))
                                / total
                                * 100
                            )
                            if total
                            else 0,
                        }
                    )
                # 组装紧凑 material：每个文件一段要素取值 JSON，体量为"各文件要素数×约 80 字"，
                # 远小于逐段摘要拼接（可达数万字符），故通常无需截断；仅极端多文件兜底截断。
                blocks: list[str] = []
                for fname, elems in file_elements:
                    if elems:
                        blocks.append(
                            f"===== 【{fname}】一致性要素取值 =====\n"
                            + json.dumps(elems, ensure_ascii=False, indent=2)
                        )
                material = "\n\n".join(blocks)
                # 文档角色映射（文件名 → 角色标签），供预筛做「同角色内比对」、material 标注角色，
                # 避免把招标/投标/评标等不同角色的主体名称误判为一致性冲突。
                role_by_filename: dict[str, str] = {}
                for _di, _d in consistency_docs:
                    _fn = _d.get("filename") or f"文档{_di + 1}"
                    _r = _d.get("file_type_name") or role_label.get(
                        _d.get("role") or _d.get("file_type") or "", "文件"
                    )
                    role_by_filename[_fn] = _r
                max_cons_chars = int(config.get("consistency_max_chars", 40000))
                if len(material) > max_cons_chars:
                    material = (
                        material[:max_cons_chars]
                        + "\n\n（因篇幅限制，仅纳入部分文件要素用于一致性比对）"
                    )
                # === 代码级预筛（根治 40k 巨 prompt 超时，符合「一致不调模型、仅不一致调模型」）===
                # 多文件场景：先按要素名跨文件比对取值，完全一致的直接跳过 LLM，仅把「取值不一致/
                # 缺失」的要素组送进模型做语义级判定（如「壹佰万」vs「1000000」是否等价、缺失是否真问题）。
                # 这样绝大多数要素（一致）不再消耗任何 LLM 调用，彻底消除一次性大 prompt 的慢调用
                # 与 1200s 超时跳过风险；不一致要素组的 material 通常仅数百~数千字。
                multi_consistency = len(consistency_docs) >= 2
                candidate_groups: list[dict[str, Any]] = []
                consistent_count = 0
                compared_count = 0
                if multi_consistency:
                    candidate_groups, consistent_count, compared_count = _consistency_precheck(
                        file_elements, role_by_filename
                    )

                stage_label = "跨文件一致性核查" if multi_consistency else "文档内一致性核查"
                stage_label += "（并行要素提取·代码预筛·结构化比对）"
                if elements:
                    ele_names = [
                        e.get("name") if isinstance(e, dict) else str(e) for e in elements
                    ]
                    stage_label += f"｜要素：{', '.join(str(n) for n in ele_names)}"

                await emit(
                    {
                        "type": "stage", "stage": "consistency",
                        "message": f"执行{stage_label}",
                        "progress": round((len(batches) + 0.4) / total * 100) if total else 0,
                    }
                )
                try:
                    if multi_consistency and not candidate_groups:
                        # 代码预筛：所有可比要素取值完全一致，无需任何 LLM 比对调用（秒级、零超时风险）
                        logger.info(
                            "一致性代码预筛：%d 组要素全部一致（已比对 %d 组），跳过 LLM 比对",
                            consistent_count, compared_count,
                        )
                        await emit(
                            {
                                "type": "stage", "stage": "consistency",
                                "message": (
                                    f"一致性要素已全部一致（共代码比对 {compared_count} 组），"
                                    f"无需模型比对"
                                ),
                                "progress": round((len(batches) + 0.5) / total * 100) if total else 0,
                            }
                        )
                    else:
                        # 需要 LLM 比对的 material：
                        #  - 多文件且有不一致组：仅组装「不一致要素组」的小 prompt（几百~几千字）；
                        #  - 单文件/未预筛：沿用上方已构建的整份要素 material（保留既有文档内一致性行为）。
                        if multi_consistency:
                            material = _build_mismatch_material(candidate_groups, role_by_filename)
                            await emit(
                                {
                                    "type": "stage", "stage": "consistency",
                                    "message": (
                                        f"代码预筛：{compared_count} 组中 {consistent_count} 组一致、"
                                        f"{len(candidate_groups)} 组不一致，正对不一致组调用模型判定"
                                    ),
                                    "progress": round((len(batches) + 0.45) / total * 100) if total else 0,
                                }
                            )
                        # 单文件分支：复用上方已构建的整份 material（含 max_cons_chars 兜底截断）
                        # 复用 _run_with_kb 的容错：网络异常时降无工具模式再重试一次，
                        # 与规则批次审核保持同等韧性。此前裸调 chat 仅 3 次同请求重试，
                        # 在 SiliconFlow 长连接抖动下 100% 失败。
                        # 注意：_run_with_kb 内部已 parse_json，返回的是含 issues 的 dict，
                        # 不可再对其 .get("content") 二次解析（否则得到 None 触发“模型输出为空”）。
                        # structured=True：material 为各文件要素取值 JSON，比对 prompt 体量小、速度快。
                        data, _consistency_traces = await asyncio.wait_for(
                            _run_with_kb(
                                prompts.build_consistency_prompt(
                                    material, mode=mode, multi_doc=multi_consistency,
                                    summarized=True, elements=elements,
                                    structured=True, focus_points=focus_points,
                                ),
                                kb_enabled=False,
                                kb_id=None,
                                web_search_enabled=False,
                                rules=None,
                                timeout=int(config.get("consistency_timeout", 600)),
                                temperature=det_temperature,
                            ),
                            timeout=float(config.get("consistency_timeout", 600)) * 2,
                        )
                        raw_issues = data.get("issues") if isinstance(data, dict) else data
                        for item in raw_issues or []:
                            if not isinstance(item, dict):
                                continue
                            sev = str(item.get("severity") or "major").lower()
                            issue = {
                                "field": str(item.get("field") or ""),
                                "severity": sev if sev in SEVERITY_WEIGHT else "major",
                                "description": str(item.get("description") or ""),
                                "values": [v for v in (item.get("values") or []) if isinstance(v, dict)],
                                "suggestion": str(item.get("suggestion") or ""),
                            }
                            issues.append(issue)
                            await emit({"type": "consistency_issue", "issue": issue})
                except asyncio.TimeoutError:
                    logger.error("一致性核查超时，跳过")
                    await emit({"type": "warning", "message": "一致性核查超时，已跳过"})
                except llm_client.LLMError as exc:
                    logger.error("一致性核查失败: %s", exc)
                    await emit({"type": "warning", "message": f"一致性核查失败: {exc}"})

            findings.sort(
                key=lambda f: (
                    {"fail": 0, "warn": 1, "unknown": 2, "pass": 3}[f["status"]],
                    SEVERITY_ORDER.get(f.get("severity", "major"), 1),
                )
            )
            # 版本清单：本次审核的完整版本指纹（引擎/规则集/数据快照/解析器/环境），
            # 随 done 事件与结果一起存证，支撑「按历史版本重跑」与一致性监控。
            version_manifest = versioning.build_version_manifest(
                docs=docs,
                mode=mode,
                ruleset_id=req_ruleset_id,
                rule_group_ids=req_rule_group_ids,
                auto_match=req_auto_match,
                rules=ordered_rules,
            )
            version_manifest["deterministic"] = det_mode
            # 法规临时规则集版本纳入完整指纹：使确定性重跑能识别「法规规则版本变了」。
            # 规则内容已含在 rule_content_hash 中（ordered_rules 含法规规则），此处额外挂
            # 可追溯的版本元数据，并把版本号并入 rule_set_version 尾注，便于重跑比对。
            version_manifest["legal_rulesets"] = [
                {
                    "id": m.get("id"),
                    "name": m.get("name"),
                    "version": m.get("version"),
                    "rule_count": m.get("rule_count"),
                    "sources_fingerprint": m.get("sources_fingerprint"),
                    "generated_at": m.get("generated_at"),
                }
                for m in (legal_rulesets or [])
            ]
            if legal_rulesets:
                legal_tag = "+LEGAL:" + "-".join(
                    f"{m.get('id')}@v{m.get('version')}"
                    for m in (legal_rulesets or [])
                    if m.get("id")
                )
                version_manifest["rule_set_version"] = (
                    version_manifest["rule_set_version"] + legal_tag
                )
            await emit(
                {
                    "type": "done",
                    "summary": _summarize(findings, issues),
                    "findings": findings,
                    "consistency_issues": issues,
                    "kb_traces": kb_traces,
                    "version_manifest": version_manifest,
                    "progress": 100,
                }
            )
        except Exception as exc:  # noqa: BLE001 - 兜底，保证前端能收到终止事件
            logger.exception("审核任务异常")
            await emit({"type": "error", "message": str(exc)})
        finally:
            # 任务结束：把本次 KB 法规依据缓存落盘（只持久化有答案的成功条目，负缓存丢弃），
            # 下次相同规则/知识库的任务直接命中磁盘缓存，跳过 KB 慢查询。
            try:
                await _save_kb_cache(kb_cache)
            except Exception:  # noqa: BLE001 - 缓存落盘失败绝不能影响任务结果
                logger.exception("KB 缓存落盘失败(忽略)")
            await queue.put(None)

    task = asyncio.create_task(worker())
    try:
        while True:
            evt = await queue.get()
            if evt is None:
                break
            yield evt
    finally:
        if not task.done():
            task.cancel()
