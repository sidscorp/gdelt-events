"""Keep the default feed cache warm so the most-common loads are instant.
GET-only (DuckDB queries, no LLM) — cheap. Run on a schedule shorter than the
feed cache TTL. The query params MUST match what the page sends so the in-process
cache key lines up (the page always sends match_types=legal, en_only=1, page=1,
per_page=50; global default window is 3h, pills snap to 24h)."""
import urllib.request, time
from pathlib import Path

BASE = "http://localhost:8015"
# Global default (3h + the 24h widen target) warms every run (~2min) — cheap,
# and keeps the two hottest combos maximally fresh regardless of data_version.
GLOBAL_COMBOS = [("", 3), ("", 24)]
# Curated pills, all at their default_hours=24 (dashboard/views.py). Warming
# these is data_version-guarded below: no point re-running the same query
# against unchanged data every 2 minutes.
PILL_COMBOS = [
    ("ai-general", 24), ("ai-regulation", 24), ("supply-chain-alerts", 24),
    ("medical-devices", 24), ("fda-medical-devices", 24),
    ("oss-vulnerabilities", 24), ("cyber-attacks", 24),
]

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
VERSION_FILE = DATA_DIR / "data_version.txt"
LAST_PILL_WARM_FILE = DATA_DIR / ".last_feed_warm"


def warm(view, hours):
    qs = f"hours={hours}&match_types=legal&en_only=1&page=1&per_page=50&warm=1"
    if view:
        qs += f"&view={view}"
    url = f"{BASE}/api/articles?{qs}"
    t0 = time.time()
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            r.read()
        print(f"[{time.strftime('%H:%M:%S')}] feed {view or 'global'}:{hours} {time.time()-t0:.2f}s", flush=True)
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] feed {view or 'global'}:{hours} FAIL {e}", flush=True)


def _current_data_version() -> str:
    try:
        return VERSION_FILE.read_text().strip()
    except OSError:
        return ""


if __name__ == "__main__":
    for v, h in GLOBAL_COMBOS:
        warm(v, h)

    version = _current_data_version()
    try:
        last_warmed = LAST_PILL_WARM_FILE.read_text().strip()
    except OSError:
        last_warmed = None

    if version and version == last_warmed:
        print(f"[{time.strftime('%H:%M:%S')}] pills: data unchanged (version {version}) — skipping", flush=True)
    else:
        for v, h in PILL_COMBOS:
            warm(v, h)
        try:
            LAST_PILL_WARM_FILE.write_text(version)
        except OSError:
            pass
