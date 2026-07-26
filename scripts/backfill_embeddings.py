"""Backfill the embedding gap left by the 2026-07-15..26 Ollama outage.

Deliberately does NOT chain pill_scorer, unlike embed_new_articles.py's main().
Embedding is free (local Ollama on the GPU); judging costs money through the
LLM gateway, so that decision is kept separate and explicit.

Emits a timing profile as it goes: per-batch latency, rolling and cumulative
throughput, and an ETA, so we learn what the pipeline actually sustains rather
than guessing from a single 1250-article sample.

Usage:
    python backfill_embeddings.py [--hours N] [--limit N] [--dry-run]
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import duckdb

REPO = Path(r"C:\Users\siddh\Code_Library\gdelt-events")
sys.path.insert(0, str(REPO / "pipeline"))

import embedding_store                       # noqa: E402
from embedder import embed_texts             # noqa: E402
from embed_new_articles import find_missing_urls, BATCH_SIZE  # noqa: E402

DB_PATH = REPO / "data" / "gdelt.duckdb"
PROFILE_PATH = REPO / "data" / "logs" / "backfill_profile.json"

# force=True is essential: embed_new_articles calls logging.basicConfig at
# import time, so without it our handlers are silently ignored (root already
# has one) and everything goes to a stdout the scheduled task discards.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(REPO / "data" / "logs" / "backfill_embeddings.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
    force=True,
)
log = logging.getLogger("backfill")


def human(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=336,
                    help="lookback window; 336 = 14d, wide enough for the outage gap")
    ap.add_argument("--limit", type=int, default=10_000_000)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    embedding_store.init()

    log.info("=== backfill start (hours=%d, limit=%d) ===", args.hours, args.limit)
    t_scan = time.time()
    con = duckdb.connect(str(DB_PATH))
    con.execute("SET threads = 2")
    missing = find_missing_urls(con, args.limit, hours_back=args.hours)
    con.close()  # release immediately - the dashboard shares this database
    scan_s = time.time() - t_scan
    log.info("scan: found %d missing URLs in %.1fs", len(missing), scan_s)

    if args.dry_run:
        log.info("dry run - stopping before embedding")
        return
    if not missing:
        log.info("nothing to embed")
        return

    total = len(missing)
    n_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
    log.info("embedding %d articles in %d batches of %d", total, n_batches, BATCH_SIZE)

    batch_times = []
    done = 0
    failed = 0
    t_start = time.time()
    t_window = time.time()
    window_done = 0

    for i in range(0, total, BATCH_SIZE):
        batch = missing[i:i + BATCH_SIZE]
        urls = [b[0] for b in batch]
        texts = []
        for _, title, desc in batch:
            text = (title or "")
            if desc:
                text += " " + desc
            text = text.strip()[:512] or "empty"
            texts.append(text)

        t_b = time.time()
        try:
            vecs = embed_texts(texts)
        except Exception as e:
            failed += len(batch)
            log.warning("batch %d failed: %s", i // BATCH_SIZE, e)
            continue
        embed_s = time.time() - t_b

        t_s = time.time()
        embedding_store.append_embeddings(list(zip(urls, vecs)))
        store_s = time.time() - t_s

        batch_times.append({"n": len(batch), "embed_s": round(embed_s, 3),
                            "store_s": round(store_s, 3)})
        done += len(batch)
        window_done += len(batch)

        # Report every ~20 batches: rolling rate is what you'd actually feel,
        # cumulative is what predicts the finish.
        if (i // BATCH_SIZE) % 20 == 0 and i > 0:
            now = time.time()
            roll = window_done / (now - t_window) if now > t_window else 0
            cum = done / (now - t_start) if now > t_start else 0
            remaining = (total - done) / cum if cum > 0 else 0
            log.info("  %d/%d (%.1f%%)  rolling %.0f/s  cum %.0f/s  eta %s",
                     done, total, 100 * done / total, roll, cum, human(remaining))
            t_window = now
            window_done = 0

    elapsed = time.time() - t_start
    rate = done / elapsed if elapsed else 0
    log.info("=== embedded %d articles in %s (%.0f/sec), %d failed ===",
             done, human(elapsed), rate, failed)

    embed_only = [b["embed_s"] for b in batch_times]
    store_only = [b["store_s"] for b in batch_times]
    if embed_only:
        embed_only_sorted = sorted(embed_only)
        p50 = embed_only_sorted[len(embed_only_sorted) // 2]
        p95 = embed_only_sorted[int(len(embed_only_sorted) * 0.95)]
        log.info("per-batch embed: p50 %.2fs  p95 %.2fs  (batch=%d => %.0f/s at p50)",
                 p50, p95, BATCH_SIZE, BATCH_SIZE / p50 if p50 else 0)
        log.info("per-batch store: total %.1fs (%.1f%% of wall time)",
                 sum(store_only), 100 * sum(store_only) / elapsed if elapsed else 0)

    profile = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "scan_seconds": round(scan_s, 1),
        "articles": done,
        "failed": failed,
        "elapsed_seconds": round(elapsed, 1),
        "rate_per_sec": round(rate, 1),
        "batch_size": BATCH_SIZE,
        "batches": batch_times,
    }
    PROFILE_PATH.write_text(json.dumps(profile, indent=2), encoding="utf-8")
    log.info("timing profile written to %s", PROFILE_PATH)

    s = embedding_store.stats()
    log.info("store now has %d active vectors", s["active"])
    log.info("NOTE: pill_scorer deliberately NOT run - judging is a separate cost decision")


if __name__ == "__main__":
    main()
