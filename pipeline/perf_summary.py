"""Summarize RUM samples (real user front-end timings) as p50/p75/p95 per metric.
Usage: python perf_summary.py [hours_back]"""
import os, sqlite3, sys
from pathlib import Path
from collections import defaultdict
# Mirror dashboard/_paths.py: honor GDELT_DATA_DIR (dev instance) else <repo>/data.
DATA_DIR = Path(os.environ.get("GDELT_DATA_DIR") or (Path(__file__).resolve().parent.parent / "data"))
DB = str(DATA_DIR / "users.db")
hours = int(sys.argv[1]) if len(sys.argv) > 1 else 24
def pct(s, p):
    if not s: return 0
    s = sorted(s); k = (len(s)-1)*p/100; f = int(k)
    return s[f] if f+1 >= len(s) else s[f]+(s[f+1]-s[f])*(k-f)
c = sqlite3.connect(DB, timeout=10)
rows = c.execute(f"SELECT metric, value FROM perf_samples WHERE ts >= datetime('now','-{hours} hours')").fetchall()
c.close()
d = defaultdict(list)
for m, v in rows: d[m].append(v)
print(f"RUM last {hours}h  ({len(rows)} samples)")
print(f"{'metric':22} {'n':>4} {'p50':>8} {'p75':>8} {'p95':>8}")
for m in sorted(d):
    vs = d[m]
    print(f"{m:22} {len(vs):4} {pct(vs,50):8.0f} {pct(vs,75):8.0f} {pct(vs,95):8.0f}")
