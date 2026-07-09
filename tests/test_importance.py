"""Unit tests for the pure importance scorer (dashboard/importance.py).

No server / DB needed — this exercises the ranking math directly.

Run with:
    pytest tests/test_importance.py -v
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Make the dashboard package importable without installing it.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))

from importance import compute_importance  # noqa: E402


def _ts(dt):
    return int(dt.strftime("%Y%m%d%H%M%S"))


def test_recent_burst_beats_old_singleton():
    now = datetime(2026, 7, 1, 12, 0, 0)
    events = [
        {"id": "burst", "n_sources": 18,
         "first_seen": _ts(now - timedelta(hours=3)),
         "latest_seen": _ts(now - timedelta(hours=1))},
        {"id": "old_single", "n_sources": 1,
         "crawled_at": _ts(now - timedelta(hours=50))},
    ]
    ranked = compute_importance(events, now=now)
    assert ranked[0]["id"] == "burst"
    assert ranked[0]["_imp"] > ranked[1]["_imp"]


def test_coverage_dominates_when_recency_equal():
    now = datetime(2026, 7, 1, 12, 0, 0)
    same = _ts(now - timedelta(hours=1))
    events = [
        {"id": "low", "n_sources": 2, "first_seen": same, "latest_seen": same},
        {"id": "high", "n_sources": 25, "first_seen": same, "latest_seen": same},
    ]
    ranked = compute_importance(events, now=now)
    assert ranked[0]["id"] == "high"


def test_all_singletons_fall_back_to_recency():
    """When coverage is constant, ranking reduces to newest-first (≈ date sort)."""
    now = datetime(2026, 7, 1, 12, 0, 0)
    events = [
        {"id": "older", "n_sources": 1, "crawled_at": _ts(now - timedelta(hours=5))},
        {"id": "newer", "n_sources": 1, "crawled_at": _ts(now - timedelta(hours=1))},
    ]
    ranked = compute_importance(events, now=now)
    assert [e["id"] for e in ranked] == ["newer", "older"]


def test_scores_are_bounded_and_present():
    now = datetime(2026, 7, 1, 12, 0, 0)
    events = [
        {"id": "a", "n_sources": 10,
         "first_seen": _ts(now - timedelta(hours=6)),
         "latest_seen": _ts(now - timedelta(hours=2))},
        {"id": "b", "n_sources": 1, "crawled_at": _ts(now - timedelta(hours=1))},
    ]
    ranked = compute_importance(events, now=now)
    for e in ranked:
        assert "_imp" in e
        assert 0.0 <= e["_imp"] <= 1.0


def test_empty_and_junk_timestamps_do_not_crash():
    now = datetime(2026, 7, 1, 12, 0, 0)
    assert compute_importance([]) == []
    events = [
        {"id": "junk", "n_sources": "not-a-number",
         "first_seen": "garbage", "latest_seen": None},
        {"id": "ok", "n_sources": 3, "crawled_at": _ts(now - timedelta(hours=1))},
    ]
    ranked = compute_importance(events, now=now)
    assert len(ranked) == 2
    assert all("_imp" in e for e in ranked)
