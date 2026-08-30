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

## 2026-08-30 — Repair a cp1252/UTF-8 double-encode in index.html and sw.js

**What** — The served homepage had been rendering "âœ¦ AI Briefing", "·", "…", "–", "●" etc. as
mojibake glyphs. 15 corrupted runs in `dashboard/templates/index.html` and 2 comment lines in
`dashboard/static/sw.js` repaired to the intended characters; cache bumped v29→v30 so clients
pick up a clean shell.

**Why** — The site publicly read "âœ¦ AI Briefing" to every visitor. Root cause: 955f727's edits
passed the files through a cp1252-reading step (the PowerShell 5.1 transcoding trap, now
documented in CHANGELOG as at least the second time this exact failure class has shipped).
Everything else in that commit (prewarm fix, in-paint progress row) is untouched — this commit
is byte-repair only.

**How it was verified** — `git show e48d8cf:…index.html` clean vs `955f727:…index.html`
corrupted pins the introduction to the latter; an exact-mapping repair script was applied to
fetched working copies (diff before/after shows only the suspect sequences); post-deploy curl of
the live page shows the label decodes to ✦ and a residual-mojibake scan returns zero markers on
both files. The other six files touched since e48d8cf scanned clean and were not modified.

**Files** — dashboard/templates/index.html, dashboard/static/sw.js, CHANGELOG.md.

**Notes** — same-class mitigation worth considering later: a one-liner `content check` in
deploy_guard or smoke.sh that asserts the label decodes (the 200-OK-with-mojibake failure passed
smoke 20/20 this morning, because every smoke assertion is API-JSON, not rendered text).

---

## 2026-08-29 (later still) - Briefing prewarm: fix an inverted demand signal, and say what is happening while a briefing is written

**What** - Two changes, one cost and one UX.
(1) `prewarm_briefings.py` now picks its combo list from `pageview_log` (what people look at),
ranked by DISTINCT VISITORS, instead of from `briefing_history` (what got generated). It warms
<=12 combos with >=2 distinct visitors in 14 days, down from the 29 it was walking.
(2) When a briefing has to be written live, the panel now says so: a labelled progress row with a
running seconds counter, and one line explaining that only the most-read views are written ahead
of time and this one is made on demand to keep running costs down.

**Why** - **The demand signal was inverted.** A `briefing_history` row is written only when a
briefing is GENERATED; a visit that hits a warm cache writes nothing. So every combo the
prewarmer successfully kept warm produced no `trigger='visit'` rows and looked like zero demand,
while combos nobody reads missed cache, wrote a visit row, and looked like demand. The list was
selecting on cache misses, which anti-correlate with popularity. It was walking 29 combos and
regenerating 13 per run, 5 runs/day = ~59 generations/day, against **27 human briefing reads per
week**. Measured over 7 days: 411 prewarm generations served 27 visits.

This also corrects PLAN-100X's cost model. Briefings, not the judge, are the dominant gdelt cost:
judge 16,739 judgements @20/call = ~836 calls = ~$0.97 (31%); briefings 438 generations x ~2.94
calls = ~1,287 calls = ~$2.13 (**69%**) over the same 7 days. gdelt total is $0.443/day.

**How it was verified** - Combo selection run against live prod usage: 12 combos, `_all:3` first,
versus 29 before. Frontend built and exercised on **dev :8016** (possible for the first time -
the dev slice rebuild was fixed earlier today): assets serve the new code, a live generation of an
uncached combo (`medical-devices:24`) returned HTTP 200 with a complete briefing. `smoke.sh`
against dev scored 19/20, the one failure being the documented dev-slice aging trap (`hours=1`
against a static snapshot rebuilt 2h earlier - it was 20/20 immediately after the rebuild, and
`language` values are present and proportional in the slice). Prod control: 20/20 before and
after.

**Files** - `pipeline/prewarm_briefings.py`, `dashboard/static/js/markdown.js`,
`dashboard/static/css/dashboard.css`, asset versions in the templates, `sw.js` CACHE bump.

**Notes** -

*Demand is ranked by distinct visitors, not views, and that changes the answer.*
`geopolitics-conflict:3` has 88 views but only **2 distinct visitors** - one enthusiast or
crawler, not breadth. `_all:3` has 495 views from **306 visitors**: 68.6% of all pageviews. The
next-broadest key has 5 visitors. Ranking on raw views would have warmed one person's habit above
views that many different people open.

*The measurement window is 18 days, not 30.* `pageview_log` starts 2026-08-12. Every percentage
above is over 2026-08-12..08-30 (723 views). Worth re-checking once there is a full month.

*The progress panel is deliberately delayed 700ms, and deliberately stays past first token.*
Both numbers come from `perf_samples`, not taste. A cached briefing lands at p50 0.2s / p90 0.8s
(n=872), so painting the state immediately would flash "writing this briefing now" on nearly
every warm load and read as slowness rather than transparency. A live generation shows its first
text at p50 0.34s but does not finish until p50 2.9s / p90 10.8s (n=379), so a note that
disappears at first token is a note nobody reads - it stays until `done`.

*Reduced-motion users get a steady dot, not a frozen ring.* A spinner with its animation removed
reads as a bullet point and implies nothing is happening.

**Not done here, and deliberately**: the judge-by-cluster item from PLAN-100X Phase 4 was
**killed on measurement** and is written up in the plan. Clustering covers 0.77% of crawled
articles (it is a near-duplicate detector for syndicated stories; minimum cluster size 2,
singletons never stored). Only 516 of 13,836 judged urls (3.7%) are clustered, spanning 217
clusters, so cluster-inheritance would cut judge calls by **2.2%**, not the ~70% assumed - a
ceiling of about $0.003/day. Do not build it.

## 2026-08-29 (later) - Phase 0: the dev slice can be rebuilt again, and the sec-tracker 404 loop is identified

**What** - Two open wounds from PLAN-100X Phase 0.
(1) Added the missing `scripts/build_dev_snapshot.py`. `rebuild_dev_slice.ps1` has invoked it
since it was written, but the script was never committed and existed nowhere on disk, so every
rebuild failed rc=2 and the dev slice sat frozen at 2026-07-19..07-26.
(2) Identified what actually produces the endless `GET /api/watchlist/status` 404s on
sec-tracker. **It is not the frontend.** See below - the fix is a one-line kill and is pending
Sidd's go-ahead, so this item is diagnosed, not yet closed.

**Why** - The dev slice being five weeks stale made dev-first verification worthless: any
short-window assertion (`hours=1`, `hours=24`) fails against a stale snapshot regardless of the
code under test, so the instruction to "test on :8016 first" could not be followed. It already
cost the entity-spine work (2026-08-29 earlier entry), which had to be built with dry runs
against prod instead.

**How it was verified** - `rebuild_dev_slice.ps1` end to end: rc=0 in 19s, dev dashboard came
back healthy on :8016. Slice window moved from 2026-07-19..07-26 to
**2026-08-22 19:46 .. 2026-08-29 22:47**, latest article 53 minutes old, 1,818,352 rows total
(gal 782,451 / gal_recent 782,451 / gkg 140,024 / article_tags 29,384 / clusters 1,836 /
cluster_members 5,774 + the reference tables whole). `tests/smoke.sh` run from the Mac against
`BASE=http://rainbow-boi:8016`: **20/20**. Prod smoke re-run: 20/20.

**Files** - `scripts/build_dev_snapshot.py` (new).

**Notes** -

*The slice is keyed on `crawled_at`, not `published_at`.* `gal.published_at` holds values from
2025 through 2026-12 because publishers lie in their metadata, so slicing on it yields an
incoherent window with a random tail. `crawled_at` is our own ingest clock. This is also what
`gal_recent` is really maintained on - it holds ~8 days of crawls while its own `published_at`
range spans 2025-08..2026-12 - so keying on `crawled_at` reproduces prod's true shape, garbage
publication timestamps included, which is the point of a fidelity slice.

*`gal_recent` was missing from the old dev slice and that quietly invalidated dev testing.*
`dashboard/articles.py::_has_gal_recent` routes around the table when it is absent, so dev was
exercising a **different query path than prod** - a dev-first check could pass on code that
fails in production. It is copied now.

*`events` (3.6M rows) and `mentions` (9.7M rows) are deliberately not copied.* Neither table is
read by anything: there is no `FROM events` or `FROM mentions` anywhere in `dashboard/` or
`pipeline/`. They are ingested and never queried. Copying them would several-times the rebuild
for data nothing looks at. Worth a separate look at whether they should still be ingested.

*The slice is built into a new file and swapped, not rebuilt in place.* DuckDB does not return
freed pages to the OS, so `CREATE OR REPLACE` in place grows the dev database on every run. The
build also refuses to swap in a slice where any of gal/gal_recent/gkg/article_tags/fda_companies
came out empty, and leaves the previous database at `gdelt.duckdb.prev`. A dropped SSH mid-build
therefore leaves the working dev database untouched.

*Second phantom reference, not fixed.* `rebuild_dev_slice.ps1`'s header points at
`scripts/kick_rebuild.ps1` for the one-shot-task pattern. That file does not exist either. The
rebuild is short enough (19s) that running it directly over SSH is fine, so this is recorded
rather than fixed.

