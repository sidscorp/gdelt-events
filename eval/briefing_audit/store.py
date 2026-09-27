"""Eval storage: its OWN SQLite file, never the dashboard's users.db.

Production data is read through a read-only connection (``mode=ro``), so the
eval can never hold a write lock on anything the live site uses.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Same override as dashboard/_paths.py, so the dev instance reads its own data.
DATA = Path(os.environ.get("GDELT_DATA_DIR") or (Path(__file__).resolve().parents[2] / "data"))
EVAL_DB = DATA / "briefing_eval.db"
USERS_DB = DATA / "users.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, started_at TEXT, finished_at TEXT,
    briefings INTEGER DEFAULT 0, sentences INTEGER DEFAULT 0, calls INTEGER DEFAULT 0,
    tokens_in INTEGER DEFAULT 0, cost_usd REAL DEFAULT 0, note TEXT
);
CREATE TABLE IF NOT EXISTS audits (
    briefing_id INTEGER PRIMARY KEY, run_id INTEGER, view_id TEXT, hours INTEGER,
    generated_at TEXT, trigger TEXT, audited_at TEXT, judge_version TEXT,
    n_sentences INTEGER, n_judged INTEGER, n_escalated INTEGER, lead_json TEXT
);
CREATE TABLE IF NOT EXISTS sentences (
    briefing_id INTEGER, idx INTEGER, section TEXT, text TEXT, cites TEXT, basis TEXT,
    flags TEXT, verdict TEXT, confidence REAL, probs TEXT, escalates REAL, adds_specifics REAL,
    judged INTEGER, escalate INTEGER, note TEXT,
    kimi_verdict TEXT, kimi_reason TEXT,
    PRIMARY KEY (briefing_id, idx)
);
CREATE INDEX IF NOT EXISTS ix_sent_verdict ON sentences (verdict);
CREATE INDEX IF NOT EXISTS ix_audit_time ON audits (generated_at);
"""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def eval_db(path: Path = EVAL_DB) -> sqlite3.Connection:
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def eval_db_ro(path: Path = EVAL_DB) -> sqlite3.Connection:
    """For readers (the admin page): no schema script, no WAL pragma, no writes.
    Raises sqlite3.OperationalError when the audit has never run."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def users_db_ro(path: Path = USERS_DB) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def pending_briefings(users: sqlite3.Connection, ev: sqlite3.Connection, since: str | None = None,
                      triggers: tuple[str, ...] | None = None, limit: int | None = None) -> list[sqlite3.Row]:
    """briefing_history rows not yet audited, oldest first."""
    done = {r[0] for r in ev.execute("SELECT briefing_id FROM audits")}
    q = ("SELECT id, view_id, hours, generated_at, trigger, briefing, sources_json, meta_json "
         "FROM briefing_history WHERE 1=1")
    args: list = []
    if since:
        q += " AND generated_at >= ?"
        args.append(since)
    if triggers:
        q += f" AND trigger IN ({','.join('?' * len(triggers))})"
        args.extend(triggers)
    q += " ORDER BY id"
    rows = [r for r in users.execute(q, args) if r["id"] not in done]
    return rows[:limit] if limit else rows


def save_audit(ev: sqlite3.Connection, run_id: int, row, units, flags, results, lead, judge_version):
    ev.execute("DELETE FROM sentences WHERE briefing_id = ?", (row["id"],))
    for u in units:
        v = results.get(u.idx, {})
        ev.execute(
            "INSERT INTO sentences (briefing_id, idx, section, text, cites, basis, flags, verdict, confidence, "
            "probs, escalates, adds_specifics, judged, escalate, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["id"], u.idx, u.section, u.text, json.dumps(u.cites), json.dumps(v.get("basis", [])),
             json.dumps(flags.get(u.idx, [])), v.get("verdict"), v.get("confidence"),
             json.dumps(v.get("probs", {})), v.get("escalates"), v.get("adds_specifics"),
             int(bool(v.get("judged"))), int(bool(v.get("escalate"))), v.get("note")))
    ev.execute(
        "INSERT OR REPLACE INTO audits (briefing_id, run_id, view_id, hours, generated_at, trigger, audited_at, "
        "judge_version, n_sentences, n_judged, n_escalated, lead_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (row["id"], run_id, row["view_id"], row["hours"], row["generated_at"], row["trigger"], now(),
         judge_version, len(units), sum(1 for v in results.values() if v.get("judged")),
         sum(1 for v in results.values() if v.get("escalate")), json.dumps(lead)))
    ev.commit()
