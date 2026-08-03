"""Turn derived facts into sentences a person can read.

Every sentence is a template filled from `derived`. There is no model here, by
design: this page exists to be correct, and a generated sentence can be fluent
and wrong. The cost of that trade is that the prose is plainer; the benefit is
that it cannot state a number nobody computed.

Rules return an Observation with a notability score; the page shows the top few.
Each rule is small and separately testable, which is the point - a wrong
sentence is worse than no sentence.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Observation:
    text: str
    score: float          # 0-100; higher surfaces first
    kind: str             # composition | growth | margin | sector | coverage


def _usd(n: float | None) -> str:
    if n is None:
        return "—"
    a, sign = abs(n), "-" if n < 0 else ""
    if a >= 1e12:
        return f"{sign}${a / 1e12:,.2f}T"
    if a >= 1e9:
        return f"{sign}${a / 1e9:,.2f}B"
    if a >= 1e6:
        return f"{sign}${a / 1e6:,.0f}M"
    return f"{sign}${a:,.0f}"


def _pct(x: float | None, dp: int = 1) -> str:
    return "—" if x is None else f"{x * 100:,.{dp}f}%"


def _period_words(fp: str) -> str:
    return "the year" if fp == "FY" else "the quarter"


# ── rules ────────────────────────────────────────────────────────────────────

def rule_nonoperating(s: dict, d: dict) -> Observation | None:
    """The GOOGL case: profit that did not come from operating the business.

    Worth stating whenever non-operating items are a large share of profit, in
    either direction - a company can just as easily be rescued by an investment
    gain as sunk by a writedown.
    """
    ni, oi, share = s.get("net_income"), s.get("operating_income"), d.get("nonop_share_pretax")
    nonop = d.get("nonop_income")
    if None in (ni, oi, share, nonop) or abs(share) < 0.20:
        return None
    if nonop > 0:
        text = (f"Net income of {_usd(ni)} exceeded operating income of {_usd(oi)}. "
                f"{_usd(nonop)} came from non-operating items net of tax — "
                f"investment gains, interest and similar — so this period's earnings "
                f"reflect more than the operating business.")
    else:
        text = (f"Net income of {_usd(ni)} came in below operating income of {_usd(oi)}. "
                f"{_usd(abs(nonop))} was absorbed by non-operating items and tax "
                f"rather than by the cost of doing business.")
    return Observation(text, 95 if abs(share) > 0.4 else 78, "composition")


def rule_revenue_growth(s: dict, d: dict) -> Observation | None:
    g = d.get("revenue_yoy")
    if g is None:
        return None
    direction = "grew" if g >= 0 else "fell"
    text = f"Revenue {direction} {_pct(abs(g))} year over year to {_usd(s.get('revenue'))}"
    score = 55 + min(abs(g) * 100, 25)
    n = d.get("rev_growth_rank_n") or 0
    if d.get("rev_growth_is_best") and n >= 4:
        text += f" — the fastest growth in {n} reported periods."
        score = 88
    elif d.get("rev_growth_is_worst") and n >= 4:
        text += f" — the weakest in {n} reported periods."
        score = 88
    else:
        text += "."
    return Observation(text, score, "growth")


def rule_decline_streak(s: dict, d: dict) -> Observation | None:
    streak = d.get("decline_streak") or 0
    if streak < 2:
        return None
    return Observation(
        f"Revenue has now declined year over year for {streak} consecutive periods.",
        85, "growth")


def rule_margin_move(s: dict, d: dict) -> Observation | None:
    pp, m = d.get("operating_margin_yoy_pp"), d.get("operating_margin")
    if pp is None or m is None or abs(pp) < 2:
        return None
    direction = "widened" if pp > 0 else "narrowed"
    return Observation(
        f"Operating margin {direction} {abs(pp):,.1f} percentage points year over year "
        f"to {_pct(m)}.", 60 + min(abs(pp), 20), "margin")


def rule_sector_position(s: dict, d: dict, company: dict) -> Observation | None:
    pct, peers = d.get("sector_gross_margin_pct"), d.get("sector_peers")
    gm, sic_desc = d.get("gross_margin"), company.get("sic_description")
    if None in (pct, gm) or not peers or peers < 5:
        return None
    if pct >= 0.75:
        where = "top quartile"
    elif pct <= 0.25:
        where = "bottom quartile"
    else:
        return None
    industry = sic_desc or f"SIC {company.get('sic')}"
    return Observation(
        f"A gross margin of {_pct(gm)} puts it in the {where} of {industry} "
        f"for this period ({peers} filers compared).", 72, "sector")


def rule_loss(s: dict, d: dict) -> Observation | None:
    ni = s.get("net_income")
    if ni is None or ni >= 0:
        return None
    return Observation(
        f"The company reported a net loss of {_usd(abs(ni))} for "
        f"{_period_words(s.get('fp',''))}.", 96, "loss")


def rule_missing_revenue(s: dict, d: dict, company: dict) -> Observation | None:
    """Absence is information. 27% of rows have no revenue, and a bare dash
    reads like a bug rather than a reporting convention.

    Suppressed for filer classes the page already frames ("This is a bank..."),
    because saying it twice in consecutive paragraphs reads as padding.
    """
    if s.get("revenue") is not None or company.get("framed"):
        return None
    return Observation(
        "This filer does not tag a single revenue line in XBRL — common for banks, "
        "insurers and REITs, which report interest or rental income instead. "
        "Income-statement figures below are still as filed.", 50, "coverage")


def rule_return_on_equity(s: dict, d: dict, company: dict) -> Observation | None:
    """For banks and insurers this is the headline number - they have no margin
    to talk about. Stated for the period covered, never annualised."""
    roe = d.get("return_on_equity")
    if roe is None:
        return None
    period = "the year" if s.get("fp") == "FY" else "the quarter"
    text = (f"Return on equity was {_pct(roe, 1)} for {period} — profit measured against "
            f"the {_usd(s.get('stockholders_equity'))} shareholders have in the business.")
    if s.get("fp") != "FY":
        text += " Quarterly, so roughly a quarter of an annual rate."
    return Observation(text, 74, "returns")


def rule_leverage(s: dict, d: dict, company: dict) -> Observation | None:
    """How much of the balance sheet is other people's money. Banks run near 90%
    by design, which is the single most surprising fact about how they work.

    The reassuring half of this sentence used to fire on the ratio alone, so ANY
    filer above 85% was told its leverage was "normal for a lender" - including
    operating companies, for which it is the opposite of normal, and the page would
    have been comforting a reader about the very thing they should look at. It now
    keys on the filer class the page has already established from SIC, which the
    route passes in as `leveraged_by_design`.
    """
    a, li = s.get("total_assets"), s.get("total_liabilities")
    if not a or li is None or a <= 0:
        return None
    share = li / a
    if share < 0.6:
        return None
    if share > 1:
        # Liabilities above assets means negative equity - a different and much
        # louder story than leverage, and one this sentence would badly understate.
        # It is also where the known data-quality tail sits (~10% of tickered rows
        # report liabilities > assets, unverified). Say nothing over saying wrong.
        return None
    text = (f"{_pct(share, 0)} of its {_usd(a)} balance sheet is funded by liabilities "
            f"rather than shareholders.")
    if share > 0.85 and company.get("leveraged_by_design"):
        text += (" That is normal for a lender: deposits and borrowing are the raw "
                 "material, not a warning sign on their own.")
    return Observation(text, 66, "leverage")


def rule_net_income_growth(s: dict, d: dict) -> Observation | None:
    """Growth for filers with no revenue line, which would otherwise say nothing."""
    if s.get("revenue") is not None:
        return None
    g = d.get("net_income_yoy")
    if g is None:
        return None
    direction = "rose" if g >= 0 else "fell"
    return Observation(
        f"Net income {direction} {_pct(abs(g))} against the same period a year earlier.",
        70, "growth")


def observations(snapshot: dict, derived: dict, company: dict,
                 limit: int = 4) -> list[Observation]:
    """Top observations for one period, most notable first."""
    d = derived or {}
    out = [
        rule_loss(snapshot, d),
        rule_nonoperating(snapshot, d),
        rule_revenue_growth(snapshot, d),
        rule_decline_streak(snapshot, d),
        rule_margin_move(snapshot, d),
        rule_sector_position(snapshot, d, company),
        rule_missing_revenue(snapshot, d, company),
        rule_net_income_growth(snapshot, d),
        rule_return_on_equity(snapshot, d, company),
        rule_leverage(snapshot, d, company),
    ]
    found = [o for o in out if o is not None]
    found.sort(key=lambda o: o.score, reverse=True)
    # One per kind: two margin sentences in a row reads like padding.
    seen, kept = set(), []
    for o in found:
        if o.kind in seen:
            continue
        seen.add(o.kind)
        kept.append(o)
    return kept[:limit]
