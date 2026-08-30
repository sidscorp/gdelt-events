"""Pre-warm the AI briefing cache for every view × time-range combo so the
dashboard always has pre-generated content to serve instantly (no "Generating…" skeletons).

Hits the local non-streaming endpoint with ?prewarm=1, which regenerates only
when the cache is stale for that window (see briefing.fresh_s). Data-version-guarded: only runs when gdelt_ingest.py has written
a new data_version.txt, skipping redundant cycles.

DEMAND-DRIVEN: the combo list is not the 17x6=102 cross-product. It is read from
`pageview_log` — the view/window pairs people actually LOOK AT, weighted by how many
distinct visitors looked.

    THIS USED TO READ `briefing_history WHERE trigger='visit'` AND THAT SIGNAL IS INVERTED.
    A briefing_history row is only written when a briefing is GENERATED. A visit that hits a
    warm cache writes nothing. So the combos this prewarmer successfully kept warm produced no
    "visit" rows and looked like zero demand, while the combos nobody reads missed cache, wrote
    a visit row, and looked like demand. The list was selecting on cache misses, which
    anti-correlate with popularity. Measured 2026-08-29: it was walking 29 combos and
    regenerating 13 per run, 5 runs/day = ~59 generations/day, against 27 human briefing reads
    per WEEK. 411 prewarms served 27 visits.

`pageview_log` records every pageview with its `briefing_key`, cache hit or miss, so it is the
honest demand signal. Measured over 2026-08-12..08-30 (723 views): `_all:3` is 68.6% of all
views with 306 distinct visitors; the next-broadest key has 5 visitors. Demand is extremely
concentrated, so a short warm list covers almost everything.

Distinct visitors, not raw views, decide: `geopolitics-conflict:3` shows 88 views from just
2 visitors — one enthusiast or crawler, not breadth. Ranking on views alone would warm it above
keys that many more people actually open.

Combos below the threshold are generated on demand the rare time someone opens one. That path is
fast and visible: measured client-side, a live briefing shows its first text at p50 0.34s and
finishes at p50 2.9s / p90 10.8s, and the UI says what is happening and why.

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



PREWARM_LOOKBACK_DAYS = 14   # trailing window for the demand signal
PREWARM_MIN_VISITORS = 2     # distinct visitors a combo needs to earn a warm slot
PREWARM_MAX_COMBOS = 12      # hard ceiling, so a burst of browsing can't explode cost
ALWAYS_WARM = [("", 3), ("", 24)]   # global feed: the front page, always instant


def _parse_briefing_key(key):
    """'geopolitics-conflict:24' -> ('geopolitics-conflict', 24); '_all:3' -> ('', 3).

    Returns None for anything malformed. The log contains at least one '_all|3' with a pipe,
    so this must not assume the separator is present or the window is numeric.
    """
    if not key or ":" not in key:
        return None
    view, _, hours = key.rpartition(":")
    try:
        hours = int(hours)
    except ValueError:
        return None
    if hours not in HOURS:
        return None
    return ("" if view in ("_all", "", None) else view), hours


def demand_combos():
    """(view_id, hours) pairs people actually look at, broadest demand first.

    Reads `pageview_log`, which records every pageview whether or not it hit a warm cache.
    See the module docstring for why `briefing_history` cannot be used for this: its rows exist
    only when a briefing was generated, so it measures cache misses, not readership.

    Ranked by DISTINCT VISITORS, then views. One person reloading a niche view all week must not
    outrank a view many different people open once.
    """
    import sqlite3
    db = DATA_DIR / "users.db"
    combos = []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT briefing_key, count(DISTINCT ip_hash) visitors, count(*) views "
            "FROM pageview_log "
            "WHERE ts >= datetime('now', ?) "
            "  AND briefing_key IS NOT NULL AND briefing_key <> '' "
            "GROUP BY briefing_key "
            "HAVING visitors >= ? "
            "ORDER BY visitors DESC, views DESC",
            (f"-{PREWARM_LOOKBACK_DAYS} day", PREWARM_MIN_VISITORS),
        ).fetchall()
        con.close()
        for key, _visitors, _views in rows:
            combo = _parse_briefing_key(key)
            if combo and combo not in combos:
                combos.append(combo)
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] demand query failed ({e}) — "
              f"falling back to ALWAYS_WARM only", flush=True)

    # The front page is warmed regardless of what the window says: it is 68.6% of all views and
    # the one page a first-time visitor is guaranteed to land on.
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
          f"(>={PREWARM_MIN_VISITORS} visitors in {PREWARM_LOOKBACK_DAYS}d) on port {args.port}", flush=True)

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
