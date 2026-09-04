"""统一存储层测试：配置加载/校验、集合多后端、热加载切换、文件目录、方言翻译。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS  {name}")
    else:
        FAIL += 1
        print(f"FAIL  {name}  {extra}")


# ---------------------------------------------------------------- 1) 配置默认值
from backend.storage import collections as st_collections
from backend.storage import relational as st_relational
from backend.storage import registry as st_registry
from backend.storage.settings import StorageConfigError
import importlib

st_settings = importlib.import_module("backend.storage.settings")  # 包内实例遮蔽子模块，须绕行

cfg = st_settings.settings.cfg
check("默认驱动 sqlite", st_settings.settings.structured_driver == "sqlite")
check("默认文件目录 = DATA_DIR", st_settings.settings.files_base_dir == Path(
    os.getenv("BCR_DATA_DIR", str(Path("backend/data"))))
    or st_settings.settings.files_base_dir.name == "data", str(st_settings.settings.files_base_dir))
check("上传目录 = base_dir/uploads", st_registry.upload_dir().name == "uploads")

# ---------------------------------------------------------------- 2) 非法配置拒绝
with tempfile.TemporaryDirectory() as td:
    bad = Path(td) / "bad.json"
    bad.write_text(json.dumps({"structured": {"driver": "mysql"}}), encoding="utf-8")
    try:
        st_settings.load_and_validate(bad)
        check("缺必填项的 mysql 配置被拒绝", False)
    except StorageConfigError:
        check("缺必填项的 mysql 配置被拒绝", True)

# ---------------------------------------------------------------- 3) 集合后端往返
with tempfile.TemporaryDirectory() as td:
    j = st_collections.JsonFileCollection(Path(td))
    j.write_all("demo", {"a": 1, "列表": [1, 2]})
    check("json 后端往返", j.read_all("demo") == {"a": 1, "列表": [1, 2]})
    check("json 后端缺失返回 None", j.read_all("nope") is None)

    s = st_collections.SqliteCollection(Path(td) / "coll.db")
    s.write_all("demo", {"a": 1})
    s.write_all("demo", {"a": 2})  # 覆写
    check("sqlite 表后端往返+覆写", s.read_all("demo") == {"a": 2})
    s.write_all("listcol", [1, 2, 3])
    check("sqlite 表后端存列表", s.read_all("listcol") == [1, 2, 3])

    # mysql/pg 集合类构造与 SQL 形状（不实际连接）
    check("mysql 集合占位符", st_collections.MysqlCollection({"host": "x"}).placeholder == "%s")
    check("pg 集合占位符", st_collections.PostgresCollection({"host": "x"}).placeholder == "%s")

# ---------------------------------------------------------------- 4) 方言翻译
cur = st_relational._Cursor.__new__(st_relational._Cursor)
cur._dialect = "mysql"
t = cur._translate(
    "INSERT OR REPLACE INTO review_audit(id, ts) VALUES (?, ?)", (1, 2.0)
)
check("INSERT OR REPLACE → REPLACE INTO (mysql)", t[0].startswith("REPLACE INTO review_audit") and "%s" in t[0], t[0] if t else "None")

cur._dialect = "postgresql"
try:
    cur._translate("INSERT OR REPLACE INTO t(a) VALUES (?)", (1,))
    check("PG 下 INSERT OR REPLACE 明确报错", False)
except st_relational.RelationalError:
    check("PG 下 INSERT OR REPLACE 明确报错", True)

cur._dialect = "mysql"
t = cur._translate("PRAGMA journal_mode=WAL", ())
check("PRAGMA journal_mode 跳过", t is None)
t = cur._translate("PRAGMA table_info(feedback_records)", ())
check("PRAGMA table_info → information_schema", "information_schema.columns" in t[0], t[0] if t else "None")
t = cur._translate("CREATE TABLE x(id INTEGER PRIMARY KEY AUTOINCREMENT, n TEXT)", ())
check("AUTOINCREMENT → AUTO_INCREMENT", "AUTO_INCREMENT" in t[0], t[0] if t else "None")
t = cur._translate("CREATE TABLE x(id TEXT PRIMARY KEY)", ())
check("TEXT PRIMARY KEY → VARCHAR(191)", "VARCHAR(191) PRIMARY KEY" in t[0], t[0] if t else "None")
t = cur._translate("SELECT * FROM t WHERE a = ? AND b = ?", (1, 2))
check("占位符 ? → %s", t[0].count("%s") == 2 and t[1] == (1, 2))

# HybridRow：键/位置双访问
row = st_relational.HybridRow({"name": "a", "type": "text"}, ("a", "text"))
check("HybridRow 键访问", row["name"] == "a")
check("HybridRow 位置访问", row[1] == "text")
check("HybridRow 迭代=值", list(row) == ["a", "text"])

# ---------------------------------------------------------------- 5) 热加载整体切换
switch_seen: list[tuple[str, str]] = []
st_registry.on_structured_switch(lambda o, n: switch_seen.append((o, n)))

with tempfile.TemporaryDirectory() as td:
    conf = Path(td) / "storage_config.json"
    os.environ["BCR_STORAGE_CONFIG"] = str(conf)
    st_settings.settings._mtime = None  # 强制下次轮询重读
    old_collection = st_registry.collection()

    # 5a. 无配置文件 → 保持默认
    check("无配置文件不切换", st_registry.collection() is old_collection)

    # 5b. 写入合法新配置（集合入 sqlite 表）→ 轮询后切换
    conf.write_text(json.dumps({
        "structured": {"driver": "sqlite", "collections_store": "table",
                       "sqlite": {"data_dir": str(Path(td) / "d1")}},
        "files": {"driver": "local", "local": {"base_dir": str(Path(td) / "files_root")}},
    }), encoding="utf-8")
    changed = st_settings.settings.reload_if_changed()
    check("热加载检测到变更", changed)
    new_backend = st_registry.collection()
    check("切换到 sqlite 表后端", isinstance(new_backend, st_collections.SqliteCollection),
          type(new_backend).__name__)
    check("切换回调触发", ("sqlite", "sqlite") in switch_seen or len(switch_seen) >= 1, str(switch_seen))
    check("上传目录随配置热切换", str(st_registry.upload_dir()).replace("\\", "/").endswith("files_root/uploads"),
          str(st_registry.upload_dir()))

    # 5c. 写坏配置 → 沿用旧配置并记录错误
    conf.write_text("{ broken json", encoding="utf-8")
    st_settings.settings._mtime = None
    changed = st_settings.settings.reload_if_changed()
    check("坏配置不切换", not changed and st_registry.collection() is new_backend)
    check("坏配置记录 last_error", bool(st_settings.settings.last_error))

    # 5d. 集合数据经新后端可读写（业务透传）
    st_registry.write_collection("test_coll", {"k": "v"})
    check("切换后集合读写正常", st_registry.read_collection("test_coll") == {"k": "v"})

# ---------------------------------------------------------------- 6) 还原默认配置
os.environ.pop("BCR_STORAGE_CONFIG", None)
st_settings.settings._mtime = None
st_settings.settings.reload_if_changed()
check("还原默认后为 JSON 文件后端", isinstance(st_registry.collection(), st_collections.JsonFileCollection),
      type(st_registry.collection()).__name__)

# ---------------------------------------------------------------- 7) 业务模块经存储层可用
from backend.services import prompt_store, legal_rules, task_store, file_store  # noqa: E402
from backend.services import users, sessions, feedback_store  # noqa: E402

check("prompts 集合可读", isinstance(prompt_store._items, dict))
check("legal_rulesets 集合可读", isinstance(legal_rules._items, dict))
check("users 集合可读", isinstance(users._load(), dict))
check("sessions 集合可读", isinstance(sessions._load(), dict))
check("feedback 连接（sqlite 驱动直连）", feedback_store._conn() is not None)

import backend.main  # noqa: E402,F401

check("backend.main 全量导入", True)

# ---------------- 11) 防回归：回调不得把变量名字符串当数据写入 ----------------
import re as _re
import glob as _glob


def test_no_string_literal_collection_writes():
    """迁移曾把 write_collection(name, '_items') 写成字符串字面量，热切换时
    会用 8 字节垃圾覆盖真实集合数据（2026-09-04 事故）。静态扫描防止复发。"""
    bad = []
    for path in _glob.glob("backend/services/*.py"):
        src = open(path, encoding="utf-8").read()
        for m in _re.finditer(r"write_collection\(\s*['\"](\w+)['\"]\s*,\s*['\"](_\w+)['\"]\s*\)", src):
            bad.append(f"{path}: write_collection('{m.group(1)}', '{m.group(2)}')")
    assert not bad, f"发现字符串字面量集合写入(应为变量): {bad}"


try:
    test_no_string_literal_collection_writes()
    check("静态扫描：无字符串字面量集合写入", True)
except AssertionError as exc:
    check("静态扫描：无字符串字面量集合写入", False, str(exc))

print(f"\nRESULT: PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
