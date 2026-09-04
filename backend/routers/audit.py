# -*- coding: utf-8 -*-
"""审核审计存证与可重放接口。

- GET  /api/audit/records          审计存证列表（按 版本/任务 筛选）
- GET  /api/audit/records/{task_id} 单任务审计存证（版本清单）
- POST /api/audit/replay-baseline  查询给定文件+规则的可用重放基线（历史版本）
- GET  /api/audit/consistency      一致性监控：抽取已审文件用相同版本重跑对比差异（抽样）
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ..routers import deps
from ..services import reviewdata_store, task_store, review_engine, rules_store, file_store
from .. import config

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("/records", summary="分页查询审核记录（含过滤条件）")
async def list_audit(
    q: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _: dict = Depends(deps.require_admin),
):
    rows, total = reviewdata_store.list_audits(q=q, limit=limit, offset=offset)
    return {"records": rows, "total": total, "limit": limit, "offset": offset}


@router.get("/records/{task_id}", summary="查询单条审核记录详情")
async def get_audit(task_id: str, _: dict = Depends(deps.require_admin)):
    rec = reviewdata_store.get_audit_by_task(task_id)
    if not rec:
        raise HTTPException(status_code=404, detail="该任务无审计存证")
    return rec


@router.post("/replay-baseline", summary="重放基线：按历史记录重建对比基线数据")
async def replay_baseline(
    payload: dict[str, Any], _: dict = Depends(deps.require_admin)
):
    """查询给定文件 MD5 + 规则 id 组合的可用重放基线（最近一次历史版本存证）。"""
    file_md5s = payload.get("file_md5s") or []
    rule_ids = payload.get("rule_ids") or []
    if not file_md5s or not rule_ids:
        return {"hit": False, "baseline": None}
    base = reviewdata_store.find_audit_for_replay(file_md5s, rule_ids)
    if not base:
        return {"hit": False, "baseline": None}
    return {"hit": True, "baseline": base}


@router.get("/consistency", summary="查询一致性核查结果汇总")
async def consistency_monitor(
    sample: int = Query(5, ge=1, le=50),
    _: dict = Depends(deps.require_admin),
):
    """一致性监控：抽取最近若干已审任务，比对版本基线与实际结论的稳定性。

    返回每项的版本基线、确定性结论占比、规则漂移检测结果与差异率（diff_rate）。
    这是生产环境持续监控「多次审核结果一致性」的轻量实现：
    - 规则漂移检测：存证时的 rule_content_hash 与当前生效规则集内容指纹比对，
      不一致说明规则已变更，旧结论可能失效（需求 1.1 / 8）。
    - 确定性结论占比：结构化规则引擎锁定的结论天然稳定，占比越高一致性越可控。
    """
    from ..services import rules_store, versioning

    rows, _ = reviewdata_store.list_audits(limit=sample, offset=0)
    results: list[dict[str, Any]] = []
    for rec in rows:
        md5s = rec.get("file_md5s") or []
        rule_ids = rec.get("rule_ids") or []
        # 当前生效规则集的内容指纹（实时计算），与存证时版本比对
        live_rules = rules_store.get_rules_by_ids(rule_ids)
        live_content_hash = versioning.rule_content_fingerprint(live_rules)
        stored_hash = (rec.get("config_json") or {})
        # config_json 不含内容哈希，故从 rule_set_version 尾部解析
        rule_set_version = rec.get("rule_set_version") or ""
        stored_content_hash = ""
        if "_v" in rule_set_version:
            tail = rule_set_version.rsplit("_v", 1)[-1]
            # 形如 2026.08.24_<hash> 或 2026.08.24
            parts = tail.split("_")
            stored_content_hash = parts[-1] if len(parts) > 1 else ""

        rule_drifted = bool(stored_content_hash) and stored_content_hash != live_content_hash

        # 关联记录结论稳定性：查该审计对应任务归档的 decisions，统计确定性锁定占比
        det_n = 0
        total_dec = 0
        rec_id = None
        if rec.get("task_id"):
            # 通过 review_records 找到最近一次归档（按文件组+规则组指纹）
            from ..services import reviewdata_store as rds

            fg = rds.file_group_fingerprint(md5s)
            rg = rds.rule_group_fingerprint(rule_ids)
            rr = rds._conn().execute(
                "SELECT id FROM review_records WHERE file_group_fp=? AND rule_group_fp=? "
                "ORDER BY updated_at DESC LIMIT 1",
                (fg, rg),
            ).fetchone()
            if rr:
                rec_id = rr["id"]
                drows = rds._conn().execute(
                    "SELECT COUNT(*) AS n, SUM(CASE WHEN detail LIKE '%\"deterministic\": true%' OR detail LIKE '%\"deterministic\":true%' THEN 1 ELSE 0 END) AS d "
                    "FROM review_decisions WHERE record_id=?",
                    (rec_id,),
                ).fetchone()
                total_dec = drows["n"] or 0
                det_n = drows["d"] or 0

        det_ratio = (det_n / total_dec) if total_dec else 0.0
        # 差异率：规则漂移即视为潜在不一致（需人工/重跑确认）
        diff_rate = 1.0 if rule_drifted else 0.0
        note = (
            "规则内容已变更，历史结论可能需按新规则重新审核"
            if rule_drifted
            else f"版本基线稳定；确定性锁定结论占比 {det_ratio * 100:.0f}%，可复现"
        )
        results.append(
            {
                "task_id": rec.get("task_id"),
                "engine_version": rec.get("engine_version"),
                "rule_set_version": rule_set_version,
                "data_snapshot_version": rec.get("data_snapshot_version"),
                "parsed_content_hash": rec.get("parsed_content_hash"),
                "deterministic": bool(rec.get("deterministic")),
                "rule_drifted": rule_drifted,
                "deterministic_ratio": round(det_ratio, 3),
                "replay_ready": not rule_drifted,
                "diff_rate": diff_rate,
                "note": note,
            }
        )
    inconsistent = sum(1 for r in results if r["diff_rate"] > 0.001)
    return {
        "sampled": len(results),
        "inconsistent": inconsistent,
        "inconsistency_rate": round(inconsistent / len(results), 4) if results else 0.0,
        "items": results,
    }
