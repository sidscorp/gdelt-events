"""Pre-warm the AI briefing cache for the hot view/time combos so users never
wait on generation. Hits the local non-streaming endpoint with refresh=1 to
force regeneration + re-cache. Run on a schedule shorter than BRIEFING_TTL_S."""
import urllib.request, time, sys

BASE = "http://localhost:8015"
PILLS = ["ai-general", "ai-regulation", "supply-chain-alerts",
         "medical-devices", "oss-vulnerabilities", "cyber-attacks"]
# (view, hours) combos to keep warm. Global default is 3h (the page default),
# so warm both 3h and 24h for global; pills snap to their 24h default_hours.
COMBOS = [("", 3), ("", 24)] + [(v, 24) for v in PILLS]

def warm(view, hours):
    url = f"{BASE}/api/briefing?hours={hours}&refresh=1" + (f"&view={view}" if view else "")
    t0 = time.time()
    try:
        with urllib.request.urlopen(url, timeout=90) as r:
            r.read()
        print(f"[{time.strftime('%H:%M:%S')}] warmed {view or 'global'}:{hours} in {time.time()-t0:.1f}s", flush=True)
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] warm {view or 'global'}:{hours} FAILED: {e}", flush=True)

if __name__ == "__main__":
    for v, h in COMBOS:
        warm(v, h)
        time.sleep(1)
