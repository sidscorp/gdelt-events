"""Jev sentence judge: does each briefing sentence say what its sources say?

One Jev call per sentence, fanned out into three questions (output is free, so
the extra two cost nothing). Criteria are written for Jev's literal reading:
each option states exactly what it means, with no negations to invert.
State holds ONLY the sentence and its sources — Jev's accuracy drops with
irrelevant context, so uncited sentences get the few closest sources, not all 40.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from .checks import best_sources
from .gateway import BudgetExceeded, Gateway, JudgeError
from .parse import Source, Unit

JUDGE_VERSION = "jev-1.13.0/q3"   # bump when questions change: verdicts aren't comparable across versions

# q2 (2026-09-27): q1 filed mixed fact+interpretation sentences under "analysis"
# and read near-synonyms ("poised to" vs "close to") as overstatement.
# q3 (2026-09-27, gold set v1): q2 called speculative sentences ("could trigger…",
# "dovetails with…") overstated — "makes it clearly bigger" read as covering
# predictions. Overstated is now only a certainty upgrade of a sourced event;
# predictions and links between stories are analysis; superlatives are specifics.
VERDICTS = {
    "supported": ("Every factual detail in the sentence is found in the sources with the same certainty. "
                  "Interpretation added to a well-supported fact still counts as supported. Wording can differ."),
    "overstated": ("The sentence reports an event from the sources but states as definite, announced or already "
                   "done something the sources describe only as signalled, proposed, planned, expected, alleged, "
                   "under investigation or possible."),
    "unsupported": ("The sentence reports a specific detail that the sources do not mention: a name, number, "
                    "event, cause, or a claim such as 'first', 'largest' or 'sharpest'."),
    "contradicted": "A fact in the sentence conflicts with what the sources say.",
    "analysis": ("The sentence's main point is interpretation: what an event means, what it could lead to, or how "
                 "it connects to other stories (for example 'could', 'may', 'if', 'underscores', 'dovetails with'). "
                 "It adds no new detail about any event."),
}
QUESTIONS = {
    "verdict": {"type": "choice",
                "instructions": "How well do the sources support the briefing sentence?",
                "criteria": VERDICTS},
    "escalates": {"type": "noul",
                  "instructions": ("The briefing sentence states as definite or already done an action that the "
                                   "sources describe only as signalled, planned, proposed, under investigation or possible.")},
    "adds_specifics": {"type": "noul",
                       "instructions": ("The briefing sentence names a person, organisation, number or action that "
                                        "none of the sources mention.")},
}
SECTION_ROLE = {"summary": "executive summary (the briefing's lead)", "highlight": "key highlight",
                "watch": "what to watch (forward-looking)", "quieter": "quieter but notable", "other": "briefing text"}
ESCALATE_FLAGS = ("uncited_summary", "number_not_in_sources", "cite_missing", "cite_not_chosen")
# "What to watch" is forward-looking by design: Jev files 100% of its factual-
# sounding predictions as problems (replay 09-27, both arms). Judged for the
# record, but excluded from metrics and never escalated.
METRIC_SECTIONS = ("summary", "highlight", "quieter", "other")


def state_for(unit: Unit, sources: dict[int, Source]) -> tuple[dict, list[int]]:
    ns = [n for n in unit.cites if n in sources] or best_sources(unit, sources, k=5)
    basis = "cited by the briefing writer" if unit.cites else "closest matching stories (the sentence cites none)"
    return {
        "briefing_sentence": unit.text,
        "sentence_role": SECTION_ROLE.get(unit.section, unit.section),
        "source_basis": basis,
        "sources": [{"id": n, "outlet": sources[n].outlet, "outlets_covering_story": sources[n].n_sources,
                     "headline": sources[n].title, "description": sources[n].description} for n in ns],
    }, ns


def judge_unit(gw: Gateway, unit: Unit, sources: dict[int, Source], flags: list[str]) -> dict:
    state, basis = state_for(unit, sources)
    if not basis:
        return {"verdict": "unsupported", "confidence": None, "probs": {}, "escalates": None,
                "adds_specifics": None, "basis": [], "judged": False, "escalate": True,
                "note": "no source could be matched"}
    a = gw.jev(state, QUESTIONS)
    v = a.get("verdict") or {}
    out = {
        "verdict": v.get("choice"), "confidence": v.get("confidence"), "probs": v.get("probabilities") or {},
        "escalates": (a.get("escalates") or {}).get("noul"),
        "adds_specifics": (a.get("adds_specifics") or {}).get("noul"),
        "basis": basis, "judged": True,
    }
    out["escalate"] = unit.section in METRIC_SECTIONS and bool(
        out["verdict"] not in ("supported", "analysis")
        or (out["confidence"] is not None and out["confidence"] < 0.5)
        or (out["escalates"] or 0) > 0.6
        or any(f.startswith(ESCALATE_FLAGS) for f in flags)
    )
    return out


def judge_units(gw: Gateway, units: list[Unit], sources: dict[int, Source],
                flags: dict[int, list[str]], workers: int = 8) -> dict[int, dict]:
    """idx -> verdict dict. A budget stop halts cleanly; other failures are
    recorded per sentence (judged=False) rather than guessed."""
    results: dict[int, dict] = {}

    def one(u: Unit):
        try:
            return u.idx, judge_unit(gw, u, sources, flags.get(u.idx, []))
        except BudgetExceeded:
            raise
        except JudgeError as e:
            return u.idx, {"verdict": None, "judged": False, "escalate": True, "note": str(e)[:200]}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for idx, res in pool.map(one, units):
            results[idx] = res
    return results
