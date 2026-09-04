"""统一数据存储配置：加载、校验、热加载。

配置文件解析顺序：
1. 环境变量 ``BCR_STORAGE_CONFIG`` 指定的路径
2. ``DATA_DIR/storage_config.json``（默认 ``backend/data/storage_config.json``）
3. 均不存在时使用内置默认值（等价于历史行为：JSON 集合 + SQLite + data 目录）

配置文件样例见仓库 ``backend/storage_config.example.json``。修改配置文件后
热加载器（见 hot.py）会在轮询间隔内自动应用，无需重启或改代码。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from .. import config as app_config

logger = logging.getLogger(__name__)

#: 结构化数据驱动 → 必填/可选键
_STRUCTURED_DRIVERS = {"sqlite", "mysql", "postgresql"}
_FILE_DRIVERS = {"local"}

_DEFAULTS: dict[str, Any] = {
    "structured": {
        "driver": "sqlite",
        "sqlite": {"data_dir": str(app_config.DATA_DIR)},
        "mysql": {
            "host": "127.0.0.1",
            "port": 3306,
            "user": "",
            "password": "",
            "database": "bidding_review",
            "charset": "utf8mb4",
            "connect_timeout": 10,
        },
        "postgresql": {
            "host": "127.0.0.1",
            "port": 5432,
            "user": "",
            "password": "",
            "database": "bidding_review",
            "connect_timeout": 10,
        },
    },
    "files": {
        "driver": "local",
        "local": {"base_dir": str(app_config.DATA_DIR)},
    },
    "hot_reload": {"enabled": True, "interval_seconds": 5},
}


class StorageConfigError(ValueError):
    """存储配置非法。"""


def config_path() -> Path:
    """当前生效的配置文件路径（未必存在）。"""
    env = os.getenv("BCR_STORAGE_CONFIG")
    if env:
        return Path(env)
    return app_config.DATA_DIR / "storage_config.json"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = json.loads(json.dumps(base))  # deep copy
    for key, val in (override or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def load_raw(path: Path | None = None) -> dict[str, Any] | None:
    """读取配置文件原始内容；文件不存在返回 None。"""
    p = path or config_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise StorageConfigError(f"存储配置文件解析失败 {p}: {exc}") from exc


def load_and_validate(path: Path | None = None) -> dict[str, Any]:
    """读取并校验配置，返回合并默认值后的完整配置。非法时抛 StorageConfigError。"""
    raw = load_raw(path)
    merged = _deep_merge(_DEFAULTS, raw or {})

    st = merged["structured"]
    driver = str(st.get("driver", "sqlite")).lower()
    if driver not in _STRUCTURED_DRIVERS:
        raise StorageConfigError(
            f"structurerd.driver 非法: {driver!r}（可选 {'/'.join(sorted(_STRUCTURED_DRIVERS))}）"
        )
    if driver in ("mysql", "postgresql"):
        sect = st.get(driver) or {}
        missing = [
            k for k in ("host", "user", "database") if not str(sect.get(k) or "").strip()
        ]
        if missing:
            raise StorageConfigError(f"structured.{driver} 缺少必填项: {', '.join(missing)}")

    files = merged["files"]
    fdriver = str(files.get("driver", "local")).lower()
    if fdriver not in _FILE_DRIVERS:
        raise StorageConfigError(
            f"files.driver 非法: {fdriver!r}（可选 {'/'.join(sorted(_FILE_DRIVERS))}）"
        )
    base_dir = str((files.get("local") or {}).get("base_dir") or "").strip()
    if not base_dir:
        raise StorageConfigError("files.local.base_dir 不能为空")

    hot = merged["hot_reload"]
    interval = float(hot.get("interval_seconds", 5))
    if interval < 1:
        raise StorageConfigError("hot_reload.interval_seconds 必须 >= 1 秒")
    merged["hot_reload"]["interval_seconds"] = interval

    return merged


def mtime(path: Path | None = None) -> float | None:
    """配置文件修改时间（不存在返回 None），热加载轮询用。"""
    p = path or config_path()
    try:
        return p.stat().st_mtime
    except OSError:
        return None


def mask_secrets(cfg: dict[str, Any]) -> dict[str, Any]:
    """打码凭据字段，供状态接口展示。"""
    masked = json.loads(json.dumps(cfg))

    def _walk(node: dict[str, Any]) -> None:
        for key, val in node.items():
            if isinstance(val, dict):
                _walk(val)
            elif isinstance(val, str) and key in ("password",):
                node[key] = "***" if val else ""

    _walk(masked)
    return masked


class Settings:
    """进程内当前生效的存储配置（线程安全读取/整体替换）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cfg: dict[str, Any] = load_and_validate()
        self._mtime: float | None = mtime()
        self.last_reload_at: str | None = None
        self.last_error: str | None = None

    # -- 读取 --------------------------------------------------------------
    @property
    def cfg(self) -> dict[str, Any]:
        with self._lock:
            return self._cfg

    @property
    def structured(self) -> dict[str, Any]:
        return self.cfg["structured"]

    @property
    def files(self) -> dict[str, Any]:
        return self.cfg["files"]

    @property
    def structured_driver(self) -> str:
        return str(self.structured["driver"]).lower()

    @property
    def files_driver(self) -> str:
        return str(self.files["driver"]).lower()

    @property
    def files_base_dir(self) -> Path:
        return Path(str(self.files["local"]["base_dir"]))

    @property
    def sqlite_data_dir(self) -> Path:
        return Path(str(self.structured["sqlite"]["data_dir"]))

    @property
    def hot_enabled(self) -> bool:
        return bool(self.cfg["hot_reload"]["enabled"])

    @property
    def hot_interval(self) -> float:
        return float(self.cfg["hot_reload"]["interval_seconds"])

    @property
    def path(self) -> Path:
        return config_path()

    # -- 模块函数委托 -------------------------------------------------------
    # 注意：包命名空间中实例 `settings` 会遮蔽同名子模块（from . import settings
    # 拿到的是实例），因此 hot.py / registry.py 等调用方只能用实例方法。为避免
    # 混淆，这里把模块级函数委托为实例方法。
    def config_path(self) -> Path:  # noqa: D102
        return config_path()

    def mask_secrets(self, cfg: dict[str, Any]) -> dict[str, Any]:  # noqa: D102
        return mask_secrets(cfg)

    # -- 热加载 ------------------------------------------------------------
    def reload_if_changed(self) -> bool:
        """配置文件 mtime 变化时重新加载并应用。返回是否发生了变更。

        解析/校验失败时保留旧配置（fail-safe），记录 last_error。
        """
        with self._lock:
            cur_mtime = mtime()
            if cur_mtime is not None and cur_mtime == self._mtime:
                return False
            try:
                new_cfg = load_and_validate()
            except StorageConfigError as exc:
                self._mtime = cur_mtime  # 避免同一份坏文件反复报错
                self.last_error = str(exc)
                logger.error("存储配置热加载失败（沿用旧配置）: %s", exc)
                return False
            old_cfg = self._cfg
            self._cfg = new_cfg
            self._mtime = cur_mtime
            self.last_error = None
            from datetime import datetime

            self.last_reload_at = datetime.now().isoformat(timespec="seconds")

        changed = old_cfg != new_cfg
        if changed:
            logger.info("存储配置已热加载更新: structured=%s files=%s",
                        new_cfg["structured"]["driver"], new_cfg["files"]["driver"])
            # 通知注册表切换后端（在锁外做，避免长操作持锁）
            from . import registry

            registry.apply(new_cfg, old_cfg)
        return changed


settings = Settings()
