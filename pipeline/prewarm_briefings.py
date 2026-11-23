"""Refresh the fixed curated briefing cache through authenticated local POSTs.

Public requests only read cached briefings. This worker loads its authorization
from GDELT_BRIEFING_WORKER_TOKEN or data/.briefing_worker_token (host-local).
It generates Global plus the curated views at 3h/24h (plus the wider windows on
the 00/12 runs), respecting cache TTLs. Other windows and custom views remain
readable only when a cache already exists. Failed or stale responses produce a
nonzero exit and do not advance the version.

Combos are warmed on a small bounded thread pool - one worker per generation
slot the dashboard will actually grant (see regen_cap). A generation takes
~4.5 minutes, so a serial loop cannot fit inside the 3h cadence, and one slow
combo used to delay every combo behind it; with a pool it delays only its own
worker. Each combo's outcome comes from the attempt state the worker response
already carries (ok / skipped_fresh / failed:<code>), so a busy slot is reported
as a skip-and-retry rather than as a failure.

Use --plan for a read-only cache inventory without HTTP or model calls.
"""

import argparse
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

CURATED_HOURS = (3, 24)
SLOW_HOURS = (6, 72)      # widened 2026-11-09: refreshed twice a day
DAILY_HOURS = (168, 720)  # refreshed once a day
# The GDELT-BriefingPrewarm trigger repeats every this many hours (8 runs/day
# since 2026-11-23; it was five fixed daily triggers with an 8h overnight gap,
# which no 3h TTL could survive). The trigger is anchored to a LOCAL clock time,
# so every hourly rule in this module is local too — a DST change moves the UTC
# hour of a run and must not silently stop a window from ever being warmed.
CADENCE_HOURS = 3
CURATED_VIEWS = (
    "",                        # global / all topics
    "ai-general",               # AI Sector
    "ai-regulation",            # AI Governance & Regulation
    "ai-defense",               # AI & Defense
    "ai-sector-impact",         # AI in Industry
    "semiconductors",           # Semiconductors
    "oss-vulnerabilities",      # Open Source Vulnerabilities
    "cyber-attacks",            # Cybersecurity
    "public-health",            # Public Health
    "medical-devices",          # Medical Devices
    "fda-agency",               # FDA
    "nih-news",                 # NIH
    "cms-news",                 # CMS
    "va-news",                  # VA
    "supply-chain-alerts",      # Supply Chain Alerts
    "geopolitics-conflict",     # Geopolitics & Conflict
    "energy-climate",           # Energy & Climate
    "europe",                   # Regions: Europe
    "asia",                     # Regions: Asia
    "us",                       # Regions: United States
)

DATA_DIR = Path(os.environ.get("GDELT_DATA_DIR") or (Path(__file__).resolve().parent.parent / "data"))
VERSION_FILE = DATA_DIR / "data_version.txt"
LAST_PREWARM_FILE = DATA_DIR / ".last_prewarm_version"
STAGGER_S = 0.5

# 420s: measured generations on 2026-11-23 took 238-276s end to end (two model calls, 180s
# budget each). Waiting less than the server needs records failed:timeout for a briefing
# that actually updated, which the freshness label then shows as a failure. The task limit
# is PT72H, so waiting costs only wall time; IgnoreNew prevents overlapping runs.
CLIENT_TIMEOUT_S = 420

# Fallback when the server's REGEN_MAX_CONCURRENT cannot be read. The pool is
# never larger than that cap: a worker beyond it can only be told failed:busy.
DEFAULT_REGEN_CAP = 2

# How long a combo deferred by a full server waits before its single retry. The
# generation it collided with is still running (or was abandoned by an earlier
# run and still holds a slot), so retrying immediately would collide again.
BUSY_RETRY_WAIT_S = 60


def current_version():
    try:
        return VERSION_FILE.read_text().strip()
    except OSError:
        return ""


def last_prewarmed_version():
    try:
        return LAST_PREWARM_FILE.read_text().strip()
    except OSError:
        return ""


def regen_cap(default=DEFAULT_REGEN_CAP):
    """How many generations the dashboard will actually run at once.

    Read from the route module's source literal rather than imported: importing
    ``dashboard.routes.api_briefing`` here would pull in Flask and the article DB
    for one integer, from a script that runs as its own process. Precedence is
    the operator override (tests, A/B measurement), the server's own constant,
    then the documented default.
    """
    override = os.environ.get("GDELT_PREWARM_WORKERS", "").strip()
    if override:
        try:
            return max(1, int(override))
        except ValueError:
            print(f"[{time.strftime('%H:%M:%S')}] ignoring GDELT_PREWARM_WORKERS={override!r}",
                  flush=True)
    src = Path(__file__).resolve().parent.parent / "dashboard" / "routes" / "api_briefing.py"
    try:
        m = re.search(r"^REGEN_MAX_CONCURRENT\s*=\s*(\d+)", src.read_text(encoding="utf-8"), re.M)
        if m:
            return max(1, int(m.group(1)))
    except OSError:
        pass
    return default


