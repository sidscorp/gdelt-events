"""Offline prompt A/B: replay stored briefing prompts with and without a fix.

    python -m eval.briefing_audit.replay --n 30 --days 3 --max-usd 0.40

Every briefing_history row stores the exact prompt the writer saw, so a prompt
change can be tested on identical inputs without touching prod or dev:
  A = the stored prompt, regenerated now (controls for run-to-run noise)
  B = the same prompt with the edits below applied as exact substitutions
Both outputs get the Tier 1 audit. Rows whose prompt lacks any anchor (older
prompt versions) are skipped rather than half-edited.

Writes data/replay/<timestamp>.jsonl; prints paired metrics.

EDITS_V1 shipped to dashboard/briefing.py on 2026-09-27, so its anchors no
longer match newer prompts (those rows are skipped). A/B the next change as
EDITS_V2 with anchors taken from the current prompt; tests/test_briefing_prompt.py
pins the shipped text to EDITS_V1's replacements.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import store
from .checks import check_unit, lead_check
from .gateway import BudgetExceeded, Gateway, JudgeError
from .judge import JUDGE_VERSION, METRIC_SECTIONS, judge_units
from .parse import parse_briefing, parse_sources
from .stats import wilson

# (anchor in the stored prompt, replacement). Anchors are verbatim from
# dashboard/briefing.py so a prompt-version change makes rows skip, not drift.
EDITS_V1 = [
    ("Be specific and concrete. No filler. No hedging. ",
     "Be specific and concrete. No filler. KEEP EACH STORY'S CERTAINTY: if a story describes a plan, "
     "proposal, signal, probe, allegation or possibility, say exactly that; never state it as decided, "
     "announced or done. Add no names, numbers, causes, reactions, or 'first'/'largest'-style claims "
     "that the story text does not contain. "),
    ("not a survey. ",
     "not a survey. Cite every factual sentence of the summary exactly as you cite highlights. "
     "Lead with a story covered by 2+ outlets or a primary news outlet; if the lead rests on a single "
     "outlet or an aggregator, attribute it ('according to <outlet>'). "),
    ("CITATIONS: After each highlight (and any specific factual claim), cite",
     "CITATIONS: After each highlight AND each factual sentence of the executive summary, cite"),
]
PROBLEM = {"overstated", "unsupported", "contradicted"}
OUT_DIR = store.DATA / "replay"


def apply_edits(prompt: str, edits) -> str | None:
    for anchor, repl in edits:
        if prompt.count(anchor) != 1:
            return None
        prompt = prompt.replace(anchor, repl)
    return prompt


def pick_rows(n: int, days: int, seed: int = 11) -> list:
    users = store.users_db_ro()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for r in users.execute("SELECT id, view_id, hours, generated_at, trigger, briefing, sources_json, meta_json "
                           "FROM briefing_history WHERE generated_at >= ? ORDER BY id", (since,)):
        prompt = json.loads(r["meta_json"] or "{}").get("prompt") or ""
        if apply_edits(prompt, EDITS_V1):
            rows.append(r)
    random.Random(seed).shuffle(rows)
    return sorted(rows[:n], key=lambda r: r["id"])


def audit_text(gw: Gateway, text: str, row) -> dict:
    prompt = json.loads(row["meta_json"] or "{}").get("prompt")
    sources = parse_sources(row["sources_json"], prompt)
    units = parse_briefing(text)
    flags = {u.idx: check_unit(u, sources) for u in units}
    res = judge_units(gw, units, sources, flags)
    sents = [{"idx": u.idx, "section": u.section, "text": u.text, "cites": u.cites, "flags": flags[u.idx],
              "verdict": res.get(u.idx, {}).get("verdict"), "confidence": res.get(u.idx, {}).get("confidence")}
             for u in units]
    chosen = {n for n, s in sources.items() if s.chosen}
    cited = {c for u in units for c in u.cites}
    return {"sentences": sents, "lead": lead_check(units, sources),
            "coverage": {"highlights": sum(u.section == "highlight" for u in units),
                         "chosen": len(chosen), "chosen_cited": len(chosen & cited)}}


def summarize(results: list[dict]) -> dict:
    out = {}
    for arm in ("A", "B"):
        s = [x for r in results for x in r[arm]["sentences"] if x["verdict"] and x["section"] in METRIC_SECTIONS]
        fact = [x for x in s if x["verdict"] != "analysis"]
        summ = [x for x in s if x["section"] == "summary"]
        leads = [r[arm]["lead"] for r in results]
        cov = [r[arm]["coverage"] for r in results]
        out[arm] = {
            "sentences": len(s), "factual": len(fact),
            "problem_rate": wilson(sum(x["verdict"] in PROBLEM for x in fact), len(fact)),
            "overstated": wilson(sum(x["verdict"] == "overstated" for x in fact), len(fact)),
            "unsupported": wilson(sum(x["verdict"] == "unsupported" for x in fact), len(fact)),
            "contradicted": wilson(sum(x["verdict"] == "contradicted" for x in fact), len(fact)),
            "summary_uncited": wilson(sum("uncited_summary" in x["flags"] for x in summ), len(summ)),
            "summary_problem": wilson(sum(x["verdict"] in PROBLEM for x in summ), len(summ)),
            "lead_problem": wilson(sum(next((x["verdict"] in PROBLEM for x in r[arm]["sentences"]
                                             if x["section"] == "summary"), False) for r in results), len(results)),
            "lead_single_source": wilson(sum(bool(l.get("lead_single_source")) for l in leads), len(leads)),
            "highlights_per_briefing": round(sum(c["highlights"] for c in cov) / max(1, len(cov)), 1),
            "chosen_cited_share": round(sum(c["chosen_cited"] for c in cov) / max(1, sum(c["chosen"] for c in cov)), 3),
        }
    # Paired bootstrap over briefings for the change in problem rate (B - A).
    per = []
    for r in results:
        f = {arm: [x for x in r[arm]["sentences"] if x["verdict"] and x["verdict"] != "analysis"
                   and x["section"] in METRIC_SECTIONS] for arm in "AB"}
        per.append(tuple((sum(x["verdict"] in PROBLEM for x in f[a]), len(f[a])) for a in "AB"))
    rng, diffs = random.Random(5), []
    for _ in range(2000):
        smp = [per[rng.randrange(len(per))] for _ in per]
        a = sum(p[0][0] for p in smp) / max(1, sum(p[0][1] for p in smp))
        b = sum(p[1][0] for p in smp) / max(1, sum(p[1][1] for p in smp))
        diffs.append(b - a)
    diffs.sort()
    point = (sum(p[1][0] for p in per) / max(1, sum(p[1][1] for p in per))
             - sum(p[0][0] for p in per) / max(1, sum(p[0][1] for p in per)))
    out["problem_rate_change_B_minus_A"] = {"point": round(point, 3), "ci95": [round(diffs[49], 3), round(diffs[1949], 3)]}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--max-usd", type=float, default=0.40)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--rescore", help="recompute metrics from a saved replay .jsonl (free)")
    a = ap.parse_args(argv)
    if a.rescore:
        recs = [json.loads(l) for l in open(a.rescore, encoding="utf-8") if l.strip()]
        ok = [r for r in recs if "error" not in r]
        print(json.dumps({"pairs": len(ok), "file": a.rescore, **summarize(ok)}, indent=1))
        return 0

    rows = pick_rows(a.n, a.days)
    print(f"replaying {len(rows)} briefings x 2 arms (judge {JUDGE_VERSION})", file=sys.stderr)
    gw = Gateway(max_usd=a.max_usd)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"{datetime.now():%Y%m%d-%H%M%S}.jsonl"

    def one(row):
        prompt = json.loads(row["meta_json"])["prompt"]
        rec = {"briefing_id": row["id"], "view_id": row["view_id"], "hours": row["hours"]}
        try:
            for arm, p in (("A", prompt), ("B", apply_edits(prompt, EDITS_V1))):
                text = gw.write(p)
                rec[arm] = {"text": text, **audit_text(gw, text, row)}
        except JudgeError as e:
            rec["error"] = str(e)[:200]
        return rec

    results, errors = [], 0
    try:
        with ThreadPoolExecutor(max_workers=a.workers) as pool, out_path.open("w", encoding="utf-8") as fh:
            for rec in pool.map(one, rows):
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                if "error" in rec:
                    errors += 1
                    print(f"  {rec['briefing_id']}: {rec['error']}", file=sys.stderr)
                else:
                    results.append(rec)
                print(f"  done {len(results)}/{len(rows)}  ${gw.spent:.4f}", file=sys.stderr)
    except BudgetExceeded as e:
        print(f"stopped: {e}", file=sys.stderr)
    print(json.dumps({"pairs": len(results), "errors": errors, "cost_usd": round(gw.spent, 4),
                      "file": str(out_path), **summarize(results)}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
