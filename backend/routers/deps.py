# -*- coding: utf-8 -*-
"""鉴权依赖：从请求头解析调用方身份，注入 request.state 供审计与作用域过滤使用。

解析顺序：
- X-Session-Token：管理员或普通用户会话令牌（账号密码登录签发）。role 由会话决定。
- X-API-Key：普通用户持有的某个 API Key。role=user。

API Key 模式额外做 scope 网关校验：按请求路径前缀要求对应权限范围，Key 的
scopes 不含该范围且非 "*" 时返回 403。管理员会话令牌免 scope 校验。

任一有效即视为已认证；均无效则 401。审计中间件在 finally 阶段读取
request.state.user_id / user_role / api_key_prefix 写入调用日志。
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from ..services import users as users_store
from ..services import sessions as sessions_store


def _set_state(
    request: Request, *, user_id: str, role: str, username: str, api_key_prefix: str | None
) -> dict[str, Any]:
    request.state.user_id = user_id
    request.state.user_role = role
    request.state.user_name = username
    request.state.api_key_prefix = api_key_prefix
    return {
        "user_id": user_id,
        "role": role,
        "username": username,
        "api_key_prefix": api_key_prefix,
    }


# scope 网关：路径前缀 -> 所需权限范围。命中且 Key 不含该范围（且非 "*"）则 403。
_SCOPE_MAP = [
    ("/api/review", "review"),
    ("/api/files", "review"),
    ("/api/reviewdata", "review"),
    ("/api/feedback", "feedback"),
    ("/api/knowledge", "kb"),
    ("/api/settings/knowledge", "kb"),
    ("/api/audit", "audit"),
    ("/api/rulesets", "rules"),
    ("/api/rule-groups", "rules"),
    ("/api/legal-rules", "rules"),  # 法规文件 → 临时审核规则集
    ("/api/file-types", "rules"),
    ("/api/settings", "settings"),
]


def _required_scope(path: str) -> str | None:
    for prefix, scope in _SCOPE_MAP:
        if path.startswith(prefix):
            return scope
    return None


def get_caller(request: Request) -> dict[str, Any]:
    """解析调用方身份（会话令牌 或 用户 API Key），失败则 401。

    直接读取 request.headers，因此既可作为 FastAPI 依赖（Depends）使用，
    也可被 require_admin 直接调用（request 由 FastAPI 注入），
    两种调用方式行为一致。返回的 dict 同时写入 request.state，
    供后续审计与作用域过滤使用。
    """
    x_session_token = request.headers.get("X-Session-Token")
    x_api_key = request.headers.get("X-API-Key")

    # 1) 会话令牌（管理员或普通用户，按会话角色决定）
    if x_session_token:
        s = sessions_store.get_session(x_session_token)
        if s:
            return _set_state(
                request,
                user_id=s["user_id"],
                role=s.get("role", "admin"),
                username=s.get("username") or "admin",
                api_key_prefix=None,
            )
        raise HTTPException(status_code=401, detail="会话已失效，请重新登录")

    # 2) 普通用户 API Key（仅 active 用户 + active key 有效）
    if x_api_key:
        u, k = users_store.get_user_and_key(x_api_key)
        if u and k:
            users_store.touch_key(u["id"], k["key_id"])
            # scope 网关校验
            req_scope = _required_scope(request.url.path)
            key_scopes = k.get("scopes") or ["*"]
            if req_scope and "*" not in key_scopes and req_scope not in key_scopes:
                raise HTTPException(
                    status_code=403,
                    detail=f"API Key 权限不足：调用需要 {req_scope} 权限范围",
                )
            return _set_state(
                request,
                user_id=u["id"],
                role=u.get("role", "user"),
                username=u.get("username") or u["id"],
                api_key_prefix=k.get("prefix"),
            )
        raise HTTPException(status_code=401, detail="无效的 api-key")

    raise HTTPException(status_code=401, detail="缺少鉴权头（X-Session-Token 或 X-API-Key）")


def require_admin(request: Request) -> dict[str, Any]:
    """要求管理员身份；非管理员返回 403。"""
    caller = get_caller(request)
    if caller["role"] != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return caller


def scope_user_id(caller: dict[str, Any]) -> str | None:
    """作用域解析：管理员看全部（返回 None）；普通用户只看自己的数据（返回 user_id）。"""
    return None if caller["role"] == "admin" else caller["user_id"]
