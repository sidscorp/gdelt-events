"""Tier 3: selection audit — did the editor pick the right stories? No LLM, free.

From each briefing's sources_json (the 40 importance-ranked candidates with
outlet counts and the editor's `chosen` flags):
  - big stories skipped: well-covered candidates (>= BIG_OUTLETS outlets) not chosen
  - single-source share of what WAS chosen
  - staleness: chosen stories repeated from the previous briefing of the same view/window
  - lead quality comes from Tier 1's lead_check (stored in audits.lead_json)
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from . import store
from .stats import wilson

BIG_OUTLETS = 5      # "a story many outlets are covering"
TOP_RANK = 15        # only count skips among the top of the ranked pool


def _sources(row) -> list[dict]:
    try:
        return [s for s in json.loads(row["sources_json"] or "[]") if isinstance(s, dict)]
    except ValueError:
        return []


def _key(s: dict) -> str:
    return (s.get("link") or s.get("title") or "").strip().lower()


def audit(days: int = 7) -> dict:
    users = store.users_db_ro()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = users.execute("SELECT id, view_id, hours, generated_at, sources_json FROM briefing_history "
                         "WHERE generated_at >= ? ORDER BY id", (since,)).fetchall()
    prev_by_view: dict[tuple, set] = {}
    n_brief = chosen_total = chosen_single = repeated = 0
    skipped = []
    skipped_titles = Counter()
    for r in rows:
        srcs = _sources(r)
        chosen = [s for s in srcs if s.get("chosen")]
        if not chosen:
            continue
        n_brief += 1
        chosen_total += len(chosen)
        chosen_single += sum(int(s.get("n_sources") or 1) <= 1 for s in chosen)
        ranked = sorted(srcs, key=lambda s: int(s.get("n") or 999))[:TOP_RANK]
        big_skipped = [s for s in ranked if not s.get("chosen") and int(s.get("n_sources") or 1) >= BIG_OUTLETS]
        for s in big_skipped:
            skipped_titles[(s.get("title") or "")[:90]] += 1
        skipped.append(len(big_skipped))
        view = (r["view_id"], r["hours"])
        keys = {_key(s) for s in chosen}
        if view in prev_by_view:
            repeated += len(keys & prev_by_view[view])
        prev_by_view[view] = keys
    return {
        "window_days": days, "briefings": n_brief,
        "chosen_single_source": wilson(chosen_single, chosen_total),
        "briefings_skipping_a_big_story": wilson(sum(1 for k in skipped if k), len(skipped)),
        "big_stories_skipped_per_briefing": round(sum(skipped) / max(1, len(skipped)), 2),
        "chosen_repeated_from_previous": wilson(repeated, chosen_total),
        "most_skipped_big_stories": skipped_titles.most_common(5),
        "thresholds": {"big_outlets": BIG_OUTLETS, "top_rank": TOP_RANK},
    }
