"""Prewarm must not be serial, and one slow combo must not fail the rest.

Issue #72. A generation takes 238-276 s end to end, so a loop that waits for
each combo cannot fit the 3 h cadence (~27 due combos x 4.5 min is ~2 h), and a
combo that outlives the client timeout used to strand the queue behind it: on
2026-11-23 the 07:00 run logged 3 successful, 77 failed of 80, nearly all of
them `failed:busy` within seconds. prewarm now warms combos on a pool sized to
the server's own REGEN_MAX_CONCURRENT, and classifies each combo from the
attempt outcome its response already carries.

These tests run a real HTTP stub that behaves like the route (including a
generation that outlives the client timeout) and pin the four things that were
broken:

  * a slow combo must not delay or fail the others;
  * the pool must never exceed the server's cap (an extra worker only collects
    failed:busy);
  * a timeout must be reported as its own combo's timeout, not as a cascade;
  * a busy slot is a skip-and-retry, and every combo still ends with a recorded
    outcome;

plus the log contract gdelt-watch and /admin/metrics parse.

Run with the repo venv:  .venv\\Scripts\\python.exe -m pytest tests\\test_prewarm_concurrency.py -v
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dashboard"))

from pipeline import prewarm_briefings as pw  # noqa: E402

SLOW_KEY = "slow-view:3h"


class StubDashboard:
    """A dashboard that answers /api/briefing the way the route does.

    ``slow`` keys sleep ``slow_s`` (a generation, at test scale); ``busy_once``
    keys answer failed:busy on their first request only; ``fail`` keys answer a
    stale cache with a provider failure. Attempt outcomes are recorded per key,
    like briefing_attempt_state.
    """

    def __init__(self, slow=(), slow_s=0.5, busy_once=(), busy=(), fail=(), fresh=()):
        self.slow = set(slow)
        self.slow_s = slow_s
        self.busy_once = set(busy_once)
        self.busy = set(busy)
        self.fail = set(fail)
        self.fresh = set(fresh)
        self.lock = threading.Lock()
        self.attempts = {}          # key -> last recorded outcome
        self.seen = []              # (key, started_at, ended_at, status)
        self.in_flight = 0
        self.max_in_flight = 0
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):   # keep pytest output clean
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                with stub.lock:
                    stub.in_flight += 1
                    stub.max_in_flight = max(stub.max_in_flight, stub.in_flight)
                started = time.time()
                try:
                    body, status = stub.answer(self.path, started)
                finally:
                    with stub.lock:
                        stub.in_flight -= 1
                raw = json.dumps(body).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass   # the client timed out and went away; that is the test

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                pass   # a client that timed out mid-response is expected here

        self.server = Server(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def answer(self, path, started):
        q = parse_qs(urlparse(path).query)
        view = (q.get("view") or [""])[0]
        hours = int((q.get("hours") or ["3"])[0])
        key = f"{view or 'global'}:{hours}h"
        if key in self.slow:
            time.sleep(self.slow_s)     # a generation that outlives the caller
        with self.lock:
            busy = key in self.busy
            if key in self.busy_once:
                self.busy_once.discard(key)
                busy = True
        if busy:
            outcome, status = "failed:busy", 503
            body = {"briefing": None, "error": "Generation in progress, no cache yet",
                    "last_attempt_outcome": outcome}
        elif key in self.fail:
            outcome, status = "failed:provider_unavailable", 200
            body = {"briefing": "yesterday", "cached": True, "stale": True,
                    "last_attempt_outcome": outcome}
        elif key in self.fresh:
            outcome, status = "skipped_fresh", 200
            body = {"briefing": "cached", "cached": True, "stale": False,
                    "last_attempt_outcome": outcome}
        else:
            outcome, status = "ok", 200
            body = {"briefing": "fresh", "cached": False, "stale": False,
                    "last_attempt_outcome": outcome}
        with self.lock:
            self.attempts[key] = outcome
            self.seen.append((key, started, time.time(), outcome))
        return body, status


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    """warm() refuses to run without the host-local worker token."""
    monkeypatch.setenv("GDELT_BRIEFING_WORKER_TOKEN", "test-token")
    monkeypatch.delenv("GDELT_PREWARM_WORKERS", raising=False)


def _run(stub, combos, **kwargs):
    kwargs.setdefault("workers", 2)
    kwargs.setdefault("timeout", 5)
    kwargs.setdefault("stagger", 0)
    kwargs.setdefault("busy_retry_wait", 0)
    lines = []
    report = pw.run_combos(combos, stub.url, log=lines.append, **kwargs)
    return report, lines


def test_one_slow_combo_does_not_delay_or_fail_the_others():
    """The whole point: a 4.5-minute combo must not be a queue."""
    combos = [("slow-view", 3), ("ai-general", 3), ("cyber-attacks", 3),
              ("europe", 3), ("asia", 3), ("us", 3)]
    with StubDashboard(slow=[SLOW_KEY], slow_s=0.6) as stub:
        report, lines = _run(stub, combos)

    assert report.outcomes[SLOW_KEY].status == "ok"
    assert report.outcomes[SLOW_KEY].elapsed >= 0.6
    for view, hours in combos:
        key = f"{view or 'global'}:{hours}h"
        assert report.outcomes[key].status == "ok", key

    # Prove they overlapped rather than merely being fast: a fast request
    # started while the slow one was still running. A serial loop cannot do this.
    slow = next(s for s in stub.seen if s[0] == SLOW_KEY)
    fast_starts = [s[1] for s in stub.seen if s[0] != SLOW_KEY]
    assert fast_starts and min(fast_starts) < slow[2]

    # ... and the run took about one slow generation, not two of them.
    assert report.elapsed < 0.6 * len(combos) / 2
    assert all("FAIL" not in line for line in lines)


def test_pool_never_exceeds_the_server_cap():
    """An extra worker beyond REGEN_MAX_CONCURRENT only collects failed:busy."""
    combos = [(f"v{i}", 3) for i in range(8)]
    with StubDashboard(slow=[f"v{i}:3h" for i in range(8)], slow_s=0.15) as stub:
        report, _ = _run(stub, combos, workers=None)   # default = the server cap

    assert pw.regen_cap() == 2, "the documented cap moved; update this test and the pool"
    assert report.workers == 2
    assert stub.max_in_flight <= 2, f"stub saw {stub.max_in_flight} concurrent requests"
    assert report.max_in_flight <= 2
    assert stub.max_in_flight == 2, "the pool never ran two generations at once"


def test_the_pool_size_is_read_from_the_server_not_hardcoded():
    """regen_cap() must track dashboard.routes.api_briefing.REGEN_MAX_CONCURRENT."""
    from routes import api_briefing as route

    assert pw.regen_cap() == route.REGEN_MAX_CONCURRENT


def test_a_timeout_is_one_combo_failure_not_a_cascade():
    """The production failure mode: the caller gives up, the server does not."""
    combos = [(k, 3) for k in ("slow-view", "ai-general", "europe", "asia", "us")]
    with StubDashboard(slow=[SLOW_KEY], slow_s=1.0) as stub:
        report, lines = _run(stub, combos, timeout=0.25)

    timed_out = report.outcomes[SLOW_KEY]
    assert timed_out.status == "fail" and timed_out.reason == "timeout"
    assert "timeout after 0.25" in next(l for l in lines if SLOW_KEY in l)
    for view, hours in combos:
        key = f"{view or 'global'}:{hours}h"
        if key != SLOW_KEY:
            assert report.outcomes[key].status == "ok", key
    assert report.count("fail", "timeout") == 1
    assert report.count("fail") == 1
    # gdelt-watch counts the literal "FAILED"; nothing here may emit it.
    assert not any("FAILED" in line for line in lines)


def test_busy_is_deferred_and_retried_not_reported_as_a_failure():
    combos = [("europe", 3), ("asia", 3), ("us", 3)]
    with StubDashboard(busy_once=["europe:3h"]) as stub:
        report, lines = _run(stub, combos)

    assert report.retried_busy == 1
    assert report.outcomes["europe:3h"].status == "ok"      # retry won
    assert report.count("fail") == 0
    assert sum("BUSY" in line for line in lines) == 1
    assert sum("FAIL" in line for line in lines) == 0
    assert all(key in stub.attempts for key in ("europe:3h", "asia:3h", "us:3h"))


def test_a_permanently_busy_combo_is_a_skip_and_still_recorded():
    combos = [("europe", 3), ("asia", 3)]
    with StubDashboard(busy=["europe:3h"]) as stub:
        report, lines = _run(stub, combos, busy_retries=0)

    assert report.outcomes["europe:3h"].status == "busy"
    assert report.count("fail") == 0        # not a failure: the next run picks it up
    assert report.outcomes["europe:3h"].reason == "busy"
    busy_line = next(l for l in lines if "BUSY" in l)
    assert "generation cap" in busy_line and "FAIL" not in busy_line


def test_every_combo_ends_with_an_outcome_recorded():
    combos = [("slow-view", 3), ("ai-general", 3), ("cyber-attacks", 3),
              ("ai-regulation", 24), ("us", 24), ("europe", 24)]
    with StubDashboard(slow=[SLOW_KEY], slow_s=0.3, fail=["cyber-attacks:3h"]) as stub:
        report, lines = _run(stub, combos)

    assert len(report.outcomes) == len(combos)
    assert set(report.outcomes) == {f"{v}:{h}h" for v, h in combos}
    # Server-side record (briefing_attempt_state in production) has one per combo,
    # including the failed one: a failure is still recorded.
    assert set(stub.attempts) == set(report.outcomes)
    assert report.outcomes["cyber-attacks:3h"].status == "fail"
    assert report.outcomes["cyber-attacks:3h"].reason == "provider_unavailable"
    assert report.attempts == len(combos)


def test_the_log_keeps_the_tags_gdelt_watch_and_metrics_parse():
    """OK/SKIP/FAIL lines, and the two summary phrases, are a published contract."""
    combos = [("ai-general", 3), ("europe", 3)]
    with StubDashboard() as stub:
        report, lines = _run(stub, combos)

    assert len(lines) == 2 and all(" OK " in line for line in lines)
    # dashboard/metrics.py looks for "pre-warming" and " done: "; gdelt-watch
    # looks for "still-fresh, no LLM call: N/M" and counts "FAILED" lines.
    assert "SKIP" in pw.format_outcome(pw.Outcome("europe:3h", "skip", "fresh", 0.1))
    assert "FAIL" in pw.format_outcome(pw.Outcome("europe:3h", "fail", "timeout", 1.0, "420s"))
    assert not any("FAILED" in line for line in lines)