*The sec-tracker 404 loop: the documented diagnosis is wrong.* PLAN-100X and the skill doc both
say "frontend 5s-polls `/api/watchlist/status` -> 404 forever". The frontend does no such thing:
there is no reference to `watchlist` and no `setInterval` anywhere in
`~/projects/sec-tracker/frontend/src` or in the built `dist` bundle, and nginx has logged exactly
one request for that path ever (24/May/2026, from curl). A `tcpdump` on loopback shows the real
caller is `User-Agent: curl/8.5.0` against `localhost:8111`. It is **PID 2093467, parent PID 1,
uptime 96 days 23 hours** - an orphaned shell loop:

    zsh -c until curl -s 'http://localhost:8111/api/watchlist/status' |
      python3 -c '...exit(0 if d.get("warm_status") == "idle" else 1)'; do sleep 5; done

An agent session on 2026-05-24 launched it to wait for a cache warm, the endpoint 404s so the
JSON parse never yields `warm_status == "idle"`, the loop's exit condition is unreachable, and it
has re-polled every 5 seconds ever since - on the order of 1.7 million requests. It was
reparented to init when that session ended. The correct fix was to kill PID 2093467; implementing
the endpoint would merely have fed a runaway loop that nothing was waiting on.

**CLOSED 2026-08-29 20:13** - Sidd ran the kill. Verified: `ps -p 2093467` gone, and
`journalctl -u sec-tracker --since '60 sec ago' | grep -c watchlist/status` = **0**, against a
steady 12/min before. (A first check 90 seconds after the kill still counted 13 hits and briefly
looked like a second poller; that window straddled the kill. The 60-second window after it is
clean.) No endpoint was added and no sec-tracker code changed - the 404s had exactly one cause
and it was not in the application.

This is the same class as the documented "ssh-spawned processes survive as unkillable orphans"
gotcha, which until now was recorded only against rainbow-boi. It happens on snambiar-linux too,
and an orphan can outlive the session that made it by months without anyone noticing, because
the only symptom is log noise on a service nobody was watching.

## 2026-08-29 - Entity spine: entity_registry + tiered alias table (PLAN-100X Phase 1)

**What** - New `pipeline/entity_registry.py` builds two tables in `data/gdelt.duckdb`:
`entity_registry` (one row per real-world company, fused from SEC filers + FDA establishments)
and `entity_alias` (news-side organisation strings mapped onto those entities, each row carrying
its match tier, an `auto_join` flag and a confidence). Plus `entity_build_log` (append-only build
reports). Offline build, ~42s over the full 2.74M-row GKG scan. **Nothing reads these tables
yet** - no dashboard, API or briefing code was touched. This is the spine only.

**Why** - The news<->company link was `fda_match_cache` name matching, whose `stripped` tier is
structurally noisy (UNESCO->Olympus, 'Patterson' the flooring firm). An attempted `fda_match`
view resurrection was rejected on 2026-08-28 for exactly that reason. Every planned surface -
follow loops, company pages, register-aware briefings - needs a company link it can trust, so
the link gets built and audited once, on its own, instead of being re-derived per feature.

**How it was verified** - Full build, read-only compute + one write burst, dashboard left
serving throughout. Coverage: 17,966 SEC filers + 8,594 FDA establishments -> 26,081 entities,
of which 260 carry both an SEC CIK and an FDA owner/operator number (299 establishments fused;
some filers own several). 8,649 entities pass the liveness test. GKG scan: 2,744,936 rows,
1,065,804 distinct normalised organisation strings -> 5,479 auto-join news edges
(3,832 T1_NAME_DISTINCT, 1,642 T1_NAME_SINGLE, 5 T2_SUCCESSOR) and 331 refused as
T3_DEAD_FILER.

Hand audit of 200 auto-join news edges drawn uniformly from that population (seed 1, identity
aliases excluded), adjudicated by hand against name/ticker/exchange/SIC - full adjudication in
`docs/entity-spine-audit-2026-08-29.md`. Two measures, deliberately not conflated:
**link precision (does the alias denote this entity) 198/200 = 98.5%, which clears the >=97%
gate**; alias ambiguity (right entity, but a share of mentions are about something else) 8/200 =
4.0%, carried into Phase 2. The 3 wrong links were `SU` -> SU Group Holdings (a 2-character
acronym fragment), `DOVER` -> Dover Corp (the town dominates the mentions), and
`GLOBAL ENTERTAINMENT` -> a shell filer. Two guards were added *after* that measurement, so the
98.5% is not circular: `MIN_SINGLE_TOKEN_LEN = 3` (drops 214 one- and two-character org strings;
the correct 3-character names RXO, DHT, IDT are unaffected) and a 3-entry `ALIAS_BLOCKLIST` with
the reason recorded inline per entry. The shipped build therefore has 5,464 auto-join news edges
(3,831 T1_NAME_DISTINCT, 1,628 T1_NAME_SINGLE, 5 T2_SUCCESSOR), 326 refused as T3_DEAD_FILER,
214 refused as too short, 3 blocklisted. Prod unaffected throughout: `tests/smoke.sh` run from
the Mac after the build, 20/20.

**Files** - `pipeline/entity_registry.py` (new). New tables `entity_registry`, `entity_alias`,
`entity_build_log` in `data/gdelt.duckdb`. No existing file modified.

**Notes** -

*The plan's domain tiers are not implementable and the spec was corrected.* PLAN-100X specifies
"T1 exact normalised legal name **or shared domain**" and "T2 suffix-stripped **+ shared
domain**". No company-domain data exists anywhere in the fleet: SEC `companies` is
(cik, ticker, name, updated_at, sic, sic_description, exchange, fiscal_year_end); `fda_companies`
is (owner_operator_number, firm_name, site_count, product_count, device_classes,
medical_specialties); GKG's domain columns are the *publisher's* domain, not the subject
company's. T2-as-written can never fire. The `domains` column is kept in the schema, empty, for
the day a source appears - do not populate it from GKG.

*The replacement discriminator is a liveness test, not a sector test.* A single-token name
auto-joins only if its entity is currently exchange-listed OR has an SEC filing period on/after
2025-01-01. Both halves are load-bearing: recency alone drops foreign private issuers (they file
20-F, so they have no `snapshots` rows at all - Shell, Prudential, Canon, Shimadzu, Sysmex,
Nihon Kohden); exchange alone keeps dead shells that still carry a ticker (MORGAN GROUP HOLDING
CO, MGHL, last filed 2023). Validated against the false joins found by hand: it rejects TESCO
CORP (the mentions are the UK grocer, not an SEC filer at all), MORGAN GROUP HOLDING CO (the
mentions are JPMorgan/Morgan Stanley), "Alphabet Holding Company, Inc." (a shell, not Google's
Alphabet - the test also picks the right Alphabet from the collision), Aurum Inc. and MERIDIAN
CO LTD, while keeping Apple, Oracle, Chevron, Visa, Shell and Canon.

*Rejected, with numbers - do not retry sector/SIC compatibility as a discriminator.* It was the
obvious substitute for the missing domain signal. Of the 260 SEC<->FDA joins, ~26 pair a
non-medical SIC with an FDA establishment and every one inspected is correct: Sony, Ricoh,
Shimadzu, Sysmex, Stericycle, Sharps Compliance, TE Connectivity, TD SYNNEX, Sanmina, Plexus,
MOOG, Procter & Gamble, Thermo Fisher, Stratasys, Nihon Kohden, Ottobock, Sectra, Teladoc,
Tempus AI, Varex, Nortech, Omnicell, Theragenics, Senseonics, Response Biomedical, Synergetics.
Industrials and electronics firms are legitimately FDA-registered (medical displays, 3D-printed
healthcare parts, sterilisation services, contract manufacture). A sector gate would discard
~10% of the correct joins to catch noise that is not there.

*Known recall cost, deliberately taken.* The liveness test blocks 331 news edges whose only SEC
row is a dead filer. Some of those mentions are real and high-volume - GOOGLE is the single
largest organisation string in GKG (~4.7k mentions per 300k rows) and its SEC row, `GOOGLE INC.`,
last filed in 2015. A small hand-curated `SUCCESSORS` map in the module redirects the ones worth
redirecting (GOOGLE->GOOGL, LINKEDIN->MSFT, RAYTHEON->RTX, SPRINT->TMUS, TIME WARNER->WBD);
names whose successor is not an SEC filer (TWITTER/X, MONSANTO/Bayer, SEARS, SAFEWAY, MCAFEE,
DIRECTV) are listed there with a null target so the next reader knows they were considered and
left blocked. Extending that map is the cheapest available recall win.

