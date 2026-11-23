"""The briefing freshness line must report observations, not promises.

Issue #71. Three things disagreed about "fresh":

  * the bar computed ``generated_at + ttl`` and printed "Next update in any
    moment" for the whole band where a 3h briefing was 3h-6h old;
  * the prewarm schedule ran 5x/day with an 8h overnight gap, so the promise was
    unachievable by construction;
  * nothing recorded whether an attempt had succeeded, so six weeks of ~90%
    prewarm failures were invisible to the page.

These tests pin all three halves: the server records every attempt per
(view, window), the public payload serves the state, and the shipped
``updateFreshnessBar`` renders it. The render assertion runs the real
``dashboard/static/js/markdown.js`` in Node (``tests/js/freshness_bar.mjs``)
rather than re-implementing its strings in Python — a second implementation
would drift and keep passing while the page lied.

Run with the repo venv:  .venv\\Scripts\\python.exe -m pytest tests\\test_briefing_freshness.py -v
"""
import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from flask import Flask

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dashboard"))
sys.path.insert(0, str(ROOT))

import models  # noqa: E402
from briefing import (  # noqa: E402
    ATTEMPT_TABLE_DDL,
    BriefingGenerationError,
    classify_generation_error,
    get_briefing_attempt,
    record_briefing_attempt,
)
from pipeline.prewarm_briefings import (  # noqa: E402
    CADENCE_HOURS,
    next_run_at,
    scheduled_hours,
)
from routes import api_briefing as route  # noqa: E402

NODE = shutil.which("node")
BAR_SCRIPT = ROOT / "tests" / "js" / "freshness_bar.mjs"


def _utc(offset=None):
    """UTC stamp in the same format briefing_cache uses."""
    return (datetime.utcnow() + (offset or timedelta())).strftime("%Y-%m-%d %H:%M:%S")


def _local_clock(utc_stamp):
    """The 24h clock the bar shows for a UTC stamp, in this machine's zone."""
    dt = datetime.strptime(utc_stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%H:%M")


def render(states):
    """innerHTML of the real freshness bar for each state dict."""
    if not NODE:
        pytest.fail("node is required to render the shipped freshness bar "
                    f"({BAR_SCRIPT}); install Node.js or run this test where it exists")
    proc = subprocess.run([NODE, str(BAR_SCRIPT), json.dumps(states)],
                          capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout)


# --- fixtures -------------------------------------------------------------------

@pytest.fixture
def users_db(tmp_path, monkeypatch):
    """A tiny users.db with the real schema pieces the route touches."""
    path = tmp_path / "users.db"

    def connect():
        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
        return con

    con = connect()
    con.execute("CREATE TABLE briefing_cache (cache_key TEXT PRIMARY KEY, briefing TEXT, "
                "article_count INTEGER, generated_at TEXT, sources_json TEXT, meta_json TEXT)")
    # The live table is created on the first recorded attempt; seed the schema
    # here so a test can start from "an attempt already happened".
    con.execute(ATTEMPT_TABLE_DDL)
    con.commit()
    con.close()
    monkeypatch.setattr(models, "get_user_db", connect)
    return path


@pytest.fixture
def client(users_db):
    route._regen_in_flight.clear()
    app = Flask(__name__)
    app.config.update(TESTING=True)
    app.register_blueprint(route.bp)
    yield app.test_client()
    route._regen_in_flight.clear()


def _seed_cache(path, key, generated_at, text="A cached update [1]."):
    con = sqlite3.connect(path)
    con.execute("INSERT OR REPLACE INTO briefing_cache VALUES (?, ?, ?, ?, ?, ?)",
                (key, text, 5, generated_at,
                 json.dumps([{"n": 1, "title": "A source", "link": "https://example.org"}]), "{}"))
    con.commit()
    con.close()


def _seed_attempt(path, view_id, hours, at, outcome, consecutive):
    con = sqlite3.connect(path)
    con.execute(
        "INSERT OR REPLACE INTO briefing_attempt_state "
        "(view_id, hours, cache_key, last_attempt_at, last_attempt_outcome, "
        " last_attempt_trigger, last_success_at, consecutive_failures, "
        " attempt_count, failure_count) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (view_id, hours, f"{view_id}:{hours}", at, outcome, "prewarm", None,
         consecutive, consecutive + 1, consecutive))
    con.commit()
    con.close()


