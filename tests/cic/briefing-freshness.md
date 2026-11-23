# 071 — The briefing freshness line reports observations, not promises

Issue: #71 — Freshness: honest briefing labels, recorded attempt outcomes, 3h prewarm cadence

Target: `http://localhost:8016` (prod: `https://gdeltmonitor.com`)

Baseline before this shipped: a 3h briefing that was 3h–6h old rendered
`Generated 5h ago · Next update in any moment` — a promise the 5-runs-a-day schedule
could not keep (8-hour overnight gap, 3-hour TTL). This script goes green only when the
line reports what was observed instead.

Read-only. Nothing here writes, submits or changes state beyond ordinary page loads.

1. Load `{TARGET}/?hours=3` and wait for the briefing panel to appear.
   EXPECT: the line under the briefing begins `Updated <age> ago` (e.g. `Updated 12m ago`),
   followed by either `next ~HH:MM` (a 24-hour clock time in your local zone) or
   `update overdue` or `last attempt <age> ago failed (<code>)`.

2. Read that same line character by character for the word "moment".
   EXPECT: the word does not appear. `any moment`, `in any moment` and
   `Next update in any moment` are all failures.

3. Load `{TARGET}/?view=nih-news&hours=3` (a niche view whose cache is usually hours or
   days old; if this view has no cached briefing at all and the panel is hidden, use any
   view whose briefing is visibly older than its window).
   EXPECT: an age in hours or days, and either `update overdue` or
   `last attempt … failed (<code>)` naming a short code such as `provider_unavailable`,
   `timeout`, `busy` or `insufficient_articles`. No countdown, no promise.

4. Open `{TARGET}/api/briefing?hours=3` in a new tab.
   EXPECT: JSON containing `last_attempt_at`, `last_attempt_outcome`,
   `consecutive_failures` and `next_eligible_at`. `last_attempt_outcome` is one of
   `ok`, `skipped_fresh`, or a string starting `failed:`. On an instance that has never
   run a generation (a fresh dev slice) all four may be `null` / `0` — that is the
   "no observation yet" case, and step 1 must then show only the age.

5. Open `{TARGET}/api/briefing?hours=24`. If `"stale": false`, return to the page.
   EXPECT: `next_eligible_at` is a UTC timestamp later than `generated_at`, and the bar on
   step 1 for that same view shows `next ~` followed by that stamp converted to your local
   clock — not a countdown in minutes.

6. Wait 60–90 seconds on the page from step 1 without reloading.
   EXPECT: the age figure advances (e.g. `Updated 12m ago` → `Updated 13m ago`) and the
   line never changes into a promise. The 15-second ticker updates the age only.

FAIL if: the word "moment" appears in the line at any point; the line shows a countdown
(`Next update in ~3h`) instead of a clock time or a stated reason; a combo past its TTL
shows anything other than an age plus `update overdue` / `last attempt … failed (…)`; or
the API lacks any of the four fields.
