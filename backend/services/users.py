# -*- coding: utf-8 -*-
"""用户与鉴权存储层。

两类身份：
- 管理员（role=admin）：账号密码登录，服务端签发会话令牌（X-Session-Token）。
- 普通用户（role=user）：自助注册 → 管理员审批 → 账号密码登录；登录后可自助生成
  多个 API Key（X-API-Key 调用接口），每个 Key 可设名称 / 状态 / 权限范围(scope)。

数据持久化于 backend/data/users.json（与 app.db 同卷，跨容器重建保留）。
用户/规则/规则集等均以此处 user_id 作为归属（owner_id）。

密码使用 pbkdf2_hmac 加盐哈希，避免引入额外依赖。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import secrets
import threading
from pathlib import Path
from typing import Any

from .. import config
from .. import storage

DATA_DIR = config.DATA_DIR
DATA_DIR.mkdir(parents=True, exist_ok=True)
# users 集合经统一存储层持久化（data/storage_config.json 可切换后端）
_lock = threading.Lock()

# 默认管理员播种：优先读环境变量；否则使用内置默认值（仅首次，无用户时）。
DEFAULT_ADMIN_USER = os.getenv("BCR_ADMIN_USER", "admin")
DEFAULT_ADMIN_PASS = os.getenv("BCR_ADMIN_PASSWORD", "admin123")

PBKDF2_ITERS = 120_000

# 用户状态机：pending(待审核) / active(已激活) / disabled(已停用) / rejected(已驳回)
ACTIVE_STATUSES = ("active",)

# API Key 权限范围候选（供 UI 展示与 scope 网关校验）
API_SCOPES = ["review", "kb", "feedback", "audit", "rules", "settings"]


# --------------------------------------------------------------------------- #
# 密码哈希
# --------------------------------------------------------------------------- #
def _hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERS)
    return "pbkdf2$" + salt.hex() + "$" + dk.hex()


def _verify_password(password: str, stored: str) -> bool:
    try:
        kind, salt_hex, hash_hex = stored.split("$", 2)
    except ValueError:
        return False
    if kind != "pbkdf2":
        return False
    salt = bytes.fromhex(salt_hex)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERS)
    return secrets.compare_digest(dk.hex(), hash_hex)


def _gen_api_key() -> tuple[str, str]:
    """返回 (api_key, prefix)。prefix 用于审计展示，不泄露完整 key。"""
    key = "bk_" + secrets.token_urlsafe(28)
    return key, key[:10]


def _iso() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------- #
# 持久化
# --------------------------------------------------------------------------- #
def _load() -> dict[str, dict[str, Any]]:
    data = storage.read_collection("users")
    return data if isinstance(data, dict) else {}


def _save(users: dict[str, dict[str, Any]]) -> None:
    try:
        storage.write_collection("users", users)
    except Exception:  # noqa: BLE001
        pass


def _migrate(u: dict[str, Any]) -> dict[str, Any]:
    """向后兼容：补全 status / api_keys 字段。存量单 api_key 迁移入数组。"""
    status = u.get("status")
    if status not in ("pending", "active", "disabled", "rejected"):
        status = "active" if u.get("is_active", True) else "disabled"
    u["status"] = status
    u["is_active"] = bool(status == "active")

    keys = u.get("api_keys")
    if not isinstance(keys, list) or not keys:
        legacy = u.get("api_key")
        if legacy:
            u["api_keys"] = [
                {
                    "key_id": "k-" + secrets.token_hex(4),
                    "api_key": legacy,
                    "prefix": u.get("api_key_prefix") or legacy[:10],
                    "name": "默认密钥",
                    "status": "active" if status == "active" else "disabled",
                    "scopes": ["*"],
                    "created_at": u.get("created_at") or _iso(),
                    "last_used_at": None,
                    "created_by": "system",
                }
            ]
        else:
            u["api_keys"] = []
    return u


def _seed_default_admin() -> None:
    """首次启动（无任何用户）时播种默认管理员。"""
    with _lock:
        users = _load()
        if users:
            return
        uid = "u-" + secrets.token_hex(6)
        api_key, prefix = _gen_api_key()
        users[uid] = {
            "id": uid,
            "username": DEFAULT_ADMIN_USER,
            "display_name": "系统管理员",
            "role": "admin",
            "password_hash": _hash_password(DEFAULT_ADMIN_PASS),
            "api_keys": [
                {
                    "key_id": "k-" + secrets.token_hex(4),
                    "api_key": api_key,
                    "prefix": prefix,
                    "name": "默认密钥",
                    "status": "active",
                    "scopes": ["*"],
                    "created_at": _iso(),
                    "last_used_at": None,
                    "created_by": "system",
                }
            ],
            "status": "active",
            "is_active": True,
            "created_at": _iso(),
            "created_by": "system",
            "last_login_at": None,
        }
        _save(users)
        # 仅首次播种时在后台日志提示默认凭据，便于登录后立即修改
        import logging

        logging.getLogger("users").warning(
            "已创建默认管理员账号 %s / %s，请尽快在「用户管理」中修改密码或停用。",
            DEFAULT_ADMIN_USER,
            DEFAULT_ADMIN_PASS,
        )


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #
def get_by_id(uid: str) -> dict[str, Any] | None:
    u = _load().get(uid)
    return _migrate(u) if u else None


def default_admin_password_in_use() -> bool:
    """检测是否仍在使用出厂默认管理员密码（用于登录后强制改密提示/门禁）。

    仅当存在用户名为 DEFAULT_ADMIN_USER 的 admin 账号，且其密码校验为
    DEFAULT_ADMIN_PASS 时返回 True。上线前若已改密或停用默认账号即返回 False。
    """
    u = get_by_username(DEFAULT_ADMIN_USER)
    if not u or u.get("role") != "admin":
        return False
    stored = u.get("password_hash")
    return bool(stored and _verify_password(DEFAULT_ADMIN_PASS, stored))


def get_by_username(username: str) -> dict[str, Any] | None:
    un = (username or "").strip()
    for u in _load().values():
        if u.get("username") == un:
            return _migrate(u)
    return None


def get_user_and_key(api_key: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """按 api_key 解析用户与匹配的 key 记录；仅 active 用户 + active key 有效。"""
    ak = (api_key or "").strip()
    if not ak:
        return None, None
    for u in _load().values():
        _migrate(u)
        if u.get("status") != "active" or not u.get("is_active"):
            continue
        for k in u.get("api_keys") or []:
            if k.get("api_key") == ak and k.get("status") == "active":
                return u, k
    return None, None


# 兼容旧调用（仅返回 user）
def get_by_api_key(api_key: str) -> dict[str, Any] | None:
    u, _ = get_user_and_key(api_key)
    return u


def list_users(include_inactive: bool = True, status: str | None = None) -> list[dict[str, Any]]:
    users = [ _migrate(u) for u in _load().values() ]
    if status:
        users = [u for u in users if u.get("status") == status]
    elif not include_inactive:
        users = [u for u in users if u.get("is_active")]
    return sorted(users, key=lambda u: u.get("created_at") or "", reverse=False)


def authenticate(username: str, password: str) -> dict[str, Any] | None:
    """密码校验：仅 active 用户通过；pending/rejected/disabled 返回 None。"""
    u = get_by_username(username)
    if not u or u.get("status") != "active" or not u.get("is_active"):
        return None
    if not u.get("password_hash") or not _verify_password(password, u["password_hash"]):
        return None
    return u


# --------------------------------------------------------------------------- #
# 密钥脱敏与字段访问
# --------------------------------------------------------------------------- #
def _mask_key(k: dict[str, Any]) -> dict[str, Any]:
    return {
        "key_id": k.get("key_id"),
        "prefix": k.get("prefix"),
        "name": k.get("name"),
        "status": k.get("status"),
        "scopes": k.get("scopes") or ["*"],
        "created_at": k.get("created_at"),
        "last_used_at": k.get("last_used_at"),
        "created_by": k.get("created_by"),
    }


def list_api_keys(uid: str) -> list[dict[str, Any]]:
    u = get_by_id(uid)
    if not u:
        return []
    return [_mask_key(k) for k in u.get("api_keys") or []]


def get_api_key(uid: str, key_id: str) -> dict[str, Any] | None:
    u = get_by_id(uid)
    if not u:
        return None
    for k in u.get("api_keys") or []:
        if k.get("key_id") == key_id:
            return k
    return None


def touch_key(uid: str, key_id: str, *, throttle_sec: int = 300) -> None:
    """更新 key 的最近使用时间（节流，避免每次请求都写盘）。"""
    now = datetime.datetime.now()
    with _lock:
        users = _load()
        u = users.get(uid)
        if not u:
            return
        for k in u.get("api_keys") or []:
            if k.get("key_id") == key_id:
                prev = k.get("last_used_at")
                update = True
                if prev:
                    try:
                        delta = (now - datetime.datetime.strptime(prev, "%Y-%m-%d %H:%M:%S")).total_seconds()
                        if delta < throttle_sec:
                            update = False
                    except ValueError:
                        pass
                if update:
                    k["last_used_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
                    _save(users)
                break


# --------------------------------------------------------------------------- #
# 变更：注册 / 审批
# --------------------------------------------------------------------------- #
def register_user(username: str, password: str, display_name: str | None = None) -> dict[str, Any]:
    """自助注册：创建 pending 用户（无密钥、无密码外泄），待管理员审批。"""
    un = (username or "").strip()
    if not un:
        raise ValueError("用户名不能为空")
    if len(password or "") < 6:
        raise ValueError("密码长度至少 6 位")
    if get_by_username(un):
        raise ValueError(f"用户名 {un} 已存在")
    uid = "u-" + secrets.token_hex(6)
    rec: dict[str, Any] = {
        "id": uid,
        "username": un,
        "display_name": display_name or un,
        "role": "user",
        "password_hash": _hash_password(password),
        "api_keys": [],
        "status": "pending",
        "is_active": False,
        "created_at": _iso(),
        "created_by": "self-register",
        "last_login_at": None,
    }
    with _lock:
        users = _load()
        users[uid] = rec
        _save(users)
    return rec


def approve_user(uid: str, admin_id: str | None = None) -> dict[str, Any]:
    with _lock:
        users = _load()
        u = users.get(uid)
        if not u:
            raise ValueError("用户不存在")
        u["status"] = "active"
        u["is_active"] = True
        u["approved_by"] = admin_id
        u["approved_at"] = _iso()
        _save(users)
    return _migrate(u)


def reject_user(uid: str, reason: str | None = None) -> dict[str, Any]:
    with _lock:
        users = _load()
        u = users.get(uid)
        if not u:
            raise ValueError("用户不存在")
        u["status"] = "rejected"
        u["is_active"] = False
        if reason:
            u["rejected_reason"] = reason
        _save(users)
    return _migrate(u)


# --------------------------------------------------------------------------- #
# 变更：密钥 CRUD
# --------------------------------------------------------------------------- #
def create_api_key(
    uid: str,
    name: str | None = None,
    scopes: list[str] | None = None,
    created_by: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """为用户新建一个 API Key，返回 (完整 key, 记录)。完整 key 仅此次返回。"""
    key, prefix = _gen_api_key()
    rec = {
        "key_id": "k-" + secrets.token_hex(6),
        "api_key": key,
        "prefix": prefix,
        "name": name or "未命名密钥",
        "status": "active",
        "scopes": scopes or ["*"],
        "created_at": _iso(),
        "last_used_at": None,
        "created_by": created_by or "user",
    }
    with _lock:
        users = _load()
        u = users.get(uid)
        if not u:
            raise ValueError("用户不存在")
        if u.get("status") != "active":
            raise ValueError("用户尚未激活，无法生成密钥")
        u.setdefault("api_keys", []).append(rec)
        _save(users)
    return key, rec


def update_api_key(
    uid: str,
    key_id: str,
    name: str | None = None,
    status: str | None = None,
    scopes: list[str] | None = None,
) -> dict[str, Any]:
    with _lock:
        users = _load()
        u = users.get(uid)
        if not u:
            raise ValueError("用户不存在")
        rec = None
        for k in u.get("api_keys") or []:
            if k.get("key_id") == key_id:
                rec = k
                break
        if not rec:
            raise ValueError("密钥不存在")
        if name is not None:
            rec["name"] = name.strip() or rec["name"]
        if status is not None:
            if status not in ("active", "disabled", "revoked"):
                raise ValueError("非法密钥状态")
            rec["status"] = status
        if scopes is not None:
            rec["scopes"] = scopes or ["*"]
        _save(users)
    return _mask_key(rec)


def revoke_api_key(uid: str, key_id: str) -> dict[str, Any]:
    return update_api_key(uid, key_id, status="revoked")


def rotate_api_key(uid: str) -> tuple[str, str]:
    """管理员重置：吊销该用户全部现有密钥并签发一枚新密钥。"""
    new_key, prefix = _gen_api_key()
    with _lock:
        users = _load()
        u = users.get(uid)
        if not u:
            raise ValueError("用户不存在")
        for k in u.get("api_keys") or []:
            k["status"] = "revoked"
        rec = {
            "key_id": "k-" + secrets.token_hex(6),
            "api_key": new_key,
            "prefix": prefix,
            "name": "重置生成",
            "status": "active",
            "scopes": ["*"],
            "created_at": _iso(),
            "last_used_at": None,
            "created_by": "admin",
        }
        u.setdefault("api_keys", []).append(rec)
        _save(users)
    return new_key, prefix


# --------------------------------------------------------------------------- #
# 变更：资料 / 状态
# --------------------------------------------------------------------------- #
def create_user(
    *,
    username: str,
    display_name: str | None = None,
    role: str = "user",
    password: str | None = None,
    created_by: str | None = None,
    approve: bool = True,
) -> dict[str, Any]:
    """管理员直接创建用户（绕过注册审核，默认立即激活）。role=user 必须有密码。"""
    un = (username or "").strip()
    if not un:
        raise ValueError("用户名不能为空")
    if get_by_username(un):
        raise ValueError(f"用户名 {un} 已存在")
    if role not in ("admin", "user"):
        raise ValueError("role 必须为 admin 或 user")
    if role == "admin" and not password:
        raise ValueError("管理员账号必须设置密码")
    if password and len(password) < 6:
        raise ValueError("密码长度至少 6 位")

    uid = "u-" + secrets.token_hex(6)
    rec: dict[str, Any] = {
        "id": uid,
        "username": un,
        "display_name": display_name or un,
        "role": role,
        "api_keys": [],
        "created_at": _iso(),
        "created_by": created_by or "admin",
        "last_login_at": None,
    }
    if password:
        rec["password_hash"] = _hash_password(password)
    # 管理员直建默认激活；如需走审核流程可传 approve=False
    rec["status"] = "active" if approve else "pending"
    rec["is_active"] = bool(approve)
    with _lock:
        users = _load()
        users[uid] = rec
        _save(users)
    return rec


def set_password(uid: str, new_password: str) -> None:
    if not new_password or len(new_password) < 6:
        raise ValueError("密码长度至少 6 位")
    with _lock:
        users = _load()
        u = users.get(uid)
        if not u:
            raise ValueError("用户不存在")
        u["password_hash"] = _hash_password(new_password)
        _save(users)


def set_active(uid: str, active: bool) -> None:
    with _lock:
        users = _load()
        u = users.get(uid)
        if not u:
            raise ValueError("用户不存在")
        u["is_active"] = bool(active)
        if bool(active):
            if u.get("status") in (None, "disabled", "rejected"):
                u["status"] = "active"
        else:
            if u.get("status") == "active":
                u["status"] = "disabled"
        _save(users)


def set_display_name(uid: str, name: str) -> None:
    with _lock:
        users = _load()
        u = users.get(uid)
        if not u:
            raise ValueError("用户不存在")
        u["display_name"] = (name or "").strip() or u["username"]
        _save(users)


def record_login(uid: str) -> None:
    with _lock:
        users = _load()
        u = users.get(uid)
        if not u:
            return
        u["last_login_at"] = _iso()
        _save(users)


def to_public(u: dict[str, Any], include_api_key: bool = False) -> dict[str, Any]:
    """对外脱敏：隐藏 password_hash 与完整 api_key；包含 status 与 masked api_keys。"""
    _migrate(u)
    keys = [_mask_key(k) for k in u.get("api_keys") or []]
    active = next((k for k in keys if k["status"] == "active"), None)
    out = {
        "id": u.get("id"),
        "username": u.get("username"),
        "display_name": u.get("display_name"),
        "role": u.get("role"),
        "status": u.get("status", "active"),
        "api_key_prefix": active["prefix"] if active else u.get("api_key_prefix"),
        "is_active": bool(u.get("is_active", u.get("status") == "active")),
        "created_at": u.get("created_at"),
        "created_by": u.get("created_by"),
        "last_login_at": u.get("last_login_at"),
        "approved_by": u.get("approved_by"),
        "approved_at": u.get("approved_at"),
        "api_keys": keys,
    }
    # include_api_key 不再回显完整密钥（安全加固）；完整 key 由专门创建接口返回。
    if include_api_key:
        out["api_key"] = None
    return out
