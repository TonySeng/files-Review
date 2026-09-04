"""审核任务存储：任务记录持久化到数据卷，跨容器重建/重启保留。

每个任务记录其请求快照、实时进度、过程日志、最终结果；并通过 asyncio.Event
支持取消。持久化文件位于 backend/data/tasks.json（与 app.db 同卷），因此
即使重建后端镜像、容器重启，历史任务数据也不会丢失。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

from .. import config
from .. import storage

# tasks 集合经统一存储层持久化（data/storage_config.json 可切换后端）
_STATUS_TERMINAL = {"completed", "failed", "cancelled"}
# running 但超过此秒数无任何进度推进 → 判定 worker 已死/挂死，强制回收。
# 阈值必须大于「两次进度心跳之间的最大墙钟间隔」：常规规则批次之间、规则↔一致性之间均有
# 心跳，间隔受单次 LLM 调用硬上限 llm_call_hard_ceil=1800s 约束；而跨文件一致性 Phase2 是
# 单次结构化比对调用，慢模型下可逼近该硬顶，且单次调用期间无法细分心跳。因此阈值须 > 1800s
# 方能覆盖任何单次合法调用——取 2000s 留安全余量（修复 failed@95% 误杀）。
_REAP_STALE_SECS = 2000


class TaskStore:
    def __init__(self) -> None:
        self._tasks: dict[str, dict[str, Any]] = {}
        self._cancel: dict[str, asyncio.Event] = {}
        # 当前进程内正在运行的工作进程 tid 集合；用于区分「正在跑」与「僵尸(running 但无 worker)」
        self._active: set[str] = set()
        self._lock = asyncio.Lock()
        self._load()
        # 驱动热切换时把内存任务表整写到新后端
        storage.on_structured_switch(
            lambda _old, _new: storage.write_collection("tasks", self._tasks)
        )

    def _load(self) -> None:
        """启动时从磁盘恢复历史任务；非终态任务标记为中断（进程已不存在，无活动工作进程）。"""
        raw = storage.read_collection("tasks")
        if not isinstance(raw, dict):
            return
        for tid, task in raw.items():
            if task.get("status") not in _STATUS_TERMINAL:
                task["status"] = "failed"
                task["error"] = (task.get("error") or "") + "（服务重启，任务中断）"
                task.setdefault("finished_at", time.time())
            self._tasks[tid] = task
        # 持久化回收结果（原实现只改内存、不落盘，导致磁盘上仍显示 running）
        self._save()

    def _save(self) -> None:
        try:
            storage.write_collection("tasks", self._tasks)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def new_id() -> str:
        import uuid

        return uuid.uuid4().hex[:12]

    async def create(self, meta: dict[str, Any], user_id: str | None = None) -> str:
        tid = self.new_id()
        now = time.time()
        task: dict[str, Any] = {
            "task_id": tid,
            "status": "pending",
            "created_at": now,
            "finished_at": None,
            # 归属用户：管理员发起为 None（系统级），普通用户发起为本人的 user_id。
            # 列表与详情据此区分/过滤归属，支撑「按用户区分审核任务」。
            # 优先用独立参数 user_id；兼容历史调用把 user_id 放进 meta 字典的写法。
            "user_id": user_id if user_id is not None else meta.get("user_id"),
            # 请求快照（用于详情展示与重放）
            "request": meta,
            "file_names": meta.get("file_names", []),
            "mode": meta.get("mode", "bid"),
            "rule_count": meta.get("rule_count", 0),
            # 实时字段
            "progress": 0,
            "progress_message": "",
            "logs": [],
            "findings": [],
            "consistency_issues": [],
            "rule_results": [],
            "kb_traces": [],
            "summary": None,
            "error": None,
        }
        async with self._lock:
            self._tasks[tid] = task
            self._cancel[tid] = asyncio.Event()
        self._save()
        # 新建任务时顺手回收其它孤儿任务（无需等待下次重启即可清理僵尸）
        try:
            await self.reap_orphans()
        except Exception:  # noqa: BLE001
            pass
        return tid

    async def get(self, tid: str) -> dict[str, Any] | None:
        async with self._lock:
            return self._tasks.get(tid)

    async def list(self, user_id: str | None = None) -> list[dict[str, Any]]:
        async with self._lock:
            items = self._tasks.values()
            if user_id is not None:
                items = [t for t in items if t.get("user_id") == user_id]
            return sorted(items, key=lambda t: t["created_at"], reverse=True)

    async def update(self, tid: str, **fields: Any) -> None:
        async with self._lock:
            task = self._tasks.get(tid)
            if task:
                task.update(fields)
                # 记录最近一次进度推进时间，供孤儿回收器判断「running 但长期无进展」
                if task.get("status") == "running":
                    task["last_progress_ts"] = time.time()
        self._save()

    # ---- 活动工作进程注册（区分「正在跑」与「僵尸」）----
    def mark_active(self, tid: str) -> None:
        self._active.add(tid)

    def mark_inactive(self, tid: str) -> None:
        self._active.discard(tid)

    async def reap_orphans(self) -> int:
        """回收孤儿/挂死任务：非终态任务若「本进程无活动 worker」或「长时间无任何进度推进」，
        标记为失败，避免僵尸任务永远停在 running。返回被回收的任务数。

        关键修正（2026-09-02）：
        旧逻辑仅凭 `tid in self._active` 判断是否存活；但 worker 挂死/被容器强杀时
        finally 未必执行，tid 会永远残留于 _active → reaper 直接跳过 → 永不回收。
        改为以「进度推进时间戳 last_progress_ts」为主判据：健康任务每轮规则/阶段都会
        update 刷新该时间戳，挂死任务的 last_progress_ts 停滞超过阈值即强制回收，
        不再受 _active 残留影响。
        """
        now = time.time()
        changed = False
        count = 0
        async with self._lock:
            for tid, task in self._tasks.items():
                if task.get("status") in _STATUS_TERMINAL:
                    continue
                age = now - float(task.get("created_at") or now)
                if age < 30:  # 规避刚创建尚未注册的竞态
                    continue
                last = task.get("last_progress_ts") or float(task.get("created_at") or now)
                in_active = tid in self._active
                fresh = (now - last) <= _REAP_STALE_SECS
                # 仅当「非本进程活动」且「近期有进度」才视为健康在跑；否则回收
                if in_active and fresh:
                    continue
                if in_active:
                    reason = "工作进程长时间无进度推进(可能挂死)"
                else:
                    reason = "任务无活动工作进程"
                task["status"] = "failed"
                task["error"] = (task.get("error") or "") + f"（{reason}，已自动标记失败；可重新发起）"
                task.setdefault("finished_at", now)
                changed = True
                count += 1
        if changed:
            self._save()
        return count

    async def _reaper_loop(self) -> None:
        """周期回收孤儿/挂死任务（兜底处理进程内 worker 被取消/崩溃/挂死但未更新状态的场景）。"""
        while True:
            await asyncio.sleep(60)
            try:
                n = await self.reap_orphans()
                if n:
                    logging.warning("孤儿任务回收器回收 %d 个僵尸任务", n)
            except Exception:  # noqa: BLE001 - 回收失败不应影响主流程
                pass

    def start_reaper(self) -> None:
        """在事件循环运行时（应用 lifespan）启动周期回收器，并持有任务引用防止被 GC。"""
        try:
            self._reaper_task = asyncio.ensure_future(self._reaper_loop())
            logging.info("孤儿任务回收器已启动")
        except Exception as exc:  # noqa: BLE001
            logging.warning("启动孤儿任务回收器失败: %s", exc)

    async def append_log(self, tid: str, text: str, level: str) -> None:
        import datetime

        async with self._lock:
            task = self._tasks.get(tid)
            if task:
                task["logs"].append(
                    {
                        "time": datetime.datetime.now().strftime("%H:%M:%S"),
                        "text": text,
                        "level": level,
                    }
                )
        self._save()

    async def set_cancel(self, tid: str) -> None:
        async with self._lock:
            event = self._cancel.get(tid)
            if event:
                event.set()

    async def is_cancelled(self, tid: str) -> bool:
        event = self._cancel.get(tid)
        return bool(event and event.is_set())

    async def remove(self, tid: str) -> None:
        async with self._lock:
            self._tasks.pop(tid, None)
            self._cancel.pop(tid, None)
        self._save()

    async def clear(self) -> None:
        async with self._lock:
            self._tasks.clear()
            self._cancel.clear()
        self._save()

    @staticmethod
    def is_terminal(status: str) -> bool:
        return status in _STATUS_TERMINAL


# 进程级单例：后端各模块共享同一 TaskStore 实例（含活动 worker 注册表与回收器）
_store_instance: "TaskStore | None" = None


def get_store() -> "TaskStore":
    global _store_instance
    if _store_instance is None:
        _store_instance = TaskStore()
    return _store_instance


def _fmt(ts: float | None) -> str | None:
    if not ts:
        return None
    import datetime

    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def to_summary(task: dict[str, Any]) -> dict[str, Any]:
    summary = task.get("summary")
    return {
        "task_id": task["task_id"],
        "status": task["status"],
        "created_at": _fmt(task.get("created_at")),
        "finished_at": _fmt(task.get("finished_at")),
        "user_id": task.get("user_id"),
        # epoch 秒时间戳：前端据此按浏览器本地时区格式化与计算时长，
        # 避免后端容器(UTC)与用户本地时区(GMT+8)不一致导致时间错位/时长偏差。
        "created_at_ts": task.get("created_at"),
        "finished_at_ts": task.get("finished_at"),
        "mode": task.get("mode", "bid"),
        "file_names": task.get("file_names", []),
        "file_types": task.get("file_types", []),
        "rule_group_ids": task.get("rule_group_ids", []),
        "auto_match": task.get("auto_match", False),
        "rule_count": task.get("rule_count", 0),
        "progress": task.get("progress", 0),
        "progress_message": task.get("progress_message", ""),
        "findings_count": len(task.get("findings", [])),
        "score": summary.get("score") if isinstance(summary, dict) else None,
        "error": task.get("error"),
    }


def to_detail(task: dict[str, Any]) -> dict[str, Any]:
    out = to_summary(task)
    out.update(
        {
        "request": task.get("request", {}),
        "logs": task.get("logs", []),
        "findings": task.get("findings", []),
        "consistency_issues": task.get("consistency_issues", []),
        "rule_results": task.get("rule_results", []),
        "kb_traces": task.get("kb_traces", []),
        "summary": task.get("summary"),
        "reflow_applied": task.get("reflow_applied", False),
        "replay_from_task_id": task.get("replay_from_task_id"),
        "version_manifest": task.get("version_manifest"),
    }
    )
    return out
