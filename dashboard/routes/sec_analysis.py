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



# ── what kind of business is this? ───────────────────────────────────────────
# Showing every filer the same six income-statement rows is what made the
# JPMorgan page three empty headings with a paragraph each. Across SIC 6021,
# gross profit is tagged in 0.2% of periods and operating income in 3.2%, while
# net income, equity and assets are all above 93%. A bank is not missing data -
# it has a different income statement.

FILER_CLASSES = {
    "bank": {
        "test": lambda sic: 6020 <= sic <= 6199,
        "label": "bank",
        "framing": ("Banks earn from interest and fees rather than selling a product, so "
                    "they file no revenue or gross-profit line. What matters instead is "
                    "the return they make on the money entrusted to them."),
        "metrics": ["net_income", "eps_basic", "return_on_equity", "return_on_assets",
                    "total_assets", "stockholders_equity"],
    },
    "insurer": {
        "test": lambda sic: 6300 <= sic <= 6411,
        "label": "insurer",
        "framing": ("Insurers take in premiums and pay out claims, so their economics show "
                    "up in investment returns and reserves rather than in a gross margin."),
        "metrics": ["revenue", "net_income", "eps_basic", "return_on_equity",
                    "total_assets", "stockholders_equity"],
    },
    "reit": {
        "test": lambda sic: 6500 <= sic <= 6599,
        "label": "real-estate company",
        "framing": ("Property companies report rental income and carry large asset bases, "
                    "so returns on assets say more than an operating margin would."),
        "metrics": ["revenue", "net_income", "eps_basic", "return_on_assets",
                    "total_assets", "stockholders_equity"],
    },
    "investment": {
        "test": lambda sic: 6722 <= sic <= 6799,
        "label": "investment company",
        "framing": ("Investment vehicles report gains on holdings rather than trading "
                    "revenue, so income can swing with markets rather than operations."),
        "metrics": ["net_income", "eps_basic", "return_on_equity",
                    "total_assets", "stockholders_equity"],
    },
}
OPERATING = {
    "label": "operating company", "framing": None,
    "metrics": ["revenue", "gross_profit", "operating_income", "net_income",
                "eps_basic", "shares_outstanding"],
}

METRIC_INFO = {
    "revenue": ("Revenue", "All money taken in from sales, before any costs are subtracted. The top line."),
    "gross_profit": ("Gross profit", "Revenue minus the direct cost of producing what was sold. What is left to cover everything else."),
    "operating_income": ("Operating income", "Profit from running the business itself \u2014 after wages, R&D and overheads, but before interest, investments and tax."),
    "net_income": ("Net income", "The bottom line: what remains after every cost, including tax and anything unrelated to normal operations."),
    "eps_basic": ("EPS (basic)", "Net income divided by shares outstanding \u2014 the profit attributable to one share."),
    "shares_outstanding": ("Shares outstanding", "How many shares exist. A falling count means buybacks; a rising one means dilution."),
    "return_on_equity": ("Return on equity", "Profit as a share of what shareholders have put in. The headline measure of how hard a bank makes its capital work."),
    "return_on_assets": ("Return on assets", "Profit as a share of everything the company holds. Low single digits is normal for a bank, which operates on borrowed money."),
    "total_assets": ("Total assets", "Everything the company owns \u2014 cash, loans, securities, property."),
    "stockholders_equity": ("Stockholders' equity", "The shareholders\u2019 residual claim: assets minus liabilities."),
}


def _filer_class(sic: str | None) -> dict:
    try:
        code = int(sic)
    except (TypeError, ValueError):
        return OPERATING
    for spec in FILER_CLASSES.values():
        if spec["test"](code):
            return spec
    return OPERATING


