"""Embed any GAL articles not yet in the embedding store.

Designed to run periodically (every 15 min via scheduler) or on-demand.
Finds URLs in the gal table that aren't in the manifest, embeds them
in batches via Ollama, and appends to the store.

Also serves as the gap-filler for articles that were in the original
URL list but never got embedded (zero vectors / trailing orphans).

Usage:
    python embed_new_articles.py              # process up to MAX_PER_RUN
    python embed_new_articles.py --all        # process everything (no cap)
    python embed_new_articles.py --status     # show current state
"""

import logging
import sys
import time
from pathlib import Path

import duckdb

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("embed_new")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import embedding_store
from embedder import embed_texts

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "gdelt.duckdb"
BATCH_SIZE = 256
MAX_PER_RUN = 50_000  # cap per run to keep cycle short
DUCKDB_CHUNK = 5_000  # rows per DuckDB query


def find_missing_urls(con, max_count: int) -> list[tuple[str, str, str]]:
    """Find GAL URLs (English, non-empty) not yet in the manifest.
    Returns up to max_count rows of (url, title, description).
    """
    # Stream through GAL in crawled_at order, batch-check against manifest
    found = []
    cursor_ts = 0

    while len(found) < max_count:
        chunk = con.execute(
            "SELECT url, crawled_at, title, description FROM gal "
            "WHERE language = 'en' AND crawled_at > ? "
            "ORDER BY crawled_at LIMIT ?",
            [cursor_ts, DUCKDB_CHUNK],
        ).fetchall()
        if not chunk:
            break

        cursor_ts = chunk[-1][1]
        urls = [row[0] for row in chunk]
        missing = set(embedding_store.get_missing_urls(urls))

        for url, _, title, desc in chunk:
            if url in missing:
                found.append((url, title or "", desc or ""))
                if len(found) >= max_count:
                    break

    return found


def embed_and_store(items: list[tuple[str, str, str]]):
    """Embed a list of (url, title, description) items and store them."""
    total = 0
    for i in range(0, len(items), BATCH_SIZE):
        batch = items[i:i + BATCH_SIZE]
        urls = [b[0] for b in batch]
        texts = []
        for _, title, desc in batch:
            text = title or ""
            if desc:
                text += " " + desc
            text = text.strip()[:512]
            if not text:
                text = "empty"
            texts.append(text)

        try:
            vecs = embed_texts(texts)
        except Exception as e:
            log.warning("Embed batch failed: %s", e)
            continue

        embedding_store.append_embeddings(list(zip(urls, vecs)))
        total += len(batch)
        if total % 1000 == 0:
            log.info("  embedded %d / %d", total, len(items))

    return total


def status():
    embedding_store.init()
    s = embedding_store.stats()
    log.info("Embedding store status:")
    log.info("  Active vectors:  %d", s["active"])
    log.info("  Pruned vectors:  %d", s["pruned"])
    log.info("  Total file rows: %d", s["total_rows"])
    log.info("  Vectors file:    %.1f MB", s["vectors_file_mb"])

    con = duckdb.connect(str(DB_PATH), read_only=True)
    n_gal = con.execute(
        "SELECT count(*) FROM gal WHERE language = 'en'"
    ).fetchone()[0]
    log.info("  GAL English:     %d articles", n_gal)
    log.info("  Coverage:        %.1f%%", s["active"] / max(n_gal, 1) * 100)
    con.close()


def main():
    args = sys.argv[1:]

    if "--status" in args:
        status()
        return

    embedding_store.init()
    max_per_run = MAX_PER_RUN if "--all" not in args else 10_000_000
    log.info("Looking for missing URLs (max %d)...", max_per_run)

    con = duckdb.connect(str(DB_PATH), read_only=True)
    con.execute("SET threads = 2")

    try:
        t0 = time.time()
        missing = find_missing_urls(con, max_per_run)
        log.info("Found %d missing URLs in %.1fs", len(missing), time.time() - t0)

        if not missing:
            log.info("Nothing to embed.")
            return

        t0 = time.time()
        n = embed_and_store(missing)
        elapsed = time.time() - t0
        rate = n / elapsed if elapsed > 0 else 0
        log.info("Embedded %d articles in %.1fs (%.0f/sec)", n, elapsed, rate)

        s = embedding_store.stats()
        log.info("Store now has %d active vectors", s["active"])
    finally:
        con.close()


if __name__ == "__main__":
    main()
