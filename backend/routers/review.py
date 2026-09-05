"""审核接口：SSE 流式返回进度与结果，并提供后台异步任务（不阻塞接口）。"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from .. import config
from ..models.schemas import ReviewRequest
from ..routers import deps
from ..services import file_store, review_engine, rules_store, task_store
from ..services import rule_groups, file_types
from ..services import reviewdata_store
from ..services import legal_rules
from ..services.task_store import to_detail, to_summary

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/review", tags=["review"])

# 模块级任务存储（单进程单例）
store = task_store.get_store()
# 保活后台任务引用，避免被 GC 中途回收
_background: set[asyncio.Task] = set()


def _resolve_base_rules(
    req: ReviewRequest,
    docs: list[dict] | None,
    user_id: str | None,
    allow_empty: bool = False,
) -> list[dict]:
    """解析既有来源（规则组 > 规则集 > 模式）的规则。

    allow_empty=True 时，来源解析为空也不报错（用于「仅用法规临时规则集审核」的场景）。
    """
    group_ids: list[str] = list(req.rule_group_ids or [])
    if req.auto_match and docs:
        for d in docs:
            ft = d.get("file_type")
            if ft:
                ft_rec = file_types.get(ft)
                if ft_rec:
                    group_ids.extend(ft_rec.get("rule_group_ids", []))

    # 去重保序
    seen: set[str] = set()
    ordered_groups: list[str] = []
    for g in group_ids:
        if g not in seen:
            seen.add(g)
            ordered_groups.append(g)

    if ordered_groups:
        rules = rule_groups.expand_rule_groups(ordered_groups, user_id)
        if not rules:
            if allow_empty:
                return []
            raise HTTPException(status_code=400, detail="所选审核规则组未包含任何有效规则")
        return rules

    # 选择了真实（非合成）规则集 → 以规则集为唯一来源
    if req.ruleset_id and not str(req.ruleset_id).startswith("mode-"):
        try:
            rules = rules_store.select_rules(req.ruleset_id, req.rule_ids, user_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="规则集不存在") from exc
    elif req.mode and req.mode in ("bid", "tender", "general"):
        # 内置模式（前端合成 mode-* 规则集）：返回对应内置规则并按勾选过滤
        rules = rules_store.get_rules_by_mode(req.mode)
        if req.rule_ids:
            wanted = set(req.rule_ids)
            rules = [r for r in rules if r["id"] in wanted]
    else:
        try:
            rules = rules_store.select_rules(req.ruleset_id, req.rule_ids, user_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="规则集不存在") from exc
    if not rules:
        if allow_empty:
            return []
        raise HTTPException(status_code=400, detail="没有启用的审核规则")
    return rules


def _resolve_legal_rules(
    ruleset_ids: list[str], user_id: str | None
) -> list[dict]:
    """解析「法规临时规则集」：由上传的法律法规文件自动生成的规则。

    与既有来源（规则组/规则集/模式）并存，按规则 id 去重。
    规则集不存在、无权限、或尚未生成完成（status != ready）时直接 400，
    避免把「规则还在抽取中」误当成「没有规则」而静默审核。
    """
    if not ruleset_ids:
        return []
    rules, missing = legal_rules.resolve_rules(ruleset_ids, user_id)
    if missing:
        raise HTTPException(
            status_code=400,
            detail=(
                f"法规临时规则集不可用（不存在、无权限或尚未生成完成）：{', '.join(missing)}。"
                "若仍在解析中，请改用「创建审核任务」（后台任务会在解析完成后自动开始审核）。"
            ),
        )
    return rules


def _has_explicit_base(req: ReviewRequest) -> bool:
    """是否显式选择了既有规则来源（规则组 / 真实规则集 / 规则清单）。

    前端总会传入合成的规则集 id（``mode-bid`` 等）与 mode，这两者不代表用户
    「主动选择了既有规则」，故不计入；只有真实 ruleset_id、规则组或显式规则
    清单才算。用于判定「只传了法规规则集」时是否走纯法规模式。
    """
    if req.rule_group_ids:
        return True
    if req.ruleset_id and not str(req.ruleset_id).startswith("mode-"):
        return True
    if req.rule_ids:
        return True
    return False


def _resolve_rules(
    req: ReviewRequest, docs: list[dict] | None = None, user_id: str | None = None
) -> list[dict]:
    """解析实际送审规则。

    两种模式：
    1. **纯法规模式**（legal_rules_only=True，或未显式选择任何既有来源）：
       只跑由法规文件自动生成的临时规则，不套用任何内置规则。
    2. **叠加模式**（显式选择了规则组/规则集/规则清单，或 legal_rules_only=False）：
       既有来源的规则在前、法规规则在后，按规则 id 去重合并。
    """
    legal_ids = [x for x in (req.legal_ruleset_ids or []) if x]
    if not legal_ids:
        return _resolve_base_rules(req, docs, user_id)

    legal_only = req.legal_rules_only
    if legal_only is None:
        legal_only = not _has_explicit_base(req)

    if legal_only:
        rules = _resolve_legal_rules(legal_ids, user_id)
        if not rules:
            raise HTTPException(status_code=400, detail="没有启用的审核规则")
        return rules

    rules: list[dict] = _resolve_base_rules(req, docs, user_id, allow_empty=True)
    seen = {str(r.get("id")) for r in rules if r.get("id")}
    for r in _resolve_legal_rules(legal_ids, user_id):
        rid = str(r.get("id") or "")
        if rid and rid in seen:
            continue
        if rid:
            seen.add(rid)
        rules.append(r)
    if not rules:
        raise HTTPException(status_code=400, detail="没有启用的审核规则")
    return rules


def _legal_ruleset_meta(ruleset_ids: list[str], user_id: str | None) -> list[dict]:
    """记录临时规则集的来源与版本，随任务持久化。

    临时规则集可被用户随时编辑或删除，若不存这份元数据，历史任务/报告里
    「依据哪份法规的哪一版规则得出的结论」将无法追溯。
    """
    out: list[dict] = []
    for rid in ruleset_ids or []:
        rec = legal_rules.get_set(rid, user_id)
        if not rec:
            continue
        out.append(
            {
                "id": rec.get("id"),
                "name": rec.get("name"),
                "version": rec.get("version"),
                "rule_count": len(rec.get("rules") or []),
                "source_files": [
                    f.get("filename") for f in (rec.get("source_files") or [])
                ],
                "sources_fingerprint": rec.get("sources_fingerprint"),
                "llm_model": (rec.get("params") or {}).get("llm_model"),
                "generated_at": rec.get("updated_at"),
            }
        )
    return out


def _legal_instruction_note(ruleset_ids: list[str], user_id: str | None) -> str:
    """把「本次规则来自哪份法规文件」写进审核说明，供模型与报告溯源。"""
    meta = _legal_ruleset_meta(ruleset_ids, user_id)
    if not meta:
        return ""
    parts = [
        f"《{m.get('name')}》(v{m.get('version')}，{m.get('rule_count')} 条，"
        f"来源：{'、'.join(m.get('source_files') or []) or '未知'})"
        for m in meta
    ]
    return (
        "【法规临时规则】本次审核的部分规则由用户上传的法规文件自动生成："
        + "；".join(parts)
        + "。这些规则的判定标准与依据条款已内嵌在规则说明中，请严格按规则描述的标准核查；"
        "若被审文档内容超出规则覆盖范围，判 unknown，不要自行扩大解释。"
    )


def _effective_kb_id(req: ReviewRequest) -> str | None:
    """任务级知识库优先；未指定时回落到系统配置的知识库（config.kb_id）。"""
    return req.kb_id if req.kb_id else config.get("kb_id")


def _effective_group_ids(req: ReviewRequest, docs: list[dict]) -> list[str]:
    """计算本次任务生效的规则组 id 列表（显式多选 ∪ 自动匹配）。"""
    group_ids: list[str] = list(req.rule_group_ids or [])
    if req.auto_match:
        for d in docs:
            ft = d.get("file_type")
            if ft:
                ft_rec = file_types.get(ft)
                if ft_rec:
                    group_ids.extend(ft_rec.get("rule_group_ids", []))
    seen: set[str] = set()
    ordered: list[str] = []
    for g in group_ids:
        if g not in seen:
            seen.add(g)
            ordered.append(g)
    return ordered


def _resolve_docs(req: ReviewRequest) -> list[dict]:
    if not req.file_ids:
        raise HTTPException(status_code=400, detail="请至少选择一个文件")
    try:
        docs = file_store.get_many(req.file_ids)
    except file_store.StoreError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    # 内容规则校验：逐文件检查可审核文本，缺失则结构化报错（不静默丢弃）
    unusable = [d.get("filename", d.get("file_id", "")) for d in docs if not (d.get("text") or "").strip()]
    if unusable:
        raise HTTPException(
            status_code=400,
            detail={"errors": [f"{f}：未提取到可审核的文本内容（空文件或解析失败）" for f in unusable]},
        )
    # 法规依据文件（role=legal）仅用于「自动生成临时审核规则」，不能当审核目标送审——
    # 否则会把法规自身当成投标/招标文件逐条核查，产出大量无意义结论。
    legal_src = [d.get("filename", d.get("file_id", "")) for d in docs if d.get("role") == "legal"]
    if legal_src:
        raise HTTPException(
            status_code=400,
            detail={
                "errors": [
                    f"{f}：该文件是法规依据文件，只能用于生成临时审核规则，不能作为审核对象。"
                    for f in legal_src
                ]
            },
        )
    # 补充文件类型名称，便于审核引擎标注上下文
    for d in docs:
        ft = d.get("file_type")
        if ft:
            d["file_type_name"] = (file_types.get(ft) or {}).get("name", "")
    return docs


def _build_on_event(tid: str, findings: list, issues: list, kb_traces: list):
    """构造事件回调：把审核引擎事件写入任务存储。"""
    mark = {"fail": "✗", "pass": "✓", "warn": "!", "unknown": "?"}

    async def on_event(evt: dict) -> None:
        etype = evt.get("type")
        if etype == "stage":
            # 仅当事件显式携带 progress 时才更新进度数值：避免缺省（未带 progress）的
            # stage 事件被默认置 0，导致审核过程中进度「跳回 0% / 卡在 0%」的观感
            # （分段校对、要素提取等子步骤只带 message 不带 progress）。
            if "progress" in evt:
                await store.update(
                    tid,
                    progress=evt["progress"],
                    progress_message=evt.get("message", ""),
                )
            else:
                await store.update(tid, progress_message=evt.get("message", ""))
            await store.append_log(tid, evt.get("message", ""), "info")
        elif etype == "kb_query":
            await store.append_log(tid, f"检索法规知识库：{evt.get('query')}", "kb")
        elif etype == "kb_result":
            await store.append_log(
                tid, f"知识库返回依据（{len(evt.get('sources', []))} 条来源）", "kb"
            )
            kb_traces.append(
                {k: evt.get(k) for k in ("query", "reason", "answer", "sources")}
            )
            await store.update(tid, kb_traces=list(kb_traces))
        elif etype == "kb_skip":
            await store.append_log(tid, evt.get("message", ""), "info")
        elif etype == "kb_error":
            await store.append_log(tid, f"知识库检索失败：{evt.get('message')}", "warn")
        elif etype == "web_search":
            await store.append_log(tid, f"联网搜索：{evt.get('query')}", "kb")
        elif etype == "web_search_result":
            await store.append_log(
                tid, f"搜索完成，找到 {evt.get('result_count')} 条结果", "ok"
            )
        elif etype == "web_search_error":
            await store.append_log(tid, f"搜索失败：{evt.get('message')}", "error")
        elif etype == "finding":
            f = evt["finding"]
            m = mark.get(f.get("status"), "?")
            await store.append_log(
                tid,
                f"{m} {f.get('rule_name') or f.get('rule_id')}：{f.get('title', '')}",
                "error" if f.get("status") == "fail" else "ok" if f.get("status") == "pass" else "warn",
            )
            findings.append(f)
            await store.update(tid, findings=list(findings))
        elif etype == "consistency_issue":
            issues.append(evt["issue"])
            await store.append_log(tid, f"一致性问题：{evt['issue'].get('field')}", "warn")
            await store.update(tid, consistency_issues=list(issues))
        elif etype == "tender_summary":
            await store.append_log(tid, "已提取招标文件关键要求", "ok")
        elif etype == "rule_results":
            # 每规则一条的最终聚合结果（N 段并行校对结论已合并去重）：20 规则 → 20 条
            await store.update(tid, rule_results=list(evt.get("results", [])))
        elif etype == "warning":
            await store.append_log(tid, evt.get("message", ""), "warn")
        elif etype == "error":
            await store.append_log(tid, evt.get("message", ""), "error")
            await store.update(tid, error=evt.get("message"))

    return on_event


async def _drive_task(tid: str, req: ReviewRequest) -> None:
    """后台执行审核，把进度/结果持续写入任务存储。"""
    terminated = False
    try:
        docs = _resolve_docs(req)
        rules = _resolve_rules(req, docs)
        file_names = [d.get("filename", "") for d in docs]

        kb_enabled = (
            config.get("kb_enabled", True) if req.kb_enabled is None else req.kb_enabled
        )
        web_search_enabled = (
            config.get("web_search_enabled", False)
            if req.web_search_enabled is None
            else req.web_search_enabled
        )
        kb_id = _effective_kb_id(req)

        # 确定性执行参数：显式指定 > 配置默认。默认开启以保证可重复审核一致。
        deterministic = (
            bool(req.deterministic_mode)
            if req.deterministic_mode is not None
            else bool(config.get("deterministic_mode", True))
        )

        await store.update(tid, status="running", progress_message="开始审核")
        store.mark_active(tid)  # 注册活动工作进程，供孤儿回收器区分「正在跑」与「僵尸」

        # 取任务归属用户，使审核期沉淀的错别字校验项正确归属本人/管理员
        task = await store.get(tid)
        task_user_id = task.get("user_id") if isinstance(task, dict) else None

        findings: list = []
        issues: list = []
        kb_traces: list = []
        on_event = _build_on_event(tid, findings, issues, kb_traces)

        gen = review_engine.run_review(
            docs=docs,
            rules=rules,
            mode=req.mode,
            kb_enabled=bool(kb_enabled),
            kb_id=kb_id,
            web_search_enabled=bool(web_search_enabled),
            extra_instruction=req.extra_instruction,
            task_id=tid,
            user_id=task_user_id,
            deterministic=deterministic,
            ruleset_id=req.ruleset_id,
            rule_group_ids=req.rule_group_ids,
            auto_match=req.auto_match,
            legal_rulesets=_legal_ruleset_meta(req.legal_ruleset_ids or [], task_user_id),
            cache_enabled=req.cache_enabled,
        )

        async for evt in gen:
            if await store.is_cancelled(tid):
                await store.append_log(tid, "任务已取消", "warn")
                await store.update(
                    tid,
                    status="cancelled",
                    finished_at=time.time(),
                    progress_message="已取消",
                )
                terminated = True
                await gen.aclose()
                return
            if evt.get("type") == "done":
                await store.update(
                    tid,
                    summary=evt.get("summary"),
                    progress=100,
                    progress_message="审核完成",
                    version_manifest=evt.get("version_manifest"),
                )
                await store.append_log(
                    tid,
                    f"审核完成：{evt['summary'].get('conclusion')}（得分 {evt['summary'].get('score')}）",
                    "ok",
                )
                await store.update(tid, status="completed", finished_at=time.time())
                terminated = True
                # 历史审核数据归档：建立「文件(MD5)+规则→结果(采纳/不采纳)」关联记录，
                # 供后续相同文件+规则组合回流校验与历史维护。
                try:
                    md5s = [d.get("md5") for d in docs if d.get("md5")]
                    rule_ids_eff = [str(r.get("id")) for r in rules if r.get("id")]
                    rule_names_eff = [str(r.get("name") or "") for r in rules if r.get("id")]
                    from ..services import feedback_store

                    decisions_by_rule = feedback_store.get_judgments_by_task(tid)
                    rec_id = reviewdata_store.archive_task(
                        task_id=tid,
                        file_md5s=md5s,
                        file_names=file_names,
                        rule_ids=rule_ids_eff,
                        rule_names=rule_names_eff,
                        findings=findings,
                        consistency_issues=issues,
                        decisions_by_rule=decisions_by_rule,
                    )
                    if rec_id:
                        await store.append_log(
                            tid, f"已归档历史审核关联记录（{rec_id}）", "ok"
                        )
                    # 审计存证：保存本次审核的完整版本清单，支撑「按历史版本重跑」与一致性监控。
                    manifest = evt.get("version_manifest") or {}
                    if md5s and rule_ids_eff:
                        audit_cfg = {
                            "llm_model": config.get("llm_model"),
                            "llm_base_url": config.get("llm_base_url"),
                            "llm_temperature": config.get("llm_temperature"),
                            "deterministic_temperature": config.get("deterministic_temperature"),
                            "concurrency": config.get("concurrency"),
                            "rules_per_batch": config.get("rules_per_batch"),
                            "kb_enabled": kb_enabled,
                            "web_search_enabled": web_search_enabled,
                            "consistency_max_chars": config.get("consistency_max_chars"),
                        }
                        audit_id = reviewdata_store.save_audit(
                            task_id=tid,
                            file_md5s=md5s,
                            rule_ids=rule_ids_eff,
                            manifest=manifest,
                            config_snapshot=audit_cfg,
                            operator="system",
                        )
                        if audit_id:
                            await store.append_log(
                                tid,
                                f"已存证审计记录（引擎 {manifest.get('engine_version','')} / "
                                f"规则 {manifest.get('rule_set_version','')} / "
                                f"数据 {manifest.get('data_snapshot_version','')}）",
                                "ok",
                            )
                except Exception as arc_exc:  # noqa: BLE001 - 归档失败不应影响已完成的任务
                    logger.warning("审核数据归档/存证失败: %s", arc_exc)
            elif evt.get("type") == "error":
                await store.update(
                    tid,
                    status="failed",
                    error=evt.get("message"),
                    finished_at=time.time(),
                )
                terminated = True
            else:
                await on_event(evt)

        if not terminated:
            await store.update(tid, status="completed", finished_at=time.time())
    except HTTPException as exc:
        await store.update(
            tid, status="failed", error=exc.detail, finished_at=time.time()
        )
    except Exception as exc:  # noqa: BLE001 - 兜底，保证任务进入终态
        logger.exception("审核任务异常")
        await store.append_log(tid, f"审核异常：{exc}", "error")
        await store.update(
            tid, status="failed", error=str(exc), finished_at=time.time()
        )
    finally:
        # 无论正常完成/异常/被取消，都注销活动工作进程，避免僵尸回收器误判
        store.mark_inactive(tid)


# --------------------- 法规解析 → 审核自动串行（后台链） ---------------------

_LEGAL_POLL_INTERVAL = 2.0  # 法规解析进度镜像轮询间隔（秒）
_LEGAL_WAIT_MAX = 3600  # 等待法规解析的总上限（秒），防极端挂死


def _mining_legal_ids(ruleset_ids: list[str], user_id: str | None) -> list[str]:
    """筛选出「尚未解析完成」的法规临时规则集 id。"""
    out: list[str] = []
    for rid in ruleset_ids or []:
        rec = legal_rules.get_set(rid, user_id)
        if rec and rec.get("status") in ("pending", "mining"):
            out.append(rid)
    return out


async def _fail_waiting_task(tid: str, message: str) -> None:
    """把等待法规解析的审核任务标记为失败（已处终态则忽略）。"""
    task = await store.get(tid)
    if not task or task_store.TaskStore.is_terminal(task.get("status", "")):
        return
    await store.append_log(tid, message, "error")
    await store.update(tid, status="failed", error=message, finished_at=time.time())
    store.mark_inactive(tid)


async def _wait_legal_then_drive(tid: str, req: ReviewRequest, mining_ids: list[str], user_id: str | None) -> None:
    """等待法规规则集解析完成后自动驱动审核任务。

    等待期间把法规解析进度/日志镜像到审核任务上（进度条先展示解析进度，
    解析完成后切回审核自身进度），实现「先解析、后审核」的连续体验。
    """
    import datetime

    mirrored_logs = 0  # 已镜像到审核任务的法规日志条数（各规则集日志累计水位）
    deadline = time.monotonic() + _LEGAL_WAIT_MAX
    try:
        while True:
            # 用户取消/系统已置终态则退出，不再驱动审核
            task = await store.get(tid)
            if not task or task_store.TaskStore.is_terminal(task.get("status", "")):
                store.mark_inactive(tid)
                return

            recs = []
            for rid in mining_ids:
                rec = legal_rules.get_set(rid, user_id)
                if not rec:
                    await _fail_waiting_task(tid, f"法规规则集 {rid} 已不存在，审核无法继续")
                    return
                recs.append(rec)

            statuses = [str(r.get("status")) for r in recs]
            if "failed" in statuses:
                bad = next(r for r in recs if str(r.get("status")) == "failed")
                await _fail_waiting_task(
                    tid,
                    f"法规规则抽取失败，审核任务终止：《{bad.get('name')}》"
                    f"{bad.get('error') or bad.get('progress_message') or ''}",
                )
                return
            if all(s == "ready" for s in statuses):
                break
            if time.monotonic() > deadline:
                await _fail_waiting_task(tid, "等待法规解析超时，审核任务终止")
                return

            # 镜像解析进度与增量日志（多规则集取进度最小者，保证不虚高）
            slowest = min(recs, key=lambda r: float(r.get("progress") or 0))
            logs = slowest.get("logs") or []
            new_logs = logs[mirrored_logs:]
            mirrored_logs = len(logs)
            fields: dict[str, Any] = {
                "progress": float(slowest.get("progress") or 0),
                "progress_message": (
                    f"法规解析中《{slowest.get('name')}》："
                    f"{slowest.get('progress_message') or ''}"
                ),
            }
            if new_logs:
                task_logs = list(task.get("logs") or [])
                for entry in new_logs:
                    task_logs.append(
                        {
                            "time": datetime.datetime.now().strftime("%H:%M:%S"),
                            "text": f"[法规解析] {entry.get('text', '')}",
                            "level": entry.get("level", "info"),
                        }
                    )
                fields["logs"] = task_logs
            await store.update(tid, **fields)
            await asyncio.sleep(_LEGAL_POLL_INTERVAL)

        # ---- 解析全部完成：回填规则元数据，交接给审核驱动 ----
        docs = _resolve_docs(req)
        rules = _resolve_rules(req, docs, user_id)

        # 法规溯源说明此刻才注入（创建时规则集尚在解析，元数据不完整）
        legal_note = _legal_instruction_note(req.legal_ruleset_ids or [], user_id)
        if legal_note:
            req.extra_instruction = (
                (req.extra_instruction or "").strip() + "\n\n" + legal_note
            ).strip()

        # 历史结果回流：与普通任务创建路径一致（规则清单此时才可解析）
        md5s = [d.get("md5") for d in docs if d.get("md5")]
        rule_ids_eff = [str(r.get("id")) for r in rules if r.get("id")]
        reflow = reviewdata_store.build_reflow_instruction(md5s, rule_ids_eff)
        reflow_applied = False
        if reflow:
            logger.info("串行任务 %s 命中历史审核对应关系，注入回流约束(%d字)", tid, len(reflow))
            req.extra_instruction = (
                (req.extra_instruction or "").strip() + "\n\n" + reflow
            ).strip()
            reflow_applied = True

        task = await store.get(tid)
        meta = dict(task.get("request") or {}) if task else {}
        meta.update(
            {
                "rule_count": len(rules),
                "rule_ids": [str(r.get("id")) for r in rules if r.get("id")],
                "rule_names": [str(r.get("name") or "") for r in rules if r.get("name")],
                "legal_rule_meta": _legal_ruleset_meta(req.legal_ruleset_ids or [], user_id),
                "extra_instruction": req.extra_instruction,
            }
        )
        names = "、".join(str(r.get("name") or "") for r in recs)
        await store.append_log(
            tid,
            f"法规解析完成（{names}）：共解析出 {len(rules)} 条规则，自动开始合规审核",
            "ok",
        )
        await store.update(
            tid,
            rule_count=len(rules),
            request=meta,
            extra_instruction=req.extra_instruction,
            reflow_applied=reflow_applied,
            progress=0,
            progress_message="法规解析完成，开始合规审核",
        )
        await _drive_task(tid, req)
    except HTTPException as exc:
        await _fail_waiting_task(tid, str(exc.detail))
    except Exception as exc:  # noqa: BLE001 - 兜底，保证任务进入终态
        logger.exception("法规解析串行链异常")
        await _fail_waiting_task(tid, f"等待法规解析时异常：{exc}")


def _spawn_chained_review(tid: str, req: ReviewRequest, mining_ids: list[str], user_id: str | None) -> None:
    """注册并保活「解析→审核」串行链后台任务。"""
    t = asyncio.create_task(_wait_legal_then_drive(tid, req, mining_ids, user_id))
    _background.add(t)
    t.add_done_callback(_background.discard)


async def _create_chained_task(
    req: ReviewRequest,
    docs: list[dict],
    mining_ids: list[str],
    user_id: str | None,
    caller: dict,
) -> dict:
    """创建「先解析后审核」的串行任务。

    与普通任务的区别：法规规则尚未解析完成，规则清单/说明在解析完成后由
    串行链回填；等待期间任务镜像法规解析进度与日志。
    """
    legal_only = (
        req.legal_rules_only
        if req.legal_rules_only is not None
        else not _has_explicit_base(req)
    )
    # 法规规则此刻不可用：纯法规模式暂无规则；叠加模式先解析既有来源
    rules = [] if legal_only else _resolve_base_rules(req, docs, user_id, allow_empty=True)
    _resolved_rules = [r for r in rules if r.get("id")]

    file_names = [d.get("filename", "") for d in docs]
    tid = await store.create(
        {
            "file_ids": req.file_ids,
            "mode": req.mode,
            "ruleset_id": req.ruleset_id,
            "rule_ids": [str(r.get("id")) for r in _resolved_rules],
            "rule_names": [str(r.get("name") or "") for r in _resolved_rules],
            "file_types": [d.get("file_type") or "" for d in docs],
            "rule_group_ids": _effective_group_ids(req, docs),
            "auto_match": req.auto_match,
            "legal_ruleset_ids": list(req.legal_ruleset_ids or []),
            "legal_rule_meta": _legal_ruleset_meta(req.legal_ruleset_ids or [], user_id),
            "waiting_legal_rulesets": mining_ids,
            "kb_enabled": req.kb_enabled,
            "kb_id": _effective_kb_id(req),
            "web_search_enabled": req.web_search_enabled,
            "cache_enabled": req.cache_enabled,
            "extra_instruction": req.extra_instruction,
            "file_names": file_names,
            "rule_count": len(rules),
        },
        user_id=user_id,
    )
    names = "、".join(
        str((legal_rules.get_set(rid, user_id) or {}).get("name") or rid)
        for rid in mining_ids
    )
    await store.update(
        tid,
        status="running",
        progress_message=f"法规解析中（{names}），解析完成后自动开始审核",
    )
    store.mark_active(tid)
    await store.append_log(tid, f"法规规则抽取进行中（{names}），解析完成后将自动开始审核", "info")
    await store.append_log(tid, "解析进度将实时同步到本任务，无需手动干预", "info")
    _spawn_chained_review(tid, req, mining_ids, user_id)
    task = await store.get(tid)
    return to_detail(task or {})


# ----------------------------- SSE（保留兼容） -----------------------------
@router.post("/stream", summary="SSE 实时审核：流式返回进度与结论事件")
async def review_stream(req: ReviewRequest, caller: dict = Depends(deps.get_caller)):
    """启动审核，以 SSE 推送阶段进度、知识库检索轨迹与最终结论。"""
    docs = _resolve_docs(req)
    rules = _resolve_rules(req, docs, caller["user_id"])

    kb_enabled = (
        config.get("kb_enabled", True) if req.kb_enabled is None else req.kb_enabled
    )
    web_search_enabled = (
        config.get("web_search_enabled", False)
        if req.web_search_enabled is None
        else req.web_search_enabled
    )
    kb_id = _effective_kb_id(req)

    async def event_source():
        try:
            async for event in review_engine.run_review(
                docs=docs,
                rules=rules,
                mode=req.mode,
                kb_enabled=bool(kb_enabled),
                kb_id=kb_id,
                web_search_enabled=bool(web_search_enabled),
                extra_instruction=req.extra_instruction,
                task_id=None,
                user_id=caller["user_id"],
                legal_rulesets=_legal_ruleset_meta(req.legal_ruleset_ids or [], caller["user_id"]),
                cache_enabled=req.cache_enabled,
            ):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as exc:  # noqa: BLE001 - 保证前端能收到错误事件
            logger.exception("审核流异常")
            payload = {"type": "error", "message": str(exc)}
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ----------------------------- 后台异步任务 -----------------------------
@router.post("/tasks", summary="创建异步审核任务")
async def create_task(req: ReviewRequest, caller: dict = Depends(deps.get_caller)):
    """提交一个后台审核任务，接口立即返回任务 ID，不阻塞。

    若引用的法规临时规则集仍在解析中，任务进入「串行等待」：先镜像解析进度，
    解析完成后自动开始审核（无需用户重新操作）。
    """
    docs = _resolve_docs(req)  # 校验文件存在与可审核内容
    user_id = caller["user_id"]

    # 法规规则集仍在抽取中 → 创建串行任务（先镜像解析进度，解析完自动审核）
    mining_ids = _mining_legal_ids(req.legal_ruleset_ids or [], user_id)
    if mining_ids:
        return await _create_chained_task(req, docs, mining_ids, user_id, caller)

    rules = _resolve_rules(req, docs, user_id)
    file_names = [d.get("filename", "") for d in docs]
    file_types_eff = [d.get("file_type") or "" for d in docs]
    rule_group_ids_eff = _effective_group_ids(req, docs)
    md5s = [d.get("md5") for d in docs if d.get("md5")]
    _resolved_rules = [r for r in rules if r.get("id")]
    rule_ids_eff = [str(r.get("id")) for r in _resolved_rules]
    rule_names_eff = [str(r.get("name") or "") for r in _resolved_rules]

    # 按历史版本重跑：若显式携带 replay_from_task_id，则强制沿用该次审核的版本清单
    # （引擎/规则集/数据快照/解析器/配置参数），确保「相同输入+相同版本 → 相同输出」。
    replay_manifest: dict | None = None
    if req.replay_from_task_id:
        base = reviewdata_store.get_audit_by_task(req.replay_from_task_id)
        if base:
            replay_manifest = base
            # 重放时强制确定性执行，不因当前配置漂移而偏离历史基线
            req.deterministic_mode = True

    # 法规临时规则：把规则来源写进审核说明，既提升判定准确性，也让报告可溯源
    legal_note = _legal_instruction_note(req.legal_ruleset_ids or [], user_id)
    if legal_note:
        req.extra_instruction = (
            (req.extra_instruction or "").strip() + "\n\n" + legal_note
        ).strip()

    tid = await store.create(
        {
            "file_ids": req.file_ids,
            "mode": req.mode,
            "ruleset_id": req.ruleset_id,
            # 持久化「解析后」的规则清单：仅传 ruleset_id/rule_group 而未显式传 rule_ids 时，
            # 回填后端解析出的完整规则列表，确保任务详情/历史存证能展示规则清单。
            "rule_ids": (req.rule_ids and len(req.rule_ids)) and list(req.rule_ids) or rule_ids_eff,
            "rule_names": rule_names_eff,
            "file_types": file_types_eff,
            "rule_group_ids": rule_group_ids_eff,
            "auto_match": req.auto_match,
            # 法规临时规则集：id 列表 + 来源/版本元数据（供历史溯源与报告解释）
            "legal_ruleset_ids": list(req.legal_ruleset_ids or []),
            "legal_rule_meta": _legal_ruleset_meta(req.legal_ruleset_ids or [], user_id),
            "kb_enabled": req.kb_enabled,
            "kb_id": _effective_kb_id(req),
            "web_search_enabled": req.web_search_enabled,
            "cache_enabled": req.cache_enabled,
            "extra_instruction": req.extra_instruction,
            "file_names": file_names,
            "rule_count": len(rules),
        },
        user_id=user_id,
    )

    # 历史结果回流：若所传文件 MD5 与本次送审规则对应关系已存在，则把历史采纳/不采纳
    # 结论(含不采纳理由)带入模型处理，确保输出与历史判定保持一致。
    md5s = [d.get("md5") for d in docs if d.get("md5")]
    rule_ids_eff = [str(r.get("id")) for r in rules if r.get("id")]
    reflow = reviewdata_store.build_reflow_instruction(md5s, rule_ids_eff)
    if reflow:
        logger.info("任务 %s 命中历史审核对应关系，注入回流约束(%d字)", tid, len(reflow))
        req.extra_instruction = (
            (req.extra_instruction or "").strip() + "\n\n" + reflow
        ).strip()
        await store.update(
            tid,
            extra_instruction=req.extra_instruction,
            reflow_applied=True,
        )

    # 按历史版本重跑：记录重放基线，强制确定性执行，确保可复现。
    if replay_manifest:
        logger.info(
            "任务 %s 按历史版本重跑(基線 task=%s)：引擎 %s / 规则 %s / 数据 %s / 解析 %s",
            tid,
            req.replay_from_task_id,
            replay_manifest.get("engine_version"),
            replay_manifest.get("rule_set_version"),
            replay_manifest.get("data_snapshot_version"),
            replay_manifest.get("parser_version"),
        )
        await store.update(
            tid,
            extra_instruction=(
                (req.extra_instruction or "").strip()
                + "\n\n【按历史版本重跑】本次审核强制沿用历史版本基线"
                f"（引擎 {replay_manifest.get('engine_version')} / "
                f"规则集 {replay_manifest.get('rule_set_version')} / "
                f"数据快照 {replay_manifest.get('data_snapshot_version')} / "
                f"解析器 {replay_manifest.get('parser_version')}），"
                "并以确定性模式执行，确保结果与历史基线一致。"
            ).strip(),
            replay_from_task_id=req.replay_from_task_id,
        )
    task = asyncio.create_task(_drive_task(tid, req))
    _background.add(task)
    task.add_done_callback(_background.discard)
    return {"task_id": tid, "status": "pending"}


@router.get("/tasks", summary="查询审核任务列表")
async def list_tasks(caller: dict = Depends(deps.get_caller)):
    """列出历史任务（最新在前）。管理员看全部，普通用户只看自己的。"""
    tasks = await store.list(user_id=deps.scope_user_id(caller))
    return {"tasks": [to_summary(t) for t in tasks]}


@router.get("/tasks/{task_id}", summary="查询审核任务详情（状态/进度/结论）")
async def get_task(task_id: str, caller: dict = Depends(deps.get_caller)):
    """获取任务详情（状态、进度、日志、结果）。普通用户仅能查看自己归属的任务。"""
    task = await store.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    # 归属校验：普通用户不能越权查看他人任务
    if caller["role"] != "admin" and task.get("user_id") not in (None, caller["user_id"]):
        raise HTTPException(status_code=404, detail="任务不存在")
    # 历史任务（从 reviewdata 归档恢复）的 findings 为空时，从归档库聚合完整结论回填，
    # 使详情页能展示完整的逐条审核结果，而非空白。
    if not task.get("findings"):
        try:
            task = dict(task)
            task["findings"] = reviewdata_store.get_findings_by_task(task_id)
        except Exception:  # noqa: BLE001 - 归档回显失败不应影响详情返回
            pass
    return to_detail(task)


@router.post("/tasks/{task_id}/cancel", summary="取消进行中的审核任务")
async def cancel_task(task_id: str):
    """取消进行中/排队中的任务。"""
    task = await store.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task_store.TaskStore.is_terminal(task["status"]):
        return to_detail(task)
    await store.set_cancel(task_id)
    await store.update(
        task_id,
        status="cancelled",
        finished_at=time.time(),
        progress_message="已取消",
    )
    return to_detail(task)


@router.delete("/tasks/{task_id}", summary="删除单条审核任务")
async def delete_task(task_id: str):
    """从任务历史中删除一条记录。"""
    await store.remove(task_id)
    return {"deleted": True}


@router.delete("/tasks", summary="批量删除审核任务")
async def clear_tasks(
    confirm: bool = Query(False, description="必须显式传 confirm=true 才能清空全部任务历史，防止误触/脚本误清"),
):
    """清空全部任务历史。需显式 confirm=true，防止误触或脚本误调时把已完成的审核任务一并清空。"""
    if not confirm:
        raise HTTPException(
            status_code=400,
            detail="清空全部任务历史需显式携带 confirm=true 参数（如 ?confirm=true）",
        )
    await store.clear()
    return {"cleared": True}
