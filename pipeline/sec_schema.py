"""SQLite store for SEC company financials.

SQLite rather than DuckDB deliberately: this is a small key-lookup table read on
every page render, sitting beside users.db, and DuckDB is single-writer - the
dashboard already contends for it. ~10.4k filers x ~24 periods is ~250k rows.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from pipeline.sec_normalize import CONCEPT_CHAINS

DB_NAME = "sec.db"

# Metric columns are generated from the concept map so the schema cannot drift
# away from what the normalizer actually produces.
METRIC_COLUMNS = sorted(CONCEPT_CHAINS)


def db_path(data_dir: Path) -> Path:
    return Path(data_dir) / DB_NAME


def connect(data_dir: Path, read_only: bool = False) -> sqlite3.Connection:
    p = db_path(data_dir)
    if read_only:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    else:
        con = sqlite3.connect(p)
    con.row_factory = sqlite3.Row
    return con


def create(con: sqlite3.Connection) -> None:
    cols = ",\n            ".join(f"{m} REAL" for m in METRIC_COLUMNS)
    con.executescript(f"""
        CREATE TABLE IF NOT EXISTS companies (
            cik        INTEGER PRIMARY KEY,
            ticker     TEXT,
            name       TEXT,
            updated_at TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_companies_ticker ON companies(ticker);

        CREATE TABLE IF NOT EXISTS snapshots (
            cik           INTEGER NOT NULL,
            period_end    TEXT    NOT NULL,
            fp            TEXT    NOT NULL,
            fy            INTEGER,
            form          TEXT,
            duration_days INTEGER,
            {cols},
            PRIMARY KEY (cik, period_end, fp)
        );
        CREATE INDEX IF NOT EXISTS ix_snapshots_cik_end
            ON snapshots(cik, period_end DESC);

        -- Every run writes a row. A pipeline that silently stops is the failure
        -- mode this system keeps hitting (Ollama, 11 days); make it queryable.
        CREATE TABLE IF NOT EXISTS ingest_log (
            ts            TEXT,
            mode          TEXT,
            ciks_touched  INTEGER,
            rows_written  INTEGER,
            elapsed_s     REAL,
            note          TEXT
        );

        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            val TEXT
        );
    """)
    con.commit()


def upsert_company(con: sqlite3.Connection, cik: int, ticker: str | None,
                   name: str | None, ts: str) -> None:
    con.execute(
        "INSERT INTO companies (cik, ticker, name, updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(cik) DO UPDATE SET ticker=excluded.ticker, "
        "name=excluded.name, updated_at=excluded.updated_at",
        (cik, ticker, name, ts),
    )


def upsert_snapshots(con: sqlite3.Connection, cik: int, rows: list[dict]) -> int:
    if not rows:
        return 0
    fixed = ["cik", "period_end", "fp", "fy", "form", "duration_days"]
    cols = fixed + METRIC_COLUMNS
    ph = ",".join("?" * len(cols))
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c not in ("cik", "period_end", "fp"))
    sql = (f"INSERT INTO snapshots ({','.join(cols)}) VALUES ({ph}) "
           f"ON CONFLICT(cik, period_end, fp) DO UPDATE SET {updates}")
    con.executemany(sql, [
        [cik, r["period_end"], r["fp"], r.get("fy"), r.get("form"), r.get("duration_days")]
        + [r.get(m) for m in METRIC_COLUMNS]
        for r in rows
    ])
    return len(rows)


def set_meta(con: sqlite3.Connection, key: str, val: str) -> None:
    con.execute("INSERT INTO meta (key, val) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET val=excluded.val", (key, val))


def get_meta(con: sqlite3.Connection, key: str) -> str | None:
    row = con.execute("SELECT val FROM meta WHERE key = ?", (key,)).fetchone()
    return row["val"] if row else None
