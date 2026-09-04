"""反馈与训练数据 / 错别字校验集存储层（SQLite）。

设计目标：
- feedback_records：采纳/不采纳反馈的完整训练数据集（含原始识别结果、用户判定、
  不采纳原因、时间戳），支撑模型调优数据的积累与检索。
- validation_items：错别字这类「可明确修正」问题的独立校验集，随每次审核自动沉淀。
- validation_filters：当用户对某条错别字识别标记为「不采纳」（业务认定非错别字）时，
  记录以「错字」为键的过滤规则；后续审核再次识别到相同错别字则自动跳过（去噪）。

数据库默认位于 backend/data/feedback.db（与配置库同卷，容器重建不丢）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from .. import storage  # noqa: E402  # 统一存储层

DB_PATH = Path(
    os.getenv("BCR_FEEDBACK_DB_PATH", str(Path(__file__).parent.parent / "data" / "feedback.db"))
)

# 错别字类规则：命中即纳入校验集并参与「相同错别字」去噪匹配
TYPO_RULE_IDS = {"gen-typo"}

_lock = threading.Lock()
# 活跃过滤规则中的错字集合（内存缓存，写后失效）
_wrong_cache: set[str] | None = None


def _now_iso() -> str:
    import datetime

    return datetime.datetime.now().isoformat(timespec="seconds")


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = storage.connect_relational("feedback", DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _init(conn)
    return conn


def _init(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS feedback_records (
            id                  TEXT PRIMARY KEY,
            task_id            TEXT,
            finding_fingerprint TEXT,
            rule_id            TEXT,
            rule_name          TEXT,
            category           TEXT,
            severity           TEXT,
            status             TEXT,
            title              TEXT,
            detail             TEXT,
            evidence           TEXT,
            location           TEXT,
            suggestion         TEXT,
            legal_basis        TEXT,
            confidence         REAL,
            involved_files     TEXT,
            judgment           TEXT,
            reject_reason      TEXT,
            is_typo            INTEGER,
            typo_wrong         TEXT,
            typo_correct       TEXT,
            source             TEXT,
            created_at         TEXT,
            UNIQUE(finding_fingerprint)
        );
        CREATE TABLE IF NOT EXISTS validation_items (
            id                  TEXT PRIMARY KEY,
            finding_fingerprint TEXT UNIQUE,
            task_id            TEXT,
            rule_id            TEXT,
            typo_wrong         TEXT,
            typo_correct       TEXT,
            original_finding   TEXT,
            status             TEXT,
            created_at         TEXT,
            updated_at         TEXT
        );
        CREATE TABLE IF NOT EXISTS validation_filters (
            id              TEXT PRIMARY KEY,
            typo_wrong      TEXT UNIQUE,
            typo_correct    TEXT,
            reject_reason   TEXT,
            feedback_id     TEXT,
            active          INTEGER DEFAULT 1,
            hit_count       INTEGER DEFAULT 0,
            created_at      TEXT,
            updated_at      TEXT,
            last_hit_at     TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_fb_judgment ON feedback_records(judgment);
        CREATE INDEX IF NOT EXISTS idx_fb_rule     ON feedback_records(rule_id);
        CREATE INDEX IF NOT EXISTS idx_fb_typo     ON feedback_records(is_typo);
        CREATE INDEX IF NOT EXISTS idx_fb_created  ON feedback_records(created_at);
        CREATE INDEX IF NOT EXISTS idx_vf_wrong    ON validation_filters(typo_wrong);
        CREATE INDEX IF NOT EXISTS idx_vi_wrong    ON validation_items(typo_wrong);
        """
    )
    # 历史库迁移：feedback_records 增加 user_id 列（反馈归属用户），便于按用户区分与过滤
    _ensure_feedback_user_id(conn)
    # 历史库迁移：validation_items 增加 user_id 列（错别字校验集归属用户）。
    # 历史沉淀的校验项默认归属管理员（在部署后由迁移脚本批量置为 admin），
    # 普通用户仅能查看自己名下沉淀的校验项，避免跨用户泄露训练数据。
    _ensure_validation_user_id(conn)
    # 历史库迁移：validation_filters 增加 deactivate_reason 列（停用补充说明）。
    # 停用过滤规则（解除去噪）须填写说明，记录保留并标记「已停用」。
    _ensure_filter_deactivate_reason(conn)
    conn.commit()


