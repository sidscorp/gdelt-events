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
from sec_explain import bars_takeaway, line_takeaway, observations
from sec_dates import format_date_context
from sec_search import search as company_search

bp = Blueprint("sec_analysis", __name__)
log = logging.getLogger("dashboard.sec")

SEC_DB = DATA_DIR / "sec.db"
PERIODS_SHOWN = 8
CHART_PERIODS = 10
NEWS_LIMIT = 5
LANDING_PAGE_SIZE = 48
LANDING_SORTS = {
    "filed_desc": ("Most recently filed", "r.filing_date DESC, c.name COLLATE NOCASE ASC"),
    "filed_asc": ("Oldest filing", "r.filing_date ASC, c.name COLLATE NOCASE ASC"),
    "period_desc": ("Most recent reporting period", "r.report_period DESC, c.name COLLATE NOCASE ASC"),
    "name_asc": ("Company name A–Z", "c.name COLLATE NOCASE ASC"),
    "name_desc": ("Company name Z–A", "c.name COLLATE NOCASE DESC"),
}
METRIC_SORTS = {
    "revenue_desc": ("Revenue", "s.revenue DESC"),
    "revenue_growth_desc": ("Revenue growth", "d.revenue_yoy DESC"),
    "operating_income_desc": ("Operating income", "s.operating_income DESC"),
    "net_income_desc": ("Net income", "s.net_income DESC"),
    "net_margin_desc": ("Net margin", "d.net_margin DESC"),
    "assets_desc": ("Total assets", "s.total_assets DESC"),
    "roe_desc": ("Return on equity", "d.return_on_equity DESC"),
    "roa_desc": ("Return on assets", "d.return_on_assets DESC"),
    "name_asc": ("Company name A-Z", "c.name COLLATE NOCASE ASC"),
}

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
        # Deposits and borrowing ARE the raw material here, so 90% leverage is the
        # business model. sec_explain.rule_leverage reads this flag rather than
        # inferring "must be a lender" from the ratio, which had it reassuring
        # over-levered operating companies.
        "leveraged_by_design": True,
        "framing": ("Banks earn from interest and fees rather than selling a product, so "
                    "they file no revenue or gross-profit line. What matters instead is "
                    "the return they make on the money entrusted to them."),
        "metrics": ["net_income", "eps_basic", "return_on_equity", "return_on_assets",
                    "total_assets", "stockholders_equity"],
    },
    "insurer": {
        "test": lambda sic: 6300 <= sic <= 6411,
        "label": "insurer",
        "leveraged_by_design": True,
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
#
# Readability contract (2026-08-28): no naked bars. Every bar carries its value
# (tooltips do not exist on touch), every bar is period-labelled, the zero
# baseline is always drawn, the range ceiling is a guide line with its figure,
# and each chart gets one computed takeaway sentence underneath.

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _short_label(pe: str) -> str:
    """'2026-06-30' -> 'Jun 26'. Every bar gets one, so they must stay short."""
    try:
        return f"{_MONTHS[int(pe[5:7]) - 1]} {pe[2:4]}"
    except (ValueError, IndexError):
        return pe[:7]


def _bar_chart(periods: list[dict], key: str, label: str = "Values",
               width=560, height=152) -> dict | None:
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
    pad_l, pad_t, pad_b = 4, 14, 16
    bw = (width - pad_l * 2) / len(pts)

    def y_of(v):
        return (height - pad_b) - ((v - lo) / span) * (height - pad_b - pad_t)

    zero_y = y_of(0)

    bars = []
    for i, (pe, v, fp) in enumerate(pts):
        y_val = y_of(v)
        top, h = min(y_val, zero_y), abs(y_val - zero_y)
        bars.append({
            "x": round(pad_l + i * bw + bw * 0.15, 1),
            "y": round(top, 1),
            "w": round(bw * 0.7, 1),
            "h": round(max(h, 1.5), 1),
            "neg": v < 0,
            "annual": fp == "FY",
            "label": _short_label(pe),
            "value": _fmt_usd(v),
            "cx": round(pad_l + i * bw + bw / 2, 1),
            "vy": round(y_val - 4 if v >= 0 else y_val + 10, 1),
            "latest": i == len(pts) - 1,
        })
    return {"bars": bars, "width": width, "height": height, "basis": basis,
            "zero_y": round(zero_y, 1),
            "hi_label": f"top value {_fmt_usd(hi)}" if hi > 0 else None,
            "hi_y": round(y_of(hi), 1),
            "take": bars_takeaway(label, vals, basis)}


def _line_chart(periods: list[dict], key: str, width=560, height=118) -> dict | None:
    """Margin trend as a polyline, with a dashed zero line when it crosses.

    Range guides label the ceiling and floor figures; the first and last points
    are labelled directly, in the NYT idiom where a reader never needs a legend.
    """
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
    hi_y = round((height - pad) - ((hi - lo) / span) * (height - pad * 2), 1)
    lo_y = round((height - pad) - ((lo - lo) / span) * (height - pad * 2), 1)
    return {"points": " ".join(f"{x},{y}" for x, y in coords),
            "dots": [{"x": x, "y": y, "value": _fmt_pct(v), "label": pe[:7]}
                     for (x, y), (pe, v) in zip(coords, pts)],
            "first": {"x": coords[0][0], "y": coords[0][1],
                      "value": _fmt_pct(vals[0]), "label": _short_label(pts[0][0])},
            "last": {"x": coords[-1][0], "y": coords[-1][1],
                     "value": _fmt_pct(vals[-1]), "label": _short_label(pts[-1][0])},
            "width": width, "height": height, "zero_y": zero_y,
            "hi": _fmt_pct(hi), "lo": _fmt_pct(lo), "hi_y": hi_y, "lo_y": lo_y,
            "take": line_takeaway(vals)}


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


def _filing_cards(con, *, sort: str = "filed_desc", limit: int = 12,
                  offset: int = 0) -> tuple[list[dict], int]:
    """One latest 10-K/10-Q per company, with a bounded, validated sort order."""
    sort = sort if sort in LANDING_SORTS else "filed_desc"
    order = LANDING_SORTS[sort][1]
    sql = """WITH ranked AS (
        SELECT f.*, row_number() over (PARTITION BY f.cik ORDER BY f.filing_date DESC, f.accession DESC) rn
        FROM filings f WHERE f.form IN ('10-K','10-Q')
      ) SELECT r.*, c.name, COALESCE((SELECT ticker FROM tickers t WHERE t.cik=r.cik ORDER BY is_primary DESC LIMIT 1),c.ticker) ticker
      FROM ranked r JOIN companies c ON c.cik=r.cik WHERE r.rn=1
      ORDER BY """ + order + " LIMIT ? OFFSET ?"
    count_sql = """WITH ranked AS (
        SELECT cik, row_number() over (PARTITION BY cik ORDER BY filing_date DESC, accession DESC) rn
        FROM filings WHERE form IN ('10-K','10-Q')
      ) SELECT count(*) FROM ranked r JOIN companies c ON c.cik=r.cik WHERE r.rn=1"""
    cards = [dict(r) for r in con.execute(sql, (limit, offset)).fetchall()]
    return cards, con.execute(count_sql).fetchone()[0]


def _add_card_metrics(con, cards: list[dict], *, collect_changes: bool = False) -> list[dict]:
    """Attach only values stored in SEC snapshots; never fabricate a card metric."""
    notable = []
    for card in cards:
        rows = [dict(r) for r in con.execute(
            "SELECT s.*, d.* FROM snapshots s LEFT JOIN derived d USING(cik,period_end,fp) "
            "WHERE s.cik=? ORDER BY s.period_end DESC", (card["cik"],))]
        if not rows:
            card["metric"], card["metric_label"] = "—", "No parsed figures"
            continue
        card["metric"] = (_fmt_usd(rows[0].get("revenue")) if rows[0].get("revenue") is not None
                          else _fmt_usd(rows[0].get("net_income")))
        card["metric_label"] = "Revenue" if rows[0].get("revenue") is not None else "Net income"
        if collect_changes and len(rows) > 1:
            change = _notable_change(rows[0], rows[1])
            if change:
                notable.append({**card, "change": change})
    return notable


def _metric_table_rows(con, *, sort: str = "revenue_desc", limit: int = LANDING_PAGE_SIZE,
                       offset: int = 0) -> tuple[list[dict], int]:
    """Latest quarterly snapshot per company, sortable only by a fixed SQL allowlist.

    Quarterly periods are deliberately separated from annual reports: ranking a
    12-month revenue figure alongside a 3-month figure is misleading.
    """
    sort = sort if sort in METRIC_SORTS else "revenue_desc"
    order = METRIC_SORTS[sort][1]
    cte = """WITH latest AS (
        SELECT s.*, row_number() over (PARTITION BY s.cik ORDER BY s.period_end DESC) rn
        FROM snapshots s WHERE s.fp <> 'FY'
      ) """
    sql = cte + """SELECT s.*, d.revenue_yoy, d.net_margin, d.return_on_equity, d.return_on_assets,
        c.name, COALESCE((SELECT ticker FROM tickers t WHERE t.cik=s.cik ORDER BY is_primary DESC LIMIT 1),c.ticker) ticker
      FROM latest s JOIN companies c ON c.cik=s.cik
      LEFT JOIN derived d USING(cik,period_end,fp)
      WHERE s.rn=1
      ORDER BY (""" + order.split()[0] + " IS NULL) ASC, " + order + " LIMIT ? OFFSET ?"
    count_sql = cte + "SELECT count(*) FROM latest s JOIN companies c ON c.cik=s.cik WHERE s.rn=1"
    rows = [dict(row) for row in con.execute(sql, (limit, offset)).fetchall()]
    return rows, con.execute(count_sql).fetchone()[0]


def _notable_change(period: dict, prior: dict) -> str | None:
    """Conservative templates over stored financial inputs; never a percent ranking."""
    if period.get("net_income") is not None and period["net_income"] < 0 and prior.get("net_income", 0) >= 0:
        return "Reported a net loss after a profitable comparable period"
    if period.get("rev_growth_is_best") and period.get("revenue_yoy") is not None:
        return f"Reported its strongest stored comparable revenue growth: {_fmt_signed_pct(period['revenue_yoy'])} YoY"
    if (period.get("decline_streak") or 0) >= 2:
        return f"Revenue has declined year over year for {period['decline_streak']} comparable periods"
    if period.get("net_margin_yoy_pp") is not None and abs(period["net_margin_yoy_pp"]) >= .03:
        return f"Net margin changed {_fmt_signed_pct(period['net_margin_yoy_pp'])} percentage points from the comparable period"
    return None


def _landing_data(con) -> tuple[list[dict], list[dict]]:
    try:
        cards, _ = _filing_cards(con)
        notable = _add_card_metrics(con, cards, collect_changes=True)
        return cards, notable[:8]
    except sqlite3.Error as e:
        log.warning("SEC landing query failed: %s", e)
        return [], []


def _company_context(con, cik: int) -> tuple[dict | None, list[dict]]:
    context = con.execute("SELECT * FROM company_context WHERE cik=?", (cik,)).fetchone()
    filings = [dict(r) for r in con.execute("SELECT * FROM filings WHERE cik=? ORDER BY filing_date DESC LIMIT 3", (cik,))]
    return (dict(context) if context else None), filings


@bp.route("/sec-analysis")
def sec_analysis():
    term = (request.args.get("ticker") or "").strip()
    mode = request.args.get("mode") if request.args.get("mode") in ("overview", "research") else "overview"
    browse = request.args.get("browse") == "all"
    metric_view = request.args.get("view") == "metrics"
    sort = request.args.get("sort") if request.args.get("sort") in LANDING_SORTS else "filed_desc"
    metric_sort = request.args.get("metric_sort") if request.args.get("metric_sort") in METRIC_SORTS else "revenue_desc"
    try:
        browse_page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        browse_page = 1
    ctx = {
        "ticker": term, "suggested": SUGGESTED, "company": None, "periods": [],
        "latest": None, "error": None, "as_of": None, "alternatives": [],
        "observations": [], "matched_by": None, "news": [],
        "rev_chart": None, "ni_chart": None, "margin_chart": None,
        "metric_rows": [], "missing_metrics": [], "filer_label": None,
        "filer_framing": None, "bs": None,
        "recent_filings": [], "top_updates": [], "mode": mode, "context": None,
        "browse": browse, "browse_sort": sort, "browse_sorts": LANDING_SORTS,
        "browse_page": browse_page, "browse_cards": [], "browse_total": 0,
        "metric_view": metric_view, "metric_sort": metric_sort, "metric_sorts": METRIC_SORTS,
        "metric_rows_table": [], "metric_total": 0,
        "filings": [], "latest_filing": None,
        "fmt_usd": _fmt_usd, "fmt_pct": _fmt_pct, "fmt_num": _fmt_num,
        "fmt_signed_pct": _fmt_signed_pct,
        "fmt_date_context": format_date_context,
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
            if metric_view:
                offset = (browse_page - 1) * LANDING_PAGE_SIZE
                rows, total = _metric_table_rows(con, sort=metric_sort, limit=LANDING_PAGE_SIZE, offset=offset)
                if offset >= total and total:
                    browse_page = max(1, (total - 1) // LANDING_PAGE_SIZE + 1)
                    rows, total = _metric_table_rows(con, sort=metric_sort, limit=LANDING_PAGE_SIZE,
                                                      offset=(browse_page - 1) * LANDING_PAGE_SIZE)
                ctx.update(browse_page=browse_page, metric_rows_table=rows, metric_total=total)
            elif browse:
                offset = (browse_page - 1) * LANDING_PAGE_SIZE
                cards, total = _filing_cards(con, sort=sort, limit=LANDING_PAGE_SIZE, offset=offset)
                if offset >= total and total:
                    browse_page, offset = max(1, (total - 1) // LANDING_PAGE_SIZE + 1), 0
                    cards, total = _filing_cards(con, sort=sort, limit=LANDING_PAGE_SIZE,
                                                  offset=(browse_page - 1) * LANDING_PAGE_SIZE)
                _add_card_metrics(con, cards)
                ctx.update(browse_page=browse_page, browse_cards=cards, browse_total=total)
            else:
                ctx["recent_filings"], ctx["top_updates"] = _landing_data(con)
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
            p["reporting_period_context"] = format_date_context(p["period_end"], label="Reporting period ended")
            periods.append(p)

        ctx["periods"] = periods[:PERIODS_SHOWN]
        ctx["latest"] = periods[0]
        ctx["context"], ctx["filings"] = _company_context(con, best["cik"])
        ctx["latest_filing"] = ctx["filings"][0] if ctx["filings"] else None
        if ctx["latest_filing"]:
            ctx["latest_filing"]["filing_context"] = format_date_context(
                ctx["latest_filing"].get("filing_date"), label="Filed")
            ctx["latest_filing"]["report_context"] = format_date_context(
                ctx["latest_filing"].get("report_period"), label="Reporting period ended")
        for filing in ctx["filings"]:
            filing["filing_context"] = format_date_context(filing.get("filing_date"), label="Filed")
            filing["report_context"] = format_date_context(filing.get("report_period"), label="Reporting period ended")
        chart_src = periods[:CHART_PERIODS]
        ctx["rev_chart"] = _bar_chart(chart_src, "revenue", "Revenue")
        ctx["ni_chart"] = _bar_chart(chart_src, "net_income", "Net income")
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

        # Tell the rules what the page already explains, so they do not repeat it.
        best = dict(best)
        best["framed"] = bool(spec.get("framing"))
        best["leveraged_by_design"] = bool(spec.get("leveraged_by_design"))
        ctx["observations"] = observations(periods[0], der.get(
            (periods[0]["period_end"], periods[0]["fp"]), {}), best, limit=4)
        ctx["news"] = _news_for(best["name"])
    except sqlite3.Error as e:
        log.warning("sec.db read failed: %s", e)
        ctx["error"] = "Financial data is temporarily unavailable."
    finally:
        con.close()

    return render_template("sec_analysis.html", **ctx)