@dataclass(frozen=True)
class Outcome:
    """One combo's result. ``status`` is ok | skip | busy | fail."""
    key: str
    status: str
    reason: str = ""
    elapsed: float = 0.0
    detail: str = ""


@dataclass
class RunReport:
    """What a run did, one entry per combo (the last attempt for each key)."""
    outcomes: dict = field(default_factory=dict)
    workers: int = 1
    elapsed: float = 0.0
    max_in_flight: int = 0
    retried_busy: int = 0
    attempts: int = 0

    def count(self, status, reason=None):
        return sum(1 for o in self.outcomes.values()
                   if o.status == status and (reason is None or o.reason == reason))


def classify(key, payload, elapsed):
    """Turn one worker response into an Outcome.

    A current server records the attempt (since #71) and echoes it as
    ``last_attempt_outcome``, so the reply says what happened instead of
    prewarm inferring it from ``stale``: a busy slot, a window with too few
    articles and a provider failure all used to look like the same generic
    "scheduled request failed". Older servers carry no such field, so the body
    shape is still a fallback.
    """
    if not isinstance(payload, dict):
        return Outcome(key, "fail", "invalid_response", elapsed)
    attempt = str(payload.get("last_attempt_outcome") or "")
    if attempt == "failed:busy":
        return Outcome(key, "busy", "busy", elapsed,
                       str(payload.get("last_attempt_detail") or "")[:120])
    if attempt.startswith("failed:"):
        return Outcome(key, "fail", attempt.split(":", 1)[1] or "unknown", elapsed,
                       str(payload.get("error") or "")[:120])
    if attempt == "ok" and payload.get("briefing"):
        return Outcome(key, "ok", "generated", elapsed)
    if attempt == "skipped_fresh":
        return Outcome(key, "skip", "fresh", elapsed)
    # Fallback for a server that records nothing (an older process still serving).
    if payload.get("briefing") and not payload.get("error") and not payload.get("stale"):
        cached = bool(payload.get("cached"))
        return Outcome(key, "skip" if cached else "ok",
                       "fresh" if cached else "generated", elapsed)
    return Outcome(key, "fail", "not_refreshed", elapsed,
                   str(payload.get("error") or "")[:120])


def _worker_token():
    token = os.environ.get("GDELT_BRIEFING_WORKER_TOKEN", "").strip()
    if not token:
        token = (DATA_DIR / ".briefing_worker_token").read_text(encoding="utf-8").strip()
    return token


def warm(view, hours, base_url, timeout=CLIENT_TIMEOUT_S, force=False):
    """Warm one combo and return its Outcome. Never raises.

    ``force=True`` sends refresh=1 (regenerate even if fresh); it exists for
    measurement and debugging, and the scheduled task never passes it, because
    the schedule relies on prewarm=1 letting the server's fresh_s() decide.
    """
    mode = "refresh" if force else "prewarm"
    params = f"hours={hours}&{mode}=1"
    if view:
        params += f"&view={view}"
    url = f"{base_url}/api/briefing?{params}"
    key = f"{view or 'global'}:{hours}h"
    try:
        token = _worker_token()
    except OSError as exc:
        return Outcome(key, "fail", "no_token", 0.0, str(exc)[:120])
    if not token:
        return Outcome(key, "fail", "no_token", 0.0,
                       "scheduled generation token is not configured")

    # Keep this token on the host; never put it in a URL or diagnostic output.
    req = urllib.request.Request(url, data=b"", method="POST",
                                 headers={"X-GDELT-Briefing-Token": token})
    t0 = time.time()
    try:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
        except urllib.error.HTTPError as exc:
            # A 503 is how the route answers when it has no cached briefing to
            # fall back on; the body still names the attempt outcome, so a busy
            # slot or a thin window stays a specific outcome instead of becoming
            # a generic request failure.
            body = exc.read() or b"{}"
    except (socket.timeout, TimeoutError):
        return Outcome(key, "fail", "timeout", time.time() - t0, f"{timeout:g}s")
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in str(reason).lower():
            return Outcome(key, "fail", "timeout", time.time() - t0, f"{timeout:g}s")
        return Outcome(key, "fail", "network", time.time() - t0, str(reason)[:120])
    except Exception as exc:  # noqa: BLE001 - a run must not die on one combo
        return Outcome(key, "fail", "request_failed", time.time() - t0,
                       f"{type(exc).__name__}: {exc}"[:120])
    elapsed = time.time() - t0
    try:
        payload = json.loads(body)
    except Exception:
        return Outcome(key, "fail", "invalid_response", elapsed)
    return classify(key, payload, elapsed)


