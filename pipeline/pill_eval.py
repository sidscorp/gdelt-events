"""LLM-judged precision evaluation for dashboard pill categories.

For each pill, samples recent tagged articles and asks an LLM judge
(cerebras-fast via the llm.snambiar.com gateway) whether each article is
actually relevant to the pill's intent. Produces a per-pill precision table
and a JSON report under data/pill_eval/.

Usage:
    python -m pipeline.pill_eval                 # all pills, N=40
    python -m pipeline.pill_eval --pills supply_chain,fda --n 40
    python -m pipeline.pill_eval --categories supply_chain__v2   # shadow cats

Precision = relevant / (relevant + irrelevant); borderline counted half.
Cost: ~$0.02 for a full run on cerebras-fast.
"""

import argparse
import json
import random
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from .config import DATA_DIR, DB_PATH

GATEWAY_URL = "https://llm.snambiar.com/v1/chat/completions"
# Fireworks gpt-oss-120b: ~10x cheaper than Cerebras GLM at observed blended
# rates ($9.35/day burn on 2026-07-08 -> ~$0.10-0.15/day). Validated against
# Cerebras-judged pills before the switch (agreement eval in pill_eval history).
JUDGE_MODEL = "accounts/fireworks/models/gpt-oss-120b"
KEY_PATH = DATA_DIR / ".openrouter_key"
OUT_DIR = DATA_DIR / "pill_eval"
BATCH = 20  # articles per judge call

# What each pill is SUPPOSED to contain — the judge's ground truth.
# Keep these aligned with dashboard/views.py descriptions (and future
# CATEGORIES[...]["description"] fields).
PILL_INTENTS: dict[str, str] = {
    "supply_chain": (
        "Events that disrupt the production, sourcing, manufacture, or transport "
        "of goods — factory incidents, port/logistics disruptions, export controls, "
        "tariffs, shortages, recalls — where the supply-chain/trade impact is part "
        "of the story, not just any disaster or geopolitical event."
    ),
    "medical_devices": (
        "News about medical devices and the medical device industry: specific "
        "device types (implants, scanners, surgical tools, diagnostics), device "
        "companies' products, regulatory clearances, clinical use."
    ),
    "fda": (
        "News involving FDA-registered medical device manufacturers acting AS "
        "medical device companies: product launches, recalls, clinical trials, "
        "regulatory actions, industry deals. NOT routine stock-market coverage "
        "(13F filings, analyst ratings, shareholder suits) and NOT companies "
        "appearing in unrelated contexts."
    ),
    "ai_general": (
        "Artificial intelligence news: model releases, AI companies, research "
        "breakthroughs, AI investments and adoption, the AI industry broadly."
    ),
    "ai_regulation": (
        "AI governance and regulation: legislation, executive orders, safety "
        "frameworks, standards bodies, compliance, court cases about AI."
    ),
    "ai_defense": (
        "Military and defense applications of AI: autonomous weapons, defense "
        "contracts for AI systems, military AI policy, AI in warfare."
    ),
    "ai_sector_impact": (
        "AI applied in specific industries (healthcare, finance, education, "
        "manufacturing, agriculture, law, creative work) — adoption, impact, "
        "sector-specific AI products."
    ),
    "semiconductors": (
        "Semiconductor industry news: chipmakers, fabs, chip supply, export "
        "controls on chips, chip technology advances, semiconductor policy."
    ),
    "oss_vulnerabilities": (
        "Security vulnerabilities and exploits in open source software and "
        "package ecosystems: CVEs, malicious packages, supply-chain attacks on "
        "software, patches and advisories."
    ),
    "cyber_attacks": (
        "Cyberattack incidents and campaigns: data breaches, ransomware, "
        "nation-state operations, hacking groups, major security incidents."
    ),
    # New categories (evaluated once they exist)
    "geopolitics_conflict": (
        "Geopolitics and armed conflict: wars, military operations, ceasefires, "
        "sanctions, coups, major diplomacy, interstate tensions."
    ),
    "energy_climate": (
        "Energy and climate: oil/gas markets, electricity grids, renewables, "
        "nuclear power, climate policy, and climate-driven events framed by "
        "their energy/infrastructure impact."
    ),
    "public_health": (
        "Public health: disease outbreaks, epidemics, vaccines and drug "
        "approvals, health systems, health policy."
    ),
    "fda_agency": (
        "News about the U.S. Food and Drug Administration as a regulatory "
        "body: drug and device approvals, recalls, warning letters, facility "
        "inspections, advisory committee meetings, policy changes and "
        "rulemaking, and FDA leadership decisions. NOT routine stock-market "
        "coverage or company financials that merely mention FDA approval."
    ),
    "nih_news": (
        "News about the National Institutes of Health: research funding and "
        "grants, major study findings, institute directors and leadership, "
        "budget and policy affecting biomedical research."
    ),
    "cms_news": (
        "News about the Centers for Medicare & Medicaid Services: rulemaking, "
        "reimbursement changes, Medicare and Medicaid policy, enrollment, and "
        "agency leadership. NOT general political debate about healthcare "
        "spending or entitlement reform."
    ),
    "va_news": (
        "News about the U.S. Department of Veterans Affairs (the federal "
        "agency): VA healthcare and hospitals, veterans' benefits and claims, "
        "VA leadership, and veterans services. NOT general military news, VFW/"
        "American Legion advocacy, foreign veterans, or political campaigns "
        "about veterans issues without VA action."
    ),
}


