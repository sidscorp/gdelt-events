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
