"""Pre-warm the AI briefing cache for every view × time-range combo so the
dashboard always has pre-generated content to serve instantly (no "Generating…" skeletons).

Hits the local non-streaming endpoint with ?prewarm=1, which regenerates only
when the cache is stale for that window (see briefing.fresh_s). Data-version-guarded: only runs when gdelt_ingest.py has written
a new data_version.txt, skipping redundant cycles.

DEMAND-DRIVEN: the combo list is not the 17x6=102 cross-product. It is read
from briefing_history — the view/window pairs a human has actually opened in
the last PREWARM_LOOKBACK_DAYS. Measured over the first week of history, only
13 of the 102 combos were EVER opened and 4 accounted for 78% of reads, so
warming the cross-product spent ~26 generations for every one a person saw.
Combos nobody opens are simply generated on demand the rare time someone does,
which then makes them "recent" and they get warmed from the next run on. The
list self-tunes; there is nothing to maintain by hand.

Failures are logged and skipped; the "keep stale until fresh" design means
users still see the last cached briefing.

Usage:
    python prewarm_briefings.py            # defaults to port 8015 (prod)
    python prewarm_briefings.py --port 8016  # dev instance
    python prewarm_briefings.py --force      # skip version check
"""

import urllib.request, time, sys, argparse
from pathlib import Path

HOURS = [3, 6, 24, 72, 168, 720]
VIEWS = [
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
]

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
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


def warm(view, hours, base_url, timeout=120):
    # prewarm=1 (not refresh=1): regenerate only if the cache is stale for THIS
    # window, so a 30-day briefing warms once a day instead of on every run.
    params = f"hours={hours}&prewarm=1"
    if view:
        params += f"&view={view}"
    url = f"{base_url}/api/briefing?{params}"
    label = f"{view or 'global'}:{hours}h"
    t0 = time.time()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read()
        elapsed = time.time() - t0
        # cached=True with no regeneration means fresh_s() said "still good" —
        # no LLM call was made. Worth seeing in the log; it is the whole saving.
        try:
            import json as _json
            skipped = bool(_json.loads(body).get("cached"))
        except Exception:
            skipped = False
        tag = "SKIP" if skipped else " OK "
        print(f"[{time.strftime('%H:%M:%S')}] {tag} {label:30s} {elapsed:.1f}s", flush=True)
        return skipped
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] FAIL {label:30s} ({e})", flush=True)



PREWARM_LOOKBACK_DAYS = 30   # how far back a "someone opened this" signal counts
PREWARM_MAX_COMBOS = 30      # hard ceiling, so a burst of browsing can't explode cost
ALWAYS_WARM = [("", 3), ("", 24)]   # global feed: the front page, always instant


def demand_combos():
    """(view_id, hours) pairs a human actually opened recently, most-read first.

    Reads trigger='visit' rows only — prewarm's own writes must not feed back in
    and keep a combo alive forever.
    """
    import sqlite3
    db = DATA_DIR / "users.db"
    combos = []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT view_id, hours, count(*) c FROM briefing_history "
            "WHERE trigger = 'visit' "
            "  AND generated_at >= date('now', ?) "
            "GROUP BY view_id, hours ORDER BY c DESC",
            (f"-{PREWARM_LOOKBACK_DAYS} day",),
        ).fetchall()
        con.close()
        for view_id, hours, _c in rows:
            combos.append(("" if view_id in (None, "_all") else view_id, int(hours)))
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] demand query failed ({e}) — "
              f"falling back to ALWAYS_WARM only", flush=True)

    for c in ALWAYS_WARM:
        if c not in combos:
            combos.append(c)
    return combos[:PREWARM_MAX_COMBOS]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8015, help="Dashboard port (default: 8015)")
    parser.add_argument("--force", action="store_true", help="Skip version check, always regenerate")
    args = parser.parse_args()

    base_url = f"http://localhost:{args.port}"

    ver = current_version()
    if not ver:
        print(f"[{time.strftime('%H:%M:%S')}] no data version — assuming first run", flush=True)
    elif not args.force and ver == last_prewarmed_version():
        print(f"[{time.strftime('%H:%M:%S')}] data unchanged (version {ver}) — skipping", flush=True)
        return

    combos = demand_combos()
    total = len(combos)
    print(f"[{time.strftime('%H:%M:%S')}] pre-warming {total} demand-selected combos "
          f"(last {PREWARM_LOOKBACK_DAYS}d of reads) on port {args.port}", flush=True)

    t_start = time.time()
    ok_count = 0
    fail_count = 0
    skipped_count = 0
    for view, hours in combos:
        if warm(view, hours, base_url):
            skipped_count += 1
        ok_count += 1  # failure is logged but not fatal; count all attempts
        if STAGGER_S:
            time.sleep(STAGGER_S)
    print(f"[{time.strftime('%H:%M:%S')}] still-fresh, no LLM call: "
          f"{skipped_count}/{total}", flush=True)

    elapsed = time.time() - t_start
    print(f"[{time.strftime('%H:%M:%S')}] done: {total} combos in {elapsed:.1f}s", flush=True)

    if ver:
        try:
            LAST_PREWARM_FILE.write_text(ver)
        except OSError:
            pass


if __name__ == "__main__":
    main()
