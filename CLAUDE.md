# CLAUDE.md — GDELT Monitor

Guidance for AI-assisted work in this repo. Live at gdeltmonitor.com.

## What this is
Real-time GDELT news pipeline + Flask dashboard. Ingests GDELT every 15 min into DuckDB,
classifies (Aho-Corasick keyword + FDA matcher), embeds (Ollama nomic-embed-text) + FAISS,
clusters near-duplicate articles into events, and serves a dashboard with custom pills,
semantic search, and AI briefings.

## Where things live
```
pipeline/   ingest → tag/fda → embed → faiss → cluster → prune  (build_clusters.py, tagger.py, fda_matcher.py, embedder.py, build_faiss_index.py, pruner.py …)
dashboard/
  app.py            thin assembly: create Flask app, logger, before/after hooks, register blueprints. NO route logic here.
  db.py             DuckDB read connection (get_db), statement timeout, _hours_cutoff
  parsers.py        pure GDELT field parsers/formatters (parse_tone, parse_locations, format_timestamp, time_ago …)
  articles.py       the article-feed ENGINE: view/filter resolution, GAL/GKG WHERE builders, fetch strategies, row transformers, rollup/dedup, feed cache, _api_articles_inner
  briefing.py       AI-briefing service: prompt build, gateway SSE stream, output normalization (_normalize_text — mirrored in static/js/markdown.js), Langfuse instrumentation, event fetch/dedup/cluster helpers
  webutil.py        _phase request-timing helper
  views.py models.py auth.py semantic_search.py serve.py _paths.py   (unchanged core)
  routes/           Flask blueprints (thin handlers calling services): pages, auth, api_feed, api_briefing, api_pills
  templates/        base.html + pages that {% extends %} it (event_detail.html is a standalone exception)
  static/css/       base.css (tokens + global dark mode) + per-page css ; static/js/ (rum, dashboard, markdown, portal, search — classic scripts, NOT modules)
tests/        smoke.sh (20 curl checks) · e2e_smoke.mjs (Playwright UI) · test_queries.py + golden_queries.json
deploy/       register_dashboard.ps1, register_task.ps1 (Windows — production), install.sh (Linux reference)
```

## Deploy / run (production = Windows, rainbow-boi)
Only the owner-approved release operator changes production. Workers commit locally;
Sidd pushes. Security fixes are deployed and verified before public publication.
Commit the candidate and verification entry first, test with isolated data and workers
disabled, then obtain approval for the exact release. Back up changed files before
transfer and compare SHA-256 afterwards. No unreviewed shared-tree edits.

## Deploying: take the lock first (REQUIRED)
Run from the repository root in PowerShell. Keep the unique token for the release;
never publish it. Every guard invocation must succeed before continuing.

```powershell
$releaseToken = [guid]::NewGuid().ToString()
powershell -File scripts\deploy_guard.ps1 status
powershell -File scripts\deploy_guard.ps1 claim release-operator "approved release" -Token $releaseToken
# Check production HEAD and dirty status against the approved bundle before writing.
# Back up, transfer the complete bundle, and verify hashes.
powershell -File scripts\restart_dash.ps1 -Owner release-operator -Token $releaseToken
# Verify actual reader content and independently check the dev instance.
powershell -File scripts\deploy_guard.ps1 release release-operator -Token $releaseToken
```

Restart refuses missing or mismatched ownership before touching services. An active
restart holds a read lease preventing lock deletion. Locks never expire automatically;
legacy, malformed or abandoned locks require explicit owner-approved recovery using
`recover -Force -ExpectedHash <inspected-sha256>`. SCP does not enforce this protocol:
only the approved operator may transfer files. Production restart is scoped to its
task and listener; it does not intentionally stop the dev instance.

## Recording changes (REQUIRED — do not skip)
Deploying by `scp` makes it very easy to change production and leave no record. Twice now
the working tree has accumulated weeks of uncommitted edits whose rationale is simply gone.
So, for any change that reaches prod:

1. **`CHANGELOG.md` entry first**, using the format at the top of that file. The **Why**
   and **How it was verified** fields are the point — git already stores the diff. Write
   the symptom that started it and the number you measured.
2. **Commit it**, touching only the files your change touched. If the working tree already
   holds someone else's uncommitted edits, do **not** sweep them into your commit — say so
   and ask. No Co-Authored-By trailers; the user runs all `git config`/push themselves.
3. **Check the user-facing docs in your area.** `templates/methodology.html` and
   `templates/about.html` make concrete claims (models, cache TTLs, thresholds) that go
   stale silently because nothing tests them. If you touched the thing a page describes,
   re-read the page.
4. **Post to the shared news feed** so other machines see it:
   `ssh snambiar@snambiar-linux "~/news/news.sh claude-code '<what changed>'"`.

## Gotchas (learned the hard way)
- **Jinja caches templates in prod** — a template or base.html change needs a dashboard **restart** to show. Static (css/js) serve fresh.
- **DuckDB is single-writer** — stop the dashboard before any write/rebuild (ingest, cluster build). Read-only connections allow many readers.
- **scp -r into an existing dir nests** (`static/css` → `static/css/css`). scp individual files into existing dirs, or rsync.
- **PowerShell over SSH eats `$_`** in inline commands — write a script file or avoid `$_`/`||`. Use `curl.exe` (not PowerShell's curl alias).
- **`/api/articles` has no top-level `source` field** (dropped in the 2026-06 GAL rewrite) — don't assert on it.
- Commits on this repo: **no Co-Authored-By trailers**; the user runs all `git config`/push themselves.

## Keys (under data/, gitignored)
- `data/.openrouter_key` — **misnamed**: holds a LiteLLM *virtual key* for the self-hosted
  gateway at llm.snambiar.com, which routes briefings and the pill judge to Fireworks.
  OpenRouter is not in the path at all (the real OpenRouter key is parked at
  `.openrouter_key.orig`).
- `data/.langfuse_key` — env-style `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`/`LANGFUSE_HOST`. Briefing tracing is no-op if absent. Project: "GDELT Monitor" on langfuse.snambiar.com.

## Tests
```bash
BASE=https://gdeltmonitor.com bash tests/smoke.sh            # 20 endpoint checks
URL=https://gdeltmonitor.com node tests/e2e_smoke.mjs        # Playwright UI smoke (needs: npm i playwright && npx playwright install chromium)
pytest tests/test_queries.py -v                              # golden queries (latency + count bounds)
```

## Ops skill
The `gdelt-ops` Claude skill has the canonical deploy/restart/rebuild commands and DB constraints.