# --- the payload the bar reads --------------------------------------------------

def test_fresh_combo_reports_a_next_run_time(users_db, client):
    """A fresh combo must carry a next-at time taken from the schedule."""
    generated_at = _utc(timedelta(minutes=-10))
    _seed_cache(users_db, "_all:24", generated_at)
    record_briefing_attempt("", 24, "ok", trigger="prewarm")

    body = client.get("/api/briefing?hours=24").json

    assert body["generated_at"] == generated_at
    assert body["last_attempt_outcome"] == "ok"
    assert body["consecutive_failures"] == 0
    assert body["last_attempt_at"]
    next_at = body["next_eligible_at"]
    assert next_at, "a fresh combo must report when the schedule will next run"
    # It is a future run, and it is not simply generated_at + ttl.
    assert datetime.strptime(next_at, "%Y-%m-%d %H:%M:%S") > datetime.utcnow()
    assert next_at != _utc(timedelta(minutes=-10, hours=8))

    html = render([{
        "generatedAt": body["generated_at"], "ttlS": body["cache_ttl_s"],
        "articleCount": body["article_count"],
        "attempt": {
            "nextEligibleAt": next_at,
            "lastAttemptAt": body["last_attempt_at"],
            "lastAttemptOutcome": body["last_attempt_outcome"],
            "consecutiveFailures": body["consecutive_failures"],
        },
    }])[0]

    assert "Updated 10m ago" in html
    assert f"next ~{_local_clock(next_at)}" in html
    assert "any moment" not in html
    assert "moment" not in html


def test_expired_combo_with_a_failed_attempt_names_the_failure(users_db, client):
    """Overdue + a failure since it came due = say so, with the code."""
    generated_at = _utc(timedelta(hours=-5))
    _seed_cache(users_db, "_all:3", generated_at)
    _seed_attempt(users_db, "_all", 3, _utc(timedelta(minutes=-4)),
                  "failed:provider_unavailable", 2)

    body = client.get("/api/briefing?hours=3").json

    assert body["stale"] is True
    assert body["last_attempt_outcome"] == "failed:provider_unavailable"
    assert body["consecutive_failures"] == 2

    html = render([{
        "generatedAt": body["generated_at"], "ttlS": body["cache_ttl_s"],
        "articleCount": body["article_count"],
        "attempt": {
            "nextEligibleAt": body["next_eligible_at"],
            "lastAttemptAt": body["last_attempt_at"],
            "lastAttemptOutcome": body["last_attempt_outcome"],
            "consecutiveFailures": body["consecutive_failures"],
        },
    }])[0]

    assert "Updated 5h ago" in html
    assert "last attempt 4m ago failed (provider_unavailable)" in html
    assert "any moment" not in html


def test_expired_combo_without_a_recent_attempt_says_overdue(users_db, client):
    """Silence is reported as silence, never as a success or a promise."""
    _seed_cache(users_db, "_all:24", _utc(timedelta(hours=-30)))
    # Last attempt happened before the cache went stale, so it explains nothing.
    _seed_attempt(users_db, "_all", 24, _utc(timedelta(hours=-31)), "ok", 0)

    body = client.get("/api/briefing?hours=24").json
    html = render([{
        "generatedAt": body["generated_at"], "ttlS": body["cache_ttl_s"],
        "articleCount": body["article_count"],
        "attempt": {
            "nextEligibleAt": body["next_eligible_at"],
            "lastAttemptAt": body["last_attempt_at"],
            "lastAttemptOutcome": body["last_attempt_outcome"],
            "consecutiveFailures": body["consecutive_failures"],
        },
    }])[0]

    assert "Updated 1d ago" in html
    assert "update overdue" in html
    assert "any moment" not in html


