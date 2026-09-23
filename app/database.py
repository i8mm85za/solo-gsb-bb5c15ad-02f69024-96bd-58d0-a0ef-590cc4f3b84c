"""SQLite persistence. The database file lives under DATA_DIR (container volume)."""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

DEFAULT_DATA_DIR = os.environ.get("CALIB_DATA_DIR", "/data")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL DEFAULT 'judge',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    contestant TEXT NOT NULL,
    judge TEXT NOT NULL,
    item TEXT NOT NULL,
    score REAL NOT NULL,
    row_order INTEGER NOT NULL,
    UNIQUE (session_id, contestant, judge, item),
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS decisions (
    score_id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    use_raw INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    source_rule_id INTEGER,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (score_id) REFERENCES scores(id) ON DELETE CASCADE,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
    FOREIGN KEY (source_rule_id) REFERENCES adjudication_rules(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS adjudication_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    name TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 100,
    judge TEXT,
    item TEXT,
    raw_min REAL,
    raw_max REAL,
    cal_min REAL,
    cal_max REAL,
    use_raw INTEGER NOT NULL DEFAULT 0,
    reason_template TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (session_id, id),
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_adjudication_rules_session
    ON adjudication_rules(session_id, position);
"""


class NotFoundError(LookupError):
    pass


def db_path() -> str:
    return os.path.join(DEFAULT_DATA_DIR, "calibration.db")


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    os.makedirs(DEFAULT_DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        # Lightweight migration for databases created before batch rules.
        if "source_rule_id" not in _table_columns(conn, "decisions"):
            conn.execute("ALTER TABLE decisions ADD COLUMN source_rule_id INTEGER")


def _nullable(value: Any) -> float | None:
    return None if value is None else float(value)


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def ensure_session(conn: sqlite3.Connection, session_id: str, scope: str = "judge") -> None:
    conn.execute(
        "INSERT INTO sessions(id, scope) VALUES(?, ?) "
        "ON CONFLICT(id) DO UPDATE SET updated_at = datetime('now')",
        (session_id, scope),
    )


def set_scope(conn: sqlite3.Connection, session_id: str, scope: str) -> None:
    cur = conn.execute("UPDATE sessions SET scope = ?, updated_at = datetime('now') WHERE id = ?", (scope, session_id))
    if cur.rowcount == 0:
        raise NotFoundError(f"会话不存在: {session_id}")


def get_scope(conn: sqlite3.Connection, session_id: str) -> str | None:
    row = conn.execute("SELECT scope FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return row["scope"] if row else None


def replace_scores(conn: sqlite3.Connection, session_id: str, records: Sequence[dict[str, Any]]) -> int:
    """Wipe the session's scores/decisions and insert new records."""
    conn.execute("DELETE FROM decisions WHERE session_id = ?", (session_id,))
    conn.execute("DELETE FROM scores WHERE session_id = ?", (session_id,))
    conn.executemany(
        "INSERT INTO scores(session_id, contestant, judge, item, score, row_order) "
        "VALUES(?, ?, ?, ?, ?, ?)",
        [
            (session_id, r["contestant"], r["judge"], r["item"], float(r["score"]), idx)
            for idx, r in enumerate(records)
        ],
    )
    conn.execute("UPDATE sessions SET updated_at = datetime('now') WHERE id = ?", (session_id,))
    return len(records)


def load_scores(conn: sqlite3.Connection, session_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, contestant, judge, item, score FROM scores "
        "WHERE session_id = ? ORDER BY row_order, id",
        (session_id,),
    ).fetchall()
    return [
        {"id": r["id"], "contestant": r["contestant"], "judge": r["judge"], "item": r["item"], "score": r["score"]}
        for r in rows
    ]


def load_decisions(conn: sqlite3.Connection, session_id: str) -> dict[int, tuple[bool, str]]:
    rows = conn.execute(
        "SELECT score_id, use_raw, reason FROM decisions WHERE session_id = ?",
        (session_id,),
    ).fetchall()
    return {r["score_id"]: (bool(r["use_raw"]), r["reason"]) for r in rows}


def _rule_from_row(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": r["id"],
        "position": r["position"],
        "name": r["name"],
        "priority": r["priority"],
        "judge": r["judge"],
        "item": r["item"],
        "raw_min": r["raw_min"],
        "raw_max": r["raw_max"],
        "cal_min": r["cal_min"],
        "cal_max": r["cal_max"],
        "use_raw": bool(r["use_raw"]),
        "reason_template": r["reason_template"],
    }


def load_rules(conn: sqlite3.Connection, session_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM adjudication_rules WHERE session_id = ? ORDER BY position, id",
        (session_id,),
    ).fetchall()
    return [_rule_from_row(r) for r in rows]


def replace_rules(
    conn: sqlite3.Connection, session_id: str, rules: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Replace the whole rule set for a session inside the current transaction.

    Old decisions' provenance is detached (source_rule_id -> NULL) before the
    rules are deleted, then the new rules are inserted in list order. Returns
    the persisted rows (with fresh ids).
    """
    conn.execute(
        "UPDATE decisions SET source_rule_id = NULL WHERE session_id = ?",
        (session_id,),
    )
    conn.execute(
        "DELETE FROM adjudication_rules WHERE session_id = ?", (session_id,)
    )
    saved: list[dict[str, Any]] = []
    for position, rule in enumerate(rules):
        cur = conn.execute(
            "INSERT INTO adjudication_rules("
            "session_id, position, name, priority, judge, item, "
            "raw_min, raw_max, cal_min, cal_max, use_raw, reason_template) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                position,
                rule["name"],
                int(rule["priority"]),
                _optional_text(rule.get("judge")),
                _optional_text(rule.get("item")),
                _nullable(rule.get("raw_min")),
                _nullable(rule.get("raw_max")),
                _nullable(rule.get("cal_min")),
                _nullable(rule.get("cal_max")),
                1 if rule["use_raw"] else 0,
                rule.get("reason_template", ""),
            ),
        )
        row = conn.execute(
            "SELECT * FROM adjudication_rules WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        saved.append(_rule_from_row(row))
    conn.execute(
        "UPDATE sessions SET updated_at = datetime('now') WHERE id = ?",
        (session_id,),
    )
    return saved


def bulk_upsert_decisions(
    conn: sqlite3.Connection,
    session_id: str,
    outcomes: Sequence[dict[str, Any]],
    source_rule_id_by_score: dict[int, int | None] | None = None,
) -> tuple[int, int]:
    """Upsert many decisions in the current transaction.

    Returns ``(written, changed)`` where written is the number of upserted
    rows and changed counts rows whose stored verdict/reason actually moved.
    """
    source_rule_id_by_score = source_rule_id_by_score or {}
    written = changed = 0
    for outcome in outcomes:
        score_id = int(outcome["score_id"])
        use_raw = 1 if outcome["use_raw"] else 0
        reason = outcome.get("reason", "")
        existing = conn.execute(
            "SELECT use_raw, reason FROM decisions WHERE score_id = ? AND session_id = ?",
            (score_id, session_id),
        ).fetchone()
        if existing is None:
            changed += 1
        elif bool(existing["use_raw"]) != bool(use_raw) or (existing["reason"] or "") != reason:
            changed += 1
        conn.execute(
            "INSERT INTO decisions(score_id, session_id, use_raw, reason, source_rule_id) "
            "VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(score_id) DO UPDATE SET use_raw=excluded.use_raw, "
            "reason=excluded.reason, source_rule_id=excluded.source_rule_id, "
            "updated_at=datetime('now')",
            (score_id, session_id, use_raw, reason, source_rule_id_by_score.get(score_id)),
        )
        written += 1
    return written, changed


def upsert_decision(conn: sqlite3.Connection, session_id: str, score_id: int, use_raw: bool, reason: str) -> None:
    score = conn.execute(
        "SELECT 1 FROM scores WHERE id = ? AND session_id = ?", (score_id, session_id)
    ).fetchone()
    if not score:
        raise NotFoundError(f"评分记录不存在: id={score_id}")
    conn.execute(
        "INSERT INTO decisions(score_id, session_id, use_raw, reason) VALUES(?, ?, ?, ?) "
        "ON CONFLICT(score_id) DO UPDATE SET use_raw=excluded.use_raw, "
        "reason=excluded.reason, updated_at=datetime('now')",
        (score_id, session_id, 1 if use_raw else 0, reason),
    )


def reset_decisions(conn: sqlite3.Connection, session_id: str) -> int:
    cur = conn.execute("DELETE FROM decisions WHERE session_id = ?", (session_id,))
    return cur.rowcount


def delete_session(conn: sqlite3.Connection, session_id: str) -> bool:
    cur = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    return cur.rowcount > 0


def session_has_data(conn: sqlite3.Connection, session_id: str) -> bool:
    row = conn.execute("SELECT COUNT(*) AS n FROM scores WHERE session_id = ?", (session_id,)).fetchone()
    return row["n"] > 0
