# -*- coding: utf-8 -*-
"""审核数据管理存储层（SQLite）。

职责：完整保留每一次审核任务的「审核文件(MD5) + 审核规则 + 审核结果(采纳/不采纳及理由)」
关联记录，支撑：
- 历史审核数据查询与关联记录维护；
- 历史结果回流：新建任务时若所传文件 MD5 与送审规则对应关系已存在，则自动把历史
  采纳/不采纳结论(含不采纳理由)带入模型处理，使输出与历史判定保持一致；
- 一致性校验：重新比对历史 finding 对应的规则是否仍适用（规则内容变更/移除时标记过期）。

与 feedback_store（错别字校验集、按 rule_id）相互独立：本模块是「按 文件MD5 + 规则组」
的关联档案，覆盖全部规则的审核结果，不局限于错别字。

数据库位于 backend/data/reviewdata.db（与任务/配置同卷，容器重建不丢）。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .. import storage  # noqa: E402  # 统一存储层

DB_PATH = Path(
    os.getenv(
        "BCR_REVIEWDATA_DB_PATH",
        str(Path(__file__).parent.parent / "data" / "reviewdata.db"),
    )
)

_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# 指纹计算
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    import datetime

    return datetime.datetime.now().isoformat(timespec="seconds")


def file_group_fingerprint(md5s: list[str]) -> str:
    """文件组指纹：对全部文件 MD5 排序后整体哈希，代表「这批文件内容」的不变身份。"""
    base = "|".join(sorted(md5s))
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:24]


def rule_group_fingerprint(rule_ids: list[str]) -> str:
    """规则组指纹：对全部送审规则 id 排序后整体哈希，代表「这套审核规则」的不变身份。"""
    base = "|".join(sorted(rule_ids))
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:24]


def finding_fingerprint(rule_id: str, content_key: str) -> str:
    """单条 finding 的稳定标识：同规则 + 同结论内容指纹 → 跨任务可对齐。"""
    base = f"{rule_id}|{content_key}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# 连接 / 初始化
# --------------------------------------------------------------------------- #
def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = storage.connect_relational("reviewdata", DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _init(conn)
    return conn


def _init(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS review_files (
            md5          TEXT PRIMARY KEY,
            filename     TEXT,
            size         INTEGER,
            ext          TEXT,
            role         TEXT,
            first_seen   TEXT,
            last_seen    TEXT,
            task_count   INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS review_records (
            id               TEXT PRIMARY KEY,
            file_group_fp    TEXT,
            rule_group_fp    TEXT,
            file_md5s        TEXT,        -- JSON list[str]
            file_names       TEXT,        -- JSON list[str]
            rule_ids         TEXT,        -- JSON list[str]
            rule_names       TEXT,        -- JSON list[str]
            task_id          TEXT,        -- 最近一次归档的任务
            finding_count    INTEGER DEFAULT 0,
            adopt_count      INTEGER DEFAULT 0,
            reject_count     INTEGER DEFAULT 0,
            created_at       TEXT,
            updated_at       TEXT
        );

        CREATE TABLE IF NOT EXISTS review_decisions (
            id                TEXT PRIMARY KEY,
            record_id         TEXT,
            file_md5s         TEXT,        -- JSON list[str]
            rule_id           TEXT,
            rule_name         TEXT,
            finding_fp        TEXT,        -- finding_fingerprint
            field             TEXT,
            status            TEXT,
            severity          TEXT,
            title             TEXT,
            detail            TEXT,
            evidence          TEXT,
            suggestion        TEXT,
            historical_judgment TEXT,     -- adopt / reject / NULL(未判定)
            reject_reason     TEXT,
            source            TEXT,        -- manual(用户点击) / auto(回流默认)
            created_at        TEXT,
            updated_at        TEXT,
            UNIQUE(record_id, rule_id, finding_fp)
        );

        CREATE INDEX IF NOT EXISTS idx_rr_file   ON review_records(file_group_fp);
        CREATE INDEX IF NOT EXISTS idx_rr_rule   ON review_records(rule_group_fp);
        CREATE INDEX IF NOT EXISTS idx_rd_rec    ON review_decisions(record_id);
        CREATE INDEX IF NOT EXISTS idx_rd_rule   ON review_decisions(rule_id);
        CREATE INDEX IF NOT EXISTS idx_rd_judg   ON review_decisions(historical_judgment);

        CREATE TABLE IF NOT EXISTS review_audit (
            id               TEXT PRIMARY KEY,
            task_id          TEXT,
            file_group_fp    TEXT,
            rule_group_fp    TEXT,
            file_md5s        TEXT,        -- JSON list[str]
            rule_ids         TEXT,        -- JSON list[str]
            rule_set_version TEXT,
            engine_version   TEXT,
            data_snapshot_version TEXT,
            parser_version   TEXT,
            parsed_content_hash TEXT,
            environment      TEXT,        -- JSON {encoding,timezone,locale}
            deterministic    INTEGER,     -- 1/0
            config_json      TEXT,        -- JSON 审核时的关键配置参数快照
            operator         TEXT,
            created_at       TEXT,
            UNIQUE(file_group_fp, rule_group_fp, engine_version, rule_set_version, data_snapshot_version, parsed_content_hash)
        );

        CREATE INDEX IF NOT EXISTS idx_ra_fg    ON review_audit(file_group_fp);
        CREATE INDEX IF NOT EXISTS idx_ra_rg    ON review_audit(rule_group_fp);
        CREATE INDEX IF NOT EXISTS idx_ra_task  ON review_audit(task_id);
        """
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
def _json_loads(s: str | None, default: Any = None) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return default


