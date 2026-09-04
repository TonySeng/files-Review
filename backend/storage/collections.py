"""结构化「集合」存储驱动。

集合（collection）= 一份业务模块整存整取的 JSON 文档（如 prompts / tasks /
legal_rulesets）。统一 API：``read_all(name)`` / ``write_all(name, data)``。
业务模块不感知后端，切换驱动只需改存储配置。

驱动：
- ``json_file``：JSON 文件 + 原子写（历史行为，默认 sqlite 驱动下的集合实现）
- ``sqlite``：单表 ``storage_collections(collection_name, data)``
- ``mysql`` / ``postgresql``：同构表，标准 SQL，驱动库懒加载（pymysql / psycopg）
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_TABLE = "storage_collections"


class CollectionBackend(ABC):
    """集合存储后端接口。实现必须保证 write_all 的原子性（不落半截数据）。"""

    @abstractmethod
    def read_all(self, name: str) -> Any | None:
        """读取整个集合；不存在返回 None。"""

    @abstractmethod
    def write_all(self, name: str, data: Any) -> None:
        """整体覆写集合。"""

    def backend_id(self) -> str:
        """后端标识：热切换时用于判断「是否真的换了后端」（需迁写内存态）。"""
        return type(self).__name__

    def healthy(self) -> bool:
        return True


def _ensure_name(name: str) -> str:
    if not name or not name.replace("_", "").isalnum():
        raise ValueError(f"非法集合名: {name!r}")
    return name


# --------------------------------------------------------------------------- #
# JSON 文件驱动
# --------------------------------------------------------------------------- #
class JsonFileCollection(CollectionBackend):
    """每个集合一个 JSON 文件，原子写（先临时文件后 os.replace）。"""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def backend_id(self) -> str:  # noqa: D102
        return f"json:{self.data_dir.resolve().as_posix()}"

    def _path(self, name: str) -> Path:
        return self.data_dir / f"{_ensure_name(name)}.json"

    def read_all(self, name: str) -> Any | None:
        p = self._path(name)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.exception("集合 %s 读取失败（%s），按缺失处理", name, p)
            return None

    def write_all(self, name: str, data: Any) -> None:
        p = self._path(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, p)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


# --------------------------------------------------------------------------- #
# SQL 表驱动（sqlite / mysql / postgresql 共用表结构语义）
# --------------------------------------------------------------------------- #
class SqlCollectionBase(CollectionBackend):
    """``storage_collections`` 单表驱动，子类提供可执行连接。"""

    placeholder = "?"

    def _connect(self):
        raise NotImplementedError

    def _ensure_table(self, conn) -> None:
        raise NotImplementedError

    def read_all(self, name: str) -> Any | None:
        _ensure_name(name)
        conn = self._connect()
        try:
            ph = self.placeholder
            cur = conn.cursor()
            cur.execute(f"SELECT data FROM {_TABLE} WHERE collection_name = {ph}", (name,))
            row = cur.fetchone()
            if row is None:
                return None
            raw = row[0] if not isinstance(row, dict) else row.get("data")
            if isinstance(raw, (dict, list)):  # JSONB 驱动可能直接反序列化
                return raw
            return json.loads(raw)
        except Exception:
            logger.exception("集合 %s 从 %s 读取失败", name, type(self).__name__)
            raise
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def write_all(self, name: str, data: Any) -> None:
        _ensure_name(name)
        payload = json.dumps(data, ensure_ascii=False)
        conn = self._connect()
        try:
            self._ensure_table(conn)
            ph = self.placeholder
            cur = conn.cursor()
            cur.execute(
                f"SELECT 1 FROM {_TABLE} WHERE collection_name = {ph}", (name,)
            )
            exists = cur.fetchone() is not None
            if exists:
                cur.execute(
                    f"UPDATE {_TABLE} SET data = {ph}, updated_at = CURRENT_TIMESTAMP "
                    f"WHERE collection_name = {ph}",
                    (payload, name),
                )
            else:
                cur.execute(
                    f"INSERT INTO {_TABLE} (collection_name, data, updated_at) "
                    f"VALUES ({ph}, {ph}, CURRENT_TIMESTAMP)",
                    (name, payload),
                )
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            logger.exception("集合 %s 写入 %s 失败", name, type(self).__name__)
            raise
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


class SqliteCollection(SqlCollectionBase):
    """SQLite 表驱动（不依赖应用库内已有 .db，独立单表）。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ready = False

    def backend_id(self) -> str:  # noqa: D102
        return f"sqlite:{self.db_path.resolve().as_posix()}"

    def _connect(self):
        import sqlite3

        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        return conn

    def _ensure_table(self, conn) -> None:
        if self._ready:
            return
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {_TABLE} ("
            "collection_name TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at TEXT)"
        )
        conn.commit()
        self._ready = True


