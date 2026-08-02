"""SEC company financials, served from the local store — with context.

Reads data/sec.db (written by pipeline/sec_ingest + sec_derive). No HTTP in the
request path, so the first paint is server-rendered and survives any other host
being down.

The page's job is not to display figures; it is to make them mean something.
Three things do that, and all are computed rather than generated:

  * observations  - sentences from pipeline/sec_explain, built only from stored
                    numbers, so the page cannot state a figure nobody computed
  * charts        - inline SVG built here, no JS and no library, so they appear
                    in the HTML a crawler sees
  * news          - what was being written about the company over the period,
                    from the GDELT feed this system already runs. That pairing
                    is the thing a generic financials site cannot do.
"""
import logging
import sqlite3
from datetime import date, timedelta

from flask import Blueprint, render_template, request

from _paths import DATA_DIR
from sec_search import search as company_search

bp = Blueprint("sec_analysis", __name__)
log = logging.getLogger("dashboard.sec")

SEC_DB = DATA_DIR / "sec.db"
PERIODS_SHOWN = 8
CHART_PERIODS = 10
NEWS_LIMIT = 5

SUGGESTED = [("AAPL", "Apple"), ("MSFT", "Microsoft"), ("GOOGL", "Alphabet"),
             ("NVDA", "Nvidia"), ("TSLA", "Tesla"), ("JPM", "JPMorgan")]


def _connect():
    if not SEC_DB.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{SEC_DB}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        return con
    except sqlite3.Error:
        return None


def _fmt_usd(n):
    if n is None:
        return "—"
    a, sign = abs(n), "-" if n < 0 else ""
    if a >= 1e12:
        return f"{sign}${a / 1e12:,.2f}T"
    if a >= 1e9:
        return f"{sign}${a / 1e9:,.2f}B"
    if a >= 1e6:
        return f"{sign}${a / 1e6:,.1f}M"
    return f"{sign}${a:,.0f}"


def _fmt_pct(n, dp=1):
    return "—" if n is None else f"{n * 100:,.{dp}f}%"


def _fmt_num(n):
    return "—" if n is None else f"{n:,.0f}"


def _fmt_signed_pct(n):
    if n is None:
        return None
    return f"{'+' if n >= 0 else ''}{n * 100:,.1f}%"


# ── charts: inline SVG, no dependencies ──────────────────────────────────────
# The site loads no charting library and no external JS; these are built as
# markup so they render server-side, work with JS disabled and stay crawlable.
# Colours come from CSS custom properties so light and dark both work.

def _bar_chart(periods: list[dict], key: str, width=560, height=120) -> dict | None:
    """Bars for one metric over time, oldest left. Negative values dip below the
    axis rather than being clipped, because losses are the interesting case."""
    pts = [(p["period_end"], p.get(key), p.get("fp")) for p in reversed(periods)
           if p.get(key) is not None]
    if len(pts) < 2:
        return None
    vals = [v for _, v, _ in pts]
    hi, lo = max(vals), min(vals)
    hi = max(hi, 0)
    lo = min(lo, 0)
    span = (hi - lo) or 1
    pad_l, pad_b = 4, 16
    bw = (width - pad_l * 2) / len(pts)
    zero_y = (height - pad_b) - ((0 - lo) / span) * (height - pad_b - 6)

    bars = []
    for i, (pe, v, fp) in enumerate(pts):
        y_val = (height - pad_b) - ((v - lo) / span) * (height - pad_b - 6)
        top, h = min(y_val, zero_y), abs(y_val - zero_y)
        bars.append({
            "x": round(pad_l + i * bw + bw * 0.15, 1),
            "y": round(top, 1),
            "w": round(bw * 0.7, 1),
            "h": round(max(h, 1.5), 1),
            "neg": v < 0,
            "annual": fp == "FY",
            "label": pe[:7],
            "value": _fmt_usd(v),
            "cx": round(pad_l + i * bw + bw / 2, 1),
        })
    return {"bars": bars, "width": width, "height": height,
            "zero_y": round(zero_y, 1), "show_zero": lo < 0}


def _line_chart(periods: list[dict], key: str, width=560, height=110) -> dict | None:
    """Margin trend as a polyline, with a dashed zero line when it crosses."""
    pts = [(p["period_end"], p.get(key)) for p in reversed(periods) if p.get(key) is not None]
    if len(pts) < 3:
        return None
    vals = [v for _, v in pts]
    hi, lo = max(vals), min(vals)
    if hi == lo:
        hi, lo = hi + 0.01, lo - 0.01
    span = hi - lo
    pad = 6
    step = (width - pad * 2) / (len(pts) - 1)
    coords = [(round(pad + i * step, 1),
               round((height - pad) - ((v - lo) / span) * (height - pad * 2), 1))
              for i, (_, v) in enumerate(pts)]
    zero_y = None
    if lo < 0 < hi:
        zero_y = round((height - pad) - ((0 - lo) / span) * (height - pad * 2), 1)
    return {"points": " ".join(f"{x},{y}" for x, y in coords),
            "dots": [{"x": x, "y": y, "value": _fmt_pct(v), "label": pe[:7]}
                     for (x, y), (pe, v) in zip(coords, pts)],
            "width": width, "height": height, "zero_y": zero_y,
            "hi": _fmt_pct(hi), "lo": _fmt_pct(lo)}


