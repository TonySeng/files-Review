"""反馈与训练数据 / 错别字校验集接口。

- POST /api/feedback            提交采纳/不采纳反馈（不采纳强制原因），落训练数据集；
                                 不采纳的错别字额外写过滤规则（自动跳过该校验项）
- GET  /api/feedback            训练数据集检索（judgment/rule_id/is_typo/关键字）
- GET  /api/feedback/stats      统计概览
- GET  /api/feedback/export      导出训练数据为 JSONL（模型微调）
- GET  /api/feedback/validation  错别字校验集列表
- GET  /api/feedback/validation/filters   活跃过滤规则
- DELETE /api/feedback/validation/filters/{id}  停用过滤规则（恢复该校验项）
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from ..models.schemas import (
    FeedbackSubmit,
    FeedbackStats,
    FilterDeactivateRequest,
    ValidationStatusUpdate,
)
from ..routers import deps
from ..services import feedback_store

router = APIRouter(prefix="/api/feedback", tags=["feedback"])


def _scope_user_id(caller: dict, query_user_id: str | None) -> str | None:
    """作用域：管理员可显式按 user_id 过滤（为空看全部），普通用户强制只看自己的。"""
    if caller["role"] == "admin":
        return query_user_id
    return caller["user_id"]


@router.post("", summary="提交审核结论反馈（采纳/修正/驳回）")
async def submit_feedback(
    payload: FeedbackSubmit, caller: dict = Depends(deps.get_caller)
):
    judgment = payload.judgment
    if judgment == "reject" and not (payload.reject_reason or "").strip():
        raise HTTPException(status_code=400, detail="不采纳时必须填写不采纳原因")
    try:
        rec = feedback_store.add_feedback(
            finding=payload.finding,
            judgment=judgment,
            reject_reason=payload.reject_reason,
            task_id=payload.task_id,
            user_id=caller["user_id"],
        )
    except Exception as exc:  # noqa: BLE001 - 落库异常不应导致前端崩溃
        raise HTTPException(status_code=500, detail=f"反馈保存失败: {exc}") from exc
    return {"record": rec, "ok": True}


@router.get("", summary="分页查询反馈记录")
async def list_feedback(
    caller: dict = Depends(deps.get_caller),
    judgment: str | None = Query(None, pattern="^(adopt|reject)$"),
    rule_id: str | None = None,
    is_typo: bool | None = None,
    q: str | None = None,
    user_id: str | None = Query(None, description="管理员按用户过滤；普通用户忽略此参数"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    scoped = _scope_user_id(caller, user_id)
    rows, total = feedback_store.list_feedback(
        judgment=judgment,
        rule_id=rule_id,
        is_typo=is_typo,
        q=q,
        user_id=scoped,
        limit=limit,
        offset=offset,
    )
    return {"records": rows, "total": total, "limit": limit, "offset": offset}


@router.get("/stats", summary="反馈统计汇总（采纳率/修正分布）")
async def feedback_stats(
    caller: dict = Depends(deps.get_caller),
    user_id: str | None = Query(None, description="管理员按用户过滤；普通用户忽略此参数"),
) -> FeedbackStats:
    scoped = _scope_user_id(caller, user_id)
    return FeedbackStats(**feedback_store.stats(user_id=scoped))


@router.get("/export", summary="导出反馈数据为 JSON 文件")
async def export_feedback(
    caller: dict = Depends(deps.get_caller),
    judgment: str | None = Query(None, pattern="^(adopt|reject)$"),
    rule_id: str | None = None,
    is_typo: bool | None = None,
    q: str | None = None,
    user_id: str | None = Query(None, description="管理员按用户过滤；普通用户忽略此参数"),
):
    scoped = _scope_user_id(caller, user_id)
    rows = feedback_store.export_jsonl(
        judgment=judgment,
        rule_id=rule_id,
        is_typo=is_typo,
        q=q,
        user_id=scoped,
    )
    import json

    body = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
    return Response(
        content=body.encode("utf-8"),
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": "attachment; filename=feedback_training_data.jsonl"
        },
    )


@router.get("/validation", summary="分页查询校验集（沉淀后的判定样本）")
async def list_validation(
    status: str | None = None,
    q: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    caller: dict = Depends(deps.get_caller),
):
    """错别字校验集列表。管理员看全部；普通用户仅看自己名下沉淀的校验项（归属隔离）。"""
    scoped = deps.scope_user_id(caller)
    rows, total = feedback_store.list_validation_items(
        status=status, q=q, user_id=scoped, limit=limit, offset=offset
    )
    return {"items": rows, "total": total, "limit": limit, "offset": offset}


@router.patch("/validation/{item_id}", summary="更新校验集条目状态（接受/拒绝/过滤）")
async def update_validation(
    item_id: str,
    payload: ValidationStatusUpdate,
    caller: dict = Depends(deps.get_caller),
):
    """在错别字校验集列表内直接更新某条校验项的判定状态（采纳/驳回/待判定）。

    普通用户只能改自己名下的校验项；管理员可改全部。驳回会写入去噪过滤规则，
    采纳会停用该错字的过滤规则。
    """
    if payload.status not in ("accepted", "rejected", "pending"):
        raise HTTPException(status_code=400, detail="status 必须为 accepted/rejected/pending")
    scope = None if caller["role"] == "admin" else caller["user_id"]
    try:
        row = feedback_store.update_validation_item_status(
            item_id,
            payload.status,
            reject_reason=payload.reject_reason,
            user_id=caller["user_id"],
            scope_user_id=scope,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not row:
        raise HTTPException(status_code=404, detail="校验项不存在或无权限")
    return row


@router.get("/validation/filters", summary="查询校验集过滤规则列表")
async def list_filters(caller: dict = Depends(deps.get_caller)):
    """过滤规则列表（含已停用）。审核去噪匹配仅使用 active=1 的规则。"""
    return {"filters": feedback_store.list_filters(active_only=False)}


@router.delete("/validation/filters/{filter_id}", summary="删除指定校验集过滤规则")
async def delete_filter(
    filter_id: str,
    payload: FilterDeactivateRequest,
    caller: dict = Depends(deps.get_caller),
):
    """停用过滤规则：记录保留并标记「已停用」，停用须填写停用补充说明。"""
    try:
        row = feedback_store.deactivate_filter(filter_id, payload.deactivate_reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not row:
        raise HTTPException(status_code=404, detail="过滤规则不存在")
    return {"filter": row, "ok": True}