def test_the_string_any_moment_is_unreachable_at_any_age():
    """Sweep every window across the band the old code lied in (issue #71)."""
    now = datetime.utcnow()
    states = []
    for ttl_s in (3 * 3600, 4 * 3600, 8 * 3600, 12 * 3600, 24 * 3600):
        for age_s in (0, 59, ttl_s - 1, ttl_s, ttl_s + 1, ttl_s + 1800,
                      ttl_s + 3599, ttl_s + 3600, ttl_s + 3601, 2 * ttl_s,
                      6 * ttl_s, 30 * 24 * 3600):
            gen = (now - timedelta(seconds=age_s)).strftime("%Y-%m-%d %H:%M:%S")
            for attempt in ({}, {"lastAttemptOutcome": "failed:timeout",
                                 "lastAttemptAt": _utc(timedelta(minutes=-1))}):
                states.append({"generatedAt": gen, "ttlS": ttl_s,
                               "articleCount": 3, "attempt": attempt})
    rendered = render(states)

    assert len(rendered) == len(states)
    for html in rendered:
        assert "any moment" not in html
        assert "moment" not in html
        # Every state still says something about age.
        assert "Updated " in html


def test_a_3h_briefing_at_5h_old_is_overdue_not_promised(users_db):
    """The exact state observed on 2026-11-22: 3h window, 5h old."""
    html = render([{
        "generatedAt": _utc(timedelta(hours=-5)), "ttlS": 3 * 3600,
        "articleCount": 12, "attempt": {},
    }])[0]
    assert "Updated 5h ago" in html
    assert "update overdue" in html


# --- recording attempts ---------------------------------------------------------

def test_consecutive_failures_climb_and_a_success_resets_them(users_db):
    record_briefing_attempt("", 3, "failed:timeout")
    state = get_briefing_attempt("", 3)
    assert state["consecutive_failures"] == 1
    assert state["last_attempt_outcome"] == "failed:timeout"
    assert state["attempt_count"] == 1 and state["failure_count"] == 1
    assert state["last_success_at"] is None

    record_briefing_attempt("", 3, "failed:provider_unavailable", detail="503 from gateway")
    state = get_briefing_attempt("", 3)
    assert state["consecutive_failures"] == 2
    assert state["attempt_count"] == 2 and state["failure_count"] == 2

    # A skip says nothing about the provider: it must not be read as a recovery.
    record_briefing_attempt("", 3, "skipped_fresh")
    state = get_briefing_attempt("", 3)
    assert state["consecutive_failures"] == 2
    assert state["last_attempt_outcome"] == "skipped_fresh"
    assert state["attempt_count"] == 3 and state["failure_count"] == 2

    record_briefing_attempt("", 3, "ok")
    state = get_briefing_attempt("", 3)
    assert state["consecutive_failures"] == 0
    assert state["last_attempt_outcome"] == "ok"
    assert state["last_success_at"]
    assert state["attempt_count"] == 4 and state["failure_count"] == 2


def test_attempt_state_is_per_combo(users_db):
    record_briefing_attempt("", 3, "failed:timeout")
    record_briefing_attempt("europe", 3, "ok")
    record_briefing_attempt("", 24, "failed:busy")
    assert get_briefing_attempt("", 3)["consecutive_failures"] == 1
    assert get_briefing_attempt("europe", 3)["consecutive_failures"] == 0
    assert get_briefing_attempt("", 24)["last_attempt_outcome"] == "failed:busy"


def test_a_first_ever_skip_is_not_a_failure(users_db):
    """A skip on a combo that has never failed must not be born a failure.

    Found live on 2026-11-23: the first recorded attempt for global:24 was a
    skip, and the insert path marked it consecutive_failures=1.
    """
    record_briefing_attempt("", 24, "skipped_fresh")
    state = get_briefing_attempt("", 24)
    assert state["consecutive_failures"] == 0
    assert state["failure_count"] == 0
    assert state["attempt_count"] == 1
    assert state["last_attempt_outcome"] == "skipped_fresh"


