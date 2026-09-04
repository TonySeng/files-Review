"""文件类型配置接口：定义可审核的文件类型并关联规则组。

所有接口需鉴权：管理员（X-Session-Token）可见/操作全部；普通用户（X-API-Key）
仅可见/操作自己拥有或共享的文件类型。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..routers import deps
from ..services import file_types as ft_store

router = APIRouter(prefix="/api/file-types", tags=["file-types"])


def _save_owner(caller: dict) -> str | None:
    """自定义资源归属创建者本人：管理员与普通用户均写入真实 user_id。
    仅代码内置预置（builtin）才系统共享；自定义数据对其他用户不可见。"""
    return caller.get("user_id")


@router.get("", summary="查询文件类型配置列表")
async def list_file_types(caller: dict = Depends(deps.get_caller)):
    return {"file_types": ft_store.list_types(deps.scope_user_id(caller))}


@router.get("/{type_id}", summary="查询单个文件类型配置详情")
async def get_file_type(type_id: str, caller: dict = Depends(deps.get_caller)):
    ft = ft_store.get(type_id, deps.scope_user_id(caller))
    if not ft:
        raise HTTPException(status_code=404, detail="文件类型不存在")
    return ft


@router.post("", summary="新建/更新文件类型配置")
async def save_file_type(payload: dict, caller: dict = Depends(deps.get_caller)):
    """新建或更新文件类型（预置类型另存为新类型）。"""
    try:
        return ft_store.save_type(payload, user_id=_save_owner(caller))
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/{type_id}", summary="删除指定文件类型配置")
async def delete_file_type(type_id: str, caller: dict = Depends(deps.get_caller)):
    owner = deps.scope_user_id(caller)
    if not ft_store.delete_type(type_id, owner):
        raise HTTPException(status_code=400, detail="文件类型不存在或为预置类型/非所属，不可删除")
    return {"deleted": True}