def _row_to_dict(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    for key in ("file_md5s", "file_names", "rule_ids", "rule_names"):
        if key in d and isinstance(d[key], str):
            d[key] = _json_loads(d[key], [])
    return d


# --------------------------------------------------------------------------- #
# 归档：一次审核完成时落库关联记录
# --------------------------------------------------------------------------- #
def archive_task(
    *,
    task_id: str,
    file_md5s: list[str],
    file_names: list[str],
    rule_ids: list[str],
    rule_names: list[str],
    findings: list[dict[str, Any]],
    consistency_issues: list[dict[str, Any]],
    # 历史判定：rule_id -> {"judgment": "adopt"/"reject", "reject_reason": str}
    decisions_by_rule: dict[str, dict[str, Any]] | None = None,
) -> str | None:
    """把一次审核结果归档为关联记录。

    返回关联记录 id（新建或复用）。同一 (文件组, 规则组) 的多次审核会更新同一条记录，
    使其始终反映最近一次结果——这正是「回流校验」所依赖的「既有对应关系」。
    """
    if not file_md5s or not rule_ids:
        return None
    fg_fp = file_group_fingerprint(file_md5s)
    rg_fp = rule_group_fingerprint(rule_ids)
    now = _now_iso()
    import uuid

    rid = uuid.uuid4().hex[:12]

    # 合并 findings 与 consistency_issues 作为"审核结果结论"列表
    conclusions: list[dict[str, Any]] = []
    for f in findings:
        conclusions.append(
            {
                "rule_id": str(f.get("rule_id") or ""),
                "rule_name": f.get("rule_name") or "",
                "field": f.get("title") or f.get("field") or "",
                "status": f.get("status") or "",
                "severity": f.get("severity") or "",
                "title": f.get("title") or "",
                "detail": f.get("detail") or "",
                "evidence": f.get("evidence") or "",
                "suggestion": f.get("suggestion") or "",
            }
        )
    for ci in consistency_issues:
        conclusions.append(
            {
                "rule_id": "consistency",
                "rule_name": "一致性核查",
                "field": ci.get("field") or "",
                "status": "fail",
                "severity": ci.get("severity") or "major",
                "title": ci.get("field") or "一致性问题",
                "detail": ci.get("description") or "",
                "evidence": "",
                "suggestion": ci.get("suggestion") or "",
            }
        )

    decisions_by_rule = decisions_by_rule or {}

    # 统计采纳/不采纳
    adopt_n = sum(1 for v in decisions_by_rule.values() if v.get("judgment") == "adopt")
    reject_n = sum(1 for v in decisions_by_rule.values() if v.get("judgment") == "reject")

    with _conn() as c:
        # 复用既有记录（同文件组 + 同规则组）
        exist = c.execute(
            "SELECT id FROM review_records WHERE file_group_fp=? AND rule_group_fp=?",
            (fg_fp, rg_fp),
        ).fetchone()
        record_id = exist["id"] if exist else rid

        if exist:
            c.execute(
                """
                UPDATE review_records SET
                    task_id=?, finding_count=?, adopt_count=?, reject_count=?,
                    file_names=?, rule_names=?, updated_at=?
                WHERE id=?
                """,
                (
                    task_id,
                    len(conclusions),
                    adopt_n,
                    reject_n,
                    json.dumps(file_names, ensure_ascii=False),
                    json.dumps(rule_names, ensure_ascii=False),
                    now,
                    record_id,
                ),
            )
            # 旧 decisions 清掉，用本次覆盖（保持最近一次一致）
            c.execute("DELETE FROM review_decisions WHERE record_id=?", (record_id,))
        else:
            c.execute(
                """
                INSERT INTO review_records(
                    id, file_group_fp, rule_group_fp, file_md5s, file_names,
                    rule_ids, rule_names, task_id, finding_count, adopt_count,
                    reject_count, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    record_id,
                    fg_fp,
                    rg_fp,
                    json.dumps(file_md5s, ensure_ascii=False),
                    json.dumps(file_names, ensure_ascii=False),
                    json.dumps(rule_ids, ensure_ascii=False),
                    json.dumps(rule_names, ensure_ascii=False),
                    task_id,
                    len(conclusions),
                    adopt_n,
                    reject_n,
                    now,
                    now,
                ),
            )

        for con in conclusions:
            rid_rule = con["rule_id"]
            content_key = f"{con.get('field') or ''}|{(con.get('detail') or '')[:60]}"
            fp = finding_fingerprint(rid_rule, content_key)
            # 该结论是否已被用户判定
            dec = decisions_by_rule.get(rid_rule)
            judgment = dec.get("judgment") if dec else None
            reason = (
                (dec.get("reject_reason") or "").strip() if dec else None
            )
            c.execute(
                """
                INSERT INTO review_decisions(
                    id, record_id, file_md5s, rule_id, rule_name, finding_fp,
                    field, status, severity, title, detail, evidence, suggestion,
                    historical_judgment, reject_reason, source, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    uuid.uuid4().hex[:12],
                    record_id,
                    json.dumps(file_md5s, ensure_ascii=False),
                    rid_rule,
                    con["rule_name"],
                    fp,
                    con["field"],
                    con["status"],
                    con["severity"],
                    con["title"],
                    con["detail"],
                    con["evidence"],
                    con["suggestion"],
                    judgment,
                    reason,
                    "manual" if judgment else "auto",
                    now,
                    now,
                ),
            )

        # 更新 review_files 统计
        for md5v, name in zip(file_md5s, file_names):
            rec = c.execute(
                "SELECT task_count FROM review_files WHERE md5=?", (md5v,)
            ).fetchone()
            if rec:
                c.execute(
                    "UPDATE review_files SET task_count=task_count+1, last_seen=?, filename=? WHERE md5=?",
                    (now, name, md5v),
                )
            else:
                c.execute(
                    """
                    INSERT INTO review_files(md5, filename, size, ext, role, first_seen, last_seen, task_count)
                    VALUES(?,?,?,?,?,?,?,1)
                    """,
                    (md5v, name, 0, "", "", now, now),
                )
    return record_id


# --------------------------------------------------------------------------- #
# 回流：新建任务前查询既有对应关系
# --------------------------------------------------------------------------- #
def lookup_history(
    file_md5s: list[str], rule_ids: list[str]
) -> dict[str, Any] | None:
    """若 (文件组, 规则组) 已存在关联记录，返回该记录及全部历史判定，供审核期回流。"""
    if not file_md5s or not rule_ids:
        return None
    fg_fp = file_group_fingerprint(file_md5s)
    rg_fp = rule_group_fingerprint(rule_ids)
    with _conn() as c:
        rec = c.execute(
            "SELECT * FROM review_records WHERE file_group_fp=? AND rule_group_fp=?",
            (fg_fp, rg_fp),
        ).fetchone()
        if not rec:
            return None
        rec_d = _row_to_dict(rec)
        rows = c.execute(
            "SELECT * FROM review_decisions WHERE record_id=?", (rec_d["id"],)
        ).fetchall()
        rec_d["decisions"] = [_row_to_dict(r) for r in rows]
    return rec_d


def build_reflow_instruction(file_md5s: list[str], rule_ids: list[str]) -> str | None:
    """生成回流约束提示文本；无历史对应关系返回 None。"""
    rec = lookup_history(file_md5s, rule_ids)
    if not rec:
        return None
    decided = [d for d in rec.get("decisions", []) if d.get("historical_judgment")]
    if not decided:
        return None
    lines = [
        "【历史审核结论约束（务必遵循，确保与既有处理结果保持一致）】",
        "以下审核项在本项目的历史审核中已有明确的处理判定，请直接沿用其结论，",
        "不要改变采纳/不采纳取向；若当前文档确有实质性变化，可在结论中说明理由。",
    ]
    for d in decided:
        if d["historical_judgment"] == "adopt":
            lines.append(
                f"- 「{d.get('rule_name') or d.get('rule_id')}」已采纳："
                f"{d.get('title') or d.get('field') or ''} —— 维持原结论（视为合规/通过）。"
            )
        else:
            lines.append(
                f"- 「{d.get('rule_name') or d.get('rule_id')}」已不采纳："
                f"{d.get('title') or d.get('field') or ''}"
                + (f"；不采纳原因：{d['reject_reason']}" if d.get("reject_reason") else "")
                + " —— 维持不采纳结论。"
            )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 查询 / 维护 / 一致性校验 接口支撑
# --------------------------------------------------------------------------- #
def list_records(
    *,
    q: str | None = None,
    judgment: str | None = None,
    limit: int = 50,
    offset: int = 0,
    task_ids: "set[str] | None" = None,
) -> tuple[list[dict[str, Any]], int]:
    """列出关联记录。

    task_ids：当为普通用户时传入其名下任务 id 集合，仅返回这些任务归档的关联记录
    （实现「查看自己创建的审核历史」的归属隔离）；管理员传 None 表示查看全部。
    空集合表示「无归属任务」，直接返回空结果。
    """
    wheres: list[str] = []
    params: list[Any] = []
    if judgment:
        if judgment == "adopt":
            wheres.append("adopt_count > 0 AND reject_count = 0")
        elif judgment == "reject":
            wheres.append("reject_count > 0")
        elif judgment == "mixed":
            wheres.append("adopt_count > 0 AND reject_count > 0")
    if q:
        like = f"%{q}%"
        wheres.append("(file_names LIKE ? OR rule_names LIKE ? OR task_id LIKE ?)")
        params.extend([like, like, like])
    if task_ids is not None:
        if not task_ids:
            return [], 0
        placeholders = ",".join("?" for _ in task_ids)
        wheres.append(f"task_id IN ({placeholders})")
        params.extend(task_ids)
    where_sql = (" WHERE " + " AND ".join(wheres)) if wheres else ""
    with _conn() as c:
        total = c.execute(
            f"SELECT COUNT(*) AS n FROM review_records{where_sql}", params
        ).fetchone()["n"]
        rows = c.execute(
            f"SELECT * FROM review_records{where_sql} "
            f"ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
    return [_row_to_dict(r) for r in rows], total


def get_record(record_id: str, task_ids: "set[str] | None" = None) -> dict[str, Any] | None:
    """单条关联记录详情。task_ids 非空时仅当该记录归属其中某个任务才返回，否则视为不存在
    （普通用户无法越权查看他人归档的关联记录）。"""
    with _conn() as c:
        rec = c.execute(
            "SELECT * FROM review_records WHERE id=?", (record_id,)
        ).fetchone()
        if not rec:
            return None
        rec_d = _row_to_dict(rec)
        if task_ids is not None and rec_d.get("task_id") not in task_ids:
            return None
        rows = c.execute(
            "SELECT * FROM review_decisions WHERE record_id=? ORDER BY rule_id, field",
            (record_id,),
        ).fetchall()
        rec_d["decisions"] = [_row_to_dict(r) for r in rows]
        files = c.execute(
            "SELECT md5, filename, role FROM review_files WHERE md5 IN "
            f"(SELECT value FROM json_each(?))",
            (json.dumps(rec_d.get("file_md5s", [])),),
        ).fetchall()
        rec_d["files"] = [_row_to_dict(r) for r in files]
    return rec_d


def get_findings_by_task(task_id: str) -> list[dict[str, Any]]:
    """按 task_id 聚合该任务全部审核决策(review_decisions)，映射为前端 Finding 形状。

    用于历史任务详情回显：从 reviewdata 归档恢复的任务在 tasks.json 中 findings 为空，
    通过本函数从 review_records→review_decisions 关联补全，使详情页展示完整审核结论。
    """
    with _conn() as c:
        recs = c.execute(
            "SELECT id, file_names FROM review_records WHERE task_id=?", (task_id,)
        ).fetchall()
        if not recs:
            return []
        record_ids = [r["id"] for r in recs]
        file_names: list[str] = []
        for r in recs:
            fns = r["file_names"]
            if isinstance(fns, list):
                file_names.extend(fns)
            elif isinstance(fns, str):
                try:
                    file_names.extend(json.loads(fns))
                except (json.JSONDecodeError, TypeError):
                    pass
        placeholders = ",".join("?" for _ in record_ids)
        rows = c.execute(
            f"SELECT * FROM review_decisions WHERE record_id IN ({placeholders}) "
            f"ORDER BY rule_id, field",
            record_ids,
        ).fetchall()
    out: list[dict[str, Any]] = []
    for raw in rows:
        d = _row_to_dict(raw)
        out.append(
            {
                "rule_id": d.get("rule_id"),
                "rule_name": d.get("rule_name") or "",
                "category": "",
                "severity": d.get("severity") or "medium",
                "status": d.get("status") or "fail",
                "title": d.get("title") or d.get("rule_name") or "",
                "detail": d.get("detail") or "",
                "evidence": d.get("evidence") or "",
                "location": d.get("field") or "",
                "suggestion": d.get("suggestion") or "",
                "legal_basis": "",
                "involved_files": file_names,
                "confidence": 1.0,
                "typo": None,
            }
        )
    return out


def update_decision(
    decision_id: str,
    *,
    judgment: str | None = None,
    reject_reason: str | None = None,
) -> dict[str, Any] | None:
    """维护（覆盖）单条历史判定：用户纠正既有采纳/不采纳结论。"""
    if judgment is None and reject_reason is None:
        raise ValueError("至少提供 judgment 或 reject_reason 之一")
    if judgment is not None and judgment not in ("adopt", "reject"):
        raise ValueError("judgment 必须为 adopt 或 reject")
    now = _now_iso()
    sets: list[str] = []
    params: list[Any] = []
    if judgment is not None:
        sets.append("historical_judgment=?")
        params.append(judgment)
        sets.append("source='manual'")
    if judgment == "adopt":
        sets.append("reject_reason=NULL")
    if reject_reason is not None:
        sets.append("reject_reason=?")
        params.append((reject_reason or "").strip() or None)
    sets.append("updated_at=?")
    params.append(now)
    params.append(decision_id)
    with _conn() as c:
        c.execute(
            f"UPDATE review_decisions SET {', '.join(sets)} WHERE id=?",
            params,
        )
        # 回流同步更新 record 统计
        rec = c.execute(
            "SELECT record_id FROM review_decisions WHERE id=?", (decision_id,)
        ).fetchone()
        if rec:
            rid = rec["record_id"]
            row = c.execute(
                "SELECT "
                "SUM(CASE WHEN historical_judgment='adopt' THEN 1 ELSE 0 END) AS a,"
                "SUM(CASE WHEN historical_judgment='reject' THEN 1 ELSE 0 END) AS r "
                "FROM review_decisions WHERE record_id=?",
                (rid,),
            ).fetchone()
            c.execute(
                "UPDATE review_records SET adopt_count=?, reject_count=?, updated_at=? WHERE id=?",
                (row["a"] or 0, row["r"] or 0, now, rid),
            )
        det = c.execute(
            "SELECT * FROM review_decisions WHERE id=?", (decision_id,)
        ).fetchone()
    return _row_to_dict(det) if det else None


def verify_consistency(record_id: str) -> dict[str, Any]:
    """一致性校验：重新比对历史 finding 对应规则是否仍适用。

    规则内容可能已被编辑/移除，导致历史判定失去依据。返回每条 decision 的 stale 标记
    与原因，以及总体健康度。
    """
    from ..services import rules_store

    rec = get_record(record_id)
    if not rec:
        return {"ok": False, "reason": "记录不存在"}
    from ..services.rules_store import get_rules_by_ids

    # 当前生效规则集合
    active_ids = {r["id"] for r in get_rules_by_ids(rec.get("rule_ids", []))}
    results: list[dict[str, Any]] = []
    stale = 0
    for d in rec.get("decisions", []):
        rid = d.get("rule_id")
        if rid == "consistency":
            status = "ok"
            note = "一致性结论，无对应规则对象"
        elif rid in active_ids:
            status = "ok"
            note = "规则仍存在"
        else:
            status = "stale"
            stale += 1
            note = "对应规则已不存在或被移除，历史判定可能失效"
        results.append(
            {
                "decision_id": d.get("id"),
                "rule_id": rid,
                "rule_name": d.get("rule_name"),
                "field": d.get("field"),
                "historical_judgment": d.get("historical_judgment"),
                "status": status,
                "note": note,
            }
        )
    return {
        "ok": True,
        "record_id": record_id,
        "total": len(results),
        "stale": stale,
        "healthy": stale == 0,
        "items": results,
    }


def stats(task_ids: "set[str] | None" = None) -> dict[str, Any]:
    """概览统计。task_ids 非空时仅统计该用户名下任务的关联记录与判定（归属隔离）。"""
    with _conn() as c:
        if task_ids is not None and not task_ids:
            return {"records": 0, "decisions": 0, "adopted": 0, "rejected": 0, "files": 0, "audits": 0}
        if task_ids is None:
            rec_filter = ""
            rec_params: list[Any] = []
        else:
            rec_ph = ",".join("?" for _ in task_ids)
            rec_filter = f" WHERE task_id IN ({rec_ph})"
            rec_params = list(task_ids)
        records = c.execute(
            f"SELECT COUNT(*) AS n FROM review_records{rec_filter}", rec_params
        ).fetchone()["n"]
        if task_ids is None:
            decisions = c.execute("SELECT COUNT(*) AS n FROM review_decisions").fetchone()["n"]
            adopted = c.execute(
                "SELECT COUNT(*) AS n FROM review_decisions WHERE historical_judgment='adopt'"
            ).fetchone()["n"]
            rejected = c.execute(
                "SELECT COUNT(*) AS n FROM review_decisions WHERE historical_judgment='reject'"
            ).fetchone()["n"]
        else:
            # 仅统计归属记录下的判定
            rec_ids = [
                r["id"]
                for r in c.execute(
                    f"SELECT id FROM review_records{rec_filter}", rec_params
                ).fetchall()
            ]
            if rec_ids:
                dec_ph = ",".join("?" for _ in rec_ids)
                decisions = c.execute(
                    f"SELECT COUNT(*) AS n FROM review_decisions WHERE record_id IN ({dec_ph})",
                    rec_ids,
                ).fetchone()["n"]
                adopted = c.execute(
                    f"SELECT COUNT(*) AS n FROM review_decisions WHERE record_id IN ({dec_ph}) AND historical_judgment='adopt'",
                    rec_ids,
                ).fetchone()["n"]
                rejected = c.execute(
                    f"SELECT COUNT(*) AS n FROM review_decisions WHERE record_id IN ({dec_ph}) AND historical_judgment='reject'",
                    rec_ids,
                ).fetchone()["n"]
            else:
                decisions = adopted = rejected = 0
        files = c.execute("SELECT COUNT(*) AS n FROM review_files").fetchone()["n"]
        audits = c.execute("SELECT COUNT(*) AS n FROM review_audit").fetchone()["n"]
    return {
        "records": records,
        "decisions": decisions,
        "adopted": adopted,
        "rejected": rejected,
        "files": files,
        "audits": audits,
    }


# --------------------------------------------------------------------------- #
# 审计存证与可重放
# --------------------------------------------------------------------------- #
def save_audit(
    *,
    task_id: str | None,
    file_md5s: list[str],
    rule_ids: list[str],
    manifest: dict[str, Any],
    config_snapshot: dict[str, Any] | None = None,
    operator: str = "system",
) -> str | None:
    """保存一次审核的完整审计记录（版本存证），支撑按历史版本重跑与一致性监控。

    唯一约束：(文件组指纹, 规则组指纹, 引擎版本, 规则集版本, 数据快照版本, 解析快照哈希)
    保证「同一输入+同一版本组合」仅一条存证（最后一次覆盖），避免重复堆积，
    同时该组合正是「可重放」的判定键。
    """
    if not file_md5s or not rule_ids:
        return None
    fg_fp = file_group_fingerprint(file_md5s)
    rg_fp = rule_group_fingerprint(rule_ids)
    import uuid

    rid = uuid.uuid4().hex[:12]
    now = _now_iso()
    with _conn() as c:
        c.execute(
            """
            INSERT OR REPLACE INTO review_audit(
                id, task_id, file_group_fp, rule_group_fp, file_md5s, rule_ids,
                rule_set_version, engine_version, data_snapshot_version, parser_version,
                parsed_content_hash, environment, deterministic, config_json, operator, created_at
            ) VALUES(
                COALESCE((SELECT id FROM review_audit WHERE
                    file_group_fp=? AND rule_group_fp=? AND engine_version=?
                    AND rule_set_version=? AND data_snapshot_version=? AND parsed_content_hash=?), ?),
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                fg_fp, rg_fp,
                manifest.get("engine_version", ""),
                manifest.get("rule_set_version", ""),
                manifest.get("data_snapshot_version", ""),
                manifest.get("parsed_content_hash", ""),
                rid,
                task_id, fg_fp, rg_fp,
                json.dumps(file_md5s, ensure_ascii=False),
                json.dumps(rule_ids, ensure_ascii=False),
                manifest.get("rule_set_version", ""),
                manifest.get("engine_version", ""),
                manifest.get("data_snapshot_version", ""),
                manifest.get("parser_version", ""),
                manifest.get("parsed_content_hash", ""),
                json.dumps(manifest.get("environment", {}), ensure_ascii=False),
                1 if manifest.get("deterministic") else 0,
                json.dumps(config_snapshot or {}, ensure_ascii=False),
                operator, now,
            ),
        )
        row = c.execute(
            "SELECT id FROM review_audit WHERE file_group_fp=? AND rule_group_fp=? "
            "AND engine_version=? AND rule_set_version=? AND data_snapshot_version=? "
            "AND parsed_content_hash=?",
            (
                fg_fp, rg_fp,
                manifest.get("engine_version", ""),
                manifest.get("rule_set_version", ""),
                manifest.get("data_snapshot_version", ""),
                manifest.get("parsed_content_hash", ""),
            ),
        ).fetchone()
    return row["id"] if row else rid


def get_audit_by_task(task_id: str) -> dict[str, Any] | None:
    """按任务 id 取该次审核的审计记录（含版本清单）。"""
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM review_audit WHERE task_id=? ORDER BY created_at DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if not row:
            return None
        return _row_to_audit(row)


def _row_to_audit(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    for key in ("file_md5s", "rule_ids", "environment", "config_json"):
        if key in d and isinstance(d[key], str):
            d[key] = _json_loads(d[key], [] if key in ("file_md5s", "rule_ids") else {})
    return d


def list_audits(
    *,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    wheres: list[str] = []
    params: list[Any] = []
    if q:
        like = f"%{q}%"
        wheres.append("(rule_set_version LIKE ? OR engine_version LIKE ? OR task_id LIKE ?)")
        params.extend([like, like, like])
    where_sql = (" WHERE " + " AND ".join(wheres)) if wheres else ""
    with _conn() as c:
        total = c.execute(
            f"SELECT COUNT(*) AS n FROM review_audit{where_sql}", params
        ).fetchone()["n"]
        rows = c.execute(
            f"SELECT * FROM review_audit{where_sql} "
            f"ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
    return [_row_to_audit(r) for r in rows], total


def find_audit_for_replay(
    file_md5s: list[str], rule_ids: list[str]
) -> dict[str, Any] | None:
    """查找可重放的审计基线：同文件组+同规则组的最近一次存证。

    用于「按历史版本重跑」：返回该次审核的版本清单，重跑时强制沿用以确保可复现。
    """
    fg_fp = file_group_fingerprint(file_md5s)
    rg_fp = rule_group_fingerprint(rule_ids)
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM review_audit WHERE file_group_fp=? AND rule_group_fp=? "
            "ORDER BY created_at DESC LIMIT 1",
            (fg_fp, rg_fp),
        ).fetchone()
        if not row:
            return None
        return _row_to_audit(row)
