# -*- coding: utf-8 -*-
"""文件类型存储：可审核的文件类型定义，并关联审核规则组（用于自动匹配）。

关键点：
- 文件类型由用户手动指定，系统**不自动识别/分类**文件内容。
- 类型可限制允许的后缀（extensions 为空表示不限制）。
- 每个类型可关联若干规则组（rule_group_ids），开启「自动匹配」后，
  审核引擎会依用户指定的类型并集套用关联规则组。
本地 JSON 持久化。
"""
from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Any

# 与 SQLite 配置库一致，JSON 持久化也落在 backend/data/ 下，
# 使「文件类型 / 规则组」配置随 docker-compose 挂载的 backend_data 卷一起持久化。
DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
from .. import storage  # noqa: E402  # 统一存储层

# file_types 集合经统一存储层持久化（data/storage_config.json 可切换后端）
_lock = threading.Lock()

# 预置文件类型（与通用审核工作台默认映射对齐）
_DEFAULT_TYPES: list[dict[str, Any]] = [
    {
        "id": "ft-tender",
        "name": "招标文件",
        "description": "招标公告、招标文件、评分办法等；审核时通常需核查完整性与合规性。",
        "builtin": True,
        "extensions": [".pdf", ".doc", ".docx"],
        "rule_group_ids": ["rg-completeness", "rg-compliance"],
    },
    {
        "id": "ft-bid",
        "name": "投标文件",
        "description": "投标函、商务标、技术标等响应文件；需核查资格、商务、技术、格式与一致性。",
        "builtin": True,
        "extensions": [".pdf", ".doc", ".docx", ".xlsx", ".xls"],
        "rule_group_ids": ["rg-completeness", "rg-consistency", "rg-format", "rg-compliance"],
    },
    {
        "id": "ft-contract",
        "name": "合同协议",
        "description": "合同、协议、备忘录等；关注条款公平性与前后一致性。",
        "builtin": True,
        "extensions": [],
        "rule_group_ids": ["rg-consistency", "rg-compliance"],
    },
    {
        "id": "ft-qualification",
        "name": "资质证明",
        "description": "营业执照、资质证书、荣誉/业绩证明等扫描件；关注要素齐全与有效期。",
        "builtin": True,
        "extensions": [".pdf", ".png", ".jpg", ".jpeg"],
        "rule_group_ids": ["rg-completeness"],
    },
    {
        "id": "ft-general",
        "name": "通用文档",
        "description": "未归入上述类型的其他文档；默认应用规范性核查。",
        "builtin": True,
        "extensions": [],
        "rule_group_ids": ["rg-format"],
    },
]


def _normalize(t: dict[str, Any]) -> dict[str, Any]:
    """补齐归属字段：无 owner_id 视为系统预置，标记系统共享。"""
    t = dict(t)
    if "owner_id" not in t:
        t["owner_id"] = "system"
    if "is_shared" not in t:
        t["is_shared"] = t["owner_id"] == "system"
    return t


def _seeded() -> list[dict[str, Any]]:
    return [_normalize(t) for t in _DEFAULT_TYPES]


def _load() -> list[dict[str, Any]]:
    data = storage.read_collection("file_types")
    if not isinstance(data, list):
        return _seeded()
    # 迁移：存量文件类型归属「系统共享」（无 owner_id 即视为系统预置）
    changed = False
    for t in data:
        if not isinstance(t, dict):
            continue
        if "owner_id" not in t:
            t["owner_id"] = "system"
            changed = True
        if "is_shared" not in t:
            t["is_shared"] = t["owner_id"] == "system"
            changed = True
    if changed:
        _save(data)
    return data


def _accessible(t: dict[str, Any], user_id: str | None) -> bool:
    if user_id is None:
        return True
    return t.get("owner_id") == user_id or bool(t.get("is_shared"))


def _save(items: list[dict[str, Any]]) -> None:
    try:
        storage.write_collection("file_types", items)
    except Exception:  # noqa: BLE001
        pass


def list_types(user_id: str | None = None) -> list[dict[str, Any]]:
    return [t for t in _load() if _accessible(t, user_id)]


def get(type_id: str, user_id: str | None = None) -> dict[str, Any] | None:
    for t in _load():
        if t["id"] == type_id:
            return t if _accessible(t, user_id) else None
    return None


def get_by_name(name: str, user_id: str | None = None) -> dict[str, Any] | None:
    for t in _load():
        if t["name"] == name:
            return t if _accessible(t, user_id) else None
    return None


def save_type(
    payload: dict[str, Any],
    user_id: str | None = None,
    is_shared: bool | None = None,
) -> dict[str, Any]:
    """新建或更新文件类型；预置类型会被复制为新类型（builtin 置 False）。

    归属语义同规则集：管理员创建→系统共享；普通用户创建→归属自己。
    """
    with _lock:
        items = _load()
        tid = payload.get("id")
        if not tid or (
            tid.startswith("ft-")
            and any(t["id"] == tid and t.get("builtin") for t in items)
        ):
            tid = f"ft-{uuid.uuid4().hex[:8]}"
        existing = next((t for t in items if t["id"] == tid), None)
        if existing:
            owner_id = existing.get("owner_id", "system")
            shared = existing.get("is_shared", True)
        else:
            owner_id = "system" if user_id is None else user_id
            shared = True if user_id is None else bool(is_shared)
        record = {
            "id": tid,
            "name": payload.get("name") or "未命名文件类型",
            "description": payload.get("description", ""),
            "builtin": False,
            "owner_id": owner_id,
            "is_shared": shared,
            "extensions": [str(e).lower() for e in (payload.get("extensions") or [])],
            "rule_group_ids": [str(g) for g in (payload.get("rule_group_ids") or [])],
        }
        for i, t in enumerate(items):
            if t["id"] == tid:
                items[i] = record
                break
        else:
            items.append(record)
        _save(items)
        return record


def delete_type(type_id: str, user_id: str | None = None) -> bool:
    if type_id.startswith("ft-") and any(
        t["id"] == type_id and t.get("builtin") for t in _load()
    ):
        return False
    with _lock:
        items = _load()
        target = next((t for t in items if t["id"] == type_id), None)
        if not target:
            return False
        if user_id is not None and target.get("owner_id") != user_id:
            return False
        remaining = [t for t in items if t["id"] != type_id]
        if len(remaining) == len(items):
            return False
        _save(remaining)
        return True
