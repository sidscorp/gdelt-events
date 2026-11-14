"""The health check must be provably able to fail.

`GDELT-EmbedWatchdog` sat dead for months while the pipeline it "watched" degraded,
so these tests pin the rule that decides failure rather than trusting the monitor.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))

import pipeline_health as ph  # noqa: E402


def test_a_stable_backlog_passes():
    """A few hundred unembedded between 15-minute drains is normal."""
    level, detail = ph.evaluate_backlog(400, [380, 410])
    assert level == "PASS", detail


def test_sustained_growth_fails():
    level, detail = ph.evaluate_backlog(700, [300, 500])
    assert level == "FAIL"
    assert "growing" in detail
    assert "300 -> 500 -> 700" in detail


def test_one_increase_is_not_growth():
    """Batches mean samples bounce; one rise says nothing."""
    assert ph.evaluate_backlog(500, [300, 200])[0] == "PASS"


def test_the_ceiling_fails_even_when_the_trend_is_flat():
    level, detail = ph.evaluate_backlog(ph.BACKLOG_CEILING + 1, [ph.BACKLOG_CEILING] * 2)
    assert level == "FAIL"
    assert "ceiling" in detail
