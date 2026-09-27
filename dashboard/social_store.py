"""SQLite state for the GDELT Monitor social publishing workflow.

This is deliberately separate from DuckDB.  Candidate review and publishing
write small rows frequently, while the news database follows a strict
single-writer discipline.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from _paths import DATA_DIR


SOCIAL_DB_PATH = Path(os.environ.get("GDELT_SOCIAL_DB") or (DATA_DIR / "social.db"))
VALID_MODES = {"off", "review", "auto"}
FINAL_STATUSES = {"published", "rejected", "expired"}


def get_social_db() -> sqlite3.Connection:
    SOCIAL_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(SOCIAL_DB_PATH), timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=15000")
    return con


def init_social_db() -> None:
    con = get_social_db()
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS social_candidates (
            cluster_id          TEXT PRIMARY KEY,
            topic_id            TEXT NOT NULL,
            topic_name          TEXT NOT NULL,
            title               TEXT NOT NULL,
            title_key           TEXT NOT NULL,
            source_count        INTEGER NOT NULL,
            coverage_span_hours REAL NOT NULL,
            first_seen          INTEGER,
            latest_seen         INTEGER,
            importance_score    REAL NOT NULL DEFAULT 0,
            member_tags         INTEGER NOT NULL DEFAULT 0,
            event_url           TEXT NOT NULL,
            members_json        TEXT NOT NULL DEFAULT '[]',
            explanation         TEXT,
            explanation_kind    TEXT NOT NULL DEFAULT 'facts',
            grounding_json      TEXT NOT NULL DEFAULT '{}',
            post_text           TEXT,
            status              TEXT NOT NULL DEFAULT 'pending',
            rejection_reason    TEXT,
            review_decision     TEXT,
            reviewed_by         INTEGER,
            reviewed_at         TEXT,
            created_at          TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at          TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_social_candidate_status
            ON social_candidates(status, created_at DESC);
        CREATE INDEX IF NOT EXISTS ix_social_candidate_topic
            ON social_candidates(topic_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS social_publications (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            cluster_id     TEXT NOT NULL UNIQUE REFERENCES social_candidates(cluster_id),
            topic_id       TEXT NOT NULL,
            record_uri     TEXT,
            record_cid     TEXT,
            record_key     TEXT NOT NULL UNIQUE,
            post_text      TEXT NOT NULL,
            event_url      TEXT NOT NULL,
            published_at   TEXT,
            like_count     INTEGER,
            repost_count   INTEGER,
            reply_count    INTEGER,
            last_checked_at TEXT,
            error_message  TEXT,
            created_at     TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS social_runs (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at     TEXT NOT NULL,
            completed_at   TEXT,
            outcome        TEXT,
            scanned        INTEGER NOT NULL DEFAULT 0,
            queued         INTEGER NOT NULL DEFAULT 0,
            published      INTEGER NOT NULL DEFAULT 0,
            error_message  TEXT
        );

        CREATE TABLE IF NOT EXISTS social_settings (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS social_daily_visits (
            day        TEXT NOT NULL,
            cluster_id TEXT NOT NULL,
            source     TEXT NOT NULL,
            visits     INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(day, cluster_id, source)
        );
        """
    )
    cols = {r[1] for r in con.execute("PRAGMA table_info(social_candidates)").fetchall()}
    if "review_decision" not in cols:
        con.execute("ALTER TABLE social_candidates ADD COLUMN review_decision TEXT")
    con.execute(
        "INSERT OR IGNORE INTO social_settings(key, value) VALUES ('mode', 'review')"
    )
    con.execute(
        "INSERT OR IGNORE INTO social_settings(key, value) VALUES ('auto_ready', '0')"
    )
    con.commit()
    con.close()


def _row_dict(row):
    return dict(row) if row is not None else None


def setting(key: str, default: str | None = None) -> str | None:
    con = get_social_db()
    row = con.execute("SELECT value FROM social_settings WHERE key=?", (key,)).fetchone()
    con.close()
    return row[0] if row else default


def set_setting(key: str, value: str) -> None:
    con = get_social_db()
    con.execute(
        "INSERT INTO social_settings(key, value, updated_at) VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
        (key, value),
    )
    con.commit()
    con.close()


def set_mode(mode: str) -> tuple[bool, str]:
    if mode not in VALID_MODES:
        return False, "invalid mode"
    if mode == "auto" and setting("auto_ready", "0") != "1":
        return False, "auto mode is locked until the reviewed pilot passes calibration"
    set_setting("mode", mode)
    return True, mode


