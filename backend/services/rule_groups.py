# -*- coding: utf-8 -*-
"""审核规则组存储：规则组是规则的命名组合，可多选套用到审核任务。

规则组与「审核规则」解耦——规则组只保存其包含的规则 id 列表，实际规则对象在
送审时通过 rules_store 解析（支持内置规则与自定义规则）。本地 JSON 持久化。
"""
from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Any

from . import rules_store

# 与 SQLite 配置库一致，JSON 持久化也落在 backend/data/ 下，
# 使「文件类型 / 规则组」配置随 docker-compose 挂载的 backend_data 卷一起持久化。
DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
from .. import storage  # noqa: E402  # 统一存储层

# rule_groups 集合经统一存储层持久化（data/storage_config.json 可切换后端）
_lock = threading.Lock()

# 预置规则组（与通用审核工作台默认映射对齐）
_DEFAULT_GROUPS: list[dict[str, Any]] = [
    {
        "id": "rg-completeness",
        "name": "完整性组",
        "description": "核查文件关键要素是否齐全：必备清单、营业执照、资质证书、保证金、附件引用等。",
        "builtin": True,
        "rule_ids": ["fmt-completeness", "qual-license", "qual-cert", "biz-bidbond", "gen-attachment"],
    },
    {
        "id": "rg-consistency",
        "name": "一致性组",
        "description": "跨文件信息一致性、与招标要求对应性、报价算术准确性、数字日期格式统一。",
        "builtin": True,
        "rule_ids": ["cons-cross", "cons-tender-match", "biz-price", "gen-format-number"],
    },
    {
        "id": "rg-format",
        "name": "规范性组",
        "description": "格式编制规范、签字盖章、错别字与术语规范、文档结构完整。",
        "builtin": True,
        "rule_ids": ["fmt-template", "fmt-seal", "gen-typo", "gen-terminology", "gen-toc"],
    },
    {
        "id": "rg-compliance",
        "name": "合规性组",
        "description": "信用与禁止投标、联合体合规、付款条款、资格条件合法性等否决/合规风险。",
        "builtin": True,
        "rule_ids": ["qual-credit", "qual-consortium", "biz-payment", "tender-qualification"],
    },
    {
        "id": "rg-signature",
        "name": "签署有效期组",
        "description": "营业执照/资质证书有效期、投标有效期与工期承诺等时效类要素。",
        "builtin": True,
        "rule_ids": ["biz-validity", "qual-cert", "qual-license"],
    },
]


def _normalize(g: dict[str, Any]) -> dict[str, Any]:
    """补齐归属字段：无 owner_id 视为系统预置，标记系统共享。"""
    g = dict(g)
    if "owner_id" not in g:
        g["owner_id"] = "system"
    if "is_shared" not in g:
        g["is_shared"] = g["owner_id"] == "system"
    return g


def _seeded() -> list[dict[str, Any]]:
    return [_normalize(g) for g in _DEFAULT_GROUPS]


def _load() -> list[dict[str, Any]]:
    data = storage.read_collection("rule_groups")
    if not isinstance(data, list):
        return _seeded()
    # 迁移：存量规则组归属「系统共享」（无论来自文件还是预置，均无 owner_id 即视为系统）
    changed = False
    for g in data:
        if not isinstance(g, dict):
            continue
        if "owner_id" not in g:
            g["owner_id"] = "system"
            changed = True
        if "is_shared" not in g:
            g["is_shared"] = g["owner_id"] == "system"
            changed = True
    if changed:
        _save(data)
    return data


def _accessible(g: dict[str, Any], user_id: str | None) -> bool:
    if user_id is None:
        return True
    return g.get("owner_id") == user_id or bool(g.get("is_shared"))


def _save(items: list[dict[str, Any]]) -> None:
    try:
        storage.write_collection("rule_groups", items)
    except Exception:  # noqa: BLE001
        pass


def list_groups(user_id: str | None = None) -> list[dict[str, Any]]:
    return [g for g in _load() if _accessible(g, user_id)]


def get(group_id: str, user_id: str | None = None) -> dict[str, Any] | None:
    for g in _load():
        if g["id"] == group_id:
            return g if _accessible(g, user_id) else None
    return None


def get_by_name(name: str, user_id: str | None = None) -> dict[str, Any] | None:
    for g in _load():
        if g["name"] == name:
            return g if _accessible(g, user_id) else None
    return None


def get_many(ids: list[str], user_id: str | None = None) -> list[dict[str, Any]]:
    want = set(ids)
    return [g for g in _load() if g["id"] in want and _accessible(g, user_id)]


def save_group(
    payload: dict[str, Any],
    user_id: str | None = None,
    is_shared: bool | None = None,
) -> dict[str, Any]:
    """新建或更新规则组；预置组会被复制为新组（builtin 置 False）。

    归属语义同规则集：管理员创建→系统共享；普通用户创建→归属自己。
    """
    with _lock:
        items = _load()
        gid = payload.get("id")
        if not gid or (
            gid.startswith("rg-")
            and any(g["id"] == gid and g.get("builtin") for g in items)
        ):
            gid = f"rg-{uuid.uuid4().hex[:8]}"
        existing = next((g for g in items if g["id"] == gid), None)
        if existing:
            owner_id = existing.get("owner_id", "system")
            shared = existing.get("is_shared", True)
        else:
            owner_id = "system" if user_id is None else user_id
            shared = True if user_id is None else bool(is_shared)
        record = {
            "id": gid,
            "name": payload.get("name") or "未命名规则组",
            "description": payload.get("description", ""),
            "builtin": False,
            "owner_id": owner_id,
            "is_shared": shared,
            "rule_ids": [str(r) for r in (payload.get("rule_ids") or [])],
        }
        for i, g in enumerate(items):
            if g["id"] == gid:
                items[i] = record
                break
        else:
            items.append(record)
        _save(items)
        return record


def delete_group(group_id: str, user_id: str | None = None) -> bool:
    if group_id.startswith("rg-") and any(
        g["id"] == group_id and g.get("builtin") for g in _load()
    ):
        return False
    with _lock:
        items = _load()
        target = next((g for g in items if g["id"] == group_id), None)
        if not target:
            return False
        if user_id is not None and target.get("owner_id") != user_id:
            return False
        remaining = [g for g in items if g["id"] != group_id]
        if len(remaining) == len(items):
            return False
        _save(remaining)
        return True


def expand_rule_groups(
    group_ids: list[str] | None, user_id: str | None = None
) -> list[dict[str, Any]]:
    """把规则组解析为去重后的规则对象列表（按内置 + 自定义规则解析）。"""
    if not group_ids:
        return []
    rule_ids: list[str] = []
    for g in get_many(group_ids, user_id):
        rule_ids.extend(g.get("rule_ids", []))
    seen: set[str] = set()
    ordered: list[str] = []
    for rid in rule_ids:
        if rid not in seen:
            seen.add(rid)
            ordered.append(rid)
    return rules_store.get_rules_by_ids(ordered, user_id)
