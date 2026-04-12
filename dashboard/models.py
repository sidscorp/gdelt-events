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

USERS_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "users.db"


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
            keywords_json TEXT NOT NULL,
            scan_description INTEGER DEFAULT 1,
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
    """)
    con.close()


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


def get_user_pills(user_id: int) -> list[dict]:
    con = get_user_db()
    rows = con.execute(
        "SELECT p.*, j.status as job_status, j.progress_pct, j.elapsed_seconds "
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
