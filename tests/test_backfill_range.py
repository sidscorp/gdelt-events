"""Backfill date ranges are UTC, because GDELT file timestamps are UTC (issue #46).

pipeline/backfill_range.py has no heavy imports, so these tests need no network,
no DuckDB and no config.
"""
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import backfill_range  # noqa: E402
from pipeline.backfill_range import resolve_range  # noqa: E402

SLOT = timedelta(minutes=15)  # GDELT v2 publishes one file set every 15 minutes
TS_FMT = "%Y%m%d%H%M%S"       # timestamp format in masterfilelist.txt / GAL names


def _included(ts: datetime, since: datetime, until: datetime) -> bool:
    """Same comparison as downloader.fetch_file_list."""
    return since.strftime(TS_FMT) <= ts.strftime(TS_FMT) <= until.strftime(TS_FMT)


def _newest_slot(now: datetime) -> datetime:
    return now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0)


def test_default_until_is_pinned_utc_now(monkeypatch):
    pinned = datetime(2026, 11, 19, 20, 7, 30)  # naive UTC
    monkeypatch.setattr(backfill_range, "utc_now", lambda: pinned)

    since, until = resolve_range(days=1)

    assert until == pinned
    assert since == pinned - timedelta(days=1)
    newest = _newest_slot(pinned)
    assert _included(newest, since, until)
    assert pinned - newest < SLOT  # newest included slot is within one slot of now


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="needs POSIX time.tzset")
def test_no_until_includes_slot_from_one_hour_ago_even_in_est(monkeypatch):
    """Regression: with a naive *local* 'now', an EST machine dropped the last ~5 hours."""
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        since, until = resolve_range(days=1)
        hour_ago_utc = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
        assert _included(_newest_slot(hour_ago_utc), since, until)
        # until must track UTC, not local time
        utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
        assert abs(until - utc_now) < timedelta(minutes=1)
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time.tzset()


def test_explicit_dates_are_utc_midnight():
    since, until = resolve_range(since="2026-02-09", until="2026-04-09")
    assert since == datetime(2026, 2, 9)
    assert until == datetime(2026, 4, 9)


def test_since_wins_over_days():
    since, _ = resolve_range(days=60, since="2026-11-01")
    assert since == datetime(2026, 11, 1)


def test_requires_days_or_since():
    with pytest.raises(ValueError):
        resolve_range()


def test_utc_now_is_naive_utc():
    now = backfill_range.utc_now()
    assert now.tzinfo is None
    assert abs(now - datetime.now(timezone.utc).replace(tzinfo=None)) < timedelta(seconds=5)
