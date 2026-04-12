# Dashboard test harness

Three layers, each independently runnable. Layers 1 and 2 are live and green. Layer 3 is a manual-run scaffold for when we start tuning the transformer classifier.

## Layer 1 — smoke tests (bash)

Fast (<10s), no dependencies beyond `curl` and `python3`.

```bash
bash tests/smoke.sh
# or against a local dashboard:
BASE=http://localhost:8015 bash tests/smoke.sh
```

Each check prints `PASS` or `FAIL` with wall-clock timing. Covers:
- `/api/stats`, `/api/views`, `/api/gal_facets` endpoint shapes
- Every `source=gal|gkg|all` × time-window combo
- Each source-specific filter (language, domain, outlet, person, org, theme)
- FDA Medical Device Companies view under every source
- Regression: nonsense queries return empty, unknown sources fall back to gal

## Layer 2 — pytest golden queries

```bash
pip install pytest   # if you don't already have it
pytest tests/test_queries.py -v

# Or target a specific case
pytest tests/test_queries.py -v -k gal_supply_chain_week

# Against a local dashboard
BASE=http://localhost:8015 pytest tests/test_queries.py -v
```

Each query in `golden_queries.json` becomes one parametrized test with latency and result-count assertions. **Add a query by appending to the JSON — no code change required.** Schema:

```json
{
  "id": "unique_slug",
  "params": { "source": "gal", "hours": 24, "q": "..." },
  "min_results": 5,
  "max_results": 100,
  "max_latency_s": 2.5,
  "description": "What you're checking and why",
  "response_shape": { "source": "gal" },
  "llm_check": true
}
```

Only `id`, `params`, and `max_latency_s` are required. Everything else is optional.

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
bash tests/smoke.sh && pytest tests/test_queries.py -v
```

If smoke.sh fails, look for the `[FAIL]` line — the assertion it tripped is printed with the first 200 bytes of the response. For pytest failures, run with `-v -s` to see HTTP status and timing per case.
