"""LLM-based relevance validation (SCAFFOLD — not yet wired into CI).

For each query in golden_queries.json flagged with `llm_check: true`, fetch the
top N articles from the dashboard, ask a local Ollama model whether each result
is on-topic, and report precision@N per query.

Run manually:
    python tests/llm_validate.py
    python tests/llm_validate.py --model dolphin-mistral:7b --top-n 10
    python tests/llm_validate.py --query gal_supply_chain_week --verbose

Requires a reachable Ollama instance. The dashboard pipeline already uses
rainbow-boi's Ollama (nomic-embed-text) for the planned transformer classifier,
so the same host answers classification prompts for these relevance checks.

This file is DELIBERATELY not imported by test_queries.py — it's not a pytest
fixture. Run it by hand when tuning query corpora or category thresholds.
"""
import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_DASHBOARD = "https://gdelt.snambiar.com"
DEFAULT_OLLAMA = "http://rainbow-boi:11434"
DEFAULT_MODEL = "gemma3:latest"  # small-ish, fast on rainbow-boi 4060

CASES_FILE = Path(__file__).parent / "golden_queries.json"

RELEVANCE_PROMPT = """You are an editor validating article relevance for a news monitoring dashboard.

The user is searching for: {query_desc}

Article:
  Title: {title}
  Description: {description}

Is this article relevant to the user's intent?

Respond with exactly this format on two lines:
VERDICT: YES | NO | PARTIAL
REASON: one short sentence
"""


UA = "gdelt-dashboard-llm-validate/1.0"


def http_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def ollama_generate(base, model, prompt, timeout=60):
    req = urllib.request.Request(
        f"{base}/api/generate",
        data=json.dumps({"model": model, "prompt": prompt, "stream": False}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())["response"]


def parse_verdict(response_text):
    """Extract YES/NO/PARTIAL from the model's reply."""
    for line in response_text.splitlines():
        line = line.strip().upper()
        if line.startswith("VERDICT:"):
            v = line.split(":", 1)[1].strip()
            for token in ("YES", "PARTIAL", "NO"):
                if v.startswith(token):
                    return token
    # Fallback: scan the whole response
    up = response_text.upper()
    if "VERDICT: YES" in up: return "YES"
    if "VERDICT: PARTIAL" in up: return "PARTIAL"
    if "VERDICT: NO" in up: return "NO"
    return "UNKNOWN"


def describe_query(case):
    """Human-readable query description for the LLM prompt."""
    if case.get("description"):
        return case["description"]
    params = case["params"]
    parts = []
    if params.get("q"): parts.append(f'keyword "{params["q"]}"')
    if params.get("title"): parts.append(f'title contains "{params["title"]}"')
    if params.get("description"): parts.append(f'description contains "{params["description"]}"')
    if params.get("person"): parts.append(f'mentions person "{params["person"]}"')
    if params.get("org"): parts.append(f'mentions org "{params["org"]}"')
    if params.get("theme"): parts.append(f'theme "{params["theme"]}"')
    if params.get("view") == "fda-medical-devices":
        parts.append("FDA-registered medical device companies")
    return "news articles about " + (" and ".join(parts) if parts else params.get("source", "general"))


def score_query(case, dashboard_base, ollama_base, model, top_n, verbose=False):
    params = dict(case["params"])
    params["per_page"] = top_n
    qs = urllib.parse.urlencode(params)
    data = http_json(f"{dashboard_base}/api/articles?{qs}")
    articles = data.get("articles", [])[:top_n]
    if not articles:
        return {"id": case["id"], "n": 0, "yes": 0, "partial": 0, "no": 0, "unknown": 0, "precision": None}

    query_desc = describe_query(case)
    verdicts = {"YES": 0, "PARTIAL": 0, "NO": 0, "UNKNOWN": 0}

    for i, art in enumerate(articles, 1):
        title = (art.get("title") or "").strip()
        desc = (art.get("description") or "").strip()[:500]
        if not title and not desc:
            verdicts["UNKNOWN"] += 1
            continue
        prompt = RELEVANCE_PROMPT.format(
            query_desc=query_desc, title=title, description=desc or "(none)"
        )
        try:
            reply = ollama_generate(ollama_base, model, prompt)
            v = parse_verdict(reply)
        except Exception as e:
            if verbose:
                print(f"    [{i}] ollama error: {e}", file=sys.stderr)
            v = "UNKNOWN"
        verdicts[v] += 1
        if verbose:
            print(f"    [{i}] {v}  {title[:80]}")

    n = len(articles)
    # Precision@N counts YES as positive; PARTIAL as half; NO/UNKNOWN as 0.
    precision = (verdicts["YES"] + 0.5 * verdicts["PARTIAL"]) / n if n else None
    return {
        "id": case["id"],
        "n": n,
        "yes": verdicts["YES"],
        "partial": verdicts["PARTIAL"],
        "no": verdicts["NO"],
        "unknown": verdicts["UNKNOWN"],
        "precision": precision,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dashboard", default=DEFAULT_DASHBOARD)
    parser.add_argument("--ollama", default=DEFAULT_OLLAMA)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--query", help="Run only this query id")
    parser.add_argument("--min-precision", type=float, default=0.6)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    cases = json.loads(CASES_FILE.read_text())["queries"]
    if args.query:
        cases = [c for c in cases if c["id"] == args.query]

    # Only score cases that opt in with llm_check=true, unless --query is used
    # to explicitly target one.
    if not args.query:
        cases = [c for c in cases if c.get("llm_check")]
        if not cases:
            print("No queries have llm_check=true in golden_queries.json.")
            print("Add \"llm_check\": true to any query to include it here.")
            print("This scaffold is intentionally opt-in until we wire it into CI.")
            return 0

    print(f"Dashboard: {args.dashboard}")
    print(f"Ollama:    {args.ollama}  model={args.model}")
    print(f"Top-N:     {args.top_n}   min_precision: {args.min_precision}")
    print()

    results = []
    for case in cases:
        print(f"[{case['id']}]  {describe_query(case)}")
        t0 = time.time()
        r = score_query(
            case, args.dashboard, args.ollama, args.model, args.top_n, args.verbose,
        )
        elapsed = time.time() - t0
        results.append(r)
        if r["precision"] is None:
            print(f"  (no articles, skipped)\n")
            continue
        print(
            f"  precision@{r['n']}={r['precision']:.2f}  "
            f"YES={r['yes']} PARTIAL={r['partial']} NO={r['no']} "
            f"UNK={r['unknown']}  ({elapsed:.1f}s)\n"
        )

    print("=== summary ===")
    failures = 0
    for r in results:
        p = r["precision"]
        if p is None:
            print(f"  {r['id']:<40}  (no results)")
            continue
        tag = "PASS" if p >= args.min_precision else "FAIL"
        if tag == "FAIL":
            failures += 1
        print(f"  [{tag}] {r['id']:<40}  {p:.2f}")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
