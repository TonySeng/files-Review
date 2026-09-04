"""存储配置热加载循环（lifespan 内启动的 asyncio 任务）。"""
from __future__ import annotations

import asyncio
import logging

from . import settings as storage_settings

logger = logging.getLogger(__name__)


async def run_hot_reload(poll_interval: float = 2.0) -> None:
    """周期检查配置文件 mtime；变更则触发 settings.reload_if_changed()。

    轮询间隔固定 2 秒（轻量 stat 调用），配置自身的 hot_reload.interval_seconds
    决定最小生效粒度说明；enabled=false 时仍轮询但跳过应用（便于观察）。
    """
    logger.info("存储配置热加载器已启动（配置: %s）", storage_settings.config_path())
    while True:
        try:
            if storage_settings.hot_enabled:
                storage_settings.reload_if_changed()
        except Exception:  # noqa: BLE001
            logger.exception("存储配置热加载异常")
        await asyncio.sleep(poll_interval)


def start() -> asyncio.Task:
    return asyncio.create_task(run_hot_reload())
