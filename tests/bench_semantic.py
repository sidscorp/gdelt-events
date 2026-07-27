"""Latency benchmark for /api/semantic_search against the rebuilt index.

Isolates the factors that actually drive response time:
  k        FAISS candidates retrieved (default 500) - the search-side cost
  hours    time window - post-filter cost + how many candidates survive
  per_page rows hydrated from DuckDB for the response
  repeat   warm vs cold (query embedding + index page cache)

Reports p50/p95 per configuration. Server-side phase timings are echoed when
the response carries them.
"""
import json
import statistics
import sys
import time
import urllib.parse
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "https://gdeltmonitor.com"
REPEATS = 2

QUERIES = [
    "ceasefire negotiations and diplomatic talks",
    "semiconductor export controls and chip manufacturing",
    "hospital ransomware attack patient data",
]


# Cloudflare 403s the default Python-urllib User-Agent.
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def call(params, timeout=180):
    url = f"{BASE}/api/semantic_search?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read())
        dt = time.time() - t0
        return dt, body, None
    except Exception as e:
        return time.time() - t0, None, str(e)[:120]


def run(label, base_params, variants):
    print(f"\n=== {label} ===")
    print(f"{'variant':<22} {'p50 s':>7} {'p95 s':>7} {'results':>8}  {'server phases'}")
    for vlabel, extra in variants:
        times, counts, phases = [], [], ""
        for q in QUERIES:
            for _ in range(REPEATS):
                p = dict(base_params)
                p.update(extra)
                p["q"] = q
                dt, body, err = call(p)
                if err:
                    print(f"{vlabel:<22} ERROR {err}")
                    times = []
                    break
                times.append(dt)
                arts = body.get("articles") or body.get("results") or []
                counts.append(len(arts))
                if "timing_ms" in body:
                    phases = str(body["timing_ms"])
                counts[-1] = body.get("total", counts[-1])
            if not times:
                break
        if not times:
            continue
        s = sorted(times)
        p50 = statistics.median(s)
        p95 = s[min(len(s) - 1, int(len(s) * 0.95))]
        avg_n = statistics.mean(counts) if counts else 0
        print(f"{vlabel:<22} {p50:7.2f} {p95:7.2f} {avg_n:8.0f}  {phases}")


print(f"target: {BASE}   queries: {len(QUERIES)}   repeats: {REPEATS}")

run("k (FAISS candidates retrieved)",
    {"hours": 168, "per_page": 25, "language": "en"},
    [("k=100", {"k": 100}), ("k=500 (default)", {"k": 500}),
     ("k=1000", {"k": 1000}), ("k=2000 (max)", {"k": 2000})])

run("hours (time window)",
    {"k": 500, "per_page": 25, "language": "en"},
    [("hours=24", {"hours": 24}), ("hours=168 (7d)", {"hours": 168}),
     ("hours=720 (30d)", {"hours": 720}), ("no hours (unbounded)", {})])

run("per_page (rows hydrated)",
    {"k": 500, "hours": 168, "language": "en"},
    [("per_page=5", {"per_page": 5}), ("per_page=25", {"per_page": 25}),
     ("per_page=100", {"per_page": 100})])

print("\n=== warm vs cold (same query repeated) ===")
p = {"q": QUERIES[0], "hours": 168, "k": 500, "per_page": 25, "language": "en"}
for i in range(5):
    dt, body, err = call(p)
    n = len((body or {}).get("articles") or (body or {}).get("results") or [])
    print(f"  call {i+1}: {dt:6.2f}s  results={n}" + (f"  ERROR {err}" if err else ""))
