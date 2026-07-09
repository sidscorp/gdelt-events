"""Per-pill threshold calibration from LLM-judge verdicts + semantic scores.

Joins the latest pill_eval v2 report (verdicts per sampled url) with each
sample's cosine score (article_tags.matched_detail in the __v2 category) and
reports, per pill:
  - score distribution by verdict (relevant vs irrelevant)
  - the lowest threshold that achieves >= --target precision on the sample
    (borderline counts 0.5), with the recall it would keep

Usage:
    python -m pipeline.calibrate_pills --report data/pill_eval/<ts>_v2_round1.json
"""

import argparse
import json
from pathlib import Path

import duckdb

from .config import DATA_DIR, DB_PATH

TARGET_DEFAULT = 0.85


def calibrate(report_path: Path, target: float):
    report = json.loads(report_path.read_text())
    con = duckdb.connect(str(DB_PATH), read_only=True)

    print(f"{'pill':28s} {'rel scores (p25/p50/p75)':26s} {'irr p50':>8s} "
          f"{'sugg_hi':>8s} {'kept%':>6s}")
    suggestions = {}
    for r in report["results"]:
        cat = r["category"]
        verdicts = r.get("verdicts") or []
        if not verdicts:
            continue
        urls = [v["url"] for v in verdicts if v.get("url")]
        ph = ",".join(["?"] * len(urls))
        score_by_url = {}
        for u, d in con.execute(
            f"SELECT article_id, matched_detail FROM article_tags "
            f"WHERE category = ? AND matched_via = 'semantic' AND article_id IN ({ph})",
            [cat] + urls,
        ).fetchall():
            try:
                score_by_url[u] = float(d)
            except (TypeError, ValueError):
                pass

        pts = []  # (score, weight_relevant)
        for v in verdicts:
            s = score_by_url.get(v.get("url"))
            if s is None:
                continue
            w = {"relevant": 1.0, "borderline": 0.5, "irrelevant": 0.0}[v["verdict"]]
            pts.append((s, w))
        if len(pts) < 10:
            print(f"{cat:28s} (only {len(pts)} scored samples — skip)")
            continue

        pts.sort()
        rel = sorted(s for s, w in pts if w == 1.0)
        irr = sorted(s for s, w in pts if w == 0.0)

        def pctl(a, p):
            return a[min(len(a) - 1, int(p * len(a)))] if a else float("nan")

        # Lowest threshold hitting target precision on the sample
        best = None
        cand = sorted({round(s, 3) for s, _ in pts})
        for t in cand:
            kept = [(s, w) for s, w in pts if s >= t]
            if not kept:
                break
            prec = sum(w for _, w in kept) / len(kept)
            if prec >= target:
                best = (t, len(kept) / len(pts))
                break
        sugg = f"{best[0]:.3f}" if best else ">max"
        kept = f"{best[1]*100:.0f}%" if best else "-"
        suggestions[cat] = best[0] if best else None
        print(f"{cat:28s} {pctl(rel,0.25):.3f}/{pctl(rel,0.5):.3f}/{pctl(rel,0.75):.3f}"
              f"{'':10s} {pctl(irr,0.5):8.3f} {sugg:>8s} {kept:>6s}")

    con.close()
    print("\nsuggestions:", json.dumps(suggestions, indent=1))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True)
    parser.add_argument("--target", type=float, default=TARGET_DEFAULT)
    args = parser.parse_args()
    calibrate(Path(args.report), args.target)


if __name__ == "__main__":
    main()
