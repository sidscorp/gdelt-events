"""Semantic search backend — loads FAISS index once, serves queries.

Lazy-loaded singleton: the index loads on first query and stays in memory.
Loading takes a few seconds and uses ~1GB RAM.
"""

import logging
import sys
import threading
import time
from pathlib import Path

import numpy as np

log = logging.getLogger("semantic_search")

BASE_DIR = Path(__file__).resolve().parent.parent / "data" / "embeddings"
INDEX_PATH = BASE_DIR / "articles.faiss"
URLS_TXT = BASE_DIR / "index_urls.txt"  # written by build_faiss_index.py, aligned with FAISS

# Add pipeline dir for embedder
_PIPELINE = str(Path(__file__).resolve().parent.parent / "pipeline")
if _PIPELINE not in sys.path:
    sys.path.insert(0, _PIPELINE)


_index = None
_urls = None
_load_lock = threading.Lock()


def _load():
    """Load FAISS index and URL list (idempotent, thread-safe)."""
    global _index, _urls
    if _index is not None:
        return
    with _load_lock:
        if _index is not None:
            return
        import faiss

        if not INDEX_PATH.exists():
            raise RuntimeError(f"FAISS index not found at {INDEX_PATH}")

        log.info("Loading FAISS index from %s...", INDEX_PATH)
        t0 = time.time()
        _index = faiss.read_index(str(INDEX_PATH))
        # Set nprobe for IVF-PQ indexes (no-op for other index types)
        try:
            _index.nprobe = 64
        except Exception:
            pass
        log.info("FAISS index loaded in %.1fs (%d vectors)",
                 time.time() - t0, _index.ntotal)

        log.info("Loading URL list from %s...", URLS_TXT)
        t0 = time.time()
        with open(URLS_TXT, encoding="utf-8") as f:
            _urls = [line.rstrip("\n") for line in f]
        log.info("Loaded %d URLs in %.1fs", len(_urls), time.time() - t0)

        if len(_urls) != _index.ntotal:
            log.warning("URL count (%d) != index size (%d). Truncating to min.",
                        len(_urls), _index.ntotal)


def is_ready() -> bool:
    """True if the index is loaded and ready for queries."""
    return _index is not None


def index_size() -> int:
    """Number of vectors in the loaded index, or 0 if not loaded."""
    return _index.ntotal if _index is not None else 0


def search(query_text: str, k: int = 100) -> list[tuple[str, float]]:
    """Semantic search: returns top-K (url, score) tuples by cosine similarity.

    Lazy-loads the index on first call. score is in [-1, 1] (cosine similarity).
    """
    _load()
    from embedder import embed_query

    # Embed the query
    qvec = embed_query(query_text)
    qarr = np.array([qvec], dtype="float32")

    # Normalize for cosine similarity (index uses inner product on normalized vectors)
    norm = np.linalg.norm(qarr)
    if norm > 0:
        qarr = qarr / norm

    # Search
    scores, indices = _index.search(qarr, k)

    # Map back to URLs
    results = []
    for i in range(len(indices[0])):
        idx = int(indices[0][i])
        if idx < 0 or idx >= len(_urls):
            continue
        score = float(scores[0][i])
        results.append((_urls[idx], score))
    return results
