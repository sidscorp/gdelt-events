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
    observations, rule_decline_streak, rule_loss, rule_margin_move,
    rule_nonoperating, rule_revenue_growth, rule_sector_position,
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


def test_missing_revenue_explains_itself():
    obs = observations(_snap(net_income=16_500_000_000), {}, {})
    assert any("does not tag" in o.text for o in obs), \
        "a bank with no revenue line should say why, not show a dash"


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
