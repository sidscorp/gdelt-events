"""The freshness gate must be provably able to fail.

Ingestion was dead for 69 hours (2026-11-16 -> 2026-11-19) while `GDELT-Ingest` exited 0
on every cycle, `GDELT-PipelineCheck` reported 0, and `/api/stats` returned HTTP 200.
A check that reads process state or HTTP status cannot see a feed that stopped, so the
gate reads `max(gal.crawled_at)` instead. These tests pin the rule AND run the real
`check_freshness()` against a fixture database that is deliberately stale, because a
check nobody has watched fail is not a check.

Run with the repo venv:  .venv\\Scripts\\python.exe -m pytest tests\\test_freshness_gate.py -v
"""
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))

import pipeline_health as ph  # noqa: E402

NOW = datetime(2026, 11, 22, 19, 0, 0, tzinfo=timezone.utc)


def _stamp(when: datetime) -> str:
    return when.strftime("%Y%m%d%H%M%S")


def _isolate():
    """Point the alert and pause files at a scratch dir so tests never touch data/.

    Returns a (directory, restore) pair; call restore() in a finally block.
    """
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    saved = (ph.FRESH_ALERT_FILE, ph.FRESH_PAUSE_FILE)
    ph.FRESH_ALERT_FILE = root / "alerts" / "freshness.json"
    ph.FRESH_PAUSE_FILE = root / ".freshness_pause"

    def restore():
        ph.FRESH_ALERT_FILE, ph.FRESH_PAUSE_FILE = saved
        tmp.cleanup()

    return root, restore


def _fixture_db(path: Path, rows: list[datetime]) -> Path:
    """A DuckDB file holding just the one column the gate reads."""
    import duckdb

    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE gal (crawled_at BIGINT)")
    for when in rows:
        con.execute("INSERT INTO gal VALUES (?)", [int(_stamp(when))])
    con.close()
    return path


# --- the rule ---------------------------------------------------------------------

def test_three_hours_stale_fails():
    """The fixture named in issue #42."""
    level, detail = ph.evaluate_freshness(_stamp(NOW - timedelta(hours=3)), now=NOW)
    assert level == "FAIL", detail
    assert "180 min old" in detail
    assert "limit 60" in detail


def test_ten_minutes_fresh_passes():
    level, detail = ph.evaluate_freshness(_stamp(NOW - timedelta(minutes=10)), now=NOW)
    assert level == "PASS", detail
    assert "10 min old" in detail


def test_the_limit_is_a_boundary_not_a_guess():
    """Exactly 60 minutes still passes; 61 fails. GDELT publishes every ~15."""
    assert ph.evaluate_freshness(_stamp(NOW - timedelta(minutes=60)), now=NOW)[0] == "PASS"
    assert ph.evaluate_freshness(_stamp(NOW - timedelta(minutes=61)), now=NOW)[0] == "FAIL"


def test_an_empty_corpus_fails():
    assert ph.evaluate_freshness(None, now=NOW)[0] == "FAIL"


def test_an_unreadable_value_fails():
    level, detail = ph.evaluate_freshness("not-a-timestamp", now=NOW)
    assert level == "FAIL"
    assert "unreadable" in detail


def test_a_future_stamp_fails():
    """A row in the future means this host's clock is behind, so ages mean nothing."""
    level, detail = ph.evaluate_freshness(_stamp(NOW + timedelta(minutes=30)), now=NOW)
    assert level == "FAIL"
    assert "future" in detail


# --- the check, against a real database ------------------------------------------

def test_a_stale_fixture_database_fails_and_writes_an_alert():
    root, restore = _isolate()
    try:
        db = _fixture_db(root / "stale.duckdb",
                         [datetime.now(timezone.utc) - timedelta(hours=3)])
        results = []
        ph.check_freshness(results, db_path=db)
        assert results and results[0][0] == "FAIL", results
        alert = ph._read_freshness_alert()
        assert alert["state"] == "unhealthy"
        assert alert["delivered"] is False
        assert alert["consecutive_failures"] == 1
        assert alert["age_minutes"] >= 179

        # A second failing cycle keeps the first-failure time and counts up.
        ph.check_freshness(results, db_path=db)
        again = ph._read_freshness_alert()
        assert again["consecutive_failures"] == 2
        assert again["first_failed_at"] == alert["first_failed_at"]
    finally:
        restore()


def test_a_fresh_fixture_database_passes_and_marks_recovery():
    root, restore = _isolate()
    try:
        stale = _fixture_db(root / "stale.duckdb",
                            [datetime.now(timezone.utc) - timedelta(hours=3)])
        ph.check_freshness([], db_path=stale)
        assert ph._read_freshness_alert()["state"] == "unhealthy"

        fresh = _fixture_db(root / "fresh.duckdb",
                            [datetime.now(timezone.utc) - timedelta(minutes=10)])
        results = []
        ph.check_freshness(results, db_path=fresh)
        assert results and results[0][0] == "PASS", results
        assert ph._read_freshness_alert()["state"] == "recovered"
    finally:
        restore()


def test_a_paused_gate_warns_instead_of_passing():
    root, restore = _isolate()
    try:
        db = _fixture_db(root / "stale.duckdb",
                         [datetime.now(timezone.utc) - timedelta(hours=3)])
        ph.FRESH_PAUSE_FILE.write_text("rebuilding the dev slice\n")
        results = []
        ph.check_freshness(results, db_path=db)
        assert results[0][0] == "WARN", results
        assert "rebuilding the dev slice" in results[0][2]
        assert not ph.FRESH_ALERT_FILE.exists()
    finally:
        restore()
