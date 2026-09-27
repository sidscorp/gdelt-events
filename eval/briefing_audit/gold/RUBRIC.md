# Gold label rubric (v2, 2026-09-27)

This is how every labeller, human or agent, labels a sentence for `claims.jsonl`.
The question is only whether the briefing said what its sources said. Judge against
the headlines and descriptions provided, not against world knowledge.

## Labels

| label | meaning |
|---|---|
| supported | The sentence reports an event of its own, and every factual detail is in the sources with the same certainty. Wording can differ. |
| overstated | The sentence reports a sourced event but raises its certainty or size. What the source calls signalled, proposed, planned, expected, alleged, possible or "may" is stated as definite, announced or done. **Turning speculation into an *announced* possibility still counts** ("may cut" → "announced a potential cut"). |
| unsupported | The sentence adds a specific detail no source mentions: a name, number, event, cause, or a superlative ("first", "largest", "world's most critical"). |
| contradicted | A fact in the sentence conflicts with the sources. This includes misstating what a number refers to (investment proposals worth $12B → "a $12B incentive"). |
| analysis | The sentence **asserts no event of its own**. It only interprets, predicts, or links and evaluates sourced stories ("could", "if", "underscores", "dovetails with"). |
| exclude | A heading or parser artifact. |

## Rules

**Tie-breaks**
- When a sentence has more than one problem, use the most severe label: contradicted, then unsupported, then overstated.
- A sourced fact combined with an invented specific is unsupported.

**Scope**
- Judge only against the sources listed with the sentence.
- Never give credit for a detail that is "probably elsewhere in the briefing".
- For an uncited sentence, if none of the listed closest matches supports it, the label is unsupported.

**Superlatives and descriptors**
- Unsupported, severity *minor*, even when they are common knowledge.

**Ambient colour vs invention**
- An unsourced interpretive mechanism on a sourced fact is still supported
  ("reinforcing optimism for memory supply amid AI-driven data growth").
- Unsupported is reserved for *checkable* inventions: a name, number, event, actor/action, cause or superlative.

**Opposite impression = contradicted**
- Asserting the opposite on the same dimension as the source is contradicted
  (source "operated stably" → "exposed the fragility"), like "higher" → "slipped".

**Stock consequence phrases** ("prompting calls for…", "prompting reassessments of…")
- They invent actors performing an action → unsupported, severity *minor*
  (major only if the invented reaction is the story).

**Severity**
- *minor*: a small add-on that doesn't change the story.
- *major*: changes what a reader would believe happened.
- **Headline metric = major problems only.** The all-problems rate is secondary, so minor-rate drift stays visible.

## Provenance
- **Claude (first labeller):** labelled all 50 with judge verdicts hidden.
- **review_2 (second agent):** labelled all 50 blind.
- **Agreement:** exact 74%, problem vs not 88% (44/50).
- **Adjudication:** 8 labels were moved to review_2's view. The 3 disputed labels were resolved on 2026-09-27
  (review_2's ruling, relayed by Sidd). A `sidd` value on any record still overrides.
