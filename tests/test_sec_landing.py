"""Tests for deterministic landing-page filing ordering and pagination inputs."""
from __future__ import annotations

import sqlite3
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "dashboard"))
sys.path.insert(0, str(ROOT))

# The query helper does not use Flask. Keep its unit test runnable with the
# system Python used for the SEC pipeline, which deliberately has no Flask.
flask = types.ModuleType("flask")
class _Blueprint:
    def __init__(self, *args, **kwargs):
        pass
    def route(self, *args, **kwargs):
        return lambda function: function
flask.Blueprint = _Blueprint
flask.render_template = lambda *args, **kwargs: None
flask.request = types.SimpleNamespace(args={})
sys.modules.setdefault("flask", flask)

from routes.sec_analysis import _filing_cards, _metric_table_rows  # noqa: E402


def _db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE filings (cik INTEGER, accession TEXT, form TEXT, filing_date TEXT, report_period TEXT);
        CREATE TABLE companies (cik INTEGER PRIMARY KEY, name TEXT, ticker TEXT);
        CREATE TABLE tickers (cik INTEGER, ticker TEXT, is_primary INTEGER);
        INSERT INTO companies VALUES (1, 'Zeta', 'ZETA'), (2, 'Alpha', 'ALPH'), (3, 'Beta', 'BETA');
        INSERT INTO tickers VALUES (1, 'ZETA', 1), (2, 'ALPH', 1), (3, 'BETA', 1);
        INSERT INTO filings VALUES
          (1, 'a1', '10-Q', '2026-09-18', '2026-06-30'),
          (1, 'a0', '10-Q', '2026-06-01', '2026-03-31'),
          (2, 'b1', '10-K', '2026-09-17', '2026-07-31'),
          (3, 'c1', '10-Q', '2026-09-18', '2026-08-15');
    """)
    return con


def test_landing_cards_use_latest_filing_per_company_and_recency_sort():
    cards, total = _filing_cards(_db(), sort="filed_desc", limit=12)
    assert total == 3
    assert [c["name"] for c in cards] == ["Beta", "Zeta", "Alpha"]
    assert all(c["filing_date"] >= "2026-09-17" for c in cards)


def test_landing_cards_support_name_sort_and_pagination():
    con = _db()
    cards, total = _filing_cards(con, sort="name_asc", limit=1, offset=1)
    assert total == 3
    assert [c["name"] for c in cards] == ["Beta"]


def test_metric_table_uses_latest_quarter_and_sorts_only_stored_values():
    con = _db()
    con.executescript("""
        CREATE TABLE snapshots (cik INTEGER, period_end TEXT, fp TEXT, fy INTEGER,
          revenue REAL, operating_income REAL, net_income REAL, total_assets REAL);
        CREATE TABLE derived (cik INTEGER, period_end TEXT, fp TEXT, revenue_yoy REAL,
          net_margin REAL, return_on_equity REAL, return_on_assets REAL);
        INSERT INTO snapshots VALUES
          (1, '2026-06-30', 'Q2', 2026, 100, 20, 15, 500),
          (1, '2026-03-31', 'Q1', 2026, 999, 10, 8, 490),
          (2, '2026-06-30', 'FY', 2026, 10000, 300, 200, 700),
          (3, '2026-06-30', 'Q2', 2026, 50, 12, 9, 200);
        INSERT INTO derived VALUES
          (1, '2026-06-30', 'Q2', .10, .15, .03, .02),
          (1, '2026-03-31', 'Q1', .20, .01, .01, .01),
          (3, '2026-06-30', 'Q2', .30, .18, .04, .03);
    """)
    rows, total = _metric_table_rows(con, sort="revenue_desc", limit=12)
    assert total == 2
    assert [(r["name"], r["revenue"], r["fp"]) for r in rows] == [
        ("Zeta", 100.0, "Q2"), ("Beta", 50.0, "Q2")]
