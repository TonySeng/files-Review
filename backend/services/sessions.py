# -*- coding: utf-8 -*-
"""管理员会话令牌存储。

管理员以账号密码登录后，服务端签发一个无状态风格的会话令牌（X-Session-Token），
存于 backend/data/sessions.json（同卷持久化，跨容器重建保留）。令牌带过期时间。
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from .. import config
from .. import storage

DATA_DIR = config.DATA_DIR
DATA_DIR.mkdir(parents=True, exist_ok=True)
# sessions 集合经统一存储层持久化（data/storage_config.json 可切换后端）
_lock = threading.Lock()
_SESSION_TTL = 60 * 60 * 24 * 7  # 7 天


def _load() -> dict[str, dict[str, Any]]:
    data = storage.read_collection("sessions")
    return data if isinstance(data, dict) else {}


def _save(sessions: dict[str, dict[str, Any]]) -> None:
    try:
        storage.write_collection("sessions", sessions)
    except Exception:  # noqa: BLE001
        pass


def _cleanup(sessions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    now = time.time()
    return {t: s for t, s in sessions.items() if s.get("expires_at", 0) > now}


def create_session(*, user_id: str, username: str, role: str) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _lock:
        sessions = _cleanup(_load())
        sessions[token] = {
            "user_id": user_id,
            "username": username,
            "role": role,
            "created_at": now,
            "expires_at": now + _SESSION_TTL,
        }
        _save(sessions)
    return token


def get_session(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    with _lock:
        sessions = _load()
    s = sessions.get(token)
    if not s:
        return None
    if s.get("expires_at", 0) <= time.time():
        delete_session(token)
        return None
    return s


def delete_session(token: str | None) -> None:
    if not token:
        return
    with _lock:
        sessions = _load()
        sessions.pop(token, None)
        _save(sessions)