class MysqlCollection(SqlCollectionBase):
    placeholder = "%s"

    def __init__(self, sect: dict[str, Any]) -> None:
        self.sect = dict(sect)
        self._ready = False

    def backend_id(self) -> str:  # noqa: D102
        return f"mysql:{self.sect.get('database')}"

    def _connect(self):
        import pymysql

        return pymysql.connect(
            host=self.sect["host"],
            port=int(self.sect.get("port", 3306)),
            user=self.sect["user"],
            password=self.sect.get("password", ""),
            database=self.sect["database"],
            charset=self.sect.get("charset", "utf8mb4"),
            connect_timeout=int(self.sect.get("connect_timeout", 10)),
            autocommit=False,
        )

    def _ensure_table(self, conn) -> None:
        if self._ready:
            return
        cur = conn.cursor()
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {_TABLE} ("
            "collection_name VARCHAR(64) PRIMARY KEY, "
            "data LONGTEXT NOT NULL, updated_at DATETIME NULL"
            ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
        )
        conn.commit()
        self._ready = True


class PostgresCollection(SqlCollectionBase):
    placeholder = "%s"

    def __init__(self, sect: dict[str, Any]) -> None:
        self.sect = dict(sect)
        self._ready = False

    def backend_id(self) -> str:  # noqa: D102
        return f"postgresql:{self.sect.get('database')}"

    def _connect(self):
        try:
            import psycopg
        except ImportError:  # psycopg2 兼容
            import psycopg2 as psycopg  # type: ignore

        return psycopg.connect(
            host=self.sect["host"],
            port=int(self.sect.get("port", 5432)),
            user=self.sect["user"],
            password=self.sect.get("password", ""),
            dbname=self.sect["database"],
            connect_timeout=int(self.sect.get("connect_timeout", 10)),
        )

    def _ensure_table(self, conn) -> None:
        if self._ready:
            return
        cur = conn.cursor()
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {_TABLE} ("
            "collection_name VARCHAR(64) PRIMARY KEY, "
            "data JSONB NOT NULL, updated_at TIMESTAMP NULL)"
        )
        conn.commit()
        self._ready = True


def build_collection_backend(structured_cfg: dict[str, Any]) -> CollectionBackend:
    """按 structured 配置段构建集合后端。驱动库缺失时抛 StorageConfigError。"""
    driver = str(structured_cfg.get("driver", "sqlite")).lower()
    if driver == "sqlite":
        data_dir = Path(str(structured_cfg["sqlite"]["data_dir"]))
        # sqlite 驱动下集合仍走 JSON 文件（历史行为等价、零迁移）；
        # 如需把集合也放进 sqlite 表，把 collections_store 设为 "table"。
        if str(structured_cfg.get("collections_store", "json")).lower() == "table":
            return SqliteCollection(data_dir / "storage_collections.db")
        return JsonFileCollection(data_dir)
    if driver == "mysql":
        from .settings import StorageConfigError

        try:
            import pymysql  # noqa: F401
        except ImportError as exc:
            raise StorageConfigError(
                "mysql 驱动需要 PyMySQL：pip install pymysql"
            ) from exc
        return MysqlCollection(structured_cfg["mysql"])
    if driver == "postgresql":
        from .settings import StorageConfigError

        try:
            import psycopg  # noqa: F401
        except ImportError:
            try:
                import psycopg2  # noqa: F401
            except ImportError as exc:
                raise StorageConfigError(
                    "postgresql 驱动需要 psycopg（或 psycopg2）：pip install psycopg[binary]"
                ) from exc
        return PostgresCollection(structured_cfg["postgresql"])
    from .settings import StorageConfigError

    raise StorageConfigError(f"未知结构化驱动: {driver}")
