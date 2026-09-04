"""审核规则组接口：规则组的增删改查与规则展开。

所有接口需鉴权：管理员（X-Session-Token）可见/操作全部；普通用户（X-API-Key）
仅可见/操作自己拥有或共享的规则组。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..routers import deps
from ..services import rule_groups as rg_store

router = APIRouter(prefix="/api/rule-groups", tags=["rule-groups"])


def _save_owner(caller: dict) -> str | None:
    """自定义资源归属创建者本人：管理员与普通用户均写入真实 user_id。
    仅代码内置预置（builtin）才系统共享；自定义数据对其他用户不可见。"""
    return caller.get("user_id")


@router.get("", summary="查询审核规则组列表")
async def list_rule_groups(caller: dict = Depends(deps.get_caller)):
    return {"rule_groups": rg_store.list_groups(deps.scope_user_id(caller))}


@router.get("/{group_id}", summary="查询单个审核规则组详情")
async def get_rule_group(group_id: str, caller: dict = Depends(deps.get_caller)):
    g = rg_store.get(group_id, deps.scope_user_id(caller))
    if not g:
        raise HTTPException(status_code=404, detail="规则组不存在")
    return g


@router.post("", summary="新建/更新审核规则组")
async def save_rule_group(payload: dict, caller: dict = Depends(deps.get_caller)):
    """新建或更新规则组（预置组另存为新组）。"""
    try:
        return rg_store.save_group(payload, user_id=_save_owner(caller))
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/{group_id}", summary="删除指定审核规则组")
async def delete_rule_group(group_id: str, caller: dict = Depends(deps.get_caller)):
    owner = deps.scope_user_id(caller)  # 管理员传 None（可删任意），用户仅自己的
    if not rg_store.delete_group(group_id, owner):
        raise HTTPException(status_code=400, detail="规则组不存在或为预置组/非所属，不可删除")
    return {"deleted": True}
