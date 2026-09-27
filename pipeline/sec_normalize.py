"""Normalize SEC XBRL companyfacts into per-period financial snapshots.

Replaces the extraction in ~/projects/sec-analyzer/backend/metrics.py, which put
wrong numbers on gdeltmonitor.com/sec-analysis. Three bugs, all reproducible from
the cached companyfacts for CIK 0001652044 (Alphabet):

1. CONCEPTS WERE RESOLVED ONCE, GLOBALLY. The old resolver picked the first tag in
   a chain present anywhere in a filer's history, then used only that tag. Alphabet
   has 87 RevenueFromContractWithCustomerExcludingAssessedTax entries but none
   ending 2026-06-30 - recent periods use Revenues. So revenue came back None, and
   gross profit with it. Here every concept in the chain contributes points and the
   earliest chain entry that actually has a fact FOR THAT PERIOD wins.

2. FACT DURATION WAS NEVER READ. A 10-Q reports both the quarter and the
   year-to-date span under the same end date and the same fp:

       Revenues  end=2026-06-30 start=2026-01-01  180d  $229.69B   <- H1
       Revenues  end=2026-06-30 start=2026-04-01   90d  $119.80B   <- Q2

   The old code parsed `end` only, so the pick between them was arbitrary. That is
   how a quarter came to show net income of $140.23B, larger than the full prior
   year. Duration is now first-class and periods are selected by it.

3. DE-CUMULATION WAS APPLIED TO BALANCE-SHEET FACTS. Subtracting the prior period
   is right for flows and meaningless for stocks:

       CommonStockSharesOutstanding 2026-06-30  12,230,000,000
       CommonStockSharesOutstanding 2025-12-31  12,088,000,000
                                     difference     142,000,000   <- what shipped

   Instants are identified by `start is None` and never de-cumulated.

Stdlib only, matching the rest of pipeline/.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date

# ── concept chains ───────────────────────────────────────────────────────────
# Ordered fallbacks; filers change tags across years and across each other.
CONCEPT_CHAINS: dict[str, list[str]] = {
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
        "RevenueNet",
    ],
    "cost_of_revenue": [
        "CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfServices",
        "CostOfGoodsSold", "CostOfSales",
    ],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss", "ProfitLoss", "NetIncomeLossAttributableToParent"],
    "eps_basic": ["EarningsPerShareBasic"],
    "eps_diluted": ["EarningsPerShareDiluted"],
    "total_assets": ["Assets"],
    # NOTE: LiabilitiesAndStockholdersEquity is deliberately NOT a fallback here.
    # It is liabilities PLUS equity - i.e. total assets - so a filer that doesn't
    # tag bare Liabilities would silently report assets as liabilities and blow up
    # debt_to_equity. Better to report nothing than something confidently wrong.
    "total_liabilities": ["Liabilities"],
    "current_assets": ["AssetsCurrent"],
    "current_liabilities": ["LiabilitiesCurrent"],
    "stockholders_equity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
    "long_term_debt": ["LongTermDebt", "LongTermDebtNoncurrent"],
    "short_term_debt": ["ShortTermBorrowings", "DebtCurrent"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue", "Cash"],
    "operating_cash_flow": ["NetCashProvidedByUsedInOperatingActivities"],
    "shares_outstanding": [
        "CommonStockSharesOutstanding",
        "EntityCommonStockSharesOutstanding",
        # WeightedAverageNumberOfSharesOutstandingBasic is a DURATION fact, unlike
        # the two above; it is last so it is only used when no instant count exists.
        "WeightedAverageNumberOfSharesOutstandingBasic",
    ],
    "r_and_d_expense": ["ResearchAndDevelopmentExpense"],
    "sga_expense": ["SellingGeneralAndAdministrativeExpense"],
    "depreciation_amortization": [
        "DepreciationDepletionAndAmortization", "DepreciationAndAmortization",
    ],
    "interest_expense": ["InterestExpense", "InterestExpenseDebt"],
}

# Balance-sheet stocks: point-in-time, no duration, never de-cumulated.
INSTANT_METRICS = frozenset({
    "total_assets", "total_liabilities", "current_assets", "current_liabilities",
    "stockholders_equity", "long_term_debt", "short_term_debt", "cash",
    "shares_outstanding",
})
FLOW_METRICS = frozenset(CONCEPT_CHAINS) - INSTANT_METRICS

# Per-share values are ratios: they are NOT summed or de-cumulated arithmetically.
RATIO_METRICS = frozenset({"eps_basic", "eps_diluted"})

QUARTER_DAYS = 91
ANNUAL_DAYS = 365
DURATION_TOLERANCE = 20          # a "quarter" in practice runs 85-98 days
ANNUAL_TOLERANCE = 30


@dataclass(frozen=True)
class Fact:
    metric: str
    concept: str
    concept_rank: int            # position in the chain; lower wins
    value: float
    end: date
    start: date | None
    fy: int
    fp: str
    form: str

    @property
    def duration_days(self) -> int | None:
        if self.start is None:
            return None
        return (self.end - self.start).days

    @property
    def is_instant(self) -> bool:
        return self.start is None


def _parse_date(s: str | None) -> date | None:
    try:
        return date.fromisoformat(s) if s else None
    except (ValueError, TypeError):
        return None


def collect_facts(companyfacts: dict) -> dict[str, list[Fact]]:
    """Every usable fact per metric, from EVERY concept in the chain.

    The old code chose one concept up front and lived with it. Collecting all of
    them and ranking by chain position lets a filer switch tags mid-history
    without a metric silently going blank.
    """
    gaap = companyfacts.get("facts", {}).get("us-gaap", {})
    dei = companyfacts.get("facts", {}).get("dei", {})
    out: dict[str, list[Fact]] = {}

    for metric, chain in CONCEPT_CHAINS.items():
        facts: list[Fact] = []
        for rank, concept in enumerate(chain):
            node = gaap.get(concept) or dei.get(concept)
            if not node:
                continue
            for unit, entries in (node.get("units") or {}).items():
                # USD, shares, USD/shares are the units we model. Anything else
                # (pure, EUR, ...) is a different measure - skip rather than mix.
                if unit not in ("USD", "shares", "USD/shares"):
                    continue
                for e in entries:
                    val, end = e.get("val"), _parse_date(e.get("end"))
                    if val is None or end is None:
                        continue
                    facts.append(Fact(
                        metric=metric, concept=concept, concept_rank=rank,
                        value=float(val), end=end, start=_parse_date(e.get("start")),
                        fy=int(e.get("fy") or 0), fp=(e.get("fp") or ""),
                        form=(e.get("form") or ""),
                    ))
        out[metric] = facts
    return out


def _target_duration(fp: str) -> int:
    return ANNUAL_DAYS if fp == "FY" else QUARTER_DAYS


def _duration_ok(days: int, fp: str) -> bool:
    tol = ANNUAL_TOLERANCE if fp == "FY" else DURATION_TOLERANCE
    return abs(days - _target_duration(fp)) <= tol


def pick(facts: list[Fact], metric: str, end: date, fp: str) -> Fact | None:
    """The single best fact for one metric in one period.

    Instants match on end date alone. Flows must match the period's natural
    duration - this is what stops a 180-day year-to-date figure being served as a
    quarter. Ties break on chain rank, then on the most recently filed value.
    """
    candidates = [f for f in facts if f.end == end]
    if not candidates:
        return None

    if metric in INSTANT_METRICS:
        inst = [f for f in candidates if f.is_instant]
        # shares_outstanding falls back to the weighted-average duration fact
        pool = inst or candidates
        return sorted(pool, key=lambda f: (f.concept_rank, -f.fy))[0]

    sized = [f for f in candidates
             if f.duration_days is not None and _duration_ok(f.duration_days, fp)]
    if not sized:
        return None
    return sorted(
        sized,
        key=lambda f: (f.concept_rank, abs(f.duration_days - _target_duration(fp))),
    )[0]


def _decumulate(facts: list[Fact], metric: str, end: date, fp: str) -> float | None:
    """Fallback for filers that report ONLY year-to-date in their 10-Qs.

    Qn = YTD(Qn) - YTD(Q(n-1)). Only ever called for flow metrics, and never for
    per-share ratios, where the subtraction is not meaningful.
    """
    if metric in INSTANT_METRICS or metric in RATIO_METRICS or fp == "FY":
        return None
    ytd = [f for f in facts
           if f.end == end and f.duration_days and f.duration_days > QUARTER_DAYS + DURATION_TOLERANCE]
    if not ytd:
        return None
    cur = sorted(ytd, key=lambda f: (f.concept_rank, -f.duration_days))[0]
    if cur.start is None:
        return None
    # The prior stretch is the same fiscal-year start with an earlier end.
    prior = [f for f in facts
             if f.start == cur.start and f.end < cur.end and f.duration_days]
    if not prior:
        return None
    prev = sorted(prior, key=lambda f: f.end, reverse=True)[0]
    return cur.value - prev.value


def periods(all_facts: dict[str, list[Fact]], limit: int = 24) -> list[tuple[date, str, int, str]]:
    """Reporting periods worth building a snapshot for, newest first.

    Driven by the income-statement flows: a period only exists if someone
    reported activity over it. Balance-sheet instants then attach by end date.
    """
    seen: dict[tuple[date, str], tuple[int, str]] = {}
    for metric in ("revenue", "net_income", "operating_income"):
        for f in all_facts.get(metric, []):
            if f.duration_days is None or not f.fp:
                continue
            if not _duration_ok(f.duration_days, f.fp):
                continue
            key = (f.end, f.fp)
            if key not in seen:
                seen[key] = (f.fy, f.form)
    ordered = sorted(seen.items(), key=lambda kv: kv[0][0], reverse=True)
    return [(end, fp, fy, form) for (end, fp), (fy, form) in ordered[:limit]]


def build_snapshots(companyfacts: dict, limit: int = 24) -> list[dict]:
    """companyfacts JSON -> per-period rows ready for the snapshots table."""
    all_facts = collect_facts(companyfacts)
    rows: list[dict] = []

    for end, fp, fy, form in periods(all_facts, limit=limit):
        row: dict = {
            "period_end": end.isoformat(), "fp": fp, "fy": fy, "form": form,
            "duration_days": _target_duration(fp),
        }
        for metric in CONCEPT_CHAINS:
            facts = all_facts.get(metric, [])
            chosen = pick(facts, metric, end, fp)
            if chosen is not None:
                row[metric] = chosen.value
                continue
            # No natively-correct fact: try de-cumulating a YTD figure.
            row[metric] = _decumulate(facts, metric, end, fp)

        # Gross profit is frequently not tagged even when both inputs are.
        if row.get("gross_profit") is None and row.get("revenue") is not None \
                and row.get("cost_of_revenue") is not None:
            row["gross_profit"] = row["revenue"] - row["cost_of_revenue"]
        rows.append(row)
    return rows


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
