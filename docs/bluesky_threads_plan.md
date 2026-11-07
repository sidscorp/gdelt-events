# Bluesky thread enrichment — build plan

Status: PLAN, signed off in principle 2026-11-06. No code yet.
Related: `docs/bluesky_setup.md` (account + credentials), CHANGELOG entries
2026-11-05 (card titles), 2026-11-06 (auto mode).

## Goal

Every published post becomes a short thread instead of a single shot:

1. **Root post** (unchanged): header + grounded explanation + evidence link,
   generated card title.
2. **Reply 1 — sources & coverage** (deterministic, no LLM).
3. **Reply 2 — context** (LLM, grounded to the same standard as the body).

Both replies ship for auto-approved and human-approved posts. Each reply is
independently skippable: context failure never blocks sources, and a reply
failure must never fail the root post (log and move on; `social_publications`
gains nullable `sources_reply_uri` / `context_reply_uri` columns — additive
only, no schema migration beyond `ALTER TABLE ADD COLUMN`).

## Item 1 — sources & coverage reply (deterministic)

Data already stored per candidate: up to 12 deduped member outlets
(`members_json`: outlet, domain, url, title, desc), `source_count`,
`first_seen`/`latest_seen`, `coverage_span_hours`, `record_uri` + `record_cid`
(captured at publish by `mark_published`).

Format (≤300 chars, plain):

```
4 sources: Washington Examiner, KRCG, NPR, Ammon News
First seen 07:46 UTC · coverage span 19h
All variants: https://gdeltmonitor.com/event/<cluster_id>?src=bsky
```

Rules: outlets listed by crawl recency, max 4 named then "+N more"; no
predictive language possible by construction; link facet on the event URL.
Implementation surface: `publish_candidate` in `pipeline/social_publisher.py`
— after a successful `createRecord`, build the reply record with
`reply: {root: {uri,cid}, parent: {uri,cid}}` (TID rkey derived from the
parent rkey + suffix, e.g. same `_tid_for` seed + `"src"`). Cost: zero.
Rate-limit math: 2 records/post × 5 posts/day — negligible.

## Item 2 — context reply (LLM, grounded)

One or two sentences of *detail the body didn't have room for*: key numbers,
what officials said, immediate background. Prompt over the same ≤12 deduped
member snippets already used for the explanation, then the SAME verification
ladder the title work built (`_deterministic_grounding` + joint LLM verify,
banned-word list, numbers-must-appear-in-sources). Falls back to *no context
reply* (not to ungrounded text) — the thread just stops at reply 1.

Constraints: ≤250 chars (reply 1 reserves none of the 300); no claims absent
from snippets; no predictive/advisory language; store the grounding record in
the candidate's `grounding_json` under a `"context"` key for auditability.
Cost: 1 draft + 1 verify call per published post ≈ $0.0005 → ≤ $0.08/mo at
the 5/day cap.

Order of work: item 1 first (pure formatting, zero hallucination surface),
item 2 behind it sharing the reply helper.

## Acceptance

- Forced-failure test: gateway down → root + sources reply still ship.
- Grounding unit tests: context with an unsupported number is rejected.
- Cost measured on 5 real posts before enabling for auto mode.
- Bluesky thread renders correctly (reply nesting, facets) on a human-approved
  post before auto mode gets it.

---

## Phase 3 (deferred, ambitious): crawling source content

Idea: fetch the actual articles behind a cluster for much richer grounding
and context, storing what we learn.

**Honest assessment — park it as a bounded experiment, not a pipeline.**

- **What it buys:** full-text grounding (today: ~300-char GDELT descriptions),
  genuinely richer context replies.
- **What it costs:**
  - *Paywalls/blocks:* a large fraction of outlets meter or 403 bots; GDELT's
    description field exists precisely because full text is hard. Expect
    maybe 50–70% fetch success.
  - *Extraction:* needs trafilatura-style boilerplate removal; another
    dependency and failure mode.
  - *Legal:* storing full article text is a copyright surface. Internal-only
    use (verification, never posted verbatim) keeps it defensible; no
    redistribution, no public display.
  - *LLM cost:* 3–5 full articles per candidate ≈ 20–40k input tokens vs
    ~2k today — 10–20× cost per candidate (~$0.005–0.01), still only
    ~$0.05/day at cap but a real jump for incremental quality.
  - *Politeness:* robots.txt respect, identified UA, per-domain rate caps —
    ~30–50 fetches/day is tiny but must be well-behaved by design.
- **The failure mode it does NOT fix:** today's post failures are editorial
  (overstatement, topic selection), not lack of source depth. Crawling
  improves the floor, not the judgment.
- **If we ever do it:** only for the context reply, only the top-3 sources,
  trafilatura + robots compliance + internal-only storage (new
  `social_source_cache` table, additive), and evaluate whether context
  quality measurably improves before any wider use.

Decision needed from Sidd before any crawling work: explicitly approve
fetching third-party article pages at all.