def _ensure_feedback_user_id(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(feedback_records)")}
    if "user_id" not in cols:
        conn.execute("ALTER TABLE feedback_records ADD COLUMN user_id TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_fb_user ON feedback_records(user_id)"
        )


def _ensure_validation_user_id(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(validation_items)")}
    if "user_id" not in cols:
        conn.execute("ALTER TABLE validation_items ADD COLUMN user_id TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_vi_user ON validation_items(user_id)"
        )


def _ensure_filter_deactivate_reason(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(validation_filters)")}
    if "deactivate_reason" not in cols:
        conn.execute("ALTER TABLE validation_filters ADD COLUMN deactivate_reason TEXT")


# --------------------------------------------------------------------------- #
# 错别字提取
# --------------------------------------------------------------------------- #
def extract_typo(finding: dict[str, Any]) -> tuple[str | None, str | None]:
    """从一条 finding 提取（错字, 正字）。

    仅「错别字」规则(gen-typo)的结论才进入校验集；其它规则（术语/格式/一致性等）
    的结论即便文本里出现「应为/改为」也属正常表述，不能被误判为错别字写进校验集。
    优先用模型结构化 typo 字段；缺失时仅对 gen-typo 规则做严格启发式兜底。
    """
    t = finding.get("typo") if isinstance(finding, dict) else None
    if isinstance(t, dict) and (t.get("wrong") or "").strip():
        return str(t["wrong"]).strip(), str(t.get("correct") or "").strip()

    # 非错别字规则：不做任何提取，直接返回空，避免把建议/说明文本当错字写进校验集。
    if str(finding.get("rule_id") or "") not in TYPO_RULE_IDS:
        return None, None

    detail = finding.get("detail") or ""
    suggestion = finding.get("suggestion") or ""
    blob = f"{detail} {suggestion}"

    # 模式：将 'X' 改为 'Y' / X 应为 Y / 错别字 'X'（仅对 gen-typo 规则启用）
    m = re.search(
        r"['\"「]?\s*(.{1,12}?)\s*['\"」]?\s*"
        r"(?:改为|应为|纠正为|应写作|修正为|建议为)\s*"
        r"['\"「]?\s*(.{1,12}?)\s*['\"」]?",
        blob,
    )
    if m:
        wrong = m.group(1).strip(" '\"「」")
        correct = m.group(2).strip(" '\"「」")
        if _is_clean_typo_pair(wrong, correct):
            return wrong, correct
    return None, None


def _is_clean_typo_pair(wrong: str | None, correct: str | None) -> bool:
    """校验（错字, 正字）是否为可信的单字/词替换，而非句子片段或建议性表述。"""
    if not wrong:
        return False
    if len(wrong) > 12 or (correct and len(correct) > 12):
        return False
    bad = (
        "应", "建议", "删", "修", "改", "取", "或",
        "，", "。", "；", "、", "（", "）", "：", "“", "”",
        "说明", "要求", "服务", "招标", "投标", "报价", "价格",
        "通知", "供应商", "规定", "条款", "条件",
    )
    if any(k in wrong for k in bad):
        return False
    if correct and any(k in correct for k in bad):
        return False
    return True


def _is_typo(finding: dict[str, Any], wrong: str | None) -> bool:
    """仅「错别字」规则且成功提取到错字，才视为可进校验集的错别字。"""
    return str(finding.get("rule_id") or "") in TYPO_RULE_IDS and bool(wrong)


def _fingerprint(task_id: str | None, rule_id: str, extra: str = "") -> str:
    """finding 唯一指纹：同一任务内同规则也可能有多条结论，必须用具体内容区分。"""
    base = f"{task_id or 'na'}|{rule_id}|{extra}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:16]


def _norm_wrong(wrong: str) -> str:
    return (wrong or "").strip()


# --------------------------------------------------------------------------- #
# 反馈写入（采纳 / 不采纳）
# --------------------------------------------------------------------------- #
def add_feedback(
    finding: dict[str, Any],
    judgment: str,
    reject_reason: str | None,
    task_id: str | None = None,
    source: str = "manual",
    user_id: str | None = None,
) -> dict[str, Any]:
    """提交一条采纳/不采纳反馈，落训练数据集；不采纳的错别字额外写过滤规则。

    user_id：反馈归属用户（普通用户本人；管理员代操作/系统回流可为 None）。
    """
    import uuid

    rule_id = str(finding.get("rule_id") or "")
    wrong, correct = extract_typo(finding)
    is_typo = _is_typo(finding, wrong)
    fp = _fingerprint(task_id, rule_id, f"{wrong or ''}|{(finding.get('detail') or '')[:48]}")
    rid = uuid.uuid4().hex[:12]
    now = _now_iso()

    rec = {
        "id": rid,
        "task_id": task_id,
        "finding_fingerprint": fp,
        "rule_id": rule_id,
        "rule_name": finding.get("rule_name") or "",
        "category": finding.get("category") or "",
        "severity": finding.get("severity") or "",
        "status": finding.get("status") or "",
        "title": finding.get("title") or "",
        "detail": finding.get("detail") or "",
        "evidence": finding.get("evidence") or "",
        "location": finding.get("location") or "",
        "suggestion": finding.get("suggestion") or "",
        "legal_basis": finding.get("legal_basis") or "",
        "confidence": float(finding.get("confidence") or 0),
        "involved_files": json.dumps(finding.get("involved_files") or [], ensure_ascii=False),
        "judgment": judgment,
        "reject_reason": (reject_reason or "").strip() or None,
        "is_typo": int(is_typo),
        "typo_wrong": wrong,
        "typo_correct": correct,
        "source": source,
        "user_id": user_id,
        "created_at": now,
    }

    with _conn() as c:
        # 同一（任务,规则）只保留最新一条判定
        c.execute(
            """
            INSERT INTO feedback_records(
                id,task_id,finding_fingerprint,rule_id,rule_name,category,severity,
                status,title,detail,evidence,location,suggestion,legal_basis,
                confidence,involved_files,judgment,reject_reason,is_typo,
                typo_wrong,typo_correct,source,user_id,created_at
            ) VALUES(
                :id,:task_id,:finding_fingerprint,:rule_id,:rule_name,:category,:severity,
                :status,:title,:detail,:evidence,:location,:suggestion,:legal_basis,
                :confidence,:involved_files,:judgment,:reject_reason,:is_typo,
                :typo_wrong,:typo_correct,:source,:user_id,:created_at
            )
            ON CONFLICT(finding_fingerprint) DO UPDATE SET
                judgment=excluded.judgment,
                reject_reason=excluded.reject_reason,
                title=excluded.title,
                detail=excluded.detail,
                evidence=excluded.evidence,
                suggestion=excluded.suggestion,
                status=excluded.status,
                is_typo=excluded.is_typo,
                typo_wrong=excluded.typo_wrong,
                typo_correct=excluded.typo_correct,
                user_id=excluded.user_id,
                created_at=excluded.created_at
            """,
            rec,
        )

        if is_typo:
            vi_status = "accepted" if judgment == "adopt" else "rejected"
            c.execute(
                """
                INSERT INTO validation_items(
                    id,finding_fingerprint,task_id,rule_id,typo_wrong,typo_correct,
                    original_finding,status,user_id,created_at,updated_at
                ) VALUES(:id,:fp,:task_id,:rule_id,:wrong,:correct,:orig,:status,:uid,:now,:now)
                ON CONFLICT(finding_fingerprint) DO UPDATE SET
                    typo_wrong=excluded.typo_wrong,
                    typo_correct=excluded.typo_correct,
                    original_finding=excluded.original_finding,
                    updated_at=excluded.updated_at,
                    user_id=COALESCE(validation_items.user_id, excluded.user_id),
                    status=CASE
                        WHEN excluded.status IN ('accepted','rejected') THEN excluded.status
                        WHEN excluded.status='filtered' AND validation_items.status NOT IN ('accepted','rejected') THEN 'filtered'
                        ELSE COALESCE(validation_items.status,'pending')
                    END
                """,
                {
                    "id": uuid.uuid4().hex[:12],
                    "fp": fp,
                    "task_id": task_id,
                    "rule_id": rule_id,
                    "wrong": wrong,
                    "correct": correct,
                    "orig": json.dumps(finding, ensure_ascii=False),
                    "status": vi_status,
                    "uid": user_id,
                    "now": now,
                },
            )

            # 不采纳的错别字 → 写入过滤规则（自动跳过该校验项）
            if judgment == "reject" and wrong:
                nw = _norm_wrong(wrong)
                c.execute(
                    """
                    INSERT INTO validation_filters(
                        id,typo_wrong,typo_correct,reject_reason,feedback_id,
                        active,hit_count,created_at,updated_at
                    ) VALUES(:id,:wrong,:correct,:reason,:fb,1,0,:now,:now)
                    ON CONFLICT(typo_wrong) DO UPDATE SET
                        active=1,
                        typo_correct=excluded.typo_correct,
                        reject_reason=excluded.reject_reason,
                        feedback_id=excluded.feedback_id,
                        updated_at=excluded.updated_at
                    """,
                    {
                        "id": uuid.uuid4().hex[:12],
                        "wrong": nw,
                        "correct": correct,
                        "reason": (reject_reason or "").strip() or None,
                        "fb": rid,
                        "now": now,
                    },
                )
                _invalidate_cache()
    return rec


def update_validation_item_status(
    item_id: str,
    status: str,
    reject_reason: str | None = None,
    user_id: str | None = None,
    scope_user_id: str | None = None,
) -> dict[str, Any] | None:
    """在错别字校验集列表内直接更新某条校验项的判定状态（采纳/驳回/待判定）。

    - accepted：标记为已采纳；若该错字曾被标记不采纳，停用对应过滤规则以恢复校验。
    - rejected：标记为不采纳；写入/激活过滤规则，后续审核自动跳过该错字。
    - pending ：恢复为待判定（不动过滤规则）。
    scope_user_id：普通用户仅可改自己名下的校验项；管理员传 None 可改全部。
    返回更新后的精简记录；校验项不存在或无权限时返回 None。
    """
    if status not in ("accepted", "rejected", "pending"):
        raise ValueError("status 必须为 accepted/rejected/pending")
    if status == "rejected" and not (reject_reason or "").strip():
        raise ValueError("驳回时必须填写补充说明")
    now = _now_iso()
    with _conn() as c:
        row = c.execute(
            "SELECT id, typo_wrong, typo_correct, user_id FROM validation_items WHERE id=?",
            (item_id,),
        ).fetchone()
        if row is None:
            return None
        if scope_user_id is not None and (row["user_id"] or "") != scope_user_id:
            return None
        wrong = row["typo_wrong"]
        correct = row["typo_correct"]
        nw = _norm_wrong(wrong) if wrong else ""

        c.execute(
            """
            UPDATE validation_items
            SET status=?, updated_at=?, user_id=COALESCE(user_id, ?)
            WHERE id=?
            """,
            (status, now, user_id, item_id),
        )

        if status == "rejected" and nw:
            c.execute(
                """
                INSERT INTO validation_filters(
                    id,typo_wrong,typo_correct,reject_reason,feedback_id,
                    active,hit_count,created_at,updated_at
                ) VALUES(:id,:wrong,:correct,:reason,:fb,1,0,:now,:now)
                ON CONFLICT(typo_wrong) DO UPDATE SET
                    active=1,
                    typo_correct=excluded.typo_correct,
                    reject_reason=excluded.reject_reason,
                    updated_at=excluded.updated_at
                """,
                {
                    "id": uuid.uuid4().hex[:12],
                    "wrong": nw,
                    "correct": correct,
                    "reason": (reject_reason or "").strip() or None,
                    "fb": item_id,
                    "now": now,
                },
            )
            _invalidate_cache()
        elif status == "accepted" and nw:
            cur = c.execute(
                "UPDATE validation_filters SET active=0, updated_at=? WHERE typo_wrong=? AND active=1",
                (now, nw),
            )
            if cur.rowcount:
                _invalidate_cache()

    return {"id": item_id, "status": status, "ok": True}


# --------------------------------------------------------------------------- #
# 审核期自动过滤（校验集去噪）
# --------------------------------------------------------------------------- #
def get_active_wrong_set() -> set[str]:
    """活跃过滤规则中的错字集合（内存缓存）。"""
    global _wrong_cache
    if _wrong_cache is None:
        _wrong_cache = _load_wrong_set()
    return _wrong_cache


def _load_wrong_set() -> set[str]:
    with _conn() as c:
        rows = c.execute(
            "SELECT typo_wrong FROM validation_filters WHERE active=1"
        ).fetchall()
    return {r["typo_wrong"] for r in rows}


def _invalidate_cache() -> None:
    global _wrong_cache
    _wrong_cache = None


def record_skip(typo_wrong: str) -> None:
    """累计某错字被自动跳过次数（去噪命中）。"""
    now = _now_iso()
    with _conn() as c:
        c.execute(
            """
            UPDATE validation_filters
            SET hit_count = hit_count + 1, last_hit_at = ?
            WHERE typo_wrong = ? AND active = 1
            """,
            (now, typo_wrong),
        )


def ingest_typo_findings(
    findings: list[dict[str, Any]], task_id: str | None, user_id: str | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """对一批 findings 应用错别字校验集过滤。

    返回 (保留的 findings, 被跳过的 findings)。被跳过的项（其错字命中活跃过滤规则）
    不进入审核结果，并累计命中次数；未命中的错别字 finding 写入校验集（pending）。
    user_id：沉淀校验项的归属用户（取自建任务的发起方），供按用户隔离查看。
    """
    active = get_active_wrong_set()
    kept: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for f in findings:
        rule_id = str(f.get("rule_id") or "")
        wrong, correct = extract_typo(f)
        if not _is_typo(f, wrong):
            kept.append(f)
            continue

        nw = _norm_wrong(wrong) if wrong else ""
        if nw and nw in active:
            record_skip(nw)
            if task_id:
                _upsert_validation_item(f, task_id, wrong, correct, "filtered", user_id=user_id)
            skipped.append(f)
            continue

        if task_id:
            _upsert_validation_item(f, task_id, wrong, correct, "pending", user_id=user_id)
        kept.append(f)

    return kept, skipped


def _upsert_validation_item(
    finding: dict[str, Any],
    task_id: str | None,
    wrong: str | None,
    correct: str | None,
    status: str,
    user_id: str | None = None,
) -> None:
    import uuid

    rule_id = str(finding.get("rule_id") or "")
    fp = _fingerprint(task_id, rule_id, f"{wrong or ''}|{(finding.get('detail') or '')[:48]}")
    now = _now_iso()
    with _conn() as c:
        c.execute(
            """
            INSERT INTO validation_items(
                id,finding_fingerprint,task_id,rule_id,typo_wrong,typo_correct,
                original_finding,status,user_id,created_at,updated_at
            ) VALUES(:id,:fp,:task_id,:rule_id,:wrong,:correct,:orig,:status,:uid,:now,:now)
            ON CONFLICT(finding_fingerprint) DO UPDATE SET
                typo_wrong=excluded.typo_wrong,
                typo_correct=excluded.typo_correct,
                original_finding=excluded.original_finding,
                updated_at=excluded.updated_at,
                user_id=COALESCE(validation_items.user_id, excluded.user_id),
                status=CASE
                    WHEN excluded.status IN ('accepted','rejected') THEN excluded.status
                    WHEN excluded.status='filtered' AND validation_items.status NOT IN ('accepted','rejected') THEN 'filtered'
                    ELSE COALESCE(validation_items.status,'pending')
                END
            """,
            {
                "id": uuid.uuid4().hex[:12],
                "fp": fp,
                "task_id": task_id,
                "rule_id": rule_id,
                "wrong": wrong,
                "correct": correct,
                "orig": json.dumps(finding, ensure_ascii=False),
                "status": status,
                "uid": user_id,
                "now": now,
            },
        )


# --------------------------------------------------------------------------- #
# 检索 / 列表 / 统计 / 导出
# --------------------------------------------------------------------------- #
def _row_to_dict(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    iv = d.get("involved_files")
    if isinstance(iv, str):
        try:
            d["involved_files"] = json.loads(iv)
        except (json.JSONDecodeError, ValueError):
            d["involved_files"] = []
    orig = d.get("original_finding")
    if isinstance(orig, str):
        try:
            d["original_finding"] = json.loads(orig)
        except (json.JSONDecodeError, ValueError):
            d["original_finding"] = None
    return d


def list_feedback(
    *,
    judgment: str | None = None,
    rule_id: str | None = None,
    is_typo: bool | None = None,
    q: str | None = None,
    user_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    wheres: list[str] = []
    params: list[Any] = []
    if judgment:
        wheres.append("judgment = ?")
        params.append(judgment)
    if rule_id:
        wheres.append("rule_id = ?")
        params.append(rule_id)
    if is_typo is not None:
        wheres.append("is_typo = ?")
        params.append(1 if is_typo else 0)
    if user_id is not None:
        wheres.append("user_id = ?")
        params.append(user_id)
    if q:
        wheres.append("(title LIKE ? OR detail LIKE ? OR evidence LIKE ? OR typo_wrong LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like, like])

    where_sql = (" WHERE " + " AND ".join(wheres)) if wheres else ""
    with _conn() as c:
        total = c.execute(
            f"SELECT COUNT(*) AS n FROM feedback_records{where_sql}", params
        ).fetchone()["n"]
        rows = c.execute(
            f"SELECT * FROM feedback_records{where_sql} "
            f"ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
    return [_row_to_dict(r) for r in rows], total


def list_validation_items(
    *,
    status: str | None = None,
    q: str | None = None,
    user_id: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    wheres: list[str] = []
    params: list[Any] = []
    if status:
        wheres.append("status = ?")
        params.append(status)
    if user_id is not None:
        # 管理员（user_id=None）看全部；普通用户仅看自己名下沉淀的校验项。
        wheres.append("user_id = ?")
        params.append(user_id)
    if q:
        wheres.append("(typo_wrong LIKE ? OR typo_correct LIKE ? OR rule_id LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like])
    where_sql = (" WHERE " + " AND ".join(wheres)) if wheres else ""
    with _conn() as c:
        total = c.execute(
            f"SELECT COUNT(*) AS n FROM validation_items{where_sql}", params
        ).fetchone()["n"]
        rows = c.execute(
            f"SELECT * FROM validation_items{where_sql} "
            f"ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
    return [_row_to_dict(r) for r in rows], total


def list_filters(active_only: bool = True) -> list[dict[str, Any]]:
    with _conn() as c:
        sql = "SELECT * FROM validation_filters"
        if active_only:
            sql += " WHERE active=1"
        sql += " ORDER BY created_at DESC"
        rows = c.execute(sql).fetchall()
    return [_row_to_dict(r) for r in rows]


def deactivate_filter(
    filter_id: str, deactivate_reason: str | None = None
) -> dict[str, Any] | None:
    """停用一条过滤规则（恢复该校验项）。停用需填写停用补充说明，记录保留并标记「已停用」。"""
    reason = (deactivate_reason or "").strip()
    if not reason:
        raise ValueError("停用过滤规则时必须填写停用补充说明")
    with _conn() as c:
        c.execute(
            "UPDATE validation_filters SET active=0, deactivate_reason=?, updated_at=? WHERE id=?",
            (reason, _now_iso(), filter_id),
        )
        if c.execute("SELECT changes()").fetchone()[0] == 0:
            return None
    _invalidate_cache()
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM validation_filters WHERE id=?", (filter_id,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def stats(user_id: str | None = None) -> dict[str, Any]:
    conds: list[str] = []
    params: list[Any] = []
    if user_id is not None:
        conds.append("user_id=?")
        params.append(user_id)
    where = (" WHERE " + " AND ".join(conds)) if conds else " WHERE 1=1"

    def _cnt(suffix: str) -> int:
        with _conn() as c:
            return c.execute(
                f"SELECT COUNT(*) AS n FROM feedback_records{where}{suffix}", params
            ).fetchone()["n"]

    total = _cnt("")
    adopt = _cnt(" AND judgment='adopt'")
    reject = _cnt(" AND judgment='reject'")
    typo_total = _cnt(" AND is_typo=1")
    with _conn() as c:
        active_filters = c.execute(
            "SELECT COUNT(*) AS n FROM validation_filters WHERE active=1"
        ).fetchone()["n"]
        vi_total = c.execute(
            f"SELECT COUNT(*) AS n FROM validation_items{where}", params
        ).fetchone()["n"]
    return {
        "total": total,
        "adopt": adopt,
        "reject": reject,
        "typo_total": typo_total,
        "active_filters": active_filters,
        "validation_items": vi_total,
    }


def get_judgments_by_task(task_id: str) -> dict[str, dict[str, Any]]:
    """返回该任务下用户已提交的判定，按 rule_id 聚合（用于审核数据回流归档）。

    同一 (task, rule) 仅保留最新一条。返回 {rule_id: {"judgment","reject_reason"}}。
    """
    out: dict[str, dict[str, Any]] = {}
    if not task_id:
        return out
    with _conn() as c:
        rows = c.execute(
            "SELECT rule_id, judgment, reject_reason FROM feedback_records "
            "WHERE task_id=? ORDER BY created_at ASC",
            (task_id,),
        ).fetchall()
    for r in rows:
        rid = str(r["rule_id"] or "")
        if not rid:
            continue
        out[rid] = {
            "judgment": r["judgment"],
            "reject_reason": r["reject_reason"],
        }
    return out


def export_jsonl(
    *,
    judgment: str | None = None,
    rule_id: str | None = None,
    is_typo: bool | None = None,
    q: str | None = None,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    """导出训练数据集为 JSONL 行（每行一条反馈记录），便于模型微调。"""
    wheres: list[str] = []
    params: list[Any] = []
    if judgment:
        wheres.append("judgment = ?")
        params.append(judgment)
    if rule_id:
        wheres.append("rule_id = ?")
        params.append(rule_id)
    if is_typo is not None:
        wheres.append("is_typo = ?")
        params.append(1 if is_typo else 0)
    if user_id is not None:
        wheres.append("user_id = ?")
        params.append(user_id)
    if q:
        wheres.append("(title LIKE ? OR detail LIKE ? OR evidence LIKE ? OR typo_wrong LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like, like])
    where_sql = (" WHERE " + " AND ".join(wheres)) if wheres else ""
    with _conn() as c:
        rows = c.execute(
            f"SELECT * FROM feedback_records{where_sql} ORDER BY created_at DESC",
            params,
        ).fetchall()
    out = []
    for r in rows:
        d = _row_to_dict(r)
        d.pop("involved_files", None)  # 已展开在 original 结构外，避免冗余
        out.append(d)
    return out
