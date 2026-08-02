"""SEC company financials, served from the local store.

Reads data/sec.db, written by pipeline/sec_ingest.py. There is deliberately no
HTTP call in this request path: the previous version fetched from a FastAPI
service on another machine on every page view, which made the page as slow and
as available as that hop. Now it is a local SQLite read, so the first paint is
server-rendered like the feed and survives snambiar-linux being down.

Period labelling matters here. A 10-Q carries both the quarter and the
year-to-date span; conflating them is what previously showed a quarter larger
than the prior full year, so every figure states the basis it was measured on.
"""
import logging
import sqlite3
from datetime import date

from flask import Blueprint, render_template, request

from _paths import DATA_DIR

bp = Blueprint("sec_analysis", __name__)
log = logging.getLogger("dashboard.sec")

SEC_DB = DATA_DIR / "sec.db"
PERIODS_SHOWN = 8

# Companies worth offering when nobody has typed anything yet — recognisable,
# and each one exercises a different reporting shape (non-calendar year, losses,
# heavy non-operating income).
SUGGESTED = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "TSLA", "JPM", "XOM"]


def _connect():
    if not SEC_DB.exists():
        return None
    try:
        return sqlite3.connect(f"file:{SEC_DB}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


def _fmt_usd(n):
    if n is None:
        return "—"
    a = abs(n)
    sign = "-" if n < 0 else ""
    if a >= 1e12:
        return f"{sign}${a / 1e12:,.2f}T"
    if a >= 1e9:
        return f"{sign}${a / 1e9:,.2f}B"
    if a >= 1e6:
        return f"{sign}${a / 1e6:,.1f}M"
    return f"{sign}${a:,.0f}"


def _fmt_pct(n):
    return "—" if n is None else f"{n * 100:,.1f}%"


def _fmt_num(n):
    return "—" if n is None else f"{n:,.0f}"


def _period_label(row) -> str:
    """'Q2 FY2026' plus the span it actually covers — the thing that was missing."""
    fp, fy = row["fp"], row["fy"]
    months = 12 if fp == "FY" else 3
    base = f"{'FY' if fp == 'FY' else fp} {fy}" if fy else (fp or "")
    return f"{base} · {months} months ended {row['period_end']}"


def _derive(row: dict) -> dict:
    """Margins and ratios, computed only where both inputs exist for the SAME period."""
    rev, ni = row.get("revenue"), row.get("net_income")
    gp, oi = row.get("gross_profit"), row.get("operating_income")
    eq, li = row.get("stockholders_equity"), row.get("total_liabilities")
    ca, cl = row.get("current_assets"), row.get("current_liabilities")
    row["gross_margin"] = gp / rev if rev and gp is not None else None
    row["operating_margin"] = oi / rev if rev and oi is not None else None
    row["net_margin"] = ni / rev if rev and ni is not None else None
    row["debt_to_equity"] = li / eq if eq and li is not None else None
    row["current_ratio"] = ca / cl if cl and ca is not None else None
    return row


@bp.route("/sec-analysis")
def sec_analysis():
    ticker = (request.args.get("ticker") or "").strip().upper()
    ctx = {
        "ticker": ticker, "suggested": SUGGESTED, "company": None,
        "periods": [], "latest": None, "error": None, "as_of": None,
        "matched_by_name": False, "other_matches": [],
        "fmt_usd": _fmt_usd, "fmt_pct": _fmt_pct, "fmt_num": _fmt_num,
    }

    con = _connect()
    if con is None:
        ctx["error"] = ("Financial data has not been collected yet. "
                        "The pipeline populates it on its next run.")
        return render_template("sec_analysis.html", **ctx)

    try:
        ctx["as_of"] = (con.execute(
            "SELECT val FROM meta WHERE key='data_version'").fetchone() or [None])[0]

        if not ticker:
            return render_template("sec_analysis.html", **ctx)

        con.row_factory = sqlite3.Row
        company = con.execute(
            "SELECT cik, ticker, name FROM companies WHERE ticker = ?", (ticker,)
        ).fetchone()

        if company is None:
            # Fall back to a name match. Tickers move between entities - SEC maps
            # XOM to a holding company CIK distinct from "Exxon Mobil Corporation",
            # so a ticker-only lookup silently misses the filer people mean. Prefer
            # a company that actually has parsed periods.
            matches = con.execute(
                "SELECT c.cik, c.ticker, c.name, count(s.cik) AS n "
                "FROM companies c LEFT JOIN snapshots s ON s.cik = c.cik "
                "WHERE upper(c.name) LIKE ? GROUP BY c.cik "
                "ORDER BY n DESC, length(c.name) LIMIT 5",
                (f"%{ticker}%",),
            ).fetchall()
            matches = [m for m in matches if m["n"]]
            if matches:
                company = matches[0]
                ctx["matched_by_name"] = True
                ctx["other_matches"] = [dict(m) for m in matches[1:]]
            else:
                ctx["error"] = (
                    f"No SEC filer matching '{ticker}'. Only companies that file XBRL "
                    f"financial statements appear here, and a ticker can belong to a "
                    f"different legal entity than the name you expect.")
                return render_template("sec_analysis.html", **ctx)

        ctx["company"] = dict(company)
        rows = con.execute(
            "SELECT * FROM snapshots WHERE cik = ? ORDER BY period_end DESC LIMIT ?",
            (company["cik"], PERIODS_SHOWN),
        ).fetchall()
        periods = [_derive(dict(r)) for r in rows]
        for p in periods:
            p["label"] = _period_label(p)
        ctx["periods"] = periods
        ctx["latest"] = periods[0] if periods else None
        if not periods:
            ctx["error"] = f"{company['name']} has no parsed financial periods yet."
    except sqlite3.Error as e:
        log.warning("sec.db read failed: %s", e)
        ctx["error"] = "Financial data is temporarily unavailable."
    finally:
        con.close()

    return render_template("sec_analysis.html", **ctx)
