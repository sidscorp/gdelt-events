"""Keep the default feed cache warm so the most-common loads are instant.
GET-only (DuckDB queries, no LLM) — cheap. Run on a schedule shorter than the
feed cache TTL. The query params MUST match what the page sends so the in-process
cache key lines up (the page always sends match_types=legal, en_only=1, page=1,
per_page=50; global default window is 3h, pills snap to 24h)."""
import urllib.request, time

BASE = "http://localhost:8015"
# Only the global default (3h + the 24h widen target). Pills are NOT frequently
# warmed — each feed query is ~2.5s, so warming all of them every 2 min would be
# too much constant DB load; instant-paint (localStorage) + the 3-min cache cover
# pill revisits, and pill briefings are still pre-warmed separately.
COMBOS = [("", 3), ("", 24)]


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


if __name__ == "__main__":
    for v, h in COMBOS:
        warm(v, h)