def _intent_for(category: str) -> str:
    base = category.replace("__v2", "").replace("custom_", "")
    if base in PILL_INTENTS:
        return PILL_INTENTS[base]
    raise SystemExit(f"No PILL_INTENT defined for category '{category}'")


def _get_key() -> str:
    return KEY_PATH.read_text().strip()


def _judge_call(prompt: str, retries: int = 3) -> str:
    payload = json.dumps({
        "model": JUDGE_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 3000,
        "temperature": 0.0,
    }).encode()
    req = urllib.request.Request(GATEWAY_URL, data=payload, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_get_key()}",
    })
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
            return (body.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
        except Exception as e:  # transient gateway/provider hiccups
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"judge call failed after {retries} tries: {last}")


def _parse_verdicts(text: str, expected_n: int) -> list[dict]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text)
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        raise ValueError("no JSON array in judge response")
    arr = json.loads(text[start:end + 1])
    out = []
    for item in arr:
        if isinstance(item, dict) and item.get("verdict") in ("relevant", "borderline", "irrelevant"):
            out.append({"n": item.get("n"), "verdict": item["verdict"],
                        "reason": str(item.get("reason", ""))[:200]})
    if len(out) < expected_n * 0.8:
        raise ValueError(f"judge returned {len(out)}/{expected_n} usable verdicts")
    return out


def _sample_articles(con, category: str, n: int, days: int) -> list[dict]:
    """Sample n recent members of a pill with title+description from gal_recent."""
    cutoff = int((datetime.utcnow() - timedelta(days=days)).strftime("%Y%m%d%H%M%S"))
    # Deterministic pseudo-random sample via hash ordering (USING SAMPLE
    # applies to the table BEFORE the WHERE filter — useless here).
    k = int(n) * 3
    if category == "fda":
        rows = con.execute(
            "SELECT DISTINCT article_id FROM fda_match_cache "
            "WHERE source_type='gal' AND crawled_at >= ? AND match_type IN ('legal','contextual') "
            f"ORDER BY hash(article_id) LIMIT {k}",
            [cutoff],
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT DISTINCT article_id FROM article_tags "
            "WHERE category = ? AND source_type='gal' AND crawled_at >= ? "
            f"ORDER BY hash(article_id) LIMIT {k}",
            [category, cutoff],
        ).fetchall()
    urls = [r[0] for r in rows]
    if not urls:
        return []
    out = []
    for i in range(0, len(urls), 400):
        chunk = urls[i:i + 400]
        ph = ",".join(["?"] * len(chunk))
        for url, title, desc in con.execute(
            f"SELECT url, title, description FROM gal_recent WHERE url IN ({ph})", chunk,
        ).fetchall():
            t = (title or "").strip()
            if len(t) > 10:
                out.append({"url": url, "title": t[:200], "desc": (desc or "").strip()[:300]})
    random.Random(42).shuffle(out)
    return out[:n]


