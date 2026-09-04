# -*- coding: utf-8 -*-
"""鉴权与管理员用户管理接口。

- POST /api/auth/login     管理员账号密码登录，返回会话令牌（X-Session-Token）
- POST /api/auth/logout    注销当前会话
- GET  /api/auth/me        获取当前调用方身份（管理员或普通用户）
- GET  /api/admin/users            管理员：用户列表
- GET  /api/admin/users/{id}       管理员：获取单个用户详情
- POST /api/admin/users            管理员：创建用户（普通用户自动下发 api-key）
- PATCH /api/admin/users/{id}      管理员：修改资料 / 启用停用 / 重置密码
- DELETE /api/admin/users/{id}     管理员：删除用户
- POST /api/admin/users/{id}/rotate-key  管理员：轮换该用户 api-key
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from ..models.schemas import (
    LoginRequest,
    LoginResponse,
    UserCreate,
    UserOut,
    UserUpdate,
    ApiKeyRotateResponse,
    RegisterRequest,
    ApiKeyCreate,
    ApiKeyUpdate,
    ApiKeyOut,
    ApiKeyCreatedResponse,
)
from ..routers import deps
from ..services import users as users_store
from ..services import sessions as sessions_store

router = APIRouter(prefix="/api/auth", tags=["auth"])

# 在进程启动时播种默认管理员（如尚不存在）
users_store._seed_default_admin()


@router.post("/register", summary="用户自助注册（创建待审核账号）", response_model=UserOut)
async def register(payload: RegisterRequest):
    """普通用户自助注册：创建 pending 用户，待管理员审批后方可登录。"""
    try:
        u = users_store.register_user(
            payload.username, payload.password, payload.display_name
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return users_store.to_public(u)


@router.post("/login", summary="账号密码登录，返回会话令牌（X-Session-Token）", response_model=LoginResponse)
async def login(payload: LoginRequest):
    u = users_store.get_by_username(payload.username)
    if not u:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    status = u.get("status")
    if status == "pending":
        raise HTTPException(status_code=403, detail="账号待审核，请联系管理员审批")
    if status == "rejected":
        raise HTTPException(status_code=403, detail="注册申请未通过审核")
    if status != "active":
        raise HTTPException(status_code=403, detail="账号已停用")
    if not users_store.authenticate(payload.username, payload.password):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = sessions_store.create_session(
        user_id=u["id"], username=u["username"], role=u["role"]
    )
    users_store.record_login(u["id"])
    return LoginResponse(token=token, user=users_store.to_public(u, include_api_key=False))


@router.post("/logout", summary="退出登录并使会话令牌失效")
async def logout(
    x_session_token: str | None = Header(default=None, alias="X-Session-Token"),
):
    """注销当前会话（仅删除服务端会话令牌）。"""
    if x_session_token:
        sessions_store.delete_session(x_session_token)
    return {"ok": True}


@router.get("/me", summary="获取当前登录用户信息", response_model=UserOut)
async def me(caller: dict[str, Any] = Depends(deps.get_caller)):
    u = users_store.get_by_id(caller["user_id"])
    if not u:
        raise HTTPException(status_code=404, detail="用户不存在")
    # 完整密钥不在此回显（安全加固）；密钥由专门的密钥管理接口返回。
    return users_store.to_public(u, include_api_key=False)


# --------------------------------------------------------------------------- #
# 管理员用户管理
# --------------------------------------------------------------------------- #
admin = APIRouter(prefix="/api/admin", tags=["admin-users"])


@admin.get("/users", response_model=list[UserOut])
async def list_users(
    status: str | None = Query(None, description="按状态过滤：pending/active/disabled/rejected"),
    _: dict[str, Any] = Depends(deps.require_admin),
):
    return [users_store.to_public(u) for u in users_store.list_users(status=status)]


@admin.get("/users/{user_id}", response_model=UserOut)
async def get_user(user_id: str, _: dict[str, Any] = Depends(deps.require_admin)):
    """管理员：获取单个用户详情（含 status、密钥脱敏列表等）。"""
    u = users_store.get_by_id(user_id)
    if not u:
        raise HTTPException(status_code=404, detail="用户不存在")
    return users_store.to_public(u)


@admin.post("/users", response_model=UserOut)
async def create_user(
    payload: UserCreate, _: dict[str, Any] = Depends(deps.require_admin)
):
    try:
        u = users_store.create_user(
            username=payload.username,
            display_name=payload.display_name,
            role=payload.role,
            password=payload.password,
            created_by="admin",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 创建时一并返回完整 api-key，便于管理员下发给用户
    return users_store.to_public(u, include_api_key=True)


@admin.patch("/users/{user_id}", response_model=UserOut)
async def update_user(
    user_id: str,
    payload: UserUpdate,
    _: dict[str, Any] = Depends(deps.require_admin),
):
    u = users_store.get_by_id(user_id)
    if not u:
        raise HTTPException(status_code=404, detail="用户不存在")
    try:
        if payload.display_name is not None:
            users_store.set_display_name(user_id, payload.display_name)
        if payload.is_active is not None:
            users_store.set_active(user_id, payload.is_active)
        if payload.password is not None:
            users_store.set_password(user_id, payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return users_store.to_public(users_store.get_by_id(user_id))


@admin.delete("/users/{user_id}")
async def delete_user(user_id: str, _: dict[str, Any] = Depends(deps.require_admin)):
    u = users_store.get_by_id(user_id)
    if not u:
        raise HTTPException(status_code=404, detail="用户不存在")
    if u.get("role") == "admin":
        # 至少保留一个管理员，避免锁死
        admins = [x for x in users_store.list_users() if x.get("role") == "admin"]
        if len(admins) <= 1:
            raise HTTPException(status_code=400, detail="至少需保留一个管理员账号")
    import threading

    with users_store._lock:
        users = users_store._load()
        users.pop(user_id, None)
        users_store._save(users)
    sessions_store.delete_session  # 会话在下次校验时自然失效
    return {"deleted": True}


@admin.post("/users/{user_id}/rotate-key", response_model=ApiKeyRotateResponse)
async def rotate_key(user_id: str, _: dict[str, Any] = Depends(deps.require_admin)):
    u = users_store.get_by_id(user_id)
    if not u:
        raise HTTPException(status_code=404, detail="用户不存在")
    new_key, prefix = users_store.rotate_api_key(user_id)
    return ApiKeyRotateResponse(user_id=user_id, api_key=new_key, api_key_prefix=prefix)


@admin.post("/users/{user_id}/approve", response_model=UserOut)
async def approve_user(
    user_id: str, caller: dict[str, Any] = Depends(deps.require_admin)
):
    """审批通过注册申请：用户状态置为 active，可正常登录并自助生成 API Key。"""
    try:
        u = users_store.approve_user(user_id, admin_id=caller.get("username"))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return users_store.to_public(u)


@admin.post("/users/{user_id}/reject", response_model=UserOut)
async def reject_user(
    user_id: str,
    payload: dict | None = None,
    _: dict[str, Any] = Depends(deps.require_admin),
):
    """驳回注册申请（reason 可选）。"""
    reason = (payload or {}).get("reason") if isinstance(payload, dict) else None
    try:
        u = users_store.reject_user(user_id, reason=reason)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return users_store.to_public(u)


# --------------------------------------------------------------------------- #
# 管理员跨用户密钥管理
# --------------------------------------------------------------------------- #
@admin.get("/users/{user_id}/keys", response_model=list[ApiKeyOut])
async def admin_list_keys(
    user_id: str, _: dict[str, Any] = Depends(deps.require_admin)
):
    u = users_store.get_by_id(user_id)
    if not u:
        raise HTTPException(status_code=404, detail="用户不存在")
    return users_store.list_api_keys(user_id)


@admin.post("/users/{user_id}/keys", response_model=ApiKeyCreatedResponse)
async def admin_create_key(
    user_id: str,
    payload: ApiKeyCreate,
    caller: dict[str, Any] = Depends(deps.require_admin),
):
    u = users_store.get_by_id(user_id)
    if not u:
        raise HTTPException(status_code=404, detail="用户不存在")
    try:
        key, rec = users_store.create_api_key(
            user_id, payload.name, payload.scopes, created_by=caller.get("username")
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ApiKeyCreatedResponse(
        key_id=rec["key_id"],
        api_key=key,
        prefix=rec["prefix"],
        name=rec["name"],
        status=rec["status"],
        scopes=rec["scopes"],
        created_at=rec["created_at"],
    )


@admin.patch("/users/{user_id}/keys/{key_id}", response_model=ApiKeyOut)
async def admin_update_key(
    user_id: str,
    key_id: str,
    payload: ApiKeyUpdate,
    _: dict[str, Any] = Depends(deps.require_admin),
):
    u = users_store.get_by_id(user_id)
    if not u:
        raise HTTPException(status_code=404, detail="用户不存在")
    try:
        return users_store.update_api_key(
            user_id, key_id, payload.name, payload.status, payload.scopes
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@admin.delete("/users/{user_id}/keys/{key_id}")
async def admin_revoke_key(
    user_id: str,
    key_id: str,
    _: dict[str, Any] = Depends(deps.require_admin),
):
    u = users_store.get_by_id(user_id)
    if not u:
        raise HTTPException(status_code=404, detail="用户不存在")
    try:
        users_store.revoke_api_key(user_id, key_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


# --------------------------------------------------------------------------- #
# 普通用户自助密钥管理（操作调用方自身；管理员会话亦可管理自己）
# --------------------------------------------------------------------------- #
user_keys = APIRouter(prefix="/api/users", tags=["user-keys"])


@user_keys.get("/keys", response_model=list[ApiKeyOut])
async def list_my_keys(caller: dict[str, Any] = Depends(deps.get_caller)):
    return users_store.list_api_keys(caller["user_id"])


@user_keys.post("/keys", response_model=ApiKeyCreatedResponse)
async def create_my_key(
    payload: ApiKeyCreate, caller: dict[str, Any] = Depends(deps.get_caller)
):
    try:
        key, rec = users_store.create_api_key(
            caller["user_id"], payload.name, payload.scopes, created_by=caller.get("username")
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ApiKeyCreatedResponse(
        key_id=rec["key_id"],
        api_key=key,
        prefix=rec["prefix"],
        name=rec["name"],
        status=rec["status"],
        scopes=rec["scopes"],
        created_at=rec["created_at"],
    )


@user_keys.patch("/keys/{key_id}", response_model=ApiKeyOut)
async def update_my_key(
    key_id: str,
    payload: ApiKeyUpdate,
    caller: dict[str, Any] = Depends(deps.get_caller),
):
    try:
        return users_store.update_api_key(
            caller["user_id"], key_id, payload.name, payload.status, payload.scopes
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@user_keys.delete("/keys/{key_id}")
async def revoke_my_key(key_id: str, caller: dict[str, Any] = Depends(deps.get_caller)):
    try:
        users_store.revoke_api_key(caller["user_id"], key_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}
