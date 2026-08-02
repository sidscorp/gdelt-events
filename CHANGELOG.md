# Changelog

Every change that reaches production gets an entry here **before** it is promoted.

The point of this file is the **Why**. Git already records *what* changed; what it
loses is the reasoning — the symptom that started it, the thing we measured, the
approach we rejected. Six months from now that context is the whole value.

Format for each entry:

```
## YYYY-MM-DD — Short title
**What** — the change, in one or two sentences.
**Why** — the symptom or goal. What was wrong, how it showed up, who noticed.
**How it was verified** — the actual check that was run, with numbers where there are numbers.
**Files** — paths touched.
**Notes** — rejected alternatives, follow-ups, anything that would surprise the next reader.
```

Newest first.

---

## 2026-08-02 — Briefing markdown renderer rewrite

**What** — Replaced the AI briefing's markdown rendering. `renderMd` in
`static/js/markdown.js` is now a block parser emitting bare semantic tags, fed by a
`normalizeBriefingText` pass and a streaming-aware `trimOpenMarkup`. The server-side
first paint (`routes/pages.py::_briefing_html`) was rewritten to produce byte-identical
HTML to the client, and the same normalization now runs at cache-write time in
`briefing.py::_normalize_text`.

**Why** — Sidd reported the briefing "does not look right, and it is not consistent —
when it loads I see the asterisks, then it updates, and sometimes it's normal and other
times it isn't." Four independent causes, all real:

1. **Two renderers disagreed.** `_briefing_html` (SSR, first paint) ran `html.escape()`
   with no inline markdown at all, so the first thing on screen showed literal `**bold**`
   and unlinked `[3]`; `markdown.js` then re-rendered it correctly a beat later. This was
   the "asterisks first, then it updates."
2. **The model cites two different ways.** Observed live: the 24h briefing emitted `[3]`,
   the 3h and 7d briefings emitted `【3】` (fullwidth CJK brackets). `linkifyCitations`
   only matched `[N]`, so `【3】` fell through as raw text in a CJK fallback font — which
   is where the wide gap before the sentence period came from. Same run-to-run variance
   produced U+202F narrow spaces ("Charli XCX") and U+2011 non-breaking hyphens.
3. **Double bullet markers.** `renderMd` inline-styled its `<ul>` with `list-style:disc`,
   and an inline style beats the stylesheet's `.briefing-body ul { list-style: none }` —
   so every bullet drew a disc *and* the orange `▸` from `li::before`.
4. **Raw `**` while streaming.** `renderMd` ran on partial text on every SSE chunk, so a
   half-arrived `**at least nine` painted literal asterisks until its closer landed.

**How it was verified** — Against two real cached briefings (3h and 7d/24h) pulled from
the live API:
- Simulated the SSE stream character by character and counted frames where raw markdown
  syntax was visible to the reader: **1636 of 1969 frames before, 0 after** (and 0 of 1662
  on the second briefing). Measured both in Node and in the live page.
- Diffed the Python SSR output against the JS output for both briefings: **byte-identical**,
  so there is no reflow after hydration.
- Visual check on dev (:8016) and on prod in dark and light themes.
- `tests/smoke.sh` 19/20 — the one failure (`gal language=en`) is ingest lag, not this
  change: the 1h window returns 0 with no filters at all, latest GDELT data was 56m old.

**Files** — `dashboard/static/js/markdown.js`, `dashboard/routes/pages.py`,
`dashboard/briefing.py`, `dashboard/static/css/dashboard.css`,
`dashboard/templates/index.html` (asset `?v=19`), `dashboard/static/sw.js`
(`gdelt-shell-v19`), `dashboard/templates/methodology.html`.

**Notes**
- The normalization is deliberately duplicated in Python and JS rather than done in only
  one place. It has to run in both: streamed chunks never pass through
  `_normalize_briefing` (that only runs at cache-write), and cached replays never pass
  through the stream path. The two are marked as mirrors of each other in comments —
  **if you change one, change the other.**
- `trimOpenMarkup` runs two passes because a closing `**` arrives one character at a time;
  removing the unmatched opener on the frame where only the first `*` has landed leaves a
  stray `*` behind. Nine frames leaked before the second pass was added.
- Also corrected two stale claims on the public `/methodology` page found while checking
  it: it said briefings go to "GLM-4.7 on Cerebras" (actually `gpt-oss-120b`) and were
  "cached for 45 minutes" (actually 60, `BRIEFING_TTL_S = 3600`).
- Not changed: briefing *wording* or prompt. This is presentation only.