def eval_pill(con, category: str, n: int, days: int) -> dict:
    intent = _intent_for(category)
    articles = _sample_articles(con, category, n, days)
    if not articles:
        return {"category": category, "sampled": 0, "precision": None, "verdicts": []}

    verdicts: list[dict] = []
    for i in range(0, len(articles), BATCH):
        batch = articles[i:i + BATCH]
        numbered = "\n".join(
            f"{j+1}. {a['title']}" + (f" — {a['desc']}" if a['desc'] else "")
            for j, a in enumerate(batch)
        )
        prompt = (
            "You are auditing a news-topic classifier. The topic is:\n"
            f"\"{intent}\"\n\n"
            f"For EACH numbered article below, judge whether it belongs to that topic.\n"
            "verdict must be one of: relevant | borderline | irrelevant.\n"
            "Judge by the article's actual subject, not by shared keywords.\n\n"
            f"Articles:\n{numbered}\n\n"
            "Output ONLY a JSON array (no prose, no code fence), one element per "
            'article: {"n": <number>, "verdict": "...", "reason": "<10 words max>"}'
        )
        parsed = _parse_verdicts(_judge_call(prompt), len(batch))
        for item in parsed:
            idx = (item.get("n") or 0) - 1
            if 0 <= idx < len(batch):
                item["title"] = batch[idx]["title"]
                item["url"] = batch[idx]["url"]
        verdicts.extend(parsed)

    rel = sum(1 for v in verdicts if v["verdict"] == "relevant")
    bord = sum(1 for v in verdicts if v["verdict"] == "borderline")
    irr = sum(1 for v in verdicts if v["verdict"] == "irrelevant")
    denom = rel + bord + irr
    precision = (rel + 0.5 * bord) / denom if denom else None
    return {
        "category": category, "sampled": len(articles),
        "relevant": rel, "borderline": bord, "irrelevant": irr,
        "precision": round(precision, 3) if precision is not None else None,
        "verdicts": verdicts,
    }


DEFAULT_PILLS = [
    "supply_chain", "medical_devices", "fda", "ai_general", "ai_regulation",
    "ai_defense", "ai_sector_impact", "semiconductors",
    "oss_vulnerabilities", "cyber_attacks",
    # These five have had PILL_INTENTS all along but were never in this list,
    # so they had never once been precision-measured — while /methodology
    # claimed "every pill measures roughly 75-94%".
    "geopolitics_conflict", "energy_climate", "public_health",
    "fda_agency", "nih_news", "cms_news", "va_news",
]


def _read_con(retries: int = 30):
    """Read-only connection for the eval.

    This eval only ever SELECTs, and it interleaves those reads with judge
    calls that take minutes. Opening it read-WRITE (as it did) held DuckDB's
    single writer lock across every LLM call, which 503s the live dashboard
    for the whole run — so measuring precision took the site down, and the
    more pills you measured the longer it stayed down.

    Retries through the ingest/scorer write windows, and sets the Windows
    spill dir: without temp_directory a read-only connection that hits its
    memory limit fails the spill and poisons every later query on it.
    """
    import duckdb
    last = None
    for _ in range(retries):
        try:
            con = duckdb.connect(str(DB_PATH), read_only=True)
            con.execute("SET threads = 2")
            con.execute("SET memory_limit = '4GB'")
            con.execute(f"SET temp_directory='{(DB_PATH.parent / 'duckdb_tmp').as_posix()}'")
            return con
        except duckdb.IOException as e:
            last = e
            time.sleep(2)
    raise RuntimeError(f"could not open read-only connection: {last}")


def main():
    parser = argparse.ArgumentParser(description="LLM-judged pill precision eval")
    parser.add_argument("--pills", help="comma-separated categories (default: all curated + fda)")
    parser.add_argument("--n", type=int, default=40)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--label", default="", help="tag for the output file, e.g. baseline")
    args = parser.parse_args()

    pills = [p.strip() for p in args.pills.split(",")] if args.pills else DEFAULT_PILLS
    con = _read_con()

    results = []
    try:
        for cat in pills:
            t0 = time.time()
            try:
                r = eval_pill(con, cat, args.n, args.days)
            except Exception as e:
                r = {"category": cat, "error": str(e), "precision": None, "sampled": 0}
            r["elapsed_s"] = round(time.time() - t0, 1)
            results.append(r)
            p = r.get("precision")
            print(f"{cat:26s} sampled={r.get('sampled', 0):3d} "
                  f"precision={p if p is not None else 'n/a':>5} "
                  f"(rel={r.get('relevant', '-')} bord={r.get('borderline', '-')} "
                  f"irr={r.get('irrelevant', '-')}) {r.get('error', '')}", flush=True)
    finally:
        con.close()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    label = f"_{args.label}" if args.label else ""
    out = OUT_DIR / f"{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}{label}.json"
    out.write_text(json.dumps({"ran_at": datetime.utcnow().isoformat(),
                               "n": args.n, "days": args.days,
                               "results": results}, indent=1))
    print(f"\nreport: {out}")

    print("\n=== worst offenders (irrelevant examples) ===")
    for r in results:
        bad = [v for v in r.get("verdicts", []) if v["verdict"] == "irrelevant"][:3]
        if bad:
            print(f"\n{r['category']} (precision {r.get('precision')}):")
            for v in bad:
                print(f"  - {v.get('title', '?')[:90]}  [{v.get('reason', '')}]")


if __name__ == "__main__":
    main()
