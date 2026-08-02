"""Pre-warm the AI briefing cache for every view × time-range combo so the
dashboard always has pre-generated content to serve instantly (no "Generating…" skeletons).

Hits the local non-streaming endpoint with ?refresh=1 to force regeneration
and re-cache. Data-version-guarded: only runs when gdelt_ingest.py has written
a new data_version.txt, skipping redundant cycles.

Scheduled twice daily (midnight + noon) via Windows Task Scheduler.
With cerebras-fast (free tier), failures from 402s are logged and skipped;
the "keep stale until fresh" design means users still see the last cached
briefing. Total: 17 views × 6 windows = 102 combos, ~45-90 min sequential.

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
    params = f"hours={hours}&refresh=1"
    if view:
        params += f"&view={view}"
    url = f"{base_url}/api/briefing?{params}"
    label = f"{view or 'global'}:{hours}h"
    t0 = time.time()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            r.read()
        elapsed = time.time() - t0
        print(f"[{time.strftime('%H:%M:%S')}]  OK  {label:30s} {elapsed:.1f}s", flush=True)
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] FAIL {label:30s} ({e})", flush=True)


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

    total = len(VIEWS) * len(HOURS)
    print(f"[{time.strftime('%H:%M:%S')}] pre-warming {total} briefing combos "
          f"({len(VIEWS)} views × {len(HOURS)} windows) on port {args.port}", flush=True)

    t_start = time.time()
    ok_count = 0
    fail_count = 0
    for view in VIEWS:
        for hours in HOURS:
            warm(view, hours, base_url)
            ok_count += 1  # failure is logged but not fatal; count all attempts
            if STAGGER_S:
                time.sleep(STAGGER_S)

    elapsed = time.time() - t_start
    print(f"[{time.strftime('%H:%M:%S')}] done: {total} combos in {elapsed:.1f}s", flush=True)

    if ver:
        try:
            LAST_PREWARM_FILE.write_text(ver)
        except OSError:
            pass


if __name__ == "__main__":
    main()
