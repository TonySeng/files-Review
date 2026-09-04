# -*- coding: utf-8 -*-
"""调用审计查询接口：接口调用历史 + AI(LLM/知识库)调用审计，数据持久化于挂载卷 SQLite。

- GET /api/audit/calls        业务接口调用历史（按路径/时间/是否错误筛选）
- GET /api/audit/ai-calls     AI 调用审计（LLM / 知识库），含耗时、状态码、prompt 体量、重试、归属 task/rule
- GET /api/audit/call-stats   汇总统计（总量/错误数/慢调用/平均耗时）
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Query

from ..routers import deps
from ..services import call_audit

router = APIRouter(prefix="/api/audit", tags=["audit-calls"])


@router.get("/calls", summary="查询外部调用审计日志（API Key 维度）")
async def list_api_calls(
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    path: str | None = None,
    since_hours: float = Query(24.0, ge=0, description="仅看最近 N 小时的调用"),
    only_error: bool = Query(False, description="只看 5xx/错误调用"),
    user_id: str | None = Query(None, description="按调用方用户筛选"),
    _: dict = Depends(deps.require_admin),
):
    since = (time.time() - since_hours * 3600.0) if since_hours > 0 else None
    rows = call_audit.get_api_calls(
        limit=limit, offset=offset, path_filter=path, since=since,
        only_error=only_error, user_id=user_id,
    )
    stats = call_audit.api_call_stats(since=since, user_id=user_id)
    return {"items": rows, "stats": stats, "limit": limit, "offset": offset}


@router.get("/ai-calls", summary="查询大模型调用审计日志（模型/耗时/token）")
async def list_ai_calls(
    kind: str | None = Query(None, description="llm | kb"),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    since_hours: float = Query(24.0, ge=0),
    only_error: bool = Query(False),
):
    since = (time.time() - since_hours * 3600.0) if since_hours > 0 else None
    rows = call_audit.get_ai_calls(
        kind=kind, limit=limit, offset=offset, since=since, only_error=only_error
    )
    stats = call_audit.ai_call_stats(since=since)
    return {"items": rows, "stats": stats, "limit": limit, "offset": offset}


@router.get("/call-stats", summary="查询调用统计汇总（按天/按接口聚合）")
async def call_stats(since_hours: float = Query(24.0, ge=0)):
    since = (time.time() - since_hours * 3600.0) if since_hours > 0 else None
    return {
        "api": call_audit.api_call_stats(since=since),
        "ai": call_audit.ai_call_stats(since=since),
        "window_hours": since_hours,
    }
