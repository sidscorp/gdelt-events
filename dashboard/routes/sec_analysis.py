"""SEC company analysis — pulls live financials from sec-analyzer on snambiar-linux."""

import logging
import time

import requests
from flask import Blueprint, render_template, request

bp = Blueprint("sec", __name__)

SEC_API = "http://snambiar-linux:8015"
TIMEOUT = 15

log = logging.getLogger("dashboard.sec")


def _fmt_usd(n):
    """Format a raw dollar value for display."""
    if n is None:
        return "\u2014"
    if abs(n) >= 1e12:
        return f"${n / 1e12:,.2f}T"
    if abs(n) >= 1e9:
        return f"${n / 1e9:,.2f}B"
    if abs(n) >= 1e6:
        return f"${n / 1e6:,.1f}M"
    return f"${n:,.0f}"


def _fmt_pct(n):
    """Format a ratio as percentage."""
    if n is None:
        return "\u2014"
    return f"{n * 100:,.1f}%"


def _fmt_ratio(n):
    """Format a ratio for display."""
    if n is None:
        return "\u2014"
    return f"{n:,.2f}"


def _fmt_shares(n):
    """Format shares outstanding."""
    if n is None:
        return "\u2014"
    if n >= 1e9:
        return f"{n / 1e9:,.2f}B"
    if n >= 1e6:
        return f"{n / 1e6:,.1f}M"
    return f"{n:,.0f}"


@bp.route("/sec-analysis")
def sec_analysis():
    ticker = (request.args.get("ticker") or "").strip().upper()
    if not ticker:
        return render_template("sec_analysis.html", ticker="", error=None)

    t0 = time.perf_counter()
    try:
        resp = requests.get(
            f"{SEC_API}/api/v1/analyze",
            params={"ticker": ticker, "skip_llm": "true"},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        elapsed = time.perf_counter() - t0
        log.info("fetched %s in %.2fs", ticker, elapsed)
    except requests.Timeout:
        log.warning("sec-analyzer timeout for %s", ticker)
        return render_template(
            "sec_analysis.html",
            ticker=ticker,
            error="The SEC analysis engine timed out. Try again in a moment.",
        )
    except requests.ConnectionError:
        log.error("sec-analyzer unreachable at %s", SEC_API)
        return render_template(
            "sec_analysis.html",
            ticker=ticker,
            error="The SEC analysis engine is currently offline.",
        )
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return render_template(
                "sec_analysis.html",
                ticker=ticker,
                error=f"Ticker '{ticker}' not found in SEC database.",
            )
        log.error("sec-analyzer HTTP error: %s", e)
        return render_template(
            "sec_analysis.html",
            ticker=ticker,
            error=f"Data fetch failed: {e}",
        )
    except Exception as e:
        log.exception("sec-analyzer unexpected error")
        return render_template(
            "sec_analysis.html",
            ticker=ticker,
            error=f"Unexpected error: {e}",
        )

    company = data.get("company", {})
    financials = data.get("financials", [])
    ratios = data.get("ratios", {})

    for s in financials:
        s["_rev"] = _fmt_usd(s.get("revenue"))
        s["_cost"] = _fmt_usd(s.get("cost_of_revenue"))
        s["_gp"] = _fmt_usd(s.get("gross_profit"))
        s["_op"] = _fmt_usd(s.get("operating_income"))
        s["_ni"] = _fmt_usd(s.get("net_income"))
        s["_eps"] = f"${s['eps_basic']:,.2f}" if s.get("eps_basic") is not None else "\u2014"
        s["_assets"] = _fmt_usd(s.get("total_assets"))
        s["_liab"] = _fmt_usd(s.get("total_liabilities"))
        s["_equity"] = _fmt_usd(s.get("stockholders_equity"))
        s["_debt"] = _fmt_usd(s.get("long_term_debt"))
        s["_cash"] = _fmt_usd(s.get("cash"))
        s["_shares"] = _fmt_shares(s.get("shares_outstanding"))
        s["_gross_margin"] = _fmt_pct(s.get("gross_margin"))
        s["_operating_margin"] = _fmt_pct(s.get("operating_margin"))
        s["_net_margin"] = _fmt_pct(s.get("net_margin"))
        s["_debt_to_equity"] = _fmt_ratio(s.get("debt_to_equity"))
        s["_current_ratio"] = _fmt_ratio(s.get("current_ratio"))
        s["_dep_amort"] = _fmt_usd(s.get("depreciation_amortization"))
        s["_interest"] = _fmt_usd(s.get("interest_expense"))
        s["_ocf"] = _fmt_usd(s.get("operating_cash_flow"))
        s["_rev_growth"] = _fmt_pct(s.get("revenue_yoy_growth"))
        s["_earn_growth"] = _fmt_pct(s.get("earnings_yoy_growth"))

    return render_template(
        "sec_analysis.html",
        ticker=ticker,
        company=company,
        financials=financials,
        ratios=ratios,
        error=None,
    )