def upsert_candidate(candidate: dict) -> bool:
    """Insert a candidate once. Returns True only for a new queue row."""
    con = get_social_db()
    before = con.total_changes
    con.execute(
        """
        INSERT OR IGNORE INTO social_candidates (
            cluster_id, topic_id, topic_name, title, title_key, source_count,
            coverage_span_hours, first_seen, latest_seen, importance_score,
            member_tags, event_url, members_json, explanation,
            explanation_kind, grounding_json, post_text, status, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            candidate["cluster_id"], candidate["topic_id"], candidate["topic_name"],
            candidate["title"], candidate["title_key"], candidate["source_count"],
            candidate["coverage_span_hours"], candidate.get("first_seen"),
            candidate.get("latest_seen"), candidate.get("importance_score", 0),
            candidate.get("member_tags", 0), candidate["event_url"],
            json.dumps(candidate.get("members") or [], ensure_ascii=False),
            candidate.get("explanation"), candidate.get("explanation_kind", "facts"),
            json.dumps(candidate.get("grounding") or {}, ensure_ascii=False),
            candidate.get("post_text"), candidate.get("status", "pending"),
            candidate["expires_at"],
        ),
    )
    inserted = con.total_changes > before
    con.commit()
    con.close()
    return inserted


def list_candidates(limit: int = 100) -> list[dict]:
    con = get_social_db()
    rows = con.execute(
        "SELECT * FROM social_candidates ORDER BY "
        "CASE status WHEN 'pending' THEN 0 WHEN 'approved' THEN 1 "
        "WHEN 'publishing' THEN 2 ELSE 3 END, created_at DESC LIMIT ?",
        (int(limit),),
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def get_candidate(cluster_id: str) -> dict | None:
    con = get_social_db()
    row = con.execute(
        "SELECT * FROM social_candidates WHERE cluster_id=?", (cluster_id,)
    ).fetchone()
    con.close()
    return _row_dict(row)


def recent_titles(hours: int = 48) -> list[tuple[str, str]]:
    con = get_social_db()
    rows = con.execute(
        "SELECT cluster_id, title_key FROM social_candidates "
        "WHERE created_at >= datetime('now', ?) AND status <> 'rejected'",
        (f"-{int(hours)} hours",),
    ).fetchall()
    con.close()
    return [(r[0], r[1]) for r in rows]


def count_created_today() -> int:
    con = get_social_db()
    row = con.execute(
        "SELECT count(*) FROM social_candidates WHERE date(created_at)=date('now')"
    ).fetchone()
    con.close()
    return int(row[0] or 0)


def review_candidate(cluster_id: str, action: str, user_id: int,
                     explanation: str | None = None,
                     reason: str | None = None) -> tuple[bool, str]:
    if action not in {"approve", "reject"}:
        return False, "invalid action"
    con = get_social_db()
    row = con.execute(
        "SELECT status, expires_at FROM social_candidates WHERE cluster_id=?",
        (cluster_id,),
    ).fetchone()
    if not row:
        con.close()
        return False, "candidate not found"
    if row["status"] in FINAL_STATUSES:
        con.close()
        return False, f"candidate is already {row['status']}"
    status = "approved" if action == "approve" else "rejected"
    if status == "approved" and row["expires_at"] <= datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"):
        con.execute(
            "UPDATE social_candidates SET status='expired', updated_at=datetime('now') "
            "WHERE cluster_id=?", (cluster_id,)
        )
        con.commit()
        con.close()
        return False, "candidate expired before approval"
    fields = [
        "status=?", "review_decision=?", "reviewed_by=?", "reviewed_at=datetime('now')",
        "rejection_reason=?", "updated_at=datetime('now')",
    ]
    values: list = [status, action, user_id, (reason or "").strip()[:500] or None]
    if explanation is not None:
        fields.extend(["explanation=?", "explanation_kind='edited'"])
        values.append(explanation.strip()[:400])
    values.append(cluster_id)
    con.execute(
        f"UPDATE social_candidates SET {', '.join(fields)} WHERE cluster_id=?", values
    )
    con.commit()
    con.close()
    return True, status


def update_post_text(cluster_id: str, post_text: str) -> None:
    con = get_social_db()
    con.execute(
        "UPDATE social_candidates SET post_text=?, updated_at=datetime('now') "
        "WHERE cluster_id=?", (post_text, cluster_id)
    )
    con.commit()
    con.close()


def expire_stale() -> int:
    con = get_social_db()
    cur = con.execute(
        "UPDATE social_candidates SET status='expired', updated_at=datetime('now') "
        "WHERE status IN ('pending','approved') AND expires_at <= datetime('now')"
    )
    con.commit()
    n = cur.rowcount
    con.close()
    return max(0, n)


def next_approved() -> dict | None:
    con = get_social_db()
    row = con.execute(
        "SELECT * FROM social_candidates WHERE status='approved' "
        "AND expires_at > datetime('now') ORDER BY importance_score DESC, created_at ASC LIMIT 1"
    ).fetchone()
    con.close()
    return _row_dict(row)


def publication_counts(topic_id: str) -> tuple[int, int, str | None]:
    con = get_social_db()
    total = con.execute(
        "SELECT count(*) FROM social_publications "
        "WHERE published_at >= datetime('now','-24 hours') AND record_uri IS NOT NULL"
    ).fetchone()[0]
    topic = con.execute(
        "SELECT count(*) FROM social_publications WHERE topic_id=? "
        "AND published_at >= datetime('now','-24 hours') AND record_uri IS NOT NULL",
        (topic_id,),
    ).fetchone()[0]
    latest = con.execute(
        "SELECT max(published_at) FROM social_publications WHERE record_uri IS NOT NULL"
    ).fetchone()[0]
    con.close()
    return int(total or 0), int(topic or 0), latest


def mark_publishing(candidate: dict, record_key: str, post_text: str) -> None:
    con = get_social_db()
    con.execute("BEGIN IMMEDIATE")
    con.execute(
        "UPDATE social_candidates SET status='publishing', post_text=?, updated_at=datetime('now') "
        "WHERE cluster_id=? AND status='approved'", (post_text, candidate["cluster_id"])
    )
    con.execute(
        "INSERT OR IGNORE INTO social_publications "
        "(cluster_id, topic_id, record_key, post_text, event_url) VALUES (?, ?, ?, ?, ?)",
        (candidate["cluster_id"], candidate["topic_id"], record_key,
         post_text, candidate["event_url"]),
    )
    con.commit()
    con.close()


def mark_published(cluster_id: str, uri: str, cid: str | None) -> None:
    con = get_social_db()
    con.execute("BEGIN IMMEDIATE")
    con.execute(
        "UPDATE social_publications SET record_uri=?, record_cid=?, "
        "published_at=datetime('now'), error_message=NULL WHERE cluster_id=?",
        (uri, cid, cluster_id),
    )
    con.execute(
        "UPDATE social_candidates SET status='published', updated_at=datetime('now') "
        "WHERE cluster_id=?", (cluster_id,)
    )
    con.commit()
    con.close()


def mark_publish_error(cluster_id: str, message: str) -> None:
    con = get_social_db()
    con.execute("BEGIN IMMEDIATE")
    con.execute(
        "UPDATE social_publications SET error_message=? WHERE cluster_id=?",
        (message[:1000], cluster_id),
    )
    con.execute(
        "UPDATE social_candidates SET status='approved', updated_at=datetime('now') "
        "WHERE cluster_id=? AND status='publishing'", (cluster_id,)
    )
    con.commit()
    con.close()


def begin_run() -> int:
    con = get_social_db()
    cur = con.execute(
        "INSERT INTO social_runs(started_at, outcome) VALUES (datetime('now'), 'running')"
    )
    con.commit()
    run_id = int(cur.lastrowid)
    con.close()
    return run_id


def finish_run(run_id: int, outcome: str, scanned: int = 0, queued: int = 0,
               published: int = 0, error: str | None = None) -> None:
    con = get_social_db()
    con.execute(
        "UPDATE social_runs SET completed_at=datetime('now'), outcome=?, scanned=?, queued=?, "
        "published=?, error_message=? WHERE id=?",
        (outcome, scanned, queued, published, (error or "")[:1000] or None, run_id),
    )
    con.commit()
    con.close()


def status_summary() -> dict:
    con = get_social_db()
    counts = {
        r[0]: int(r[1]) for r in con.execute(
            "SELECT status, count(*) FROM social_candidates GROUP BY status"
        ).fetchall()
    }
    last_run = con.execute(
        "SELECT * FROM social_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    last_pub = con.execute(
        "SELECT * FROM social_publications WHERE record_uri IS NOT NULL "
        "ORDER BY published_at DESC LIMIT 1"
    ).fetchone()
    reviewed = con.execute(
        "SELECT count(*) FROM social_candidates WHERE reviewed_at IS NOT NULL"
    ).fetchone()[0]
    approved = con.execute(
        "SELECT count(*) FROM social_candidates WHERE reviewed_at IS NOT NULL "
        "AND review_decision='approve'"
    ).fetchone()[0]
    visits = con.execute(
        "SELECT coalesce(sum(visits),0) FROM social_daily_visits "
        "WHERE day >= date('now','-30 days')"
    ).fetchone()[0]
    con.close()
    return {
        "mode": setting("mode", "review"),
        "auto_ready": setting("auto_ready", "0") == "1",
        "counts": counts,
        "last_run": _row_dict(last_run),
        "last_publication": _row_dict(last_pub),
        "reviewed": int(reviewed or 0),
        "approved": int(approved or 0),
        "approval_rate": round((approved or 0) / reviewed, 3) if reviewed else None,
        "bluesky_visits_30d": int(visits or 0),
    }


def published_context(cluster_id: str) -> dict | None:
    con = get_social_db()
    row = con.execute(
        "SELECT c.topic_name, c.explanation, c.explanation_kind, c.source_count, "
        "p.record_uri, p.published_at FROM social_candidates c "
        "JOIN social_publications p USING(cluster_id) "
        "WHERE c.cluster_id=? AND p.record_uri IS NOT NULL",
        (cluster_id,),
    ).fetchone()
    con.close()
    return _row_dict(row)


def record_visit(cluster_id: str, source: str) -> None:
    source = (source or "")[:30].lower()
    if source != "bsky":
        return
    con = get_social_db()
    con.execute(
        "INSERT INTO social_daily_visits(day, cluster_id, source, visits) "
        "VALUES (date('now'), ?, ?, 1) "
        "ON CONFLICT(day,cluster_id,source) DO UPDATE SET visits=visits+1",
        (cluster_id, source),
    )
    con.commit()
    con.close()
