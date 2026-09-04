# -*- coding: utf-8 -*-
"""API 与 AI(LLM / 知识库) 调用审计：落盘到持久卷 SQLite，容器重启不丢，可供历史查询。

解决的问题：原先「今天有哪些接口被调用过 / AI 调用是否超时或 429」只能靠瞬时的 docker logs，
容器一重启或日志滚动就再也查不到。本模块把所有业务请求与每一次大模型/知识库调用
（方法、路径、耗时、状态码、错误信息、prompt 体量、重试次数、归属 task/rule）持久化，
并提供查询接口，让「调用历史」成为可回放的审计数据。

写入使用同步 sqlite3 + 线程锁，调用方通过 asyncio.to_thread 提交，避免阻塞事件循环。
所有写操作对异常一律吞掉，绝不影响主流程（审计失败不能拖垮审核）。
"""
from __future__ import annotations

import sqlite3
import threading
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from .. import config
from .. import storage

DB_PATH = Path(str(config.DATA_DIR)) / "audit_calls.db"
_lock = threading.Lock()

# 调用上下文：审核引擎在批次处理时设置，用于把 AI 调用归因到具体 task / rule。
_ctx_task_id: ContextVar[str | None] = ContextVar("audit_task_id", default=None)
_ctx_rule_id: ContextVar[str | None] = ContextVar("audit_rule_id", default=None)

# 行数上限：超过后裁剪最旧记录，避免无限增长拖垮磁盘/查询。
_MAX_ROWS = 500_000
_PRUNE_EVERY = 2000  # 每写入约 2000 行触发一次裁剪检查
_prune_counter = 0


def set_context(*, task_id: str | None = None, rule_id: str | None = None) -> None:
    if task_id is not None:
        _ctx_task_id.set(task_id)
    if rule_id is not None:
        _ctx_rule_id.set(rule_id)


def current_task() -> str | None:
    return _ctx_task_id.get()


def current_rule() -> str | None:
    return _ctx_rule_id.get()


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: list[tuple[str, str]]) -> None:
    """幂等地补齐缺失列（SQLite 不支持 ADD COLUMN IF NOT EXISTS）。

    用于兼容旧版本已存在的表：建表语句随代码演进新增了归属列（user_id 等），
    但 CREATE TABLE IF NOT EXISTS 不会给已有表补列，会导致 INSERT 静默失败、
    审计日志整体停写。这里按需 ALTER。
    """
    try:
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return
    for col, ctype in columns:
        if col not in existing:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ctype}")
            except Exception:
                pass


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = storage.connect_relational("audit", DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS api_calls ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, req_id TEXT,"
        "method TEXT, path TEXT, status INTEGER, duration_ms REAL,"
        "client TEXT, has_error INTEGER, resp_size INTEGER,"
        "user_id TEXT, api_key_prefix TEXT, role TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS ai_calls ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT,"
        "model TEXT, base_url TEXT, prompt_chars INTEGER, resp_chars INTEGER,"
        "duration_ms REAL, status TEXT, error TEXT, retries INTEGER,"
        "task_id TEXT, rule_id TEXT)"
    )
    # 兼容旧表：补齐归属列（如用户维度 user_id 等），避免 INSERT 因缺列静默失败。
    _ensure_columns(
        conn,
        "api_calls",
        [("user_id", "TEXT"), ("api_key_prefix", "TEXT"), ("role", "TEXT")],
    )
    _ensure_columns(
        conn,
        "ai_calls",
        [("task_id", "TEXT"), ("rule_id", "TEXT")],
    )
    conn.commit()
    return conn


def _maybe_prune(table: str) -> None:
    global _prune_counter
    _prune_counter += 1
    if _prune_counter % _PRUNE_EVERY != 0:
        return
    try:
        conn = _conn()
        try:
            cur = conn.execute(f"SELECT COUNT(*) FROM {table}")
            n = cur.fetchone()[0]
            if n > _MAX_ROWS:
                surplus = n - _MAX_ROWS
                conn.execute(
                    f"DELETE FROM {table} WHERE id IN ("
                    f"SELECT id FROM {table} ORDER BY id ASC LIMIT ?)",
                    (surplus,),
                )
                conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def log_api_call(
    *,
    ts: float,
    req_id: str,
    method: str,
    path: str,
    status: int,
    duration_ms: float,
    client: str,
    has_error: bool = False,
    resp_size: int | None = None,
    user_id: str | None = None,
    role: str | None = None,
    api_key_prefix: str | None = None,
) -> None:
    try:
        with _lock:
            conn = _conn()
            try:
                conn.execute(
                    "INSERT INTO api_calls"
                    "(ts, req_id, method, path, status, duration_ms, client, has_error, resp_size, user_id, api_key_prefix, role)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        ts,
                        req_id,
                        method,
                        path,
                        status,
                        round(duration_ms, 2),
                        client,
                        int(bool(has_error)),
                        resp_size,
                        user_id,
                        api_key_prefix,
                        role,
                    ),
                )
                conn.commit()
                _maybe_prune("api_calls")
            finally:
                conn.close()
    except Exception:
        pass