def test_no_attempt_row_reads_as_no_observation(users_db):
    assert get_briefing_attempt("", 720) is None


@pytest.mark.parametrize("query, expected", [
    ("prewarm=1&hours=24", "skipped_fresh"),
    # A cache-free combo with nothing to summarise.
    ("prewarm=1&hours=3", "failed:insufficient_articles"),
])
def test_worker_records_skip_and_insufficient_articles(users_db, client, monkeypatch, query, expected):
    """Prewarm's two quiet outcomes are recorded, not lost."""
    monkeypatch.setenv("GDELT_BRIEFING_WORKER_TOKEN", "test-worker-token")
    if "24" in query:
        _seed_cache(users_db, "_all:24", _utc(timedelta(minutes=-5)))
    monkeypatch.setattr(route, "get_threads", lambda key: [])
    monkeypatch.setattr(route, "_fetch_briefing_events", lambda *a: ([], "Global", "News"))

    response = client.post(f"/api/briefing?{query}",
                           headers={"X-GDELT-Briefing-Token": "test-worker-token"})
    assert response.status_code == 200
    hours = 24 if "24" in query else 3
    state = get_briefing_attempt("", hours)
    assert state is not None
    assert state["last_attempt_outcome"] == expected


def test_a_window_we_cannot_read_is_not_reported_as_a_quiet_window(users_db, client, monkeypatch):
    """A locked/failing database must not be labelled "insufficient articles".

    Observed live on 2026-11-23: a forced global 3h refresh returned
    failed:insufficient_articles in 3.9 s while the same query in a fresh process
    returned 40 events. _fetch_briefing_events used to swallow the failure.
    """
    monkeypatch.setenv("GDELT_BRIEFING_WORKER_TOKEN", "test-worker-token")
    _seed_cache(users_db, "_all:3", _utc(timedelta(hours=-5)))
    _seed_attempt(users_db, "_all", 3, _utc(timedelta(hours=-4)), "ok", 0)

    def locked(*a, **k):
        raise BriefingGenerationError("db_busy", "no database connection available")

    monkeypatch.setattr(route, "get_threads", lambda key: [])
    monkeypatch.setattr(route, "_fetch_briefing_events", locked)

    response = client.post("/api/briefing?hours=3&prewarm=1",
                           headers={"X-GDELT-Briefing-Token": "test-worker-token"})
    assert response.status_code == 200 and response.json["stale"] is True
    assert response.json["last_attempt_outcome"] == "failed:db_busy"
    assert response.json["consecutive_failures"] == 1


def test_fetch_briefing_events_names_a_missing_connection(monkeypatch):
    from briefing import _fetch_briefing_events
    import briefing as briefing_mod
    monkeypatch.setattr(briefing_mod, "get_db", lambda: None)
    with pytest.raises(BriefingGenerationError) as exc:
        _fetch_briefing_events("", 3)
    assert exc.value.code == "db_busy"


def test_worker_records_a_generation_failure_code(users_db, client, monkeypatch):
    monkeypatch.setenv("GDELT_BRIEFING_WORKER_TOKEN", "test-worker-token")
    _seed_cache(users_db, "_all:3", _utc(timedelta(hours=-5)))

    def boom(*a, **k):
        raise BriefingGenerationError("provider_unavailable", "503 from gateway")

    monkeypatch.setattr(route, "get_threads", lambda key: [])
    monkeypatch.setattr(route, "_fetch_briefing_events",
                        lambda *a: ([{"n": 1}, {"n": 2}], "Global", "News"))
    monkeypatch.setattr(route, "_editor_pass", lambda sources, *a: (sources, [], {}))
    monkeypatch.setattr(route, "_build_briefing_prompt", lambda *a, **k: "synthetic prompt")
    monkeypatch.setattr(route, "_generate_briefing", boom)
    monkeypatch.setattr(route, "_normalize_briefing", lambda text, *a: text)

    response = client.post("/api/briefing?hours=3&prewarm=1",
                           headers={"X-GDELT-Briefing-Token": "test-worker-token"})
    # The reader still gets the stale briefing, and the payload says why.
    assert response.status_code == 200
    assert response.json["stale"] is True
    assert response.json["last_attempt_outcome"] == "failed:provider_unavailable"
    assert response.json["consecutive_failures"] == 1
    state = get_briefing_attempt("", 3)
    assert state["last_attempt_outcome"] == "failed:provider_unavailable"
    assert "503 from gateway" in (state["last_attempt_detail"] or "")