*The ambiguity class is a real, measured limitation and Phase 2 must handle it.* No build-time
name rule can separate "shares fell on the Nasdaq" from "Nasdaq, Inc. reported earnings" - and
`NASDAQ` is the single largest alias in the corpus at 38,931 mentions. Mention-level
disambiguation (article context against the entity's sector) is an entry condition for the
follow loop: without it, a follower of NDAQ receives every market story on the site.

*Second identity gap, found while measuring, not yet closed.* `fda_regulatory_events` carries
only a free-text `firm_name` and no `owner_operator_number`, so the register does not join to
`fda_companies` by key. Exact normalised-name match covers 607 of its 1,237 distinct firms
(49.1%); only 83 (6.7%) reach an SEC filer. So `entity_registry.fda_firm_ids` links an entity to
the *establishment* list, not to the register entries a company page would show. Phase 3 needs
that second hop built and measured on its own.

*Dev-first was not possible for this one.* The dev slice is a static 7-day snapshot whose
rebuild is currently broken (`scripts/build_dev_snapshot.py` is referenced by
`rebuild_dev_slice.ps1` and absent from both trees - PLAN-100X Phase 0, still open), and a
partial GKG cannot validate a full-corpus entity build. Mitigation used instead: the build is
read-only until a single terminal write burst, it creates only new tables, and it was run twice
in `--dry-run` (300k sample, then the full corpus) before anything was written.

## 2026-08-28 (later) — FDA panel: interactive rows, canonical source links, honest footnote

**What** — Rows in "Recent FDA Actions" are now interactive: clicking a row expands it to the full
product description + reason (the 120-char clamp was revealed only via title-tooltip — dead on
touch). Firm names keep their org-filter jump with a real affordance ("Filter the news to this
company"). 510(k) rows link to the canonical FDA clearance summary page
(`accessdata.fda.gov/scripts/cdrh/cfdocs/cfpmn/pmn.cfm?ID=<K-number>`, reachability verified by
hand on K260563 → 200). Enforcement/recall records have no stable public URL (IRES does not
deep-link; fda.gov search sits behind a bot challenge), so they expand in place instead. Panel
gains a footnote: straight from FDA feeds, *not read by the AI briefing*, and how to interact.

**Why** — Sidd asked three questions on screenshot review: (1) does the briefing read the panel
data — no (briefing.py has zero FDA-register awareness; separate design decision deferred, the
footnote covers the expectation gap), (2) rows aren't clickable — now they are, (3) is
openfda-agent integrated — it is not; the two systems share only the gateway. No code change on
Q1/Q3 tonight: mixing register facts into the citation-indexed briefing is a bigger contract move
than a UI session wants.

**How it was verified** — dev :8016 for syntax/styling; prod re-loaded with live data: panel
shows (200), rows render badges/firms/dates; 510(k) anchor URL verified live. Smoke suite 20/20
re-run after the earlier deploy; unchanged files untouched.

**Files** — dashboard/static/js/markdown.js (v24), dashboard/static/css/dashboard.css (v21),
dashboard/templates/index.html (footnote), dashboard/static/sw.js (shell v27).

**Notes** — Second deploy of the evening inside the same lock discipline. The clickable-reason
rows deliberately do not use `<details>` to keep the flex row layout.

---
## 2026-08-28 — SEC charts you can actually read, the FDA panel back from the dead, and honest hosting claims

**What** —
*SEC page*: every bar now carries its own value printed above it (no hover needed — values were
tooltip-only, which does not exist on touch), every bar is period-labelled (`Jun 26`, not
every-other one), the zero baseline is always drawn, a dotted guide marks the window high with
its figure, and each chart gets one computed takeaway sentence underneath ("Latest: $198M —
rising in 3 of the last 6 quarters"). The margin chart labels its floor, ceiling, and latest
point. New template functions `bars_takeaway`/`line_takeaway` in sec_explain keep the no-LLM
contract: every sentence is a template over stored numbers.
*FDA panel*: the "Recent FDA Actions" panel now opens on the **FDA agency pill view** (and any
future fda_match view). It was unreachable before: its JS gate required view kind `fda_match`,
which zero views have had since the July overhaul converted Medical Device Companies to a pill,
and even had a view qualified it could not show: `#fdaEventsPanel{display:none}` was never
overridden (`loadFdaEvents` set inline display to `''`, which falls right back to the stylesheet
rule — the panel could literally never have been visible since that CSS landed).
*Copy truth*: the footer said "self-hosted on personal hardware" while the briefing/judge LLM
runs on Fireworks. Footer now says pipeline & site run on personal hardware, AI writing by a
hosted open-weights model; About/Methodology attribute the model host (Fireworks) separately
from our self-hosted gateway. Our own GPU claim stays scoped to embeddings, which it is
actually true of.

**Why** — Sidd's screenshots said it: the SEC trend charts had no axes, no values, and an
illegible every-other-tick label — pure Tufte, zero comprehension. The FDA side was invisible
despite the feature existing (see the two-layer hide above). And the self-hosted claim was
false advertising the moment briefings left the building.

**How it was verified** — On the dev instance (:8016): tests/test_sec_explain.py 18/18 pass
incl. 2 new takeaway tests and the byte-identical-copies guard; /sec-analysis?ticker=BAH
renders 16 value labels, guides, and 3 takeaway sentences (screenshot-reviewed); the FDA view
shows the panel (empty-state text on the dev slice, whose fda_regulatory_events is empty);
footer renders the new copy; /api/views intact. All files fresh-fetched from prod and diffed
pre-deploy.

**Files** — dashboard/routes/sec_analysis.py, dashboard/templates/sec_analysis.html,
dashboard/static/css/sec.css, dashboard/sec_explain.py, pipeline/sec_explain.py,
tests/test_sec_explain.py, dashboard/static/js/dashboard.js, dashboard/static/js/markdown.js,
dashboard/templates/index.html (footer + asset versions), dashboard/templates/about.html,
dashboard/templates/methodology.html, dashboard/static/sw.js (cache v24→v25).

**Notes** — A wider FDA Companies resurrection (a new `kind=fda_match` view) was built and
rejected in dev: the name-match feed returned UNESCO-Olympus/Alma-Center/Patterson-flooring
noise (common-word firm collisions), the GKG cache branch ignores `match_types` entirely, and
`matched_name` never surfaces into the API payload, so the "FDA co." badge can't render.
Documents as the follow-up: to revive that view properly, constrain the GKG branch by match
type, surface matched_name on the unified feed, and gate stripped names semantically. sw.js
shell-cache only serves `/`; sec-analysis/methodology/about are not shell-cached, so only the
index shell needed the cache bump.

---

## 2026-08-26 — Briefings get an editor, and say who they passed over

**What** — The briefing is now two LLM calls. An **editor** reads 40 candidates (up from 12)
and picks ~10, recording one line on why each was chosen and why anything that is not a news
article was rejected; a **writer** then composes from that selection. Briefings roughly doubled
in length (3-5 sentence lede, 8-12 two-sentence highlights, a "What to watch" paragraph and a
"Quieter but notable" pick). The ⓘ panel shows the whole selection — chosen, rejected, and
considered-but-not-selected. Story threads were unfrozen and re-keyed per view. New
`pipeline/textfilters.py` drops pages that are not articles at all.

**Why** — Selection was pure arithmetic: the Importance score took the top 12 and the model
narrated them in order. There was no editorial judgement anywhere in the product, and on the
global view slots 7-12 were routinely a section index ("Food and drink - Hull Live"), a job
posting, and an evergreen ETF listicle — all visible to any visitor through the ⓘ panel.

The 12 was itself a cost cut (50 → 20 → 12, commit `1aabb80`, "Cut briefing LLM spend ~30x").
That commit's savings came from four other changes — leaving Cerebras, demand-driven prewarm,
skipping thread updates, scaling freshness. Measured against seven real stored prompts a source
line is ~64 tokens, so 12 → 40 costs about **$0.24/month**. It was a bad trade made under
8×-worse pricing, and it starved the model of anything to choose between.

Deliberately NOT filtered: low-salience *real* news. A High Court ruling in Allahabad or a fatal
crash near Middlesbrough is a real event, and keeping such stories in the pool is what makes the
briefing worth reading twice. Only structural non-articles are hard-dropped; the editor is asked
to include at least one consequential but under-covered story.

Threads were frozen: updates were switched off during prewarm in that same commit and organic
visits are rare, so several lists had not moved since 30 July while every prompt still described
them as "ongoing story threads you have been tracking". They now update on prewarm too, gated to
once per view per 3h and, for prewarm, only for views a human has actually opened in 30 days
(the same demand signal `prewarm_briefings.py` already uses). Re-keyed from `cache_key`
(view×hours) to `view_id`: a storyline belongs to a topic, not a time window, and one view was
carrying up to six divergent thread lists — 106 rows collapsed to 18.

**How it was verified** — End to end against live production data in a throwaway copy of the
dashboard (prod untouched, real data read-only):

- **Citation integrity**, the highest-risk regression: the writer cites *candidate* numbers, so
  `sources_json` keeps all 40 in 1..40 order. A generated briefing cited
  `[3,4,6,9,10,13,19,27,37,38]` — exactly the chosen set, non-consecutive, none out of range,
  each indexing back to itself. A renumbering slip here would point every citation at the wrong
  article while looking entirely normal.
- **Editor quality**: on Geopolitics it rejected an opinion column as "Opinion piece, no new
  event" and a sensational claim as "lacks verifiable event", while selecting Qatar-Iran
  mediation and a Hormuz shipping warning.
- **Fail-open**: with `_chat` monkeypatched to return junk, and separately to raise, selection
  falls back to the Importance top-10 and the briefing proceeds unchanged.
- **Thread migration**: 106 → 18 rows, one per view, deduped to each view's most recent list;
  re-running the migration is a no-op.
- **Junk filter**: 6,000 live titles, 0.92% dropped, and **0 of the top 300 clusters** — the
  population briefings actually draw from. Every drop was eyeballed.
- Output length landed at ~1,090 tokens, the intended ~2×. `tests/smoke.sh` 20/20.

**Files** — `pipeline/textfilters.py` (new), `pipeline/build_clusters.py`,
`dashboard/briefing.py`, `dashboard/routes/api_briefing.py`, `dashboard/articles.py`,
`dashboard/webutil.py`, `dashboard/models.py`, `dashboard/routes/pages.py`,
`dashboard/templates/methodology.html`, `dashboard/static/js/dashboard.js`,
`dashboard/static/sw.js`.

**Notes** — Measured cost: editor ~$0.00082, writer ~$0.00091, **~$0.0017/briefing ≈
$2.26/month** at ~1,300 generations, plus ~$0.59/month for threads. Budget was $2-3.

Two things learned the hard way, both worth remembering:

1. **`gpt-oss-120b` is a reasoning model and reasoning tokens are billed as output.** At
   `max_tokens=1800` the editor returned a **completely empty** response — not truncated, empty
   — because the budget was spent before the JSON began. It needs 4000. The writer's existing
   8000 carries the same note.
2. **Asking for a verdict on every candidate cost 3× more than asking only about the ones it
   acts on.** Emitting `chosen`/`not_news` only, with `reasoning_effort: "low"`, cut editor
   output from ~2,400-3,400 tokens to ~700 and brought the month from $4.03 to $2.26. The ⓘ
   panel still lists everything else as "considered but not selected" by difference.

Also fixed: the SSE path bound `generated_at` only inside `if briefing:` but reported it in the
terminal event, so a falsy normalization raised `UnboundLocalError` mid-stream and reached the
client as a silently truncated response. And the non-streaming path recorded a strictly smaller
`meta_json` than the SSE path, so prewarmed briefings — the ones most visitors see — had a
thinner ⓘ panel than human-triggered ones; both now write the full set.

`pipeline/textfilters.py` is stdlib-only on purpose: importing `build_clusters` into Flask would
run its module-level `logging.basicConfig` and reconfigure the root logger in the web process.

## 2026-08-26 — Measuring pill precision took the site down; the page reported numbers nobody had checked

**What** — `pill_eval` now opens a **read-only** DuckDB connection instead of a read-write one,
and its default pill list covers all 17 categories that have intents (seven were never
measured). `/methodology` now states precision measured from the newest `pill_eval` report,
injected via `_doc_facts()`, rather than hand-written prose.

**Why** — Two problems, one causing the other.

1. **The eval held the write lock through every LLM call.** `main()` opened
   `_open_connection(DB_PATH)` (read-write) and kept it for the whole run while `eval_pill`
   made judge calls taking minutes. DuckDB is single-writer, so measuring precision 503'd the
   live feed for the entire run — and the more pills you measured, the longer the site stayed
   down. This is exactly the failure the repo's own rule warns about ("never hold a write
   connection through LLM calls"). The eval only ever SELECTs, so read-only is correct.
   Verified during a 17-pill run: `/api/stats` answered in 0.11s throughout.

2. **The page's numbers were unverifiable and, once checked, wrong.** `/methodology` claimed
   "every pill measures roughly 75–94%". Those figures were hand-copied in July and survived
   the six weeks during which the judge was not running at all (see the 2026-08-22 entry). The
   first full measurement after the fix says otherwise:

   | | precision |
   |---|---|
   | Geopolitics & Conflict, FDA | 0.950 |
   | Energy & Climate | 0.912 |
   | Supply Chain | 0.725 (was 0.25 under keywords) |
   | **Cybersecurity** | **0.613** |
   | **AI Governance** | **0.600** |
   | **Semiconductors** | **0.562** |

   Range 56–95%, median 76% across 16 live pills. The claimed 75% floor was wrong for four
   pills. The page now publishes the real spread and names the three weakest, because a
   transparency page that flatters is worse than no page.

   Precision facts are now read from the newest report in `data/pill_eval/` at render time, so
   they cannot drift from what was last measured. Seven pills that had `PILL_INTENTS` but were
   missing from `DEFAULT_PILLS` — including `geopolitics_conflict`, `public_health` and
   `energy_climate`, three of the busiest — had never been measured once.

Also corrected on the page: it rendered `BRIEFING_MODEL` where it meant the **judge** model
(correct only by coincidence — both are `gpt-oss-120b` today, and it would have gone quietly
wrong the first time either was repointed); it described "Medical Device Companies" as a live
pill, which it has not been since `meddev_companies` was removed; and it implied judging is
instantaneous, when tagging runs at ingest and judging a cycle behind it, so the newest cards
can briefly show a `keyword` badge.

**How it was verified** — 17-pill eval run end to end while polling the live site (0.11s
responses throughout, no 503s). `_pill_precision_facts()` exercised against the real report
before deploy. Live page re-fetched after restart: renders "56% to 95%, median 76% across 16
pills"; the strings "75–94" and "Medical Device Companies" are gone. `tests/smoke.sh` 20/20.

**Files** — `pipeline/pill_eval.py`, `dashboard/routes/pages.py`,
`dashboard/templates/methodology.html`.

**Notes** — `fda` is deliberately excluded from the published range: it samples the raw FDA
name-match cache, which no longer backs any live pill, and scores 0.100. Including it would
advertise a 10% floor for something no reader can open.

Not fixed: `pill_eval`'s "worst offenders" summary crashes with `UnicodeEncodeError` on
Windows' cp1252 console when a title contains a non-breaking hyphen. It fires *after* the JSON
report is written, so no data is lost — but the traceback makes a successful run look failed.

The three weak pills (Semiconductors, AI Governance, Cybersecurity) are a tuning problem in
their `PILL_INTENTS` wording and thresholds, not a mechanism problem, and are the obvious next
piece of work.

## 2026-08-22 — The LLM judge had never seen a single keyword-tagged article

**What** — `pill_scorer.stage_batch` now gates the judge on *judged* status
(`matched_via='judge'`) instead of *tagged* status. Added `_judged_tags()`. A verdict on a
keyword-tagged article now replaces its keyword row (approved → judge row stands, keyword row
dropped; irrelevant → dropped outright). The demotion DELETE gained `matched_via <> 'judge'`,
and `score_new` no longer advances the watermark through a judge outage.

**Why** — The pill judge has been a no-op for keyword matches since the 2026-07-09 flip, so
`/methodology`'s central claim ("Only judge-approved articles get in") has been false for six
weeks and is falsifiable from the UI by anyone who clicks an inclusion badge.

When `SUFFIX` became `""`, `target_cat` became identical to the live category, which made
`already = _existing_tags(con, target_cat, urls)` the *same query* as
`kw_tagged = _existing_tags(con, k, urls)`. So `to_judge = cand - already` could only ever
contain semantic-net-only URLs — every keyword hit was its own skip set. Two consequences:
keyword false positives entered pills unjudged and permanently, and the demotion path that
exists to remove them within one cycle was unreachable dead code (`demoted` has been 0 since
the flip).

Measured on production, last 7 days: articles carrying **both** a keyword and a judge row in
the same category — which the intended design would produce constantly — numbered **0 across
all 16 categories**. Supply Chain, the pill whose keyword-only precision was measured at 25%
in July, was 6,582 unjudged keyword articles out of 6,787 (97%). Public Health 2,013/2,123
(95%). A grocery-store fire ("Fire outbreak rocks SPAR outlet in Calabar") reached the Public
Health briefing on the keyword "outbreak".

Two further defects fixed in the same pass, both found while tracing the first:

1. **The demotion DELETE had no `matched_via` predicate.** Inserts are applied before deletes
   in `score_new`, so once approvals started producing both a delete (of the keyword row) and
   a fresh judge row, the unqualified DELETE would have taken the judge row with it and
   dropped the article from the pill entirely — a data-loss bug that only becomes reachable
   *because* of this fix.
2. **The watermark advanced through gateway outages.** `score_new` wrote `WATERMARK`
   unconditionally, while a judge failure merely `continue`d. Since the watermark is a
   monotonic store `row_index` with no rewind, every article embedded during an outage was
   silently condemned to keyword-only membership forever, with nothing reporting it. It now
   halts without advancing and logs at WARNING; the next run retries those rows (idempotent —
   `_judged_tags` skips anything already ruled on).

**How it was verified** — A/B dry-run against live production data. `stage_batch` stages on a
read-only connection and writes nothing, so the old and new modules were run over the *same*
1,500-article chunk from the embedding store, with real judge calls:

| | judged | inserts | keyword rows replaced/demoted |
|---|---|---|---|
| OLD | 4 | 0 | 0 |
| NEW | 77 | 44 (supply_chain 36, public_health 8) | 67 (supply_chain 58, public_health 9) |

The 4 the old path judged were the semantic-net-only leftovers; the other 73 were keyword
articles it had never looked at. A 43% rejection rate on those two pills is consistent with
the 25% keyword precision measured in July. Backfill of the preceding 14 days via
`rescore_pills` + `flip_pills --mode replace` is tracked separately.

**Files** — `pipeline/pill_scorer.py`.

**Notes** — Judging keyword candidates costs roughly **$0.06–0.08/day** more (~2–3k additional
articles at ~$0.000026 each), which about doubles judge spend; GDELT's measured total was
$1.40/7d before this. Expect curated pills — Supply Chain most visibly — to shrink. That is
the feature working, not a regression.

Rejected: adding a `matched_via` index. The judged lookup rides the existing
`(source_type, article_id)` index with an IN-list of ≤400, and the dry-run's 27s for 1,500
articles is dominated by judge latency, not the lookup.

Not fixed here, deliberately: `calibrate_pills.py` parses `matched_detail` as a float, which
only works for `matched_via='semantic'` rows — judge rows store `"verdict|score"` and are
silently skipped, so calibration currently sees almost nothing on judged pills.

## 2026-08-02 — The daily SEC job could not have run; leverage sentence stopped reassuring the wrong companies

**What** — Fixed `GDELT-SecIngest`, whose derive stage would have failed on its first
firing. `sec_derive.py` now logs to a file and records its own `ingest_log` row.
`rule_leverage` no longer tells over-levered operating companies that their leverage is
normal. `/sec-analysis` is now linked from the header and present in `sitemap.xml`.

**Why** — Three things, found reviewing the feature the day it shipped:

1. **The task's second stage could not start.** The VBS ran `python -m pipeline.sec_derive`,
   but a scheduled task inherits `C:\Windows\System32` as its working directory, so the
   package was not importable — confirmed directly: `ModuleNotFoundError: No module named
   'pipeline'`. The task had never fired (`LastRunTime` 11/30/1999), so nothing had surfaced
   it. `&&` would have passed after a successful ingest and derive would have died on import,
   leaving **fresh snapshots with stale derived context** — precisely the failure the wrapper's
   own comment says it exists to prevent. Nothing would have reported it: this was the only
   wrapper in `C:\Users\siddh\bin\` with no output redirect, and `ingest_log` only ever
   recorded the ingest stage.
2. **`rule_leverage` reassured the wrong filers.** The "that is normal for a lender" clause
   fired on the ratio alone. Boeing, SIC 3721, sits at **96%** liabilities-to-assets — the page
   would have told a reader that was normal for a lender and not a warning sign. The rule also
   had no guard above 100%, where the known unverified data tail sits (~10% of tickered rows
   report liabilities > assets).
3. **The page was unreachable.** No link from anywhere, and absent from a `sitemap.xml` that
   advertises 520 other URLs — a week after #6 was closed for serving crawlers blank documents.

**How it was verified** — Task re-registered and **run on demand**: both stages completed and
wrote `ingest_log` rows — `daily` 416 companies / 9,384 rows / 236.8s, then `derive` 15,909
companies / 289,153 rows / 16.1s, both `ok`, with `derived_at` correctly newer than
`data_version`. On dev: Boeing keeps "96% of its $165.87B balance sheet is funded by
liabilities" and loses the lender clause; JPMorgan keeps both. `sitemap.xml` serves 7
`/sec-analysis` URLs; the header link renders. Tests **26 passed** (was 23). `smoke.sh` 20/20
against dev.

**Files** — `scripts/register_sec_task.ps1`, `pipeline/sec_derive.py`,
`pipeline/sec_explain.py` + `dashboard/sec_explain.py`, `dashboard/routes/sec_analysis.py`,
`dashboard/routes/pages.py`, `dashboard/templates/index.html`, `tests/test_sec_explain.py`.

**Notes**
- Derive is now called by **absolute path**, matching every other wrapper here. `-m` is the
  odd one out and should stay that way; `sec_derive.py` puts the repo root on `sys.path` itself.
- The redirect wraps **both** stages in parentheses so a failure occurring before logging is
  configured still lands in `data/logs/sec_task.log`. That is the only reason this class of bug
  would be visible next time.
- The lender clause now keys on a `leveraged_by_design` flag on the bank and insurer specs in
  `routes/sec_analysis.py`, not on the label string — renaming a label should not silently
  disable a sentence.
- Added `test_sec_explain_copies_have_not_diverged`: the two `sec_explain.py` files are
  duplicates, tests import the pipeline copy and prod serves the dashboard one. The guard is a
  stopgap; deduplicating them is its own issue.
- **`sync_dev.ps1` syncs the git ref, not the working tree.** Uncommitted edits do not reach
  dev — scp them across or commit first. Cost a confused minute here; worth knowing.
- **`scripts/restart_dash.ps1` did not exist.** It had been renamed to
  `~\restart_dash.ps1.bak_20260802` and never replaced, so the restart path the gdelt-ops skill
  documents was dead. Rewritten on the `restart_dev.ps1` pattern: it stops the
  `GDELT-Dashboard` task and whatever holds :8015, and nothing else. The old one killed **every**
  `python.exe` on the box — dev, ingest, embedder and Ollama along with prod — which is why the
  skill carried a warning never to aim it at dev. Verified: dev's pid was unchanged across a
  prod restart.
- SEC returns **403**, not 404, for a daily index on a day it published none (weekends).
  `ciks_that_filed` already treats any fetch failure as empty, so `--days-back 3` covers it.
- Not addressed: the derive stage rewrites `derived` wholesale while the site serves reads from
  the same SQLite file. Nothing observed, but it is a daily write burst against a live reader.

---

## 2026-08-02 — Financials page: stop misleading, adapt to the business, add light theme

**What** — Charts no longer mix period lengths; search ranks listed parents above
subsidiaries; the metric set adapts to the kind of filer; ROE/ROA added; a theme toggle now
appears on every page; balance sheet shown as proportions.

**Why** — Reviewing the live JPMorgan page found three things that actively mislead, which
matters more than polish on a page whose purpose is understanding:

1. **Metrics banks never report led the page.** For SIC 6021 (4,226 periods) gross profit
   is tagged **0.2%** of the time and operating income **3.2%**, against net income
   **99.4%** and equity **98.0%**. The most prominent section was three empty headings with
   a paragraph of definition each.
2. **Charts mixed annual and quarterly bars on one axis** — a full year at 4x the height of
   its neighbours reads as a spectacular quarter. On JPM the revenue chart showed *only*
   annual bars while its caption described "the quarters beside them".
3. **Search sent users to a financing subsidiary.** Both JPMorgan CIKs hold 24 periods, and
   the tiebreak was `max(revenue)` — NULL for banks — so ranking collapsed.

**How it was fixed**
- `_bar_chart` picks quarters *or* annual, never both, and labels which basis it plotted.
- Search ranks **listed → size (revenue or assets/10) → …**; `jp morgan`, `jpmorgan`, `JPM`,
  `wells fargo` and `citigroup` all now reach the listed parent.
- Filer class from SIC picks the metric set. Banks lead with net income, EPS, **ROE/ROA**
  (97.4% / 93.3% coverage), assets and equity — six filled rows where there were three
  dashes — plus a framing sentence explaining *why* a bank has no revenue line.
- New observation rules that work without a revenue line: return on equity, leverage, and
  net-income growth. JPM went from one sentence to three.

**How it was verified** — Prod: JPM, Apple, Intel, Prologis each get the right class and
metric set; charts report a single basis; light theme resolves (`bg rgb(255,255,248)`,
`--pos #1f7a68`). `test_sec_explain.py` 13/13. Dashboard checked for regression at runtime:
exactly **one** toggle, header button still `position: static`, feed still renders 50 rows.

**Files** — `dashboard/sec_search.py`, `dashboard/routes/sec_analysis.py`,
`dashboard/sec_explain.py` + `pipeline/sec_explain.py`, `pipeline/sec_derive.py`,
`pipeline/sec_schema.py`, `dashboard/templates/sec_analysis.html`,
`dashboard/templates/base.html`, `dashboard/static/css/sec.css`, `static/css/base.css`.

**Notes**
- **Caught a self-inflicted regression before it shipped.** The global toggle was first
  written as `.theme-toggle` — the class the dashboard header already uses and
  `dashboard.css` already styles. `position: fixed` in `base.css` would have yanked the
  dashboard's own button into the corner. Renamed `.page-theme-toggle`, and the script
  removes itself when `#themeBtn` is present so no page shows two.
- ROE for a quarter is the **quarter's** return. Deliberately not annualised; the sentence
  says so, because multiplying by four would overstate it.
- `derived` needed an ALTER migration for the new columns — `CREATE TABLE IF NOT EXISTS`
  does not add columns to an existing table (same trap as `companies` earlier today).
- The "filer does not tag revenue" observation is suppressed once the framing box says the
  same thing; two consecutive paragraphs making one point reads as padding.
- Colour tokens (`--pos`, `--neg`, `--ni-bar`) now differ per theme; the originals were
  picked against dark only and were muddy on white.

---

## 2026-08-02 — Financials page: widescreen, and it explains the metrics

**What** — `/sec-analysis` gets its own stylesheet (`static/css/sec.css`), a two-column
widescreen layout, a plain-language definition under every metric, and a "How to read
this" glossary.

**Why** — The page was inheriting `about.css`, which sets `body { max-width: 640px }`.
That is right for prose and wrong for financial data: six numeric columns of history were
scrolling sideways on a desktop monitor. And the figures assumed the reader already knew
what "operating margin" or "percentage points" meant, which defeats the point of a page
whose job is to make numbers understandable.

**How it was verified** — Prod renders 10 metric definitions, 6 glossary entries, 3 SVG
charts and the two-column grid for GOOGL, INTC and `jp morgan` alike. Visual check at
1568px: definitions sit under each figure, annual bars read as distinct from quarters,
margin trend legible. Balance-sheet arithmetic checks out on screen — Alphabet's
$281.50B liabilities + $640.48B equity = $921.98B assets.

**Files** — `dashboard/static/css/sec.css` (new), `dashboard/templates/sec_analysis.html`.

**Notes**
- Layout collapses to one column under 900px, and the glossary from two columns to one.
- The definitions are always visible rather than hidden behind hover: a tooltip is
  useless on touch and invisible to anyone skimming.
- Left behind: the `.sec-*` rules appended to `about.css` earlier today are now dead,
  since this page no longer loads that sheet. Harmless but worth deleting; not removed
  now because `about.css` still carries the live note-form styles and this was not the
  moment to risk them.
- `smoke.sh` 19/20 — the `gal language=en` failure is ingest lag again (`hours=1` returns
  0 with no filters at all; latest data 49m old, ingest is hourly), not a regression.

---

## 2026-08-02 — /sec-analysis explains the numbers instead of listing them

**What** — The financials page now says what the figures mean, shows their shape, and
finds the company you meant. New `pipeline/sec_derive.py` (growth, margins, streaks,
composition, sector percentiles), `pipeline/sec_explain.py` (deterministic observations),
`dashboard/sec_search.py` (ranked search cascade), inline-SVG charts, and SIC/multi-class
tickers from SEC's bulk submissions archive.

**Why** — The page was a correct table, and a correct table is still just numbers. It
showed GOOGL Q2 net income of $112.19B and said nothing about the fact that most of it
did not come from operating the business. It also required knowing the exact ticker.

**Explanations are computed, never generated.** No LLM anywhere in this feature: zero
cost, zero latency, and a page that exists to be correct cannot hallucinate a figure.
The trade is plainer prose.

**How it was verified**
- **13 tests** in `tests/test_sec_explain.py`, including a guard that no observation may
  state a number absent from the inputs — mutation-checked by inserting a fabricated
  `$4.44B`, which it caught. `tests/test_sec_normalize.py` stays 10/10; `smoke.sh` 20/20.
- Search resolves all of: `AAPL`, `goog`→Alphabet (multi-class), `apple`→Apple Inc. (not
  Applied Materials — size-ranked), `exxon`→XOM, `microsft`→MSFT (fuzzy),
  `jp morgan`→JPM (punctuation-squashed), `berkshire`→BRK-B.
- Charts render **server-side**: 3 `<svg>`, 20 bars and a polyline present in raw HTML
  with JS disabled.
- Backfill: 17,934 companies with SIC, 10,065 tickers, 289,153 derived rows, 14,201
  sector buckets.

**Files** — new `pipeline/sec_derive.py`, `pipeline/sec_explain.py`,
`dashboard/sec_search.py`, `tests/test_sec_explain.py`; modified `pipeline/sec_ingest.py`
(`--submissions`), `pipeline/sec_schema.py`, `dashboard/routes/sec_analysis.py`,
`dashboard/templates/sec_analysis.html`, `dashboard/static/css/about.css`,
`scripts/register_sec_task.ps1`.

**Notes**
- **Two bugs caught in review before shipping.** The composition sentence originally said
  `$71.42B came from outside normal operations`; that figure is net income minus operating
  income, which nets non-operating income *against tax*, so it now says "net of tax".
  And Intel's **$11.03B net loss was being suppressed** — the loss and composition rules
  shared a `kind`, and the dedup dropped the lower-scoring one. A net loss now has its own
  kind and always leads.
- The submissions archive holds **979,405 entities** — overwhelmingly individuals filing
  Forms 3/4/5. Ingesting them all buried real companies in search, so the pipeline now
  prunes any filer with neither financials nor a ticker (961,471 removed, 17,934 kept).
- `sec_derive` is chained after `sec_ingest` in the daily task; a refresh that skipped it
  would show new numbers with stale context.
- **News pairing is deferred, not abandoned** — it is the actual differentiator. Two
  blockers, both recorded in `routes/sec_analysis.py`: `_api_articles_inner` reads
  `g._req_phases` and raises under a synthetic request context, and the feed's rolling
  60-day window means only *recent* coverage can ever be shown, never the filing period.
- **`?org=` is broken** and needs its own issue: `source=gkg` alone returns 80 results,
  `source=gkg&org=Alphabet` returns 1,046,175 — a filter that increases the result count.

---

## 2026-08-02 — SEC financials: correct the numbers, then pre-collect them

**What** — Rewrote SEC XBRL extraction and turned `/sec-analysis` from a live per-request
API call into a locally-collected store. New `pipeline/sec_normalize.py`,
`sec_schema.py`, `sec_ingest.py`; `dashboard/routes/sec_analysis.py` now reads
`data/sec.db` directly.

**Why** — The page went live earlier today publishing **wrong financials**. For GOOGL:
blank Revenue, a Q2 net income of $140.23B (larger than all of FY2025's $132.17B), and
142 million shares against ~12.2 billion actual. Three distinct bugs, all confirmed
against real filings rather than inferred:

1. **Concepts were resolved once, globally.** Alphabet has 87
   `RevenueFromContractWithCustomerExcludingAssessedTax` entries but none ending
   2026-06-30 — recent periods use `Revenues`. The resolver locked onto the stale tag
   and returned `None`. Now every concept in a chain contributes and the earliest one
   with a fact *for that period* wins.
2. **Fact duration was never read.** A 10-Q carries both spans under the same end date
   and `fp`: `Revenues` 180d = $229.69B (H1) and 90d = $119.80B (Q2). The old code
   parsed `end` only, so the pick was arbitrary. Duration is now first-class.
3. **De-cumulation was applied to balance-sheet facts.** `12,230,000,000 −
   12,088,000,000 = 142,000,000` — that is where the share count came from. Instants
   (`start is None`) are never de-cumulated.
4. Latent: removed `LiabilitiesAndStockholdersEquity` from the `total_liabilities`
   chain. It is liabilities *plus equity*, i.e. total assets.

**How it was verified** — 10 tests in `tests/test_sec_normalize.py`, offline against
trimmed fixtures (60 KB / 73 KB, verified to reproduce the full multi-MB files).
**Mutation-tested**: each original bug was reintroduced and confirmed to fail 2–3 tests —
tests that pass without being able to fail prove nothing. Invariants (quarter never
exceeds its own fiscal year, `eps × shares ≈ net income`, instants non-negative) catch
the class rather than the three instances. MSFT included as a non-calendar-fiscal-year
filer so the logic is not fitted to Alphabet.

Backfill: **15,909 companies, 289,153 periods, 50 MB, 5m52s**. Spot-checked AAPL (Sept
year-end → Q3), NVDA (Jan year-end → Q1 FY2027), MSFT (June year-end → FY), AMZN, TSLA.
Live on prod: GOOGL now reads **$119.80B / $112.19B / 12,230,000,000**.

**Files** — `pipeline/sec_normalize.py`, `pipeline/sec_schema.py`,
`pipeline/sec_ingest.py`, `scripts/register_sec_task.ps1`,
`tests/test_sec_normalize.py` + `tests/fixtures/`, `dashboard/routes/sec_analysis.py`,
`dashboard/templates/sec_analysis.html`, `dashboard/static/css/about.css`.

**Notes**
- **Freshness without polling:** financial facts change only when a company files, and
  SEC publishes who filed. Backfill is one 1.4 GB `companyfacts.zip`; the daily job reads
  the ~1.1 MB filing index and refetches only the ~180 CIKs that filed a 10-K/10-Q
  (~90s). `GDELT-SecIngest` runs 06:40 daily with `--days-back 3` so a weekend or a
  missed run self-heals.
- Ingest writes `ingest_log`; the daily `gdelt-watch` alerts if no successful run in 48h.
  Every silent-failure incident here has come from a job stopping unnoticed.
- **Known data-quality caveats, not yet resolved:** ~10% of tickered rows show
  liabilities > assets (plausible for negative-equity filers, but unverified), 0.6% show
  a quarter exceeding its fiscal year, and revenue coverage is 73% (banks and REITs do
  not tag `Revenues`). Large caps spot-check clean; the long tail is weaker.
  `shares = 100` for EIDP/Smurfit/NSTAR is **correct** — wholly-owned subsidiaries.
- Ticker lookup falls back to a name match: SEC maps `XOM` to a holding-company CIK
  distinct from "Exxon Mobil Corporation", so ticker-only lookup missed the filer people
  mean.
- `~/projects/sec-analyzer` and its FastAPI are no longer in the request path.

---

## 2026-08-02 — Fix empty SSR on curated view pages (#6)

**What** — `warm_feed.py`'s "already warmed" marker now keys on the dashboard's process
identity as well as the data version. New DB-free endpoint `/api/warmstate` reports
`{boot, entries}`; `articles._BOOT_ID` identifies the process that owns the feed cache.

**Why** — `_ssr_feed` serves the server-rendered first paint out of `articles._feed_cache`,
which is an **in-process dict**. `warm_feed.py` skipped warming all 16 curated pills
whenever `data_version` was unchanged — an assumption that only holds if the cache
outlives the process. It doesn't. Every dashboard restart emptied the cache while the
marker still read "warmed", so all 16 pills stayed cold until the next ingest bumped the
version, and their SSR first paint was **empty HTML**.

That is not cosmetic: `sitemap.xml` advertises 520 URLs including these view pages, and
Cloudflare shows BingBot and GoogleBot actively crawling them. Crawlers were being served
blank documents from a large fraction of the site — the exact problem #6 was opened to
fix, still live on views after being fixed for `/`. Measured before the fix, on prod:
`/` and `?view=ai-general` had 50 articles in raw HTML; `?view=geopolitics-conflict` and
`?view=public-health` had **0**.

**How it was verified** — On prod, straight after a restart: `/api/warmstate` reported
`entries: 0` and both views served 0 articles. Run 1 of `warm_feed.py` warmed the pills;
run 2 printed **"pills: data + process unchanged — skipping"**, confirming the guard still
suppresses redundant work; cache settled at **18 entries** (2 global + 16 pills). View
pages then served 50 / 50 / 50 articles. `tests/test_ssr.py` against prod went from
**10 passed / 1 failed to 11 passed / 0 failed**.

**Files** — `dashboard/articles.py`, `dashboard/routes/api_feed.py`,
`pipeline/warm_feed.py`.

**Notes**
- `/api/warmstate` deliberately touches no database. `/api/stats` returns 503 while
  DuckDB is busy, and a warm loop that cannot distinguish "cache is cold" from "stats is
  busy" is worse than no probe.
- Fixing this in `restart_dash.ps1` (deleting the marker) was rejected: it would only
  cover restarts that go through that script, not crashes, reboots or OOM kills.
- Re-warming all 16 pills on every 2-minute cycle was also rejected — the original author
  guarded it deliberately, and the guard is right, it was just keyed on the wrong thing.
- Low counts on some pills (`va-news` 1, `medical-devices` 25) are genuine 24h article
  volumes, not cache misses. `test_view_raw_html_has_articles` samples
  `geopolitics-conflict`, which is well populated.

---

## 2026-08-02 — Documentation facts injected from code; /about and /methodology rewritten

**What** — `pages.py::_doc_facts()` gathers `BRIEFING_MODEL`, `BRIEFING_EVENT_LIMIT`,
`fresh_s()`, the `importance.py` weights and the live `VIEWS` registry, and injects them
into `/about` and `/methodology`. Both pages now render those numbers from the source of
truth. "What it does" was rewritten against the live pill registry, and `/methodology`
gained four explanatory sections aimed at a technical reader.

**Why** — Every concrete claim on those pages was hand-copied prose duplicating a
constant in code, and **six were found wrong in a single day**: "GLM-4.7 on Cerebras",
"Gemini 2.5 Flash via OpenRouter", "top 50 events", "cached for 45 minutes", "cached for
60 minutes", and "a few built-in monitoring views" when there were 16. Rewriting alone
would have reset the clock on the same failure. Two of the three views "What it does"
named by hand no longer existed under those names.

The new `/methodology` sections answer the questions a technical reader actually has and
the terse spec did not: why raw article counts mislead (GDELT indexes syndication, not
events), what an "event" is and where the clustering is known to be wrong, why keyword
matching scored 11–25% precision and what judge-gating changed, and where briefings can
still be wrong despite being grounded. Substantive technical writing is also the kind of
content that earns links — relevant while GoogleBot is crawling ~10x less than BingBot.

**How it was verified** — On dev then prod, the injected values resolve to the real
config: "**16** curated topic feeds across **4** areas" with the group lists generated
from `VIEWS`, "top **12** events", weights **0.5 / 0.3 / 0.2**, freshness "**3** hours …
**24** hours", model `gpt-oss-120b`. `grep -c '{{'` returns **0** on both live pages, so
no template expression leaked. `/about`, `/methodology`, `/` all 200.

**Files** — `dashboard/routes/pages.py`, `dashboard/templates/about.html`,
`dashboard/templates/methodology.html`.

**Notes**
- Each lookup is wrapped in its own `try/except` with a literal fallback in the template
  (`{{ event_limit or 12 }}`), so an import failure degrades to the old behaviour rather
  than 500-ing a public page.
- Remaining hand-written numbers: 44,000 sources, 60-day retention, the 15-minute ingest
  cadence, and the 11–25% / 75–94% precision figures. The first three could be injected
  from `pipeline/config.py`; the precision figures are measurements from a point-in-time
  audit and should stay prose.
- The honest-limitations paragraphs are deliberate: a news-ranking tool's central
  credibility question is what it gets wrong, and the previous page never said.

---

## 2026-08-02 — About page: LinkedIn, visitor notes, AI-use disclosure

**What** — `/about` gains a LinkedIn link, a public "leave a note" box (`POST /api/note`
→ `visitor_notes` in `users.db`), and a one-line disclosure of AI assistance under
"Built by". New notes surface in the daily `gdelt-watch` run and Matrix-alert.

**Why** — Sidd wanted a way for readers to reach him, and to state plainly how the
project was built. Two stale claims were found on the page while editing and fixed in
the same pass: the prose said briefings run on *"GLM-4.7 on Cerebras"* and the tech-stack
list said *"Gemini 2.5 Flash via OpenRouter"* — both wrong, and OpenRouter is not in the
path at all. Same rot as `/methodology` had.

**How it was verified** — Against dev then prod: rate limit returns **429 after 3
submits/hour**; a filled honeypot returns `ok:true` to the bot but **stores nothing**
(confirmed absent from both databases); an empty note is rejected 400. A real note
posted to prod appeared in `visitor_notes` and the watch reported
`ALERT 1 new visitor note(s)`. The setup test row was then deleted so it would not fire
a false alert. `/about` 200, LinkedIn present, AI line present, and grep confirms zero
occurrences of GLM-4.7 / Gemini / OpenRouter remain.

**Files** — `dashboard/routes/api_feed.py`, `dashboard/templates/about.html`,
`dashboard/static/css/about.css` (`?v=2`), plus `gdelt_demand_report.py` /
`gdelt_watch.py` on the ops boxes.

**Notes**
- The IP is stored only as a **daily-salted hash** — enough to rate-limit, not enough to
  identify anyone, and it cannot link a visitor across days.
- Notes are never rendered back into HTML anywhere; if that ever changes, they must be
  escaped, since the content is attacker-controlled.
- A Buy Me a Coffee button was requested but deferred — Sidd will supply the URL.
- The honeypot is positioned off-screen rather than `display:none`, which some bots skip.
- This was the first deploy to use `deploy_guard.ps1 claim`, and it worked.

---

## 2026-08-02 — Deploy lock for the shared production tree

**What** — `scripts/deploy_guard.ps1` (`status` / `claim` / `release`), plus a check at
the top of `restart_dash.ps1` that shouts when another agent holds the lock or when the
tree has uncommitted files. Documented as required in CLAUDE.md.

**Why** — Several agents (claude-code, OpenCode/DeepSeek) deploy into this one working
tree by `scp` with no coordination. Earlier today two of them overwrote each other ten
minutes apart: the second reverted a completed renderer fix and left `routes/pages.py`
importing a `briefing._normalize_text` that no longer existed, so gdeltmonitor.com
returned **500** until it was restored. Nothing was permanently lost only because the
work had already been committed to a branch. The changelog discipline added earlier
records history after the fact; it does nothing to prevent the collision itself.

**How it was verified** — Simulated the exact scenario: agent A claims, agent B's claim
is **refused with exit 1** naming the holder and the reason, `restart_dash.ps1` surfaces
`LOCK ACTIVE owner=... reason=...`, a release by the wrong owner is refused, and a
release by the rightful owner succeeds. `status` against the live tree correctly reported
the branch, HEAD and the 3 uncommitted files.

**Files** — `scripts/deploy_guard.ps1` (new), `C:\Users\siddh\restart_dash.ps1`
(outside the repo; backed up to `restart_dash.ps1.bak_20260802`), `CLAUDE.md`.

**Notes**
- **The lock is advisory.** `scp` cannot be intercepted, so an agent that ignores the
  guard still overwrites whatever it likes. This makes collisions loud, not impossible.
  Committing before deploying remains the only thing that makes them recoverable.
- `restart_dash.ps1` warns but never blocks — refusing to restart during an incident
  would be worse than the collision being warned about.
- It also now reminds you that it killed the :8016 dev instance, which is true of every
  run and has bitten repeatedly.

---

## 2026-08-02 — Prewarm respects freshness (`prewarm=1`)

**What** — New `prewarm=1` request mode. It tags history as `prewarm` and suppresses the
thread update exactly like `refresh=1`, but regenerates **only when the cache is stale for
that window**. `prewarm_briefings.py` now uses it. `refresh=1` keeps its old force-regenerate
behaviour for manual and debugging use.

**Why** — The window-aware freshness added earlier the same day only governed *visits*.
`refresh=1` sets `needs_regen = True` unconditionally, so the scheduled job rewrote all 13
combos on every run regardless of age — a 30-day briefing (fresh for 24h) would have been
regenerated five times a day for content that had barely moved. Roughly 40% of prewarm spend,
and it meant the freshness work applied only to the cheaper half of the traffic.

**How it was verified** — On dev: `prewarm=1` against a fresh combo returned `cached: True`
with no regeneration, while `refresh=1` against the same combo regenerated (`cached: False`).
A full prewarm run reported **"still-fresh, no LLM call: 4/6"**. History rows from both modes
tag `trigger='prewarm'`, and `_all:720`'s `briefing_threads` row stayed at 17:02:22 across two
prewarm generations (17:56, 18:05), confirming thread updates stay suppressed. Prod after
deploy: `/`, `/api/stats`, `/?view=ai_sector` all 200, and `prewarm=1` on a fresh combo
returned `cached: True`.

**Files** — `dashboard/routes/api_briefing.py`, `pipeline/prewarm_briefings.py`.

**Notes**
- `warm()` now returns whether it skipped, and a run prints `still-fresh, no LLM call: N/M`
  — that line is the cheapest way to see what a schedule actually costs.
- The schedule (`GDELT-BriefingPrewarm`) is **still disabled**. It is now self-limiting, so
  enabling it at 5×/day can no longer over-fire.
- **The demand list is currently seeded partly with agent test traffic.** Of eight `visit`
  rows on 2026-08-02, ~5 were generated by this session and a concurrent one — `_all:168h`
  is in the warm list almost certainly only because of a `curl`. It self-corrects as real
  reads accumulate; worth re-checking `demand_combos()` after a week of ordinary use.

---

## 2026-08-02 — Cut briefing LLM spend ~30x

**What** — Five changes to how briefings are generated: model back to
`gpt-oss-120b`; thread updates skipped on prewarm generations; prewarm made
demand-driven instead of a 17×6 cross-product; freshness scaled to the window being
summarized; event list deduped and cut from 20 to 12.

**Why** — Briefings were 89% of all LLM spend across the fleet ($20.80 of $23.45 over
14 days), and a plan to pre-warm every combo 5×/day would have taken it to ~$130/month.
Each change targets a measured cause:

1. **`cerebras-fast` was believed free — it isn't.** Observed blended rate ~$1.90/1M
   made it 8× costlier per briefing than the `gpt-oss-120b` it had replaced that
   morning, and the largest single line item in the fleet. `pill_eval.py:31` already
   records this exact conclusion for the pill judge, which was migrated off Cerebras
   for the same reason and validated with an agreement eval.
2. **Thread updates fired on every generation.** Measured in the GDELT Langfuse
   project over 14 days: 1,749 `gdelt-briefing` calls, 1,638 `gdelt-thread-update` —
   0.94 per briefing, at 1,179 in / 649 out each. That is 0.84× the cost of the
   briefing itself, and thread continuity is only ever read by a human opening the
   panel. Prewarm now skips it.
3. **Prewarm warmed 102 combos for ~9 daily reads.** `briefing_history` shows only
   **13 of 102 combos ever opened**, 7 of 17 views, 3 of 6 windows, with `3h`+`24h`
   at 98.3% and the top 4 combos at 78%. Prewarm now selects from `trigger='visit'`
   history, so it warms what is read and stops warming what isn't. Cold combos
   generate on demand the rare time someone opens one, and are warmed thereafter.
4. **Flat 1h freshness would have defeated a 4-hourly prewarm** — for three of every
   four hours a visit regenerated anyway, paying for prewarm *and* on-demand.
   `fresh_s(hours)` now scales 3h→3h … 720h→24h.
5. **~3 duplicate titles per prompt**, from clustering leaving near-identical
   headlines in the ranked list — wasted tokens and pushed the model to write the
   same bullet twice.

**How it was verified** — On dev (:8016) with a real forced generation: prompt
**2,398 → 1,601 tokens**, events 20 → 12, **duplicate titles 3 → 0**. `fresh_s`
returns 3/4/6/12/24/24h across the six windows. `demand_combos()` against production
history selects **13 combos, not 102**. Thread-update guard confirmed by timestamp: a
prewarm briefing generated 17:56:08 left `_all:720`'s `briefing_threads` row at
17:02:22 — no update fired. Prod after deploy: `/`, `/api/stats`, `/?view=ai_sector`,
`/methodology` all 200; `tests/smoke.sh` 19/20 (the `gal language=en` failure is
ingest lag — the 1h window returns 0 with no filters at all).

**Files** — `dashboard/briefing.py`, `dashboard/routes/api_briefing.py`,
`pipeline/prewarm_briefings.py`, `dashboard/templates/methodology.html`.

**Notes**
- Projected ~**$1.40/month** for briefings (13 combos × 5 prewarms + ~9 visits/day),
  against ~$132/month for the 102-combo × 5 plan on Cerebras with threads everywhere.
  This is a projection from measured token counts and configured rates — **the real
  number should be checked against `spend_lib` in a few days.**
- Running briefings on rainbow-boi's local GPU was considered and rejected: ~$0.22/mo
  of electricity vs ~$1.40 cloud, for a saving of about a dollar a month, in exchange
  for coupling briefings to the GPU that already runs Frigate and the embedding model
  (which silently died for 11 days in July), plus citation quality risk on a small model.
- Cached briefings written before this change keep their 20 sources until they age out.
- Cerebras dollar figures are LiteLLM's modeled cost — that entry has no explicit price
  in the gateway config, so it falls back to the built-in map. Token counts are exact;
  the dollars are worth sanity-checking against Cerebras's own billing.

---

## 2026-08-02 — Serve-cached-then-regenerate briefings; all 17 views pre-warmed

**What** — Model switched to `cerebras-fast`; `BRIEFING_TTL_S` renamed to
`BRIEFING_FRESH_S` (it gates background regeneration, not eviction). `/api/briefing`
rewritten to always serve cached content immediately and then, if that cache is stale,
stream a freshly generated briefing on the same connection, with a concurrency guard so
two visitors can't trigger duplicate generations for one key. `prewarm_briefings.py`
expanded from the global view to all 17 pills, still version-guarded.

**Why** — Briefings were slow to first paint on a cache miss, and a stale cache showed
nothing while regenerating. Serving the stale copy first makes the panel useful instantly.

**How it was verified** — Author reported homepage, API, pill briefing and SSE stream all
200. Note that this was status-code verification only; it did not exercise the two-phase
stream's rendering, and two defects survived it (see the entry below).

**Files** — `dashboard/briefing.py`, `dashboard/routes/api_briefing.py`,
`pipeline/prewarm_briefings.py`, `dashboard/static/js/markdown.js`.

**Notes**
- Authored by a separate agent session (OpenCode/DeepSeek) working the same tree
  concurrently; summary supplied by Sidd and used as the basis for this entry.
- **Still pending, deliberately not done:** `GDELT-BriefingPrewarm` is still DISABLED in
  Task Scheduler. Enabling it for all 17 views is a cost decision, not a code one — see
  the open question at the bottom of this entry's follow-up in the next entry.

## 2026-08-02 — Reconcile the concurrent briefing deploys

**What** — Re-applied the markdown renderer rewrite on top of the concurrent session's
backend changes, and fixed two defects that its deploy introduced.

**Why** — Two agents deployed to the same production tree within ten minutes. The second
`scp` of `markdown.js` and `briefing.py` reverted the renderer to the old version, and for
a window left `pages.py` importing a `_normalize_text` that no longer existed — the
homepage returned **500** until `_normalize_text` was restored. Beyond that:

1. **Duplicated briefings.** The new flow emits the full cached briefing
   (`done:false`) and then streams a fresh one on the same connection, but the client did
   `fullText += data.text` with no reset — so a stale briefing rendered *twice*, cached
   copy with the regenerated one appended. The server re-sends `sources` to open phase
   two; the client now treats that as "what follows replaces what I have".
2. **Encoding damage.** `markdown.js` came back with the `•` in the list-marker character
   class mangled to replacement characters, and `·` changed to `×` in the source-count
   separator. The renderer now spells those as `•` etc. escapes so the file is
   pure ASCII and cannot be corrupted by an editor round-trip again.

**How it was verified** — Replayed the exact SSE event sequence `api_briefing.py` emits
for a stale cache: **2 briefings rendered before the fix, 1 after**, and the one shown is
the regenerated text. Re-ran the streaming scan (0 of 1969 and 0 of 1662 frames leak raw
syntax) and the SSR-vs-client diff (byte-identical). Prod: `/`, `/api/stats`,
`/methodology`, `/?view=ai_sector`, `/?hours=24` all 200, renderer confirmed live, visual
check in dark mode.

**Files** — `dashboard/static/js/markdown.js` (merge), `dashboard/templates/index.html`
(`?v=20`), `dashboard/static/sw.js` (`gdelt-shell-v20`).

**Notes**
- Asset version went to v20 because `?v=19` was served with two *different* contents
  during the collision, so some clients hold a bad copy under that key.
- Kept from the other session: the `data.meta` / `refreshed` / `cached` meta label and the
  `\\'` escaping fix in the FDA row. Reverted: the `×` separator (restored `·`).
- Commit `85f94c7` describes those edits as drift "whose rationale was not recorded". That
  was written before it was known they were live, in-flight work from an active session —
  the characterization is unfair and the entry above supplies the missing rationale.
- **The real lesson is not in the code.** Two agents `scp`-ing into one working tree with
  no lock is what caused both the outage and the lost work. A deploy lock is proposed but
  not built.

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
