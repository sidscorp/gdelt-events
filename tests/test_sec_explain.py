"""Tests for derived facts and the observation rules.

A wrong sentence on this page is worse than no sentence, so these assert the
arithmetic AND the guard that no observation may state a number that was not
computed. Pure functions, no database, no network.

    python tests/test_sec_explain.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.sec_derive import compute_for_company, _pct_change, _percentile  # noqa: E402
from pipeline.sec_explain import (  # noqa: E402
    bars_takeaway, line_takeaway, observations, rule_decline_streak,
    rule_leverage, rule_loss, rule_margin_move, rule_nonoperating,
    rule_revenue_growth, rule_sector_position,
)

GOOGL_CO = {"sic": "7370", "sic_description": "Services-Computer Programming"}


def _snap(**kw):
    base = {"cik": 1, "period_end": "2026-06-30", "fp": "Q2", "fy": 2026, "form": "10-Q",
            "revenue": None, "net_income": None, "operating_income": None,
            "gross_profit": None, "current_assets": None, "current_liabilities": None}
    base.update(kw)
    return base


# ── derive arithmetic ────────────────────────────────────────────────────────

def test_pct_change_handles_zero_and_none():
    assert _pct_change(110, 100) == 0.1
    assert _pct_change(None, 100) is None
    assert _pct_change(100, 0) is None          # not infinity
    assert _pct_change(50, -100) == 1.5         # abs() denominator


def test_yoy_compares_like_quarter_not_previous_quarter():
    """Comparing Q2 to Q1 would make every seasonal business look volatile."""
    rows = [
        _snap(period_end="2026-06-30", fp="Q2", fy=2026, revenue=120),
        _snap(period_end="2026-03-31", fp="Q1", fy=2026, revenue=200),
        _snap(period_end="2025-06-30", fp="Q2", fy=2025, revenue=100),
    ]
    d = {r["period_end"]: r for r in compute_for_company(rows)}
    assert abs(d["2026-06-30"]["revenue_yoy"] - 0.2) < 1e-9      # vs Q2 last year
    assert abs(d["2026-06-30"]["revenue_qoq"] - (-0.4)) < 1e-9   # vs Q1, separately


def test_decline_streak_counts_consecutive_yoy_falls():
    rows = [
        _snap(period_end="2026-06-30", fp="Q2", fy=2026, revenue=80),
        _snap(period_end="2025-06-30", fp="Q2", fy=2025, revenue=90),
        _snap(period_end="2024-06-30", fp="Q2", fy=2024, revenue=100),
    ]
    d = {r["period_end"]: r for r in compute_for_company(rows)}
    assert d["2026-06-30"]["decline_streak"] == 2


def test_percentile_is_fraction_at_or_below():
    assert _percentile([1, 2, 3, 4], 3) == 0.75
    assert _percentile([], 3) is None


# ── observation rules ────────────────────────────────────────────────────────

def test_nonoperating_fires_for_googl_shape():
    """Net income far above operating income is the fact a table hides."""
    s = _snap(revenue=119_796_000_000, operating_income=40_770_000_000,
              net_income=112_193_000_000)
    d = {"nonop_income": 71_423_000_000, "nonop_share_pretax": 0.6366}
    o = rule_nonoperating(s, d)
    assert o is not None
    assert "$112.19B" in o.text and "$40.77B" in o.text and "$71.42B" in o.text
    # It must NOT claim this is purely a gain: the figure is net of tax.
    assert "net of tax" in o.text


def test_nonoperating_silent_when_operations_explain_profit():
    s = _snap(revenue=100, operating_income=30, net_income=28)
    assert rule_nonoperating(s, {"nonop_income": -2, "nonop_share_pretax": -0.0625}) is None


def test_loss_is_reported_and_not_suppressed_by_composition():
    """Intel: an $11.03B loss was hidden because both sentences shared a kind."""
    s = _snap(revenue=16_130_000_000, operating_income=1_800_000_000,
              net_income=-11_030_000_000)
    d = {"nonop_income": -12_830_000_000, "nonop_share_pretax": -0.877,
         "revenue_yoy": 0.254}
    obs = observations(s, d, {})
    kinds = [o.kind for o in obs]
    assert "loss" in kinds, kinds
    assert obs[0].kind == "loss", "a net loss must lead"
    assert "net loss of $11.03B" in obs[0].text


def test_growth_superlative_needs_enough_history():
    s = _snap(revenue=100)
    weak = rule_revenue_growth(s, {"revenue_yoy": 0.2, "rev_growth_is_best": 1,
                                   "rev_growth_rank_n": 3})
    assert "fastest" not in weak.text, "3 periods is not enough to call it a record"
    strong = rule_revenue_growth(s, {"revenue_yoy": 0.2, "rev_growth_is_best": 1,
                                     "rev_growth_rank_n": 8})
    assert "fastest growth in 8" in strong.text


def test_margin_move_ignores_noise():
    s = _snap(revenue=100)
    assert rule_margin_move(s, {"operating_margin_yoy_pp": 0.4,
                                "operating_margin": 0.30}) is None
    o = rule_margin_move(s, {"operating_margin_yoy_pp": -3.2, "operating_margin": 0.34})
    assert "narrowed 3.2 percentage points" in o.text and "34.0%" in o.text


def test_sector_claim_requires_a_real_peer_group():
    s = _snap(revenue=100)
    d = {"sector_gross_margin_pct": 0.9, "gross_margin": 0.6, "sector_peers": 3}
    assert rule_sector_position(s, d, GOOGL_CO) is None, "3 peers is not an industry"
    d["sector_peers"] = 214
    o = rule_sector_position(s, d, GOOGL_CO)
    assert "top quartile" in o.text and "214 filers" in o.text


def test_mid_pack_sector_position_says_nothing():
    s = _snap(revenue=100)
    d = {"sector_gross_margin_pct": 0.5, "gross_margin": 0.4, "sector_peers": 100}
    assert rule_sector_position(s, d, GOOGL_CO) is None


def test_no_observation_invents_a_number():
    """Every figure in the prose must trace to the inputs. Guards the whole
    category of failure this feature exists to avoid."""
    s = _snap(revenue=119_796_000_000, operating_income=40_770_000_000,
              net_income=112_193_000_000, gross_profit=73_850_000_000)
    d = {"nonop_income": 71_423_000_000, "nonop_share_pretax": 0.6366,
         "revenue_yoy": 0.242, "rev_growth_is_best": 1, "rev_growth_rank_n": 8,
         "operating_margin": 0.3403, "operating_margin_yoy_pp": -2.7,
         "gross_margin": 0.6165, "sector_gross_margin_pct": 0.82, "sector_peers": 214}
    allowed = set()
    for v in list(s.values()) + list(d.values()):
        if isinstance(v, (int, float)):
            for scale, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
                if abs(v) >= scale:
                    # The regex below extracts bare numerals, so whitelist both
                    # "112.19B" and the "112.19" it will actually see.
                    allowed.add(f"{abs(v)/scale:,.2f}{suf}")
                    allowed.add(f"{abs(v)/scale:,.2f}")
                    allowed.add(f"{abs(v)/scale:,.0f}{suf}")
                    allowed.add(f"{abs(v)/scale:,.0f}")
            allowed.update({f"{abs(v)*100:,.1f}", f"{abs(v)*100:,.0f}",
                            f"{abs(v):,.1f}", f"{abs(v):,.0f}", str(int(abs(v)))})
    for o in observations(s, d, GOOGL_CO, limit=6):
        for num in re.findall(r"\d[\d,]*(?:\.\d+)?", o.text):
            assert num in allowed, f"unexplained number {num!r} in: {o.text}"


def test_leverage_reassures_only_actual_lenders():
    """The sentence used to fire on the ratio alone, so a distressed operating
    company was told 93% leverage is "normal for a lender"."""
    s = _snap(total_assets=4_900_000_000_000, total_liabilities=4_557_000_000_000)

    bank = rule_leverage(s, {}, {"leveraged_by_design": True})
    assert "normal for a lender" in bank.text

    opco = rule_leverage(s, {}, {"leveraged_by_design": False})
    assert opco is not None, "the leverage figure itself is still worth stating"
    assert "normal for a lender" not in opco.text
    assert "93%" in opco.text


def test_leverage_silent_when_liabilities_exceed_assets():
    """Negative equity is a louder story than leverage, and it is where the
    unverified data-quality tail sits. >100% funded by liabilities is not a
    sentence this page should print."""
    s = _snap(total_assets=100_000_000, total_liabilities=115_000_000)
    assert rule_leverage(s, {}, {"leveraged_by_design": False}) is None
    assert rule_leverage(s, {}, {"leveraged_by_design": True}) is None


def test_sec_explain_copies_have_not_diverged():
    """pipeline/sec_explain.py and dashboard/sec_explain.py are duplicates. These
    tests import the pipeline copy; the dashboard serves its own. Until they are
    deduplicated, an edit to one and not the other is invisible - so assert it."""
    root = Path(__file__).resolve().parent.parent
    pipe = (root / "pipeline" / "sec_explain.py").read_bytes()
    dash = (root / "dashboard" / "sec_explain.py").read_bytes()
    assert pipe == dash, (
        "pipeline/sec_explain.py and dashboard/sec_explain.py have diverged - "
        "prod serves the dashboard copy, these tests cover the pipeline one")


def test_missing_revenue_explains_itself():
    obs = observations(_snap(net_income=16_500_000_000), {}, {})
    assert any("does not tag" in o.text for o in obs), \
        "a bank with no revenue line should say why, not show a dash"


# ── chart takeaways ────────────────────────────────────────────────────────────

def test_bars_takeaway_calls_out_window_high_and_low():
    t = bars_takeaway("Revenue", [100, 120, 90, 140], "quarterly")
    assert "$140" in t and "highest" in t, t
    t = bars_takeaway("Net income", [50, 40, 10], "quarterly")
    assert "lowest" in t, t
    t = bars_takeaway("Revenue", [100, 90, 130, 120], "annual")
    assert "years" in t and "rising in 1 of the last 3" in t, t
    assert bars_takeaway("Revenue", [100], "quarterly") is None


def test_line_takeaway_states_direction():
    t = line_takeaway([0.10, 0.14, 0.20])
    assert "widened" in t and "10.0%" in t and "20.0%" in t, t
    assert "narrowed" in line_takeaway([0.30, 0.22, 0.15])
    assert "flat" in line_takeaway([0.300, 0.304, 0.301]).lower()
    assert line_takeaway([0.1, 0.2]) is None


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
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{len(fns) - failed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