def test_worker_records_success_and_resets_failures(users_db, client, monkeypatch):
    monkeypatch.setenv("GDELT_BRIEFING_WORKER_TOKEN", "test-worker-token")
    _seed_cache(users_db, "_all:3", _utc(timedelta(hours=-5)))
    record_briefing_attempt("", 3, "failed:timeout")
    record_briefing_attempt("", 3, "failed:timeout")

    monkeypatch.setattr(route, "get_threads", lambda key: [])
    monkeypatch.setattr(route, "_fetch_briefing_events",
                        lambda *a: ([{"n": 1}, {"n": 2}], "Global", "News"))
    monkeypatch.setattr(route, "_editor_pass", lambda sources, *a: (sources, [], {}))
    monkeypatch.setattr(route, "_build_briefing_prompt", lambda *a, **k: "synthetic prompt")
    monkeypatch.setattr(route, "_generate_briefing", lambda *a, **k: "Synthetic update [1].")
    monkeypatch.setattr(route, "_normalize_briefing", lambda text, *a: text)
    monkeypatch.setattr(route, "record_briefing_history", lambda *a, **k: None)
    monkeypatch.setattr(route, "should_update_threads", lambda *a: False)

    response = client.post("/api/briefing?hours=3&prewarm=1",
                           headers={"X-GDELT-Briefing-Token": "test-worker-token"})
    assert response.status_code == 200 and response.json["cached"] is False
    assert response.json["consecutive_failures"] == 0
    assert response.json["last_attempt_outcome"] == "ok"
    assert get_briefing_attempt("", 3)["consecutive_failures"] == 0


# --- error codes and the schedule ------------------------------------------------

def test_generation_errors_map_to_the_codes_the_label_shows():
    from urllib.error import HTTPError, URLError
    import socket

    assert classify_generation_error(HTTPError("u", 503, "busy", {}, None)) == "provider_unavailable"
    assert classify_generation_error(HTTPError("u", 429, "slow down", {}, None)) == "provider_unavailable"
    assert classify_generation_error(HTTPError("u", 401, "no", {}, None)) == "http_401"
    assert classify_generation_error(socket.timeout("read timed out")) == "timeout"
    assert classify_generation_error(URLError(socket.timeout("read timed out"))) == "timeout"
    assert classify_generation_error(URLError("name resolution")) == "provider_unreachable"
    assert classify_generation_error(RuntimeError("no gateway key")) == "no_gateway_key"
    assert classify_generation_error(ValueError("something else")) == "generation_error"


def test_the_cadence_is_three_hours():
    assert CADENCE_HOURS == 3
    # 3h and 24h warm on every run; the long windows ride the day's edges.
    assert scheduled_hours(datetime(2026, 11, 23, 3)) == [3, 24]
    assert scheduled_hours(datetime(2026, 11, 23, 12)) == [3, 24, 6, 72]
    assert scheduled_hours(datetime(2026, 11, 23, 0)) == [3, 24, 6, 72, 168, 720]


def test_next_run_at_is_governed_by_the_schedule():
    after = datetime.utcnow()
    # The short windows are warmed on every run, so the next run is at most one
    # cadence away; longer windows wait for their slot.
    for hours, limit_h in ((3, 4), (24, 4), (6, 13), (72, 13), (168, 25)):
        got = next_run_at(hours, after)
        assert got is not None, hours
        assert got > after, hours
        assert (got - after) <= timedelta(hours=limit_h), (hours, got - after)
    assert next_run_at(3, after) == next_run_at(24, after)
    assert next_run_at(6, after) == next_run_at(72, after)