# DEFERRED: pairing filings with news coverage.
#
# This is the differentiator - nobody else has SEC financials and a 44,000-source
# news feed in one system - but two things block it and neither is a quick fix:
#
#   1. articles._api_articles_inner() reads g._req_phases, which app.py's
#      before_request hook installs. Calling it from a synthetic
#      test_request_context raises AttributeError: _req_phases. It needs either a
#      real request or a refactor to make the timing helper optional.
#   2. The feed keeps a rolling ~60-day window, so a quarter ending three months
#      ago has no coverage left. Querying a filing period returned 0 while the
#      last 30 days returned 2,194 - so this can only ever show RECENT coverage,
#      which needs saying on the page rather than implying it matches the period.
#
# Also note `org=` is broken (adding it increases the result count), so whatever
# is built should match on `q=` instead.
def _news_for(company_name: str) -> list[dict]:
    return []


def _derive_rows(con, cik: int) -> dict:
    return {(r["period_end"], r["fp"]): dict(r) for r in con.execute(
        "SELECT * FROM derived WHERE cik = ?", (cik,))}


@bp.route("/sec-analysis")
def sec_analysis():
    term = (request.args.get("ticker") or "").strip()
    ctx = {
        "ticker": term, "suggested": SUGGESTED, "company": None, "periods": [],
        "latest": None, "error": None, "as_of": None, "alternatives": [],
        "observations": [], "matched_by": None, "news": [],
        "rev_chart": None, "ni_chart": None, "margin_chart": None,
        "fmt_usd": _fmt_usd, "fmt_pct": _fmt_pct, "fmt_num": _fmt_num,
        "fmt_signed_pct": _fmt_signed_pct,
    }

    con = _connect()
    if con is None:
        ctx["error"] = ("Financial data has not been collected yet — the pipeline "
                        "populates it on its next run.")
        return render_template("sec_analysis.html", **ctx)

    try:
        row = con.execute("SELECT val FROM meta WHERE key='data_version'").fetchone()
        ctx["as_of"] = row["val"] if row else None
        if not term:
            return render_template("sec_analysis.html", **ctx)

        hits = company_search(con, term, limit=6)
        if not hits:
            ctx["error"] = (f"Nothing found for “{term}”. Try a ticker (AAPL), a company "
                            f"name (Apple), or part of one.")
            return render_template("sec_analysis.html", **ctx)

        best = hits[0]
        ctx["company"] = best
        ctx["alternatives"] = hits[1:]
        ctx["matched_by"] = best.get("match")

        rows = con.execute(
            "SELECT * FROM snapshots WHERE cik = ? ORDER BY period_end DESC LIMIT ?",
            (best["cik"], max(PERIODS_SHOWN, CHART_PERIODS)),
        ).fetchall()
        if not rows:
            ctx["error"] = f"{best['name']} has no parsed financial periods yet."
            return render_template("sec_analysis.html", **ctx)

        der = _derive_rows(con, best["cik"])
        periods = []
        for r in rows:
            p = dict(r)
            d = der.get((p["period_end"], p["fp"]), {})
            p.update({k: v for k, v in d.items()
                      if k not in ("cik", "period_end", "fp")})
            months = 12 if p["fp"] == "FY" else 3
            p["label"] = (f"{p['fp']} {p['fy']} · {months} months ended {p['period_end']}")
            p["short"] = f"{p['fp']} {p['fy']}"
            periods.append(p)

        ctx["periods"] = periods[:PERIODS_SHOWN]
        ctx["latest"] = periods[0]
        chart_src = periods[:CHART_PERIODS]
        ctx["rev_chart"] = _bar_chart(chart_src, "revenue")
        ctx["ni_chart"] = _bar_chart(chart_src, "net_income")
        ctx["margin_chart"] = _line_chart(chart_src, "operating_margin")

        from sec_explain import observations
        ctx["observations"] = observations(periods[0], der.get(
            (periods[0]["period_end"], periods[0]["fp"]), {}), best, limit=4)
        ctx["news"] = _news_for(best["name"])
    except sqlite3.Error as e:
        log.warning("sec.db read failed: %s", e)
        ctx["error"] = "Financial data is temporarily unavailable."
    finally:
        con.close()

    return render_template("sec_analysis.html", **ctx)