def _metric_rows(latest: dict, spec: dict) -> tuple[list[dict], list[str]]:
    """(rows worth showing, names of the ones this filer never reports)."""
    shown, missing = [], []
    for key in spec["metrics"]:
        name, definition = METRIC_INFO[key]
        v = latest.get(key)
        if v is None:
            missing.append(name)
            continue
        if key == "eps_basic":
            value = f"${v:,.2f}"
        elif key in ("return_on_equity", "return_on_assets"):
            value = _fmt_pct(v, 2)
        elif key == "shares_outstanding":
            value = _fmt_num(v)
        else:
            value = _fmt_usd(v)
        row = {"name": name, "definition": definition, "value": value, "key": key}
        if key == "revenue" and latest.get("revenue_yoy") is not None:
            row["delta"] = _fmt_signed_pct(latest["revenue_yoy"]) + " vs a year ago"
            row["dir"] = "up" if latest["revenue_yoy"] >= 0 else "down"
        if key == "net_income" and latest.get("net_income_yoy") is not None:
            row["delta"] = _fmt_signed_pct(latest["net_income_yoy"]) + " vs a year ago"
            row["dir"] = "up" if latest["net_income_yoy"] >= 0 else "down"
        if key in ("return_on_equity", "return_on_assets"):
            row["sub"] = "this period, not annualised"
        if key == "gross_profit" and latest.get("gross_margin") is not None:
            row["sub"] = _fmt_pct(latest["gross_margin"]) + " of revenue"
        if key == "operating_income" and latest.get("operating_margin") is not None:
            row["sub"] = _fmt_pct(latest["operating_margin"]) + " of revenue"
        shown.append(row)
    return shown, missing


# ── charts: inline SVG, no dependencies ──────────────────────────────────────
# The site loads no charting library and no external JS; these are built as
# markup so they render server-side, work with JS disabled and stay crawlable.
# Colours come from CSS custom properties so light and dark both work.

def _bar_chart(periods: list[dict], key: str, width=560, height=120) -> dict | None:
    """Bars for one metric over time, oldest left, ALL THE SAME PERIOD LENGTH.

    Mixing a full year in among quarters put a bar four times the height of its
    neighbours on the same axis, which reads as a spectacular quarter rather than
    a different unit. Quarters are preferred; a filer that reports only annually
    gets an annual chart, and the caption says which it is.
    """
    quarters = [p for p in periods if p.get("fp") != "FY"]
    annual = [p for p in periods if p.get("fp") == "FY"]
    chosen = quarters if len([p for p in quarters if p.get(key) is not None]) >= 2 else annual
    basis = "quarterly" if chosen is quarters else "annual"

    pts = [(p["period_end"], p.get(key), p.get("fp")) for p in reversed(chosen)
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
    return {"bars": bars, "width": width, "height": height, "basis": basis,
            "zero_y": round(zero_y, 1), "show_zero": lo < 0}


def _line_chart(periods: list[dict], key: str, width=560, height=110) -> dict | None:
    """Margin trend as a polyline, with a dashed zero line when it crosses."""
    q = [p for p in periods if p.get("fp") != "FY"]
    src = q if len([p for p in q if p.get(key) is not None]) >= 3 else periods
    pts = [(p["period_end"], p.get(key)) for p in reversed(src) if p.get(key) is not None]
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
        "metric_rows": [], "missing_metrics": [], "filer_label": None,
        "filer_framing": None, "bs": None,
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

        spec = _filer_class(best.get("sic"))
        ctx["filer_label"] = spec["label"]
        ctx["filer_framing"] = spec.get("framing")
        ctx["metric_rows"], ctx["missing_metrics"] = _metric_rows(periods[0], spec)

        # Balance sheet as proportions: a single bar showing liabilities and
        # equity as shares of total assets teaches the accounting identity far
        # better than three numbers stacked in a list.
        a = periods[0].get("total_assets")
        li, eq = periods[0].get("total_liabilities"), periods[0].get("stockholders_equity")
        if a and a > 0 and (li is not None or eq is not None):
            li = li if li is not None else (a - eq if eq is not None else None)
            eq = eq if eq is not None else (a - li if li is not None else None)
            if li is not None and eq is not None and li >= 0:
                ctx["bs"] = {
                    "assets": _fmt_usd(a), "liab": _fmt_usd(li), "eq": _fmt_usd(eq),
                    "liab_pct": round(100 * li / a, 1),
                    "eq_pct": round(100 * max(eq, 0) / a, 1),
                    "negative_equity": eq < 0,
                }

        from sec_explain import observations
        # Tell the rules what the page already explains, so they do not repeat it.
        best = dict(best)
        best["framed"] = bool(spec.get("framing"))
        ctx["observations"] = observations(periods[0], der.get(
            (periods[0]["period_end"], periods[0]["fp"]), {}), best, limit=4)
        ctx["news"] = _news_for(best["name"])
    except sqlite3.Error as e:
        log.warning("sec.db read failed: %s", e)
        ctx["error"] = "Financial data is temporarily unavailable."
    finally:
        con.close()

    return render_template("sec_analysis.html", **ctx)