def log_ai_call(
    *,
    kind: str,
    model: str,
    base_url: str,
    prompt_chars: int,
    resp_chars: int,
    duration_ms: float,
    status: str,
    error: str | None = None,
    retries: int = 0,
) -> None:
    try:
        with _lock:
            conn = _conn()
            try:
                conn.execute(
                    "INSERT INTO ai_calls"
                    "(ts, kind, model, base_url, prompt_chars, resp_chars, duration_ms, status, error, retries, task_id, rule_id)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        __import__("time").time(),
                        kind,
                        model,
                        base_url,
                        prompt_chars,
                        resp_chars,
                        round(duration_ms, 2),
                        status,
                        (error or "")[:500],
                        retries,
                        current_task(),
                        current_rule(),
                    ),
                )
                conn.commit()
                _maybe_prune("ai_calls")
            finally:
                conn.close()
    except Exception:
        pass


def get_api_calls(
    *,
    limit: int = 200,
    offset: int = 0,
    path_filter: str | None = None,
    since: float | None = None,
    only_error: bool = False,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    try:
        with _lock:
            conn = _conn()
            try:
                sql = "SELECT id, ts, req_id, method, path, status, duration_ms, client, has_error, resp_size, user_id, api_key_prefix, role FROM api_calls WHERE 1=1"
                args: list[Any] = []
                if path_filter:
                    sql += " AND path LIKE ?"
                    args.append(f"%{path_filter}%")
                if since is not None:
                    sql += " AND ts >= ?"
                    args.append(since)
                if only_error:
                    sql += " AND has_error = 1"
                if user_id:
                    sql += " AND user_id = ?"
                    args.append(user_id)
                sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
                args.extend([limit, offset])
                cur = conn.execute(sql, args)
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
            finally:
                conn.close()
    except Exception:
        return []


def get_ai_calls(
    *,
    kind: str | None = None,
    limit: int = 200,
    offset: int = 0,
    since: float | None = None,
    only_error: bool = False,
) -> list[dict[str, Any]]:
    try:
        with _lock:
            conn = _conn()
            try:
                sql = "SELECT id, ts, kind, model, base_url, prompt_chars, resp_chars, duration_ms, status, error, retries, task_id, rule_id FROM ai_calls WHERE 1=1"
                args: list[Any] = []
                if kind:
                    sql += " AND kind = ?"
                    args.append(kind)
                if since is not None:
                    sql += " AND ts >= ?"
                    args.append(since)
                if only_error:
                    sql += " AND status = 'error'"
                sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
                args.extend([limit, offset])
                cur = conn.execute(sql, args)
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
            finally:
                conn.close()
    except Exception:
        return []


def api_call_stats(since: float | None = None, user_id: str | None = None) -> dict[str, Any]:
    try:
        with _lock:
            conn = _conn()
            try:
                cond = " WHERE 1=1"
                args: list[Any] = []
                if since is not None:
                    cond += " AND ts >= ?"
                    args.append(since)
                if user_id:
                    cond += " AND user_id = ?"
                    args.append(user_id)
                total = conn.execute(f"SELECT COUNT(*) FROM api_calls{cond}", args).fetchone()[0]
                errs = conn.execute(
                    f"SELECT COUNT(*) FROM api_calls{cond} AND has_error = 1", args
                ).fetchone()[0]
                slow = conn.execute(
                    f"SELECT COUNT(*) FROM api_calls{cond} AND duration_ms > 5000", args
                ).fetchone()[0]
                avg = conn.execute(
                    f"SELECT AVG(duration_ms) FROM api_calls{cond}", args
                ).fetchone()[0]
                return {
                    "total": total,
                    "errors": errs,
                    "slow_gt_5s": slow,
                    "avg_duration_ms": round(avg, 2) if avg is not None else 0.0,
                }
            finally:
                conn.close()
    except Exception:
        return {"total": 0, "errors": 0, "slow_gt_5s": 0, "avg_duration_ms": 0.0}


def ai_call_stats(since: float | None = None) -> dict[str, Any]:
    try:
        with _lock:
            conn = _conn()
            try:
                cond = " WHERE ts >= ?" if since is not None else ""
                args = [since] if since is not None else []
                total = conn.execute(f"SELECT COUNT(*) FROM ai_calls{cond}", args).fetchone()[0]
                errs = conn.execute(
                    f"SELECT COUNT(*) FROM ai_calls{cond} AND status = 'error'", args
                ).fetchone()[0]
                avg = conn.execute(
                    f"SELECT AVG(duration_ms) FROM ai_calls{cond}", args
                ).fetchone()[0]
                return {
                    "total": total,
                    "errors": errs,
                    "avg_duration_ms": round(avg, 2) if avg is not None else 0.0,
                }
            finally:
                conn.close()
    except Exception:
        return {"total": 0, "errors": 0, "avg_duration_ms": 0.0}
