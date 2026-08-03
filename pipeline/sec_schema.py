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

        -- One CIK files under several tickers (GOOGL/GOOG, BRK-A/BRK-B). Keeping
        -- only one meant half of a company's share classes resolved to nothing.
        CREATE TABLE IF NOT EXISTS tickers (
            ticker     TEXT NOT NULL,
            cik        INTEGER NOT NULL,
            is_primary INTEGER DEFAULT 0,
            exchange   TEXT,
            PRIMARY KEY (ticker, cik)
        );
        CREATE INDEX IF NOT EXISTS ix_tickers_cik ON tickers(cik);

        -- Everything a sentence on the page can be built from. Computed, never
        -- generated: if a number is not in here, no observation may state it.
        CREATE TABLE IF NOT EXISTS derived (
            cik INTEGER NOT NULL, period_end TEXT NOT NULL, fp TEXT NOT NULL,
            revenue_yoy REAL, revenue_qoq REAL,
            net_income_yoy REAL, operating_income_yoy REAL,
            gross_margin REAL, operating_margin REAL, net_margin REAL,
            operating_margin_yoy_pp REAL, net_margin_yoy_pp REAL,
            nonop_income REAL, nonop_share_pretax REAL,
            rev_growth_rank_n INTEGER, rev_growth_is_best INTEGER,
            rev_growth_is_worst INTEGER, decline_streak INTEGER,
            return_on_equity REAL, return_on_assets REAL,
            sector_gross_margin_pct REAL, sector_net_margin_pct REAL,
            sector_peers INTEGER,
            PRIMARY KEY (cik, period_end, fp)
        );

        CREATE TABLE IF NOT EXISTS sector_stats (
            sic TEXT NOT NULL, period_end TEXT NOT NULL, metric TEXT NOT NULL,
            p25 REAL, p50 REAL, p75 REAL, n INTEGER,
            PRIMARY KEY (sic, period_end, metric)
        );
    """)

    # Columns added after the first release; ALTER is the migration path since
    # the table already exists in production with 15,909 rows.
    # Same story for `derived`: ROE/ROA were added after the table shipped, and
    # CREATE TABLE IF NOT EXISTS does not add columns to an existing table.
    have_d = {r[1] for r in con.execute("PRAGMA table_info(derived)")}
    for col in ("return_on_equity", "return_on_assets"):
        if col not in have_d:
            con.execute(f"ALTER TABLE derived ADD COLUMN {col} REAL")

    have = {r[1] for r in con.execute("PRAGMA table_info(companies)")}
    for col, decl in (("sic", "TEXT"), ("sic_description", "TEXT"),
                      ("exchange", "TEXT"), ("fiscal_year_end", "TEXT")):
        if col not in have:
            con.execute(f"ALTER TABLE companies ADD COLUMN {col} {decl}")
    con.execute("CREATE INDEX IF NOT EXISTS ix_companies_sic ON companies(sic)")

    # FTS5 over company names so "alphabet" and a typo both find something. Kept
    # as a plain table rebuilt by sec_derive rather than trigger-synced: the
    # ingest writes in bulk and triggers would fire 289k times.
    con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS companies_fts "
                "USING fts5(name, ticker, cik UNINDEXED, tokenize='porter unicode61')")
    con.commit()


def upsert_company(con: sqlite3.Connection, cik: int, ticker: str | None,
                   name: str | None, ts: str) -> None:
    # COALESCE so a companyfacts-only refresh never wipes SIC/exchange that the
    # submissions pass supplied.
    con.execute(
        "INSERT INTO companies (cik, ticker, name, updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(cik) DO UPDATE SET "
        "  ticker=COALESCE(excluded.ticker, companies.ticker), "
        "  name=COALESCE(excluded.name, companies.name), "
        "  updated_at=excluded.updated_at",
        (cik, ticker, name, ts),
    )


def upsert_profile(con: sqlite3.Connection, cik: int, name: str | None,
                   sic: str | None, sic_desc: str | None, exchange: str | None,
                   fye: str | None, tickers: list[str], ts: str) -> None:
    """Company profile from SEC submissions: industry, exchange, all tickers."""
    con.execute(
        "INSERT INTO companies (cik, ticker, name, sic, sic_description, exchange, "
        "  fiscal_year_end, updated_at) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(cik) DO UPDATE SET "
        "  ticker=COALESCE(excluded.ticker, companies.ticker), "
        "  name=COALESCE(excluded.name, companies.name), sic=excluded.sic, "
        "  sic_description=excluded.sic_description, exchange=excluded.exchange, "
        "  fiscal_year_end=excluded.fiscal_year_end, updated_at=excluded.updated_at",
        (cik, (tickers[0] if tickers else None), name, sic, sic_desc, exchange, fye, ts),
    )
    for i, t in enumerate(tickers):
        con.execute("INSERT OR REPLACE INTO tickers (ticker, cik, is_primary, exchange) "
                    "VALUES (?,?,?,?)", (t.upper(), cik, 1 if i == 0 else 0, exchange))


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