def format_outcome(out):
    """One log line per attempt; the OK/SKIP/FAIL tags are parsed downstream."""
    tag = {"ok": " OK ", "skip": "SKIP", "busy": "BUSY", "fail": "FAIL"}.get(out.status, "????")
    line = f"[{time.strftime('%H:%M:%S')}] {tag} {out.key:30s} {out.elapsed:5.1f}s"
    if out.status == "fail":
        if out.reason == "timeout":
            line += ("  (timeout after " + (out.detail or "the client timeout")
                     + "; the server may still finish this one)")
        else:
            line += f"  (failed:{out.reason})"
    elif out.status == "busy":
        line += "  (server at its generation cap; not refreshed)"
    return line


def run_combos(combos, base_url, *, workers=None, timeout=CLIENT_TIMEOUT_S, force=False,
               stagger=STAGGER_S, busy_retries=1, busy_retry_wait=BUSY_RETRY_WAIT_S,
               log=print):
    """Warm every combo on a bounded pool and return a RunReport.

    The pool is the size of the server's slot cap because that is all the server
    will grant: extra workers would only collect failed:busy. A combo told busy
    is deferred and retried once after the queue drains - it is a skip, not a
    failure, since the generation it collided with is still running and the next
    scheduled run would pick it up anyway.
    """
    cap = regen_cap()
    workers = max(1, min(int(workers), cap)) if workers else cap
    report = RunReport(workers=workers)
    t_start = time.time()
    state = {"in_flight": 0, "max": 0}
    guard = threading.Lock()

    def _task(view, hours):
        if stagger:
            time.sleep(stagger)
        with guard:
            state["in_flight"] += 1
            state["max"] = max(state["max"], state["in_flight"])
        try:
            return warm(view, hours, base_url, timeout=timeout, force=force)
        finally:
            with guard:
                state["in_flight"] -= 1

    pending = list(combos)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="prewarm") as pool:
        tries = 0
        while pending:
            futures = {pool.submit(_task, view, hours): (view, hours) for view, hours in pending}
            deferred = []
            for fut in as_completed(futures):
                view, hours = futures[fut]
                try:
                    out = fut.result()
                except Exception as exc:  # noqa: BLE001 - one worker must not end the run
                    out = Outcome(f"{view or 'global'}:{hours}h", "fail", "worker_error",
                                  time.time() - t_start,
                                  f"{type(exc).__name__}: {exc}"[:120])
                log(format_outcome(out))
                report.attempts += 1
                report.outcomes[out.key] = out
                if out.status == "busy":
                    deferred.append((view, hours))
            if not deferred or tries >= busy_retries:
                break
            tries += 1
            report.retried_busy += len(deferred)
            pending = deferred
            if busy_retry_wait:
                time.sleep(busy_retry_wait)
    report.elapsed = time.time() - t_start
    report.max_in_flight = state["max"]
    return report


def scheduled_hours(now=None):
    """Windows this run should warm. 3h/24h every run; the other four windows
    used to never refresh at all — 69/103 cached briefings were >4 weeks stale
    on 2026-11-09. 6h/3d join the runs at 00 and 12 local, 7d/30d the 00 local
    run. The server's own fresh_s() gate still decides whether a combo actually
    regenerates, so these only cost LLM calls when due.

    Local, not UTC: the trigger is a local-clock repetition (see CADENCE_HOURS),
    and the previous UTC test made the widened windows depend on the DST offset
    — at UTC-4 no run lands on hour 0 or 12, so they would stop refreshing.
    """
    from datetime import datetime
    h = (now or datetime.now()).hour
    hours = list(CURATED_HOURS)
    if h % 12 == 0:
        hours += SLOW_HOURS
    if h == 0:
        hours += DAILY_HOURS
    return hours


def next_run_at(hours, after=None):
    """The next scheduled run that may warm this window, as naive UTC.

    This is what the reader's freshness line counts down to, so it has to be
    the schedule, not the TTL: the TTL only says when a cache stops counting as
    fresh, and a run that lands before that moment will skip the combo. Before
    2026-11-23 the line was derived from the TTL alone, so a 3h briefing showed
    "next update in any moment" through an 8-hour hole in the schedule.

    ``after`` is naive UTC, the convention used by briefing_cache.generated_at.
    Returns None if no run in the next three weeks warms this window.
    """
    from datetime import datetime, timedelta
    # Exact local<->UTC offset for right now (whole minutes; DST-aware). Reading
    # utcnow() and now() as two clock samples instead rounds to 11:59:59.999999
    # at a minute boundary, which the bar then prints as "next ~11:59" for a
    # 12:00 run.
    tz_offset = datetime.now().astimezone().utcoffset() or timedelta()
    after_local = (after or datetime.utcnow()) + tz_offset
    slot = after_local.replace(minute=0, second=0, microsecond=0)
    slot -= timedelta(hours=slot.hour % CADENCE_HOURS)
    if slot < after_local:
        slot += timedelta(hours=CADENCE_HOURS)
    for _ in range(8 * 21):
        if int(hours) in scheduled_hours(slot):
            return slot - tz_offset
        slot += timedelta(hours=CADENCE_HOURS)
    return None


