"""Refresh the fixed curated briefing cache through authenticated local POSTs.

Public requests only read cached briefings. This worker loads its authorization
from GDELT_BRIEFING_WORKER_TOKEN or data/.briefing_worker_token (host-local).
It generates Global plus the 16 curated views at 3h/24h, respecting cache TTLs.
Other windows and custom views remain readable only when a cache already exists.
Failed or stale responses produce a nonzero exit and do not advance the version.

Use --plan for a read-only cache inventory without HTTP or model calls.
"""

import urllib.request, time, sys, argparse
import os
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


# 240s, not 120: a generation now runs two model calls with a 180s budget each and has
# measured ~180s end to end. At 120s prewarm aborted while the server kept working, and
# because regeneration holds ONE global slot, every following combo then failed instantly
# with failed:busy. The task limit is PT72H, so waiting costs only wall time.
def warm(view, hours, base_url, timeout=240):
    # prewarm=1 (not refresh=1): regenerate only if the cache is stale for THIS
    # window, so a 30-day briefing warms once a day instead of on every run.
    params = f"hours={hours}&prewarm=1"
    if view:
        params += f"&view={view}"
    url = f"{base_url}/api/briefing?{params}"
    label = f"{view or 'global'}:{hours}h"
    t0 = time.time()
    try:
        token = os.environ.get("GDELT_BRIEFING_WORKER_TOKEN", "").strip()
        if not token:
            token = (DATA_DIR / ".briefing_worker_token").read_text(encoding="utf-8").strip()
        if not token:
            raise ValueError("Scheduled generation token is not configured")
        # Keep this token on the host; never put it in a URL or diagnostic output.
        req = urllib.request.Request(url, data=b"", method="POST",
                                     headers={"X-GDELT-Briefing-Token": token})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
        elapsed = time.time() - t0
        # cached=True with no regeneration means fresh_s() said "still good" —
        # no LLM call was made. Worth seeing in the log; it is the whole saving.
        try:
            import json as _json
            payload = _json.loads(body)
            if payload.get("error") or payload.get("stale") or not payload.get("briefing"):
                raise ValueError("Briefing was not refreshed")
            skipped = bool(payload.get("cached"))
        except Exception:
            raise ValueError("Invalid scheduled generation response") from None
        tag = "SKIP" if skipped else " OK "
        print(f"[{time.strftime('%H:%M:%S')}] {tag} {label:30s} {elapsed:.1f}s", flush=True)
        return skipped
    except Exception:
        print(f"[{time.strftime('%H:%M:%S')}] FAIL {label:30s} (scheduled request failed)", flush=True)



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
    total = len(combos)
    print(f"[{time.strftime('%H:%M:%S')}] pre-warming {total} curated 3h/24h combos on port {args.port}", flush=True)

    t_start = time.time()
    ok_count = 0
    fail_count = 0
    skipped_count = 0
    for view, hours in combos:
        result = warm(view, hours, base_url)
        if result is None:
            fail_count += 1
        elif result:
            skipped_count += 1
            ok_count += 1
        else:
            ok_count += 1
        if STAGGER_S:
            time.sleep(STAGGER_S)
    print(f"[{time.strftime('%H:%M:%S')}] still-fresh, no LLM call: "
          f"{skipped_count}/{total}", flush=True)

    elapsed = time.time() - t_start
    print(f"[{time.strftime('%H:%M:%S')}] done: {ok_count} successful, {fail_count} failed "
          f"of {total} combos in {elapsed:.1f}s", flush=True)

    if ver and not fail_count:
        try:
            LAST_PREWARM_FILE.write_text(ver)
        except OSError:
            pass
    return 1 if fail_count else 0


if __name__ == "__main__":
    sys.exit(main())
