"""SQLite models for user auth and custom pills.

Separate from DuckDB (which handles article data) because DuckDB is
single-writer and user operations need concurrent writes.
"""

import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from werkzeug.security import generate_password_hash, check_password_hash

from _paths import USERS_DB_PATH


def get_user_db() -> sqlite3.Connection:
    con = sqlite3.connect(str(USERS_DB_PATH))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_user_db():
    con = get_user_db()
    con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            display_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            is_approved INTEGER DEFAULT 0,
            is_admin INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS custom_pills (
            id TEXT PRIMARY KEY,
            user_id INTEGER REFERENCES users(id),
            name TEXT NOT NULL,
            keywords_json TEXT NOT NULL DEFAULT '[]',
            scan_description INTEGER DEFAULT 1,
            pill_type TEXT DEFAULT 'keyword',
            description_text TEXT,
            description_embedding BLOB,
            similarity_threshold REAL DEFAULT 0.55,
            scan_days INTEGER DEFAULT 7,
            created_at TEXT DEFAULT (datetime('now')),
            article_count INTEGER DEFAULT 0,
            last_built_at TEXT
        );

        CREATE TABLE IF NOT EXISTS pill_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pill_id TEXT REFERENCES custom_pills(id) ON DELETE CASCADE,
            status TEXT DEFAULT 'queued',
            progress_pct INTEGER DEFAULT 0,
            rows_scanned INTEGER DEFAULT 0,
            rows_matched INTEGER DEFAULT 0,
            started_at TEXT,
            completed_at TEXT,
            error_message TEXT,
            elapsed_seconds REAL
        );


        CREATE TABLE IF NOT EXISTS briefing_cache (
            cache_key TEXT PRIMARY KEY,
            briefing TEXT NOT NULL,
            article_count INTEGER,
            generated_at TEXT DEFAULT (datetime('now'))
        );
    """)
    # Migrate: add columns if missing (idempotent)
    _migrate_semantic_columns(con)
    _migrate_briefing_columns(con)
    _migrate_perf_table(con)
    con.close()


def _migrate_perf_table(con):
    """RUM samples (real-user front-end timings) posted by the client beacon."""
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS perf_samples (
            id     INTEGER PRIMARY KEY AUTOINCREMENT,
            ts     TEXT DEFAULT (datetime('now')),
            metric TEXT,
            value  REAL,
            view   TEXT,
            hours  INTEGER
        )
        """
    )
    con.commit()


# ---------------------------------------------------------------------------
# User operations
# ---------------------------------------------------------------------------

def create_user(email: str, display_name: str, password: str) -> int:
    con = get_user_db()
    is_first = con.execute("SELECT count(*) FROM users").fetchone()[0] == 0
    cur = con.execute(
        "INSERT INTO users (email, display_name, password_hash, is_approved, is_admin) "
        "VALUES (?, ?, ?, ?, ?)",
        (email.lower().strip(), display_name.strip(),
         generate_password_hash(password),
         1 if is_first else 0,
         1 if is_first else 0),
    )
    con.commit()
    uid = cur.lastrowid
    con.close()
    return uid


def authenticate(email: str, password: str) -> dict | None:
    con = get_user_db()
    row = con.execute(
        "SELECT * FROM users WHERE email = ?", (email.lower().strip(),)
    ).fetchone()
    con.close()
    if row and check_password_hash(row["password_hash"], password):
        return dict(row)
    return None


def get_user_by_id(user_id: int) -> dict | None:
    con = get_user_db()
    row = con.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    con.close()
    return dict(row) if row else None


def list_users() -> list[dict]:
    con = get_user_db()
    rows = con.execute(
        "SELECT u.*, (SELECT count(*) FROM custom_pills WHERE user_id=u.id) as pill_count "
        "FROM users u ORDER BY u.created_at DESC"
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def approve_user(user_id: int):
    con = get_user_db()
    con.execute("UPDATE users SET is_approved = 1 WHERE id = ?", (user_id,))
    con.commit()
    con.close()


def reject_user(user_id: int):
    con = get_user_db()
    con.execute("DELETE FROM users WHERE id = ? AND is_admin = 0", (user_id,))
    con.commit()
    con.close()


def email_exists(email: str) -> bool:
    con = get_user_db()
    row = con.execute(
        "SELECT 1 FROM users WHERE email = ?", (email.lower().strip(),)
    ).fetchone()
    con.close()
    return row is not None


# ---------------------------------------------------------------------------
# Pill operations
# ---------------------------------------------------------------------------

def _migrate_briefing_columns(con):
    """Add the briefing citations column if it doesn't exist (for upgrades)."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(briefing_cache)").fetchall()}
    if "sources_json" not in cols:
        con.execute("ALTER TABLE briefing_cache ADD COLUMN sources_json TEXT")
    con.commit()


def _migrate_semantic_columns(con):
    """Add semantic pill columns if they don't exist (for upgrades)."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(custom_pills)").fetchall()}
    migrations = {
        "pill_type": "ALTER TABLE custom_pills ADD COLUMN pill_type TEXT DEFAULT 'keyword'",
        "description_text": "ALTER TABLE custom_pills ADD COLUMN description_text TEXT",
        "description_embedding": "ALTER TABLE custom_pills ADD COLUMN description_embedding BLOB",
        "similarity_threshold": "ALTER TABLE custom_pills ADD COLUMN similarity_threshold REAL DEFAULT 0.55",
        "scan_days": "ALTER TABLE custom_pills ADD COLUMN scan_days INTEGER DEFAULT 7",
    }
    for col, sql in migrations.items():
        if col not in cols:
            con.execute(sql)
    con.commit()


def _slugify(name: str) -> str:
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower().strip()).strip("-")
    return slug[:50] or f"pill-{secrets.token_hex(4)}"


