"""Build FAISS IVF-PQ index from the embedding store's active vectors.

Uses product quantization to compress vectors from 3KB to ~96 bytes each,
keeping the index under 1GB (vs 37GB+ for HNSW which OOM'd). Trade-off
is ~90-95% recall vs ~99% for HNSW — fine for search.

Run nightly or after large batch updates. Atomically swaps the index file.

Usage:
    python build_faiss_index.py
"""

import json
import logging
import sys
import time
from pathlib import Path

import faiss
import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("build_faiss")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import embedding_store

# Single source of truth for the embeddings dir (honors GDELT_DATA_DIR).
BASE_DIR = embedding_store.BASE_DIR
INDEX_PATH = BASE_DIR / "articles.faiss"
URLS_FILE = BASE_DIR / "index_urls.txt"
META_JSON = BASE_DIR / "index_meta.json"
INDEX_TMP = BASE_DIR / "articles.faiss.tmp"
URLS_TMP = BASE_DIR / "index_urls.txt.tmp"

EMBED_DIM = 768

# IVF-PQ parameters (memory-efficient alternative to HNSW)
NLIST = 4096        # Voronoi cells for partitioning
M_PQ = 48           # sub-quantizers (768 / 48 = 16 dims each)
NBITS = 8           # bits per sub-quantizer code
TRAIN_SAMPLE = 200_000  # vectors to train codebook on
NPROBE = 64         # cells to probe at search time (higher = better recall)


def main():
    embedding_store.init()

    log.info("Loading active vectors and URLs from store...")
    t0 = time.time()
    vectors, urls = embedding_store.get_active_vectors_and_urls()
    log.info("Loaded %d vectors in %.1fs (%.1f GB)",
             len(vectors), time.time() - t0, vectors.nbytes / 1e9)

    if len(vectors) == 0:
        log.error("No active vectors. Aborting.")
        return

    # Normalize for cosine similarity via inner product
    log.info("Normalizing vectors...")
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    vectors /= norms

    n = vectors.shape[0]
    log.info("Building IVF-PQ index: n=%d, d=%d, nlist=%d, m_pq=%d",
             n, EMBED_DIM, NLIST, M_PQ)

    # Build IVF-PQ index
    quantizer = faiss.IndexFlatIP(EMBED_DIM)
    index = faiss.IndexIVFPQ(quantizer, EMBED_DIM, NLIST, M_PQ, NBITS,
                              faiss.METRIC_INNER_PRODUCT)

    # Train on a representative sample
    train_n = min(TRAIN_SAMPLE, n)
    log.info("Training codebook on %d sample vectors...", train_n)
    t0 = time.time()
    sample_idx = np.random.choice(n, train_n, replace=False)
    index.train(vectors[sample_idx])
    log.info("Training done in %.1fs", time.time() - t0)

    # Add all vectors
    log.info("Adding %d vectors to index...", n)
    t0 = time.time()
    index.add(vectors)
    elapsed = time.time() - t0
    log.info("Index built in %.1fs (%.0f vectors/sec)", elapsed, n / elapsed)

    # Set search-time probe count
    index.nprobe = NPROBE

    # Atomic swap: write to .tmp, then rename
    log.info("Writing index to temp file...")
    faiss.write_index(index, str(INDEX_TMP))
    with open(URLS_TMP, "w", encoding="utf-8") as f:
        for u in urls:
            f.write(u + "\n")

    INDEX_TMP.replace(INDEX_PATH)
    URLS_TMP.replace(URLS_FILE)

    # Save metadata
    meta = {
        "n_vectors": n,
        "dim": EMBED_DIM,
        "metric": "inner_product (cosine on normalized vectors)",
        "index_type": "IVF-PQ",
        "nlist": NLIST,
        "m_pq": M_PQ,
        "nbits": NBITS,
        "nprobe": NPROBE,
        "train_sample": train_n,
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(META_JSON, "w") as f:
        json.dump(meta, f, indent=2)

    size_mb = INDEX_PATH.stat().st_size / 1e6
    log.info("DONE. Index: %.1f MB at %s", size_mb, INDEX_PATH)


if __name__ == "__main__":
    main()
