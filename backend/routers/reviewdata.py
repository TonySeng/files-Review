# -*- coding: utf-8 -*-
"""审核数据管理接口：历史审核关联记录查询、维护与一致性校验。

- GET  /api/reviewdata/records          关联记录列表（支持 关键字/判定类型 筛选）
- GET  /api/reviewdata/records/{id}      单条记录详情（含全部历史判定、关联文件）
- PATCH /api/reviewdata/records/{id}/decision  维护单条历史判定（纠正采纳/不采纳）
- POST /api/reviewdata/verify            一致性校验（重新比对规则是否仍适用）
- GET  /api/reviewdata/stats             概览统计
- POST /api/reviewdata/lookup            回流预检：给定文件 MD5 + 规则 id 是否命中历史
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..routers import deps
from ..services import reviewdata_store, task_store

router = APIRouter(prefix="/api/reviewdata", tags=["reviewdata"])


async def _owned_task_ids(caller: dict[str, Any]) -> "set[str] | None":
    """普通用户返回其名下任务 id 集合；管理员（scope=None）返回 None 表示查看全部。

    关联记录按 task_id 归属隔离，普通用户仅能查看自己创建的审核历史关联记录，
    满足「查看自己创建的审核历史任务」的需求，并避免跨用户数据泄露。
    """
    user_id = deps.scope_user_id(caller)
    if user_id is None:
        return None
    tasks = await task_store.get_store().list(user_id=user_id)
    return {t["task_id"] for t in tasks}


class DecisionUpdate(BaseModel):
    judgment: str | None = Field(None, pattern="^(adopt|reject)$")
    reject_reason: str | None = None


class VerifyRequest(BaseModel):
    record_id: str


class LookupRequest(BaseModel):
    file_md5s: list[str]
    rule_ids: list[str]


@router.get("/stats", summary="历史审核数据统计汇总")
async def stats(caller: dict = Depends(deps.get_caller)):
    task_ids = await _owned_task_ids(caller)
    return reviewdata_store.stats(task_ids)


@router.get("/records", summary="分页查询历史审核记录")
async def list_records(
    q: str | None = None,
    judgment: str | None = Query(None, pattern="^(adopt|reject|mixed)$"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    caller: dict = Depends(deps.get_caller),
):
    task_ids = await _owned_task_ids(caller)
    rows, total = reviewdata_store.list_records(
        q=q, judgment=judgment, limit=limit, offset=offset, task_ids=task_ids
    )
    return {"records": rows, "total": total, "limit": limit, "offset": offset}


@router.get("/records/{record_id}", summary="查询历史审核记录详情")
async def get_record(record_id: str, caller: dict = Depends(deps.get_caller)):
    task_ids = await _owned_task_ids(caller)
    rec = reviewdata_store.get_record(record_id, task_ids=task_ids)
    if not rec:
        raise HTTPException(status_code=404, detail="关联记录不存在")
    return rec


@router.patch("/records/{record_id}/decision", summary="更新历史记录的人工决策")
async def update_decision(
    record_id: str,
    decision_id: str,
    payload: DecisionUpdate,
    caller: dict = Depends(deps.get_caller),
):
    # record_id 用于校验记录存在（并按归属隔离：普通用户仅能操作自己的记录）
    task_ids = await _owned_task_ids(caller)
    rec = reviewdata_store.get_record(record_id, task_ids=task_ids)
    if not rec:
        raise HTTPException(status_code=404, detail="关联记录不存在")
    try:
        updated = reviewdata_store.update_decision(
            decision_id,
            judgment=payload.judgment,
            reject_reason=payload.reject_reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not updated:
        raise HTTPException(status_code=404, detail="历史判定不存在")
    return {"decision": updated, "ok": True}


@router.post("/verify", summary="校验历史记录完整性（防篡改校验）")
async def verify(payload: VerifyRequest, caller: dict = Depends(deps.get_caller)):
    # 校验记录归属：普通用户仅能校验自己名下的关联记录
    task_ids = await _owned_task_ids(caller)
    rec = reviewdata_store.get_record(payload.record_id, task_ids=task_ids)
    if not rec:
        raise HTTPException(status_code=404, detail="关联记录不存在")
    result = reviewdata_store.verify_consistency(payload.record_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("reason", "记录不存在"))
    return result


@router.post("/lookup", summary="按要素查询历史审核数据")
async def lookup(payload: LookupRequest, _: dict = Depends(deps.get_caller)):
    # 回流预检为内部检索能力，不按用户隔离（审核引擎在新建任务时需要全局历史结论做回流）。
    rec = reviewdata_store.lookup_history(payload.file_md5s, payload.rule_ids)
    if not rec:
        return {"hit": False, "record": None}
    return {"hit": True, "record": rec}
