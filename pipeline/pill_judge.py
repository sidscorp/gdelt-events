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

import argparse
import json
import logging
from dataclasses import dataclass

try:
    from .pill_eval import (
        PILL_INTENTS,
        JudgeError,
        JudgeResponseError,
        _judge_call,
        _parse_verdicts,
        judge_preflight,
    )
except ImportError:
    from pipeline.pill_eval import (
        PILL_INTENTS,
        JudgeError,
        JudgeResponseError,
        _judge_call,
        _parse_verdicts,
        judge_preflight,
    )

log = logging.getLogger("pill_judge")

BATCH = 20
MIN_SPLIT = 5  # smallest half-batch retried after a malformed response
# Membership is lenient on 'borderline' (a topical-adjacent story in the pill
# is better than a missing one); 'irrelevant' is the only rejection.
ACCEPT = {"relevant", "borderline"}


@dataclass
class JudgeCircuitBreaker:
    """One breaker per scorer run; the first fatal failure stops later calls."""

    opened: bool = False
    failure_code: str | None = None
    failure_message: str | None = None
    calls: int = 0

    def trip(self, error: Exception) -> None:
        if self.opened:
            return
        self.opened = True
        self.failure_code = getattr(error, "code", "unexpected_error")
        self.failure_message = str(error) or error.__class__.__name__
        log.error(
            "pill judge circuit opened: code=%s message=%s",
            self.failure_code,
            self.failure_message,
        )


def preflight(breaker: JudgeCircuitBreaker) -> bool:
    """Validate local credentials without making a paid/network request."""
    if breaker.opened:
        return False
    try:
        source = judge_preflight()
    except JudgeError as exc:
        breaker.trip(exc)
        return False
    log.info("pill judge credential preflight passed: source=%s", source)
    return True


def intent_for(category: str) -> str | None:
    base = category.replace("__v2", "")
    return PILL_INTENTS.get(base)


def judge(category: str, items: list[dict],
          breaker: JudgeCircuitBreaker | None = None) -> dict[str, str] | None:
    """items: [{url, title, desc}] -> {url: verdict}. None on total failure
    (caller falls back to keyword behavior and holds the watermark)."""
    breaker = breaker or JudgeCircuitBreaker()
    if breaker.opened:
        return None
    intent = intent_for(category)
    if not intent or not items:
        return {}
    out: dict[str, str] = {}
    for i in range(0, len(items), BATCH):
        if not _judge_batch(intent, items[i:i + BATCH], breaker, out):
            return None
    return out


def _judge_batch(intent: str, batch: list[dict], breaker: JudgeCircuitBreaker,
                 out: dict[str, str]) -> bool:
    """Judge one batch into `out`. A malformed or truncated reply is retried as
    two halves (down to MIN_SPLIT) before the breaker trips: one odd batch must
    not halt the scorer, because a halt holds the watermark and the next cycle
    re-judges (and re-pays for) the whole chunk."""
    try:
        _judge_one(intent, batch, breaker, out)
        return True
    except JudgeResponseError as exc:
        if len(batch) >= 2 * MIN_SPLIT and not breaker.opened:
            log.warning("pill judge: %s on a batch of %d — retrying as two halves", exc, len(batch))
            mid = len(batch) // 2
            return (_judge_batch(intent, batch[:mid], breaker, out)
                    and _judge_batch(intent, batch[mid:], breaker, out))
        breaker.trip(exc)
        return False
    except JudgeError as exc:
        breaker.trip(exc)
        return False
    except Exception as exc:
        # Unexpected exceptions are fatal for this run too. Continuing
        # could advance the monotonic watermark past unjudged articles.
        breaker.trip(exc)
        return False


def _judge_one(intent: str, batch: list[dict], breaker: JudgeCircuitBreaker,
               out: dict[str, str]) -> None:
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
    breaker.calls += 1
    verdicts = _parse_verdicts(_judge_call(prompt), len(batch))
    seen: set[int] = set()
    got: dict[str, str] = {}
    for v in verdicts:
        idx = (v.get("n") or 0) - 1
        if 0 <= idx < len(batch):
            if idx in seen:
                raise JudgeResponseError(
                    "judge returned a duplicate article verdict"
                )
            seen.add(idx)
            got[batch[idx]["url"]] = v["verdict"]
    if len(seen) != len(batch):
        raise JudgeResponseError(
            f"judge mapped {len(seen)}/{len(batch)} article verdicts"
        )
    out.update(got)  # only a fully-mapped batch counts


def live_canary() -> dict:
    """Make one non-mutating live call that validates the complete contract."""
    breaker = JudgeCircuitBreaker()
    if not preflight(breaker):
        return {"ok": False, "failure_code": breaker.failure_code, "calls": 0}
    verdicts = judge(
        "ai_general",
        [{
            "url": "canary://pill-judge",
            "title": "Research laboratory releases a new artificial intelligence model",
            "desc": "The release describes model capabilities, training, and availability.",
        }],
        breaker=breaker,
    )
    if verdicts is None:
        return {
            "ok": False,
            "failure_code": breaker.failure_code,
            "calls": breaker.calls,
        }
    verdict = verdicts.get("canary://pill-judge")
    return {
        "ok": verdict in {"relevant", "borderline", "irrelevant"},
        "verdict": verdict,
        "calls": breaker.calls,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Pill judge health utilities")
    parser.add_argument(
        "--live-canary", action="store_true",
        help="make one paid, non-mutating gateway call",
    )
    args = parser.parse_args()
    if not args.live_canary:
        parser.error("choose --live-canary")
    result = live_canary()
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