def create_pill(user_id: int, name: str, keywords: list[str],
                scan_description: bool = True) -> str:
    pill_id = _slugify(name)
    con = get_user_db()
    # Ensure unique id
    base = pill_id
    counter = 1
    while con.execute("SELECT 1 FROM custom_pills WHERE id=?", (pill_id,)).fetchone():
        pill_id = f"{base}-{counter}"
        counter += 1
    con.execute(
        "INSERT INTO custom_pills (id, user_id, name, keywords_json, scan_description) "
        "VALUES (?, ?, ?, ?, ?)",
        (pill_id, user_id, name.strip(), json.dumps(keywords), int(scan_description)),
    )
    con.execute(
        "INSERT INTO pill_jobs (pill_id, status) VALUES (?, 'queued')",
        (pill_id,),
    )
    con.commit()
    con.close()
    return pill_id


def create_semantic_pill(user_id: int, name: str, description: str,
                         embedding: bytes, threshold: float = 0.55,
                         scan_days: int = 7) -> str:
    """Create a semantic pill that matches articles by embedding similarity."""
    pill_id = _slugify(name)
    con = get_user_db()
    base = pill_id
    counter = 1
    while con.execute("SELECT 1 FROM custom_pills WHERE id=?", (pill_id,)).fetchone():
        pill_id = f"{base}-{counter}"
        counter += 1
    con.execute(
        "INSERT INTO custom_pills "
        "(id, user_id, name, pill_type, description_text, description_embedding, "
        " similarity_threshold, scan_days, keywords_json, scan_description) "
        "VALUES (?, ?, ?, 'semantic', ?, ?, ?, ?, '[]', 1)",
        (pill_id, user_id, name.strip(), description.strip(),
         embedding, threshold, scan_days),
    )
    con.execute(
        "INSERT INTO pill_jobs (pill_id, status) VALUES (?, 'queued')",
        (pill_id,),
    )
    con.commit()
    con.close()
    return pill_id


def get_user_pills(user_id: int) -> list[dict]:
    con = get_user_db()
    rows = con.execute(
        "SELECT p.id, p.user_id, p.name, p.keywords_json, p.scan_description, "
        "p.pill_type, p.description_text, p.similarity_threshold, p.scan_days, "
        "p.created_at, p.article_count, p.last_built_at, "
        "j.status as job_status, j.progress_pct, j.elapsed_seconds "
        "FROM custom_pills p "
        "LEFT JOIN pill_jobs j ON j.pill_id = p.id "
        "WHERE p.user_id = ? "
        "ORDER BY p.created_at DESC",
        (user_id,),
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def get_pill(pill_id: str) -> dict | None:
    con = get_user_db()
    row = con.execute("SELECT * FROM custom_pills WHERE id = ?", (pill_id,)).fetchone()
    con.close()
    return dict(row) if row else None


def get_pill_job_status(pill_id: str) -> dict | None:
    con = get_user_db()
    row = con.execute(
        "SELECT * FROM pill_jobs WHERE pill_id = ? ORDER BY id DESC LIMIT 1",
        (pill_id,),
    ).fetchone()
    con.close()
    return dict(row) if row else None


def delete_pill(pill_id: str):
    con = get_user_db()
    con.execute("DELETE FROM pill_jobs WHERE pill_id = ?", (pill_id,))
    con.execute("DELETE FROM custom_pills WHERE id = ?", (pill_id,))
    con.commit()
    con.close()


def get_all_custom_pills() -> list[dict]:
    """All custom pills across all users — for the incremental tagger."""
    con = get_user_db()
    rows = con.execute("SELECT id, keywords_json, scan_description FROM custom_pills").fetchall()
    con.close()
    return [dict(r) for r in rows]