def curated_combos(now=None):
    """Stable cache keys for this run's window schedule; never infer custom
    pills from traffic."""
    return [(view, hours) for view in CURATED_VIEWS for hours in scheduled_hours(now)]


def cache_plan(combos=None, now=None):
    """Read-only cache status for --plan and unit tests."""
    import sqlite3
    from datetime import datetime, timezone
    combos = combos or curated_combos()
    now = now or datetime.now(timezone.utc)
    rows = {}
    try:
        con = sqlite3.connect(f"file:{DATA_DIR / 'users.db'}?mode=ro", uri=True)
        rows = {r[0]: r[1] for r in con.execute("SELECT cache_key, generated_at FROM briefing_cache")}
        con.close()
    except Exception:
        pass
    plan = []
    for view, hours in combos:
        key = f"{view or '_all'}:{hours}"
        generated_at = rows.get(key)
        fresh = False
        if generated_at:
            try:
                age = (now.replace(tzinfo=None) - datetime.strptime(generated_at, "%Y-%m-%d %H:%M:%S")).total_seconds()
                fresh = age < (3 if hours == 3 else 8) * 3600
            except ValueError:
                pass
        plan.append({"key": key, "view": view, "hours": hours, "fresh": fresh,
                     "action": "skip" if fresh else "generate"})
    return plan


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8015, help="Dashboard port (default: 8015)")
    parser.add_argument("--force", action="store_true", help="Skip version check, always regenerate")
    parser.add_argument("--workers", type=int, default=None,
                        help="Pool size; clamped to the server's REGEN_MAX_CONCURRENT")
    parser.add_argument("--plan", action="store_true", help="Report cache coverage; make no HTTP calls")
    args = parser.parse_args()

    base_url = f"http://localhost:{args.port}"

    ver = current_version()
    if not ver:
        print(f"[{time.strftime('%H:%M:%S')}] no data version — assuming first run", flush=True)
    combos = curated_combos()
    plan = cache_plan(combos)
    if args.plan:
        for row in plan:
            print(f"{row['action'].upper():8s} {row['key']}")
        print(f"{len(plan)} curated keys: {sum(r['fresh'] for r in plan)} fresh, "
              f"{sum(not r['fresh'] for r in plan)} due")
        return
    cap = regen_cap()
    workers = cap
    if args.workers:
        workers = min(max(1, args.workers), cap)
        if args.workers > cap:
            print(f"[{time.strftime('%H:%M:%S')}] --workers {args.workers} clamped to the server "
                  f"cap {cap} (a larger pool only collects failed:busy)", flush=True)
    total = len(combos)
    print(f"[{time.strftime('%H:%M:%S')}] pre-warming {total} curated combos on port {args.port} "
          f"with {workers} worker{'s' if workers != 1 else ''}"
          + (" (forced regeneration)" if args.force else ""), flush=True)

    report = run_combos(combos, base_url, workers=workers, force=args.force)
    skipped = report.count("skip")
    print(f"[{time.strftime('%H:%M:%S')}] still-fresh, no LLM call: "
          f"{skipped}/{total}", flush=True)
    if report.retried_busy:
        print(f"[{time.strftime('%H:%M:%S')}] {report.retried_busy} combo(s) found the server "
              f"at its generation cap and were retried once", flush=True)

    ok_count = report.count("ok")
    busy_count = report.count("busy")
    fail_count = report.count("fail")
    timeout_count = report.count("fail", "timeout")
    print(f"[{time.strftime('%H:%M:%S')}] done: {ok_count} successful, {skipped} still fresh, "
          f"{busy_count} busy-skipped, {fail_count} failed ({timeout_count} timeout) "
          f"of {total} combos in {report.elapsed:.1f}s "
          f"({workers} workers, max in flight {report.max_in_flight})", flush=True)

    if ver and not fail_count:
        try:
            LAST_PREWARM_FILE.write_text(ver)
        except OSError:
            pass
    return 1 if fail_count else 0


if __name__ == "__main__":
    sys.exit(main())
