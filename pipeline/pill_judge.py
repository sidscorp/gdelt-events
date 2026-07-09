"""LLM relevance judge for pill membership (the precision layer).

Candidates (keyword hits, FDA name matches, loose semantic net) are judged
once by cerebras-fast before being tagged: batched 20 articles/call, strict
JSON verdicts. Reuses the intents + gateway client from pipeline.pill_eval —
the SAME judge that measures precision decides membership, so eval and
production can't drift apart.

Failure policy: if the gateway is down, candidates go UNJUDGED and are left
to the caller (incremental keeps keyword tags as-is: old behavior; backfill
skips them) — pills degrade to keyword quality, never break.
"""

import logging

try:
    from .pill_eval import PILL_INTENTS, _judge_call, _parse_verdicts
except ImportError:
    from pipeline.pill_eval import PILL_INTENTS, _judge_call, _parse_verdicts

log = logging.getLogger("pill_judge")

BATCH = 20
# Membership is lenient on 'borderline' (a topical-adjacent story in the pill
# is better than a missing one); 'irrelevant' is the only rejection.
ACCEPT = {"relevant", "borderline"}


def intent_for(category: str) -> str | None:
    base = category.replace("__v2", "")
    return PILL_INTENTS.get(base)


def judge(category: str, items: list[dict]) -> dict[str, str] | None:
    """items: [{url, title, desc}] -> {url: verdict}. None on total failure
    (caller falls back to keyword behavior). Partial batch failures are
    skipped (treated as unjudged: not in the returned dict)."""
    intent = intent_for(category)
    if not intent or not items:
        return {}
    out: dict[str, str] = {}
    failures = 0
    for i in range(0, len(items), BATCH):
        batch = items[i:i + BATCH]
        numbered = "\n".join(
            f"{j+1}. {a['title'][:200]}" + (f" — {a['desc'][:300]}" if a.get('desc') else "")
            for j, a in enumerate(batch)
        )
        prompt = (
            "You are the relevance gate for a news-topic feed. The topic is:\n"
            f"\"{intent}\"\n\n"
            "For EACH numbered article below, judge whether it belongs in that feed.\n"
            "verdict must be one of: relevant | borderline | irrelevant.\n"
            "Judge by the article's actual subject, not by shared keywords. "
            "Reject listing/quote/profile pages that are not news articles.\n\n"
            f"Articles:\n{numbered}\n\n"
            "Output ONLY a JSON array (no prose, no code fence), one element per "
            'article: {"n": <number>, "verdict": "...", "reason": "<10 words max>"}'
        )
        try:
            verdicts = _parse_verdicts(_judge_call(prompt), len(batch))
            for v in verdicts:
                idx = (v.get("n") or 0) - 1
                if 0 <= idx < len(batch):
                    out[batch[idx]["url"]] = v["verdict"]
        except Exception as e:
            failures += 1
            log.warning("judge batch failed for %s: %s", category, e)
            if failures >= 3 and not out:
                return None  # gateway likely down — signal total failure
    return out
