"""Prune embeddings whose URLs are no longer in GAL (60-day rolling retention).

Marks them as 'pruned' in the manifest. The vectors stay in vectors.bin
until the next FAISS rebuild, which only includes 'active' embeddings.

Run daily.

Usage:
    python prune_embeddings.py
"""

import logging
import sys
import time
from pathlib import Path

import duckdb

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("prune")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import embedding_store

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "gdelt.duckdb"


def main():
    embedding_store.init()

    s = embedding_store.stats()
    log.info("Before prune: %d active, %d pruned", s["active"], s["pruned"])

    # Get all active URLs from the manifest
    log.info("Loading active URLs from manifest...")
    con = embedding_store._conn()
    active_urls = {r[0] for r in con.execute(
        "SELECT url FROM embeddings WHERE status='active'"
    ).fetchall()}
    con.close()
    log.info("Active URLs in store: %d", len(active_urls))

    # Get all current URLs from GAL
    log.info("Loading current URLs from GAL...")
    duck = duckdb.connect(str(DB_PATH), read_only=False)
    duck.execute("SET threads = 4")
    gal_urls = {r[0] for r in duck.execute(
        "SELECT url FROM gal WHERE language = 'en'"
    ).fetchall()}
    duck.close()
    log.info("URLs in GAL: %d", len(gal_urls))

    # URLs in store but no longer in GAL = pruned by retention
    to_prune = active_urls - gal_urls
    log.info("URLs to prune: %d", len(to_prune))

    if not to_prune:
        log.info("Nothing to prune.")
        return

    # Mark in batches
    BATCH = 5000
    pruned_total = 0
    to_prune_list = list(to_prune)
    for i in range(0, len(to_prune_list), BATCH):
        batch = to_prune_list[i:i + BATCH]
        n = embedding_store.mark_pruned(batch)
        pruned_total += n

    s = embedding_store.stats()
    log.info("After prune: %d active, %d pruned (marked %d this run)",
             s["active"], s["pruned"], pruned_total)


if __name__ == "__main__":
    main()
