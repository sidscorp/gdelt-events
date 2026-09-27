"""Gold set: human-reviewed labels that measure the JUDGE, not the briefings.

    python -m eval.briefing_audit.gold sample --n 50 --out gold/candidates.jsonl
    python -m eval.briefing_audit.gold regress [--gold gold/claims.jsonl]

Sampling is stratified by the judge's own verdict (and section) so the rare
classes — overstated, unsupported, contradicted — are present in numbers
large enough to measure recall on. A random sample would be ~70% "supported"
and say little about the failures we care about.

Labels use the same scale as the judge and the SAME evidence the writer had
(headline + description of the cited sources), because Tier 1 asks "did the
briefing say what its sources said", not "is it true in the world" (Tier 2).
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

from . import store
from .judge import JUDGE_VERSION
from .parse import parse_sources
from .stats import wilson

GOLD_DIR = Path(__file__).resolve().parent / "gold"
QUOTA = {"overstated": 12, "unsupported": 8, "contradicted": 4, "supported": 16, "analysis": 10}


def sample(n: int, out: Path, seed: int = 7) -> int:
    ev, users = store.eval_db(), store.users_db_ro()
    rows = ev.execute(
        "SELECT s.briefing_id, s.idx, s.section, s.text, s.cites, s.basis, s.flags, s.verdict, s.confidence "
        "FROM sentences s JOIN audits a ON a.briefing_id = s.briefing_id "
        "WHERE s.judged = 1 AND a.judge_version = ?", (JUDGE_VERSION,)).fetchall()
    by = defaultdict(list)
    for r in rows:
        by[r["verdict"]].append(r)
    rng = random.Random(seed)
    picked = []
    scale = n / sum(QUOTA.values())
    for verdict, q in QUOTA.items():
        pool = by.get(verdict, [])
        # Summary sentences are over-sampled: the lead is where drift hurts most.
        pool.sort(key=lambda r: (r["section"] != "summary", rng.random()))
        picked += pool[:max(1, round(q * scale))]
    # Always include the incident that started this (09-27 13:52 H-1B lead), if audited.
    h1b = ev.execute("SELECT s.* FROM sentences s WHERE s.briefing_id = 4041 AND s.idx = 0").fetchone()
    if h1b and all((p["briefing_id"], p["idx"]) != (4041, 0) for p in picked):
        picked.append(h1b)
    cache: dict[int, dict] = {}
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for r in picked:
            bid = r["briefing_id"]
            if bid not in cache:
                b = users.execute("SELECT sources_json, meta_json FROM briefing_history WHERE id = ?", (bid,)).fetchone()
                prompt = json.loads(b["meta_json"] or "{}").get("prompt")
                cache[bid] = parse_sources(b["sources_json"], prompt)
            srcs = cache[bid]
            basis = json.loads(r["basis"] or "[]")
            fh.write(json.dumps({
                "id": f"{bid}:{r['idx']}", "section": r["section"], "sentence": r["text"],
                "cited": json.loads(r["cites"] or "[]"), "flags": json.loads(r["flags"] or "[]"),
                "sources": [{"n": k, "outlet": srcs[k].outlet, "outlets": srcs[k].n_sources,
                             "headline": srcs[k].title, "description": srcs[k].description} for k in basis if k in srcs],
                "judge": {"version": JUDGE_VERSION, "verdict": r["verdict"], "confidence": r["confidence"]},
                "label": None, "reason": None, "labeler": None,
            }) + "\n")
    return len(picked)


def _live_verdicts(gold: list[dict], max_usd: float) -> dict[str, str]:
    """Re-judge each gold sentence with the CURRENT questions, rebuilding the
    unit and sources from users.db exactly as a real audit would. Not stored."""
    from .checks import check_unit
    from .gateway import Gateway
    from .judge import judge_unit
    from .parse import Unit
    users, gw, out = store.users_db_ro(), Gateway(max_usd=max_usd), {}
    for g in gold:
        bid, _ = map(int, g["id"].split(":"))
        b = users.execute("SELECT sources_json, meta_json FROM briefing_history WHERE id = ?", (bid,)).fetchone()
        srcs = parse_sources(b["sources_json"], json.loads(b["meta_json"] or "{}").get("prompt"))
        u = Unit(0, g["section"], g["sentence"], g["cited"])
        out[g["id"]] = judge_unit(gw, u, srcs, check_unit(u, srcs)).get("verdict")
    print(f"live re-judge: {gw.calls} calls, ${gw.spent:.5f}", file=sys.stderr)
    return out


def regress(gold_path: Path, live: bool = False, max_usd: float = 0.02) -> dict:
    """Judge-vs-gold agreement. By default compares the judge's STORED verdicts
    (same version, free); --live re-judges the gold sentences with the current
    questions, which is how a criteria change is evaluated before a re-audit."""
    ev = store.eval_db()
    gold = [json.loads(l) for l in gold_path.open(encoding="utf-8") if l.strip()]
    # Sidd's spot-check overrides Claude's label; "exclude" marks parser artifacts.
    for g in gold:
        if g.get("sidd"):
            g["label"] = g["sidd"]
    gold = [g for g in gold if g.get("label") and g["label"] != "exclude"]
    fresh = _live_verdicts(gold, max_usd) if live else {}
    pairs, majors, missing = [], [], 0
    for g in gold:
        if live:
            verdict = fresh.get(g["id"])
        else:
            bid, idx = map(int, g["id"].split(":"))
            r = ev.execute("SELECT verdict FROM sentences s JOIN audits a ON a.briefing_id = s.briefing_id "
                           "WHERE s.briefing_id=? AND s.idx=? AND a.judge_version=?", (bid, idx, JUDGE_VERSION)).fetchone()
            verdict = r["verdict"] if r else None
        if not verdict:
            missing += 1
            continue
        pairs.append((g["label"], verdict))
        if g.get("severity") == "major":
            majors.append(verdict)
    agree = sum(a == b for a, b in pairs)
    # The decision that matters operationally: is this sentence a PROBLEM
    # (overstated/unsupported/contradicted) or not?
    prob = {"overstated", "unsupported", "contradicted"}
    tp = sum(a in prob and b in prob for a, b in pairs)
    fp = sum(a not in prob and b in prob for a, b in pairs)
    fn = sum(a in prob and b not in prob for a, b in pairs)
    per = {}
    for cls in sorted({a for a, _ in pairs} | {b for _, b in pairs}):
        t = sum(a == cls and b == cls for a, b in pairs)
        per[cls] = {"gold": sum(a == cls for a, _ in pairs), "judged": sum(b == cls for _, b in pairs),
                    "precision": round(t / max(1, sum(b == cls for _, b in pairs)), 3),
                    "recall": round(t / max(1, sum(a == cls for a, _ in pairs)), 3)}
    return {
        "judge_version": JUDGE_VERSION, "n": len(pairs), "missing": missing,
        "exact_agreement": wilson(agree, len(pairs)),
        # The public-note gate (>= 85%): does the judge agree on problem vs not?
        # supported<->analysis swaps don't change what a reader is told.
        # Headline metric per RUBRIC.md: of the problems that change what a
        # reader believes (gold severity=major), how many does the judge flag?
        "major_recall": wilson(sum(v in prob for v in majors), len(majors)),
        "problem_agreement": wilson(sum((a in prob) == (b in prob) for a, b in pairs), len(pairs)),
        "problem_detection": {"precision": round(tp / max(1, tp + fp), 3), "recall": round(tp / max(1, tp + fn), 3),
                              "tp": tp, "fp": fp, "fn": fn},
        "per_class": per,
        "confusion": dict(Counter(f"{a}->{b}" for a, b in pairs if a != b).most_common(8)),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample"); s.add_argument("--n", type=int, default=50); s.add_argument("--out", default=str(GOLD_DIR / "candidates.jsonl"))
    r = sub.add_parser("regress"); r.add_argument("--gold", default=str(GOLD_DIR / "claims.jsonl"))
    r.add_argument("--live", action="store_true", help="re-judge gold sentences with the current questions")
    r.add_argument("--max-usd", type=float, default=0.02)
    a = ap.parse_args(argv)
    if a.cmd == "sample":
        print(f"sampled {sample(a.n, Path(a.out))} sentences -> {a.out}")
    else:
        print(json.dumps(regress(Path(a.gold), a.live, a.max_usd), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
