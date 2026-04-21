"""Migrate from chunked vectors_NNN.npy + urls.txt to the embedding_store.

Reads the chunked files, identifies zero vectors (from prior crashes),
maps them to URLs, and writes only valid (url, vector) pairs into the
new store. Reports how many were skipped/missing.

Run once after install. Idempotent — won't duplicate if rerun.

Usage:
    python migrate_to_store.py
"""

import logging
import sys
import time
from pathlib import Path

import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("migrate")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import embedding_store

BASE_DIR = Path(__file__).resolve().parent.parent / "data" / "embeddings"
URLS_TXT = BASE_DIR / "urls.txt"
EMBED_DIM = 768


def main():
    embedding_store.init()

    existing = embedding_store.active_count()
    if existing > 0:
        log.warning("Store already has %d active embeddings. Skipping migration "
                    "to avoid duplicates. Delete manifest.db and vectors.bin to redo.",
                    existing)
        return

    # Load URLs
    log.info("Loading URLs from %s...", URLS_TXT)
    with open(URLS_TXT, encoding="utf-8") as f:
        urls = [line.rstrip("\n") for line in f]
    log.info("Total URLs: %d", len(urls))

    # Load chunks one at a time, processing in place
    chunk_files = sorted(BASE_DIR.glob("vectors_*.npy"))
    log.info("Found %d chunk files", len(chunk_files))

    total_processed = 0
    total_zero = 0
    total_migrated = 0
    url_idx = 0  # cursor into URL list

    BATCH_SIZE = 5000  # write to store in batches

    for cf in chunk_files:
        log.info("Loading %s...", cf.name)
        chunk = np.load(str(cf))
        n_rows = chunk.shape[0]

        # Available URLs for this chunk
        chunk_urls = urls[url_idx:url_idx + n_rows]
        if len(chunk_urls) < n_rows:
            log.warning("URL underrun at chunk %s: %d urls for %d vectors",
                        cf.name, len(chunk_urls), n_rows)
            n_rows = len(chunk_urls)
            chunk = chunk[:n_rows]

        # Find zero vectors (broken from crashes)
        norms = np.linalg.norm(chunk, axis=1)
        zero_mask = norms == 0
        n_zero = int(zero_mask.sum())
        n_valid = n_rows - n_zero

        log.info("  rows=%d valid=%d zero=%d", n_rows, n_valid, n_zero)

        # Batch-insert valid (url, vector) pairs
        batch = []
        for i in range(n_rows):
            if zero_mask[i]:
                continue
            batch.append((chunk_urls[i], chunk[i].tolist()))
            if len(batch) >= BATCH_SIZE:
                embedding_store.append_embeddings(batch)
                total_migrated += len(batch)
                batch = []

        if batch:
            embedding_store.append_embeddings(batch)
            total_migrated += len(batch)

        total_processed += n_rows
        total_zero += n_zero
        url_idx += n_rows

        # Free memory
        del chunk

    # Trailing URLs without any vectors at all
    n_orphan = len(urls) - url_idx
    log.info("Trailing URLs without vectors (will be re-embedded later): %d",
             n_orphan)

    log.info("MIGRATION COMPLETE")
    log.info("  Total processed: %d", total_processed)
    log.info("  Migrated:        %d", total_migrated)
    log.info("  Zero vectors:    %d (will be re-embedded)", total_zero)
    log.info("  Trailing orphan: %d (will be re-embedded)", n_orphan)
    log.info("  Store stats:     %s", embedding_store.stats())


if __name__ == "__main__":
    main()
