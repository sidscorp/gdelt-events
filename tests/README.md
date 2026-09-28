# Dashboard test harness

Three layers, each independently runnable. The live layers require a running
dashboard and mutable current-news data, so the default offline pytest suite
excludes them. Layer 3 is a manual relevance tool and never gates CI.

## Layer 1 — smoke tests (bash)

No dependencies beyond `curl` and `python3`.

```bash
bash tests/smoke.sh
# or against a local dashboard:
BASE=http://localhost:8015 bash tests/smoke.sh
```

Each check prints `PASS` or `FAIL` with wall-clock timing. Covers:
- `/api/stats`, `/api/views`, `/api/gal_facets` endpoint shapes
- GAL reading-feed time windows and filters
- Curated view availability
- Regression: nonsense queries return empty

The API's old GKG/all source selector is retired. GKG remains pipeline metadata,
not a reader-facing feed mode.

## Layer 2 — pytest golden queries

```bash
pip install pytest   # if you don't already have it
pytest -m live tests/test_queries.py -v

# Or target a specific case
pytest -m live tests/test_queries.py -v -k gal_supply_chain

# Against a local dashboard
BASE=http://localhost:8015 pytest -m live tests/test_queries.py -v
```

Each query in `golden_queries.json` becomes one parametrized test with latency and result-count assertions. **Add a query by appending to the JSON — no code change required.** Schema:

```json
{
  "id": "unique_slug",
  "params": { "hours": 24, "q": "..." },
  "min_results": 1,
  "max_results": 100,
  "max_latency_s": 2.5,
  "description": "What you're checking and why",
  "response_shape": { "page": 1 },
  "llm_check": true
}
```

Only `id`, `params`, and `max_latency_s` are required. Everything else is
optional. Keep content floors low: publisher presence and article volume change
with the news cycle and are checked by operational health metrics instead.

## Layer 3 — LLM relevance validation (scaffold, manual)

Uses the local Ollama on rainbow-boi to score top-N article relevance against each query's intent. **Not wired into CI** — this is for tuning, not gating.

```bash
# Score every query flagged with llm_check: true
python tests/llm_validate.py

# Target one query, see every verdict
python tests/llm_validate.py --query fda_view_gal --verbose

# Use a specific model
python tests/llm_validate.py --model dolphin-mistral:7b --top-n 10
```

Reports `precision@N` per query. `YES` counts as 1.0, `PARTIAL` as 0.5, `NO`/`UNKNOWN` as 0. Exits non-zero if any query falls below `--min-precision` (default 0.6).

To opt a query into LLM scoring, add `"llm_check": true` to its entry in `golden_queries.json`. We'll turn this on for the supply chain and device recall categories once the transformer classifier lands.

## Running everything

```bash
bash tests/smoke.sh && pytest -m live tests/test_queries.py -v
```

If smoke.sh fails, look for the `[FAIL]` line — the assertion it tripped is printed with the first 200 bytes of the response. For pytest failures, run with `-v -s` to see HTTP status and timing per case.
