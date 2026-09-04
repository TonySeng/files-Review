# -*- coding: utf-8 -*-
"""管理员聚合视图：跨用户的审核任务、反馈与训练数据检索（均支持按用户过滤）。

所有接口需管理员会话（X-Session-Token）。普通用户仅能看到自己的数据，
此处聚合接口用于「管理员查看全部用户的请求记录 / 反馈 / 训练数据并区分归属」。

说明：用户管理接口位于 auth.admin（/api/admin/users 等）；审计调用日志
（/api/audit/calls）已独立在 call_audit 路由器并限定管理员。本模块聚焦
任务与反馈两块业务数据的跨用户聚合。
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from ..routers import deps
from ..services import task_store, feedback_store

router = APIRouter(prefix="/api/admin", tags=["admin-aggregate"])


@router.get("/tasks", summary="查看全部用户的审核任务（管理员）")
async def list_all_tasks(
    user_id: str | None = Query(None, description="按用户过滤；不传返回全部用户"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _: dict = Depends(deps.require_admin),
):
    """管理员查看全部审核任务，可按 user_id 区分归属。"""
    tasks = await task_store.get_store().list(user_id=user_id)
    page = tasks[offset : offset + limit]
    return {
        "tasks": [task_store.to_summary(t) for t in page],
        "total": len(tasks),
        "limit": limit,
        "offset": offset,
    }


@router.get("/feedback", summary="查看全部用户的反馈数据（管理员）")
async def list_all_feedback(
    user_id: str | None = Query(None, description="按用户过滤；不传返回全部用户"),
    judgment: str | None = Query(None, pattern="^(adopt|reject)$"),
    rule_id: str | None = None,
    is_typo: bool | None = None,
    q: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _: dict = Depends(deps.require_admin),
):
    """管理员查看全部反馈与训练数据，可按 user_id 区分归属。"""
    rows, total = feedback_store.list_feedback(
        judgment=judgment,
        rule_id=rule_id,
        is_typo=is_typo,
        q=q,
        limit=limit,
        offset=offset,
        user_id=user_id,
    )
    return {"records": rows, "total": total, "limit": limit, "offset": offset}


@router.get("/feedback/export", summary="导出全部反馈数据为 JSON 文件（管理员）")
async def export_all_feedback(
    user_id: str | None = Query(None, description="按用户过滤；不传导出全部用户"),
    judgment: str | None = Query(None, pattern="^(adopt|reject)$"),
    rule_id: str | None = None,
    is_typo: bool | None = None,
    q: str | None = None,
    _: dict = Depends(deps.require_admin),
):
    """导出全部（或指定用户）反馈训练数据为 JSONL，供模型微调。"""
    rows = feedback_store.export_jsonl(
        judgment=judgment,
        rule_id=rule_id,
        is_typo=is_typo,
        q=q,
        user_id=user_id,
    )
    body = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
    return Response(
        content=body.encode("utf-8"),
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": "attachment; filename=feedback_training_data.jsonl"
        },
    )
