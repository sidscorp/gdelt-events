"""Briefing accuracy audit — entry point.

    python -m eval.briefing_audit.run tier1 [--max-usd 0.01]       # new briefings since last run (15-min task)
    python -m eval.briefing_audit.run backfill --days 7 --max-usd 0.40
    python -m eval.briefing_audit.run report [--days 7]            # metrics with 95% CIs

Tier 1 = deterministic checks + one Jev call per sentence (see judge.py).
Every run is capped twice: this process stops at --max-usd of billed tokens, and
the gateway key itself has a $3/30d budget.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

from . import store
from .checks import check_unit, lead_check
from .gateway import BudgetExceeded, Gateway
from .judge import JUDGE_VERSION, METRIC_SECTIONS, judge_units
from .parse import parse_briefing, parse_sources
from .stats import wilson


def audit_rows(rows, max_usd: float, kind: str) -> dict:
    ev = store.eval_db()
    run_id = ev.execute("INSERT INTO runs (kind, started_at) VALUES (?, ?)", (kind, store.now())).lastrowid
    ev.commit()
    gw = Gateway(max_usd=max_usd)
    done = sentences = 0
    note = None
    try:
        for row in rows:
            try:
                prompt = json.loads(row["meta_json"] or "{}").get("prompt")
            except ValueError:
                prompt = None
            sources = parse_sources(row["sources_json"], prompt)
            units = parse_briefing(row["briefing"])
            flags = {u.idx: check_unit(u, sources) for u in units}
            results = judge_units(gw, units, sources, flags)
            store.save_audit(ev, run_id, row, units, flags, results, lead_check(units, sources), JUDGE_VERSION)
            done += 1
            sentences += len(units)
    except BudgetExceeded as e:
        note = str(e)  # the briefing in progress is NOT saved, so it is retried next run
    ev.execute("UPDATE runs SET finished_at=?, briefings=?, sentences=?, calls=?, tokens_in=?, cost_usd=?, note=? "
               "WHERE id=?", (store.now(), done, sentences, gw.calls, gw.tokens_in, gw.spent, note, run_id))
    ev.commit()
    return {"run_id": run_id, "briefings": done, "pending": len(rows) - done, "sentences": sentences,
            "calls": gw.calls, "cost_usd": round(gw.spent, 5), "note": note}


def report(days: int) -> dict:
    ev = store.eval_db()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = ev.execute(
        "SELECT s.section, s.verdict, s.flags, s.escalate FROM sentences s JOIN audits a ON a.briefing_id = s.briefing_id "
        "WHERE a.generated_at >= ? AND a.judge_version = ? AND s.judged = 1", (since, JUDGE_VERSION)).fetchall()
    audits = ev.execute("SELECT lead_json FROM audits WHERE generated_at >= ? AND judge_version = ?",
                        (since, JUDGE_VERSION)).fetchall()
    rows = [r for r in rows if r["section"] in METRIC_SECTIONS]
    factual = [r for r in rows if r["verdict"] != "analysis"]
    n = len(factual)
    by = Counter(r["verdict"] for r in factual)
    leads = [json.loads(a["lead_json"] or "{}") for a in audits]
    summary = [r for r in rows if r["section"] == "summary"]
    out = {
        "window_days": days, "judge_version": JUDGE_VERSION, "briefings": len(audits),
        "sentences_judged": len(rows), "factual_sentences": n,
        "supported": wilson(by["supported"], n), "overstated": wilson(by["overstated"], n),
        "unsupported": wilson(by["unsupported"], n), "contradicted": wilson(by["contradicted"], n),
        "summary_uncited": wilson(sum("uncited_summary" in (r["flags"] or "") for r in summary), len(summary)),
        "summary_overstated": wilson(sum(r["verdict"] == "overstated" for r in summary), len(summary)),
        "lead_single_source": wilson(sum(bool(l.get("lead_single_source")) for l in leads), len(leads)),
        "lead_aggregator": wilson(sum(bool(l.get("lead_aggregator")) for l in leads), len(leads)),
        "escalated_to_kimi": wilson(sum(r["escalate"] for r in rows), len(rows)),
        "cost_usd": round(ev.execute("SELECT coalesce(sum(cost_usd),0) FROM runs WHERE started_at >= ?", (since,)).fetchone()[0], 4),
    }
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    t1 = sub.add_parser("tier1"); t1.add_argument("--max-usd", type=float, default=0.01)
    t1.add_argument("--visits-only", action="store_true", help="budget-saving mode: skip prewarm briefings")
    bf = sub.add_parser("backfill"); bf.add_argument("--days", type=int, default=7); bf.add_argument("--max-usd", type=float, default=0.40)
    bf.add_argument("--ids", default="", help="comma-separated briefing ids (re-audits them even if already done)")
    rp = sub.add_parser("report"); rp.add_argument("--days", type=int, default=7)
    t3 = sub.add_parser("tier3", help="selection audit (free)"); t3.add_argument("--days", type=int, default=7)
    a = ap.parse_args(argv)

    if a.cmd == "report":
        print(json.dumps(report(a.days), indent=1))
        return 0
    if a.cmd == "tier3":
        from .selection import audit
        print(json.dumps(audit(a.days), indent=1, ensure_ascii=False))
        return 0
    users, ev = store.users_db_ro(), store.eval_db()
    if a.cmd == "backfill" and a.ids:
        ids = [int(x) for x in a.ids.split(",") if x.strip()]
        rows = users.execute(
            "SELECT id, view_id, hours, generated_at, trigger, briefing, sources_json, meta_json "
            f"FROM briefing_history WHERE id IN ({','.join('?' * len(ids))}) ORDER BY id", ids).fetchall()
    elif a.cmd == "backfill":
        since = (datetime.now(timezone.utc) - timedelta(days=a.days)).strftime("%Y-%m-%d %H:%M:%S")
        rows = store.pending_briefings(users, ev, since=since)
    else:
        # New rows only: anything generated since the newest audited briefing (minus a small overlap).
        last = ev.execute("SELECT max(generated_at) FROM audits").fetchone()[0]
        since = last or (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        rows = store.pending_briefings(users, ev, since=since, triggers=("visit",) if a.visits_only else None)
    print(json.dumps(audit_rows(rows, a.max_usd, a.cmd)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
