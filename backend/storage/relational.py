"""关系数据库连接工厂与方言适配。

业务模块通过 ``connect(logical_name, sqlite_path)`` 获取连接：
- 驱动为 ``sqlite``（默认）：直接连接 ``sqlite_path``（与历史行为完全一致，
  模块原有的 BCR_* 路径环境变量继续生效）。
- 驱动为 ``mysql`` / ``postgresql``：按逻辑库名（main/feedback/audit/reviewdata）
  在目标库中使用 ``t_<逻辑名>`` 前缀的同构表，并通过方言翻译层转换存量 SQL
  （``?`` 占位符、``PRAGMA``、``AUTOINCREMENT``、``TEXT PRIMARY KEY``、
  ``INSERT OR REPLACE``）。

返回的连接包装器对齐 sqlite3.Connection 的存量用法子集：
``execute / executemany / cursor / commit / rollback / close / row_factory``，
游标行对象同时支持 ``row["col"]`` 与 ``row[0]`` 两种取值方式。

⚠️ MySQL/PostgreSQL 适配为标准 SQL 实现，在本开发环境（无 MySQL/PG 实例）
未做集成实测；切换驱动前请先在测试环境验证。
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: 逻辑库名 → 表前缀（映射到外部 RDB 中的表）
_LOGICAL_TABLES = {"main": "t_main", "feedback": "t_feedback", "audit": "t_audit", "reviewdata": "t_reviewdata"}

# sqlite → 目标方言 的 DDL 片段翻译
_RE_AUTOINCREMENT = re.compile(r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", re.I)
_RE_TEXT_PK = re.compile(r"\bTEXT\s+PRIMARY\s+KEY\b", re.I)
_RE_INSERT_OR_REPLACE = re.compile(r"\bINSERT\s+OR\s+REPLACE\s+INTO\b", re.I)
_RE_PRAGMA_TABLE_INFO = re.compile(r"\bPRAGMA\s+table_info\(\s*([A-Za-z_][\w]*)\s*\)", re.I)
_RE_PRAGMA_IGNORE = re.compile(r"\bPRAGMA\s+(journal_mode|foreign_keys|synchronous)\b", re.I)

# PRAGMA table_info 模拟查询：列顺序对齐 sqlite3（cid,name,type,notnull,dflt,pk），
# 其中 notnull/pk 用布尔语义，位置访问 r[1] 取列名与存量代码兼容。
_INFO_SQL = {
    "mysql": (
        "SELECT 0 AS cid, COLUMN_NAME AS name, DATA_TYPE AS type, "
        "IF(IS_NULLABLE='NO',1,0) AS notnull, COLUMN_DEFAULT AS dflt_value, "
        "IF(COLUMN_KEY='PRI',1,0) AS pk FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = {ph} ORDER BY ORDINAL_POSITION"
    ),
    "postgresql": (
        "SELECT 0 AS cid, column_name AS name, data_type AS type, "
        "CASE WHEN is_nullable='NO' THEN 1 ELSE 0 END AS notnull, "
        "column_default AS dflt_value, "
        "CASE WHEN tc.constraint_type='PRIMARY KEY' THEN 1 ELSE 0 END AS pk "
        "FROM information_schema.columns c "
        "LEFT JOIN information_schema.key_column_usage kcu "
        "  ON kcu.table_schema=c.table_schema AND kcu.table_name=c.table_name "
        " AND kcu.column_name=c.column_name "
        "LEFT JOIN information_schema.table_constraints tc "
        "  ON tc.constraint_name=kcu.constraint_name AND tc.table_name=c.table_name "
        " AND tc.constraint_type='PRIMARY KEY' "
        "WHERE c.table_name = {ph} ORDER BY c.ordinal_position"
    ),
}


class RelationalError(RuntimeError):
    """关系库适配错误。"""


class HybridRow(dict):
    """同时支持 ``row["col"]``、``row[0]`` 与按值迭代的行对象。"""

    def __init__(self, mapping: dict[str, Any] | None, values: tuple | list) -> None:
        super().__init__(mapping or {})
        self._values = tuple(values)

    def __getitem__(self, key):  # noqa: D105
        if isinstance(key, (int, slice)) or (isinstance(key, str) and key not in self):
            return self._values[key]
        return super().__getitem__(key)

    def __iter__(self):  # noqa: D105
        return iter(self._values)

    def __len__(self) -> int:  # noqa: D105
        return len(self._values)

    def keys(self):  # noqa: D105
        return super().keys()


class _Cursor:
    """游标包装：翻译 SQL、包装行为 HybridRow。"""

    def __init__(self, raw, dialect: str) -> None:
        self._raw = raw
        self._dialect = dialect
        self.lastrowid = getattr(raw, "lastrowid", None)
        self.rowcount = getattr(raw, "rowcount", -1)
        self.description = getattr(raw, "description", None)

    # -- SQL 翻译 -----------------------------------------------------------
    def _translate(self, sql: str, params: Any) -> tuple[str, Any] | None:
        if _RE_PRAGMA_IGNORE.search(sql):
            return None  # sqlite 专属开关，其他方言直接跳过
        m = _RE_PRAGMA_TABLE_INFO.search(sql)
        if m:
            table = m.group(1)
            info = _INFO_SQL[self._dialect].format(ph="%s")
            return info, (table,)
        out = _RE_AUTOINCREMENT.sub(
            "INT PRIMARY KEY AUTO_INCREMENT" if self._dialect == "mysql" else "BIGSERIAL PRIMARY KEY",
            sql,
        )
        out = _RE_TEXT_PK.sub("VARCHAR(191) PRIMARY KEY", out)
        if _RE_INSERT_OR_REPLACE.search(out):
            if self._dialect == "mysql":
                out = _RE_INSERT_OR_REPLACE.sub("REPLACE INTO", out)
            else:
                raise RelationalError(
                    "PostgreSQL 适配暂不支持 INSERT OR REPLACE（无法推导冲突键）；"
                    "请改用显式 UPSERT 或将该逻辑库保留在 sqlite 驱动下"
                )
        if self._dialect != "sqlite":
            out = out.replace("?", "%s")
        return out, params

    def _wrap_rows(self, rows: Any) -> Any:
        if rows is None:
            return None
        desc = self.description
        names = [d[0] for d in desc] if desc else []
        wrapped = []
        for row in rows:
            if isinstance(row, dict):
                wrapped.append(HybridRow(row, tuple(row.values())))
            else:
                wrapped.append(HybridRow(dict(zip(names, row)) if names else None, tuple(row)))
        return wrapped

    def execute(self, sql: str, params: Any = ()) -> "_Cursor":
        t = self._translate(sql, params)
        if t is None:
            self.rowcount = -1
            return self
        sql_t, params_t = t
        self._raw.execute(sql_t, params_t)
        self.lastrowid = getattr(self._raw, "lastrowid", None)
        self.rowcount = getattr(self._raw, "rowcount", -1)
        self.description = getattr(self._raw, "description", None)
        return self

    def executemany(self, sql: str, seq: Any) -> "_Cursor":
        t = self._translate(sql, None)
        if t is not None:
            self._raw.executemany(t[0], seq)
        return self

    def fetchone(self) -> Any:
        return self._wrap_one(self._raw.fetchone())

    def fetchall(self) -> list[Any]:
        return self._wrap_rows(self._raw.fetchall())

    def fetchmany(self, size: int = 1) -> list[Any]:
        return self._wrap_rows(self._raw.fetchmany(size))

    def _wrap_one(self, row: Any) -> Any:
        if row is None:
            return None
        wrapped = self._wrap_rows([row])
        return wrapped[0]

    def close(self) -> None:
        try:
            self._raw.close()
        except Exception:  # noqa: BLE001
            pass

    def __iter__(self):  # noqa: D105
        for row in self._raw:
            yield self._wrap_one(row)

    # sqlite3 属性兼容（少量代码可能读）
    @property
    def connection(self):  # noqa: D105
        return self._raw


class _RdbConnection:
    """MySQL/PG 连接包装器，暴露 sqlite3 风格接口。"""

    def __init__(self, raw_conn, dialect: str) -> None:
        self._raw = raw_conn
        self._dialect = dialect
        self.row_factory = None  # 兼容赋值，无实际作用（行包装始终启用）

    def cursor(self) -> _Cursor:  # noqa: D102
        return _Cursor(self._raw.cursor(), self._dialect)

    def execute(self, sql: str, params: Any = ()) -> _Cursor:  # noqa: D102
        return self.cursor().execute(sql, params)

    def executemany(self, sql: str, seq: Any) -> _Cursor:  # noqa: D102
        return self.cursor().executemany(sql, seq)

    def commit(self) -> None:  # noqa: D102
        self._raw.commit()

    def rollback(self) -> None:  # noqa: D102
        self._raw.rollback()

    def close(self) -> None:  # noqa: D102
        try:
            self._raw.close()
        except Exception:  # noqa: BLE001
            pass

    @property
    def in_transaction(self) -> bool:  # noqa: D105
        return bool(getattr(self._raw, "in_transaction", False))


def connect_rdb(logical: str, sect: dict[str, Any]) -> _RdbConnection:
    """按逻辑库名建立外部 RDB 连接。"""
    prefix = _LOGICAL_TABLES.get(logical)
    if not prefix:
        raise RelationalError(f"未知逻辑库名: {logical!r}（可选 {sorted(_LOGICAL_TABLES)}）")
    driver = str(sect.get("driver", "sqlite")).lower()
    if driver == "mysql":
        import pymysql

        cfg = sect["mysql"]
        raw = pymysql.connect(
            host=cfg["host"],
            port=int(cfg.get("port", 3306)),
            user=cfg["user"],
            password=cfg.get("password", ""),
            database=cfg["database"],
            charset=cfg.get("charset", "utf8mb4"),
            connect_timeout=int(cfg.get("connect_timeout", 10)),
            autocommit=False,
        )
        return _RdbConnection(raw, "mysql")
    if driver == "postgresql":
        try:
            import psycopg
        except ImportError:
            import psycopg2 as psycopg  # type: ignore

        cfg = sect["postgresql"]
        raw = psycopg.connect(
            host=cfg["host"],
            port=int(cfg.get("port", 5432)),
            user=cfg["user"],
            password=cfg.get("password", ""),
            dbname=cfg["database"],
            connect_timeout=int(cfg.get("connect_timeout", 10)),
        )
        return _RdbConnection(raw, "postgresql")
    raise RelationalError(f"外部关系驱动不支持: {driver}")


# --------------------------------------------------------------------------- #
# 对外统一入口
# --------------------------------------------------------------------------- #
def connect(logical: str, sqlite_path: Path, structured_cfg: dict[str, Any]) -> Any:
    """统一连接入口。

    - sqlite 驱动：直连 ``sqlite_path``（模块既有行为，含 Row 工厂等）；
    - mysql/postgresql：走方言适配（逻辑表 ``t_<logical>``）。
    """
    driver = str(structured_cfg.get("driver", "sqlite")).lower()
    if driver == "sqlite":
        p = Path(sqlite_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(str(p), timeout=10.0)
    return connect_rdb(logical, structured_cfg)


_ensure_lock = threading.Lock()
_ensured: set[str] = set()


def ensure_schema(conn: Any, logical: str, driver: str) -> None:
    """外部 RDB 下确保逻辑库默认结构存在（sqlite 驱动由各模块自建，跳过）。"""
    if driver == "sqlite":
        return
    key = f"{driver}:{logical}"
    with _ensure_lock:
        if key in _ensured:
            return
        logger.info("外部关系驱动 %s：逻辑库 %s 使用同构表结构（由各模块首次访问时自建）", driver, logical)
        _ensured.add(key)
