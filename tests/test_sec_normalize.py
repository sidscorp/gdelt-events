"""Regression + invariant tests for SEC companyfacts normalization.

Guards the three bugs that shipped wrong financials to gdeltmonitor.com/sec-analysis
on 2026-08-02, and the class of bug they belong to.

Runs offline against trimmed fixtures in tests/fixtures/ - no SEC calls, no rate
limits, no network flakiness. Fixtures were verified to reproduce the same results
as the full multi-megabyte companyfacts files.

    pytest tests/test_sec_normalize.py -v
    python  tests/test_sec_normalize.py       # runs without pytest too
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.sec_normalize import (  # noqa: E402
    CONCEPT_CHAINS, FLOW_METRICS, INSTANT_METRICS, RATIO_METRICS,
    build_snapshots, collect_facts,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load(name: str) -> dict:
    with open(FIXTURES / f"companyfacts_{name}.json", encoding="utf-8") as fh:
        return json.load(fh)


def _rows(name: str, limit: int = 8) -> list[dict]:
    return build_snapshots(_load(name), limit=limit)


def _find(rows: list[dict], period_end: str) -> dict:
    for r in rows:
        if r["period_end"] == period_end:
            return r
    raise AssertionError(f"no snapshot for {period_end}; got {[r['period_end'] for r in rows]}")


# ── the three shipped bugs ───────────────────────────────────────────────────

def test_revenue_survives_a_tag_change():
    """Bug 1: Alphabet stopped tagging RevenueFromContractWithCustomer... and the
    resolver, which picked one concept globally, returned None from then on."""
    q2 = _find(_rows("googl"), "2026-06-30")
    assert q2["revenue"] is not None, "revenue went blank again"
    assert abs(q2["revenue"] - 119_796_000_000) < 1e6, q2["revenue"]
    # gross profit is derived from revenue, so it died with it
    assert q2["gross_profit"] is not None


def test_quarter_is_the_quarter_not_year_to_date():
    """Bug 2: a 10-Q carries both the 90-day and the year-to-date span under the
    same end date and fp. Picking the wrong one showed a quarter larger than the
    prior full year."""
    q2 = _find(_rows("googl"), "2026-06-30")
    assert abs(q2["net_income"] - 112_193_000_000) < 1e6, q2["net_income"]
    assert q2["net_income"] != 174_771_000_000, "picked the year-to-date figure"
    assert abs(q2["revenue"] - 119_796_000_000) < 1e6
    assert q2["revenue"] != 229_692_000_000, "picked the year-to-date figure"


def test_instants_are_never_decumulated():
    """Bug 3: subtracting the prior period from a point-in-time balance-sheet fact
    turned 12.23 billion shares into 142 million (12,230M - 12,088M)."""
    q2 = _find(_rows("googl"), "2026-06-30")
    assert abs(q2["shares_outstanding"] - 12_230_000_000) < 1e6, q2["shares_outstanding"]
    assert q2["shares_outstanding"] != 142_000_000, "de-cumulated an instant again"


def test_total_liabilities_never_falls_back_to_assets():
    """Bug 4 (latent): LiabilitiesAndStockholdersEquity is assets, not liabilities."""
    assert "LiabilitiesAndStockholdersEquity" not in CONCEPT_CHAINS["total_liabilities"]
    for name in ("googl", "msft"):
        for r in _rows(name):
            if r.get("total_liabilities") and r.get("total_assets"):
                assert r["total_liabilities"] < r["total_assets"], \
                    f"{name} {r['period_end']}: liabilities >= assets"


# ── invariants: catch the class, not just these three cases ──────────────────

def test_quarter_never_exceeds_its_own_fiscal_year():
    """The single sharpest smell for period-mixing."""
    for name in ("googl", "msft"):
        rows = _rows(name, limit=16)
        fy = {r["fy"]: r for r in rows if r["fp"] == "FY"}
        for r in rows:
            if r["fp"] == "FY" or r["fy"] not in fy:
                continue
            year = fy[r["fy"]]
            for metric in ("revenue", "net_income", "operating_income"):
                q, y = r.get(metric), year.get(metric)
                if q is None or y is None or y <= 0:
                    continue
                assert q <= y * 1.02, \
                    f"{name} {r['period_end']} {metric}: quarter {q:,.0f} > FY {y:,.0f}"


def test_eps_is_consistent_with_net_income_and_shares():
    """eps x shares should land near net income. Catches a share count or an
    earnings figure drawn from the wrong period."""
    for name in ("googl", "msft"):
        for r in _rows(name):
            eps, sh, ni = r.get("eps_basic"), r.get("shares_outstanding"), r.get("net_income")
            if not all(isinstance(v, (int, float)) and v for v in (eps, sh, ni)):
                continue
            implied = eps * sh
            assert 0.5 < implied / ni < 2.0, (
                f"{name} {r['period_end']}: eps {eps} x shares {sh:,.0f} = {implied:,.0f} "
                f"vs net income {ni:,.0f}")


def test_instant_metrics_are_not_negative():
    for name in ("googl", "msft"):
        for r in _rows(name):
            for metric in INSTANT_METRICS:
                v = r.get(metric)
                if v is not None:
                    assert v >= 0, f"{name} {r['period_end']} {metric} = {v}"


def test_flow_facts_carry_a_duration_and_instants_do_not():
    """The structural property the old code was missing entirely."""
    facts = collect_facts(_load("googl"))
    for metric in FLOW_METRICS - RATIO_METRICS:
        for f in facts.get(metric, []):
            assert f.duration_days is not None, f"{metric} flow fact with no start"
    for metric in ("total_assets", "stockholders_equity", "cash"):
        instants = [f for f in facts.get(metric, []) if f.is_instant]
        assert instants, f"{metric} should have point-in-time facts"


def test_non_calendar_fiscal_year_is_handled():
    """MSFT's fiscal year ends in June. A calendar-year assumption mislabels every
    period, so this is the guard against fitting the logic to Alphabet."""
    rows = _rows("msft", limit=8)
    june = _find(rows, "2026-06-30")
    assert june["fp"] == "FY" and june["form"] == "10-K", june
    march = _find(rows, "2026-03-31")
    assert march["fp"] == "Q3", march      # Q3 of a June fiscal year, not Q1


def test_quarters_sum_to_the_year():
    """Independent check that de-cumulation and duration selection agree."""
    rows = _rows("msft", limit=16)
    fy = _find(rows, "2026-06-30")
    quarters = [r for r in rows if r["fy"] == fy["fy"] and r["fp"] in ("Q1", "Q2", "Q3")]
    assert len(quarters) == 3, [r["period_end"] for r in quarters]
    three = sum(r["revenue"] for r in quarters)
    assert 0.5 < three / fy["revenue"] < 0.85, (
        f"Q1-Q3 revenue {three:,.0f} vs FY {fy['revenue']:,.0f}")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as e:
            print(f"  FAIL  {fn.__name__}: {e}")
            failed += 1
        except Exception as e:
            # A crash is a failure, not a reason to abandon the run. Catching only
            # AssertionError here meant one broken metric aborted the whole suite
            # and hid every result after it.
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{len(fns) - failed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
