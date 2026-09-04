"""存储注册表：持有当前生效的后端实例，配置变更时整体切换。"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .collections import CollectionBackend, build_collection_backend
from .settings import StorageConfigError, settings as _settings

logger = logging.getLogger(__name__)

_switch_lock = threading.Lock()
_collection: CollectionBackend | None = None
_collection_driver: str = ""
_switch_cbs: list[Callable[[str, str], None]] = []  # (old_driver, new_driver)

#: 业务集合名静态清单。读穿式存储（sessions/users/file_types/rule_groups/
#: custom_rulesets）没有内存缓存，切换回调无法替它们搬数据，必须在 registry
#: 层统一做旧→新拷贝；内存态模块（prompts/legal_rulesets/tasks/files_meta）
#: 的回调随后会以权威内存态覆写，二者语义不冲突。
KNOWN_COLLECTIONS = (
    "custom_rulesets",
    "file_types",
    "files_meta",
    "legal_rulesets",
    "prompts",
    "rule_groups",
    "sessions",
    "tasks",
    "users",
)
_known_collections: set[str] = set(KNOWN_COLLECTIONS)


def _collection_backend(cfg: dict[str, Any]) -> CollectionBackend:
    return build_collection_backend(cfg["structured"])


def apply(new_cfg: dict[str, Any], old_cfg: dict[str, Any]) -> None:
    """应用新配置：重建集合后端并触发切换回调（数据以内存为权威，回调把
    各业务模块的当前内存态整写到新后端，保证切换不丢数据）。"""
    global _collection, _collection_driver

    old_driver = str(old_cfg["structured"]["driver"]).lower()
    new_driver = str(new_cfg["structured"]["driver"]).lower()

    with _switch_lock:
        backend = _collection_backend(new_cfg)  # 失败会抛出，保持旧后端不动
        # 健康探测：读写一次探活，避免切换到连不上的库
        probe = "__storage_probe__"
        try:
            backend.write_all(probe, {"ts": datetime.now().isoformat(timespec="seconds")})
            backend.read_all(probe)
        except Exception as exc:  # noqa: BLE001
            raise StorageConfigError(f"新存储后端探活失败: {exc}") from exc
        old_backend = _collection
        old_backend_id = old_backend.backend_id() if old_backend else None
        new_backend_id = backend.backend_id()
        _collection = backend
        _collection_driver = new_driver

        # 后端实例（或其数据位置）发生变化即触发迁写回调——不仅限于 driver 变化：
        # 同 driver 下换集合形态（json→表）或换 data_dir/库，同样需要把内存态写入新后端。
        if old_backend_id is not None and old_backend_id != new_backend_id:
            logger.warning("结构化存储后端切换: %s → %s（driver %s → %s）",
                           old_backend_id, new_backend_id, old_driver, new_driver)
            # 第一步：把全部已知集合从旧后端整读整写到新后端。这一步对读穿式
            # 存储（sessions 等）是数据搬运的唯一机会——它们没有内存缓存，回调
            # 也无从替它们读旧后端（此时 registry 已指向新后端）。
            for name in sorted(_known_collections):
                try:
                    data = old_backend.read_all(name)
                    if data is not None:
                        backend.write_all(name, data)
                except Exception:  # noqa: BLE001
                    logger.exception("集合 %s 旧→新后端拷贝失败", name)
            # 第二步：业务模块回调以内存态覆写（内存为权威），读穿式集合保持
            # 第一步拷贝的结果。
            for cb in _switch_cbs:
                try:
                    cb(old_driver, new_driver)
                except Exception:  # noqa: BLE001
                    logger.exception("存储切换回调执行失败: %r", cb)

    new_base = Path(str(new_cfg["files"]["local"]["base_dir"]))
    old_base = Path(str(old_cfg["files"]["local"]["base_dir"]))
    if new_base != old_base:
        new_base.mkdir(parents=True, exist_ok=True)
        logger.warning(
            "文件存储目录变更: %s → %s（旧目录中已上传文件不会自动搬迁，"
            "历史文件如需保留请手动迁移）",
            old_base, new_base,
        )


# --------------------------------------------------------------------------- #
# 业务模块访问入口
# --------------------------------------------------------------------------- #
def collection() -> CollectionBackend:
    """当前集合后端（懒初始化）。"""
    global _collection, _collection_driver
    if _collection is None:
        with _switch_lock:
            if _collection is None:
                _collection = _collection_backend(_settings.cfg)
                _collection_driver = _settings.structured_driver
    return _collection


def read_collection(name: str) -> Any | None:
    _known_collections.add(name)
    return collection().read_all(name)


def write_collection(name: str, data: Any) -> None:
    _known_collections.add(name)
    collection().write_all(name, data)


def on_structured_switch(cb: Callable[[str, str], None]) -> None:
    """注册驱动切换回调：业务模块用它把内存态迁写到新后端。"""
    _switch_cbs.append(cb)
    # 若进程启动后已发生切换（热加载先于模块导入），补触发一次
    if _collection is not None and _collection_driver and _collection_driver != "sqlite":
        try:
            cb("sqlite", _collection_driver)
        except Exception:  # noqa: BLE001
            logger.exception("存储切换补触发回调失败: %r", cb)


def connect_relational(logical: str, sqlite_path: Path):
    """关系库统一连接入口（见 relational.connect）。"""
    from . import relational

    return relational.connect(logical, sqlite_path, _settings.structured)


def upload_dir() -> Path:
    """上传文件目录 = files.local.base_dir / uploads（动态解析，热切换即生效）。"""
    return _settings.files_base_dir / "uploads"


def snapshot() -> dict[str, Any]:
    """当前存储状态（供管理端点展示）。"""
    cfg = _settings.cfg
    return {
        "config_path": str(_settings.path),
        "config_file_exists": _settings.path.exists(),
        "structured_driver": _settings.structured_driver,
        "collections_backend": type(collection()).__name__,
        "files_driver": _settings.files_driver,
        "files_base_dir": str(_settings.files_base_dir),
        "upload_dir": str(upload_dir()),
        "hot_reload": {
            "enabled": _settings.hot_enabled,
            "interval_seconds": _settings.hot_interval,
        },
        "last_reload_at": _settings.last_reload_at,
        "last_error": _settings.last_error,
        "config_masked": _settings.mask_secrets(cfg) if cfg else None,
    }
