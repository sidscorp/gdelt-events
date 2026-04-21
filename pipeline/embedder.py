"""Embedding client for semantic pill classification.

Calls Ollama's nomic-embed-text to produce
768-dim embeddings for article text. Used by pill_worker to classify
articles by cosine similarity against a user's natural-language description.
"""

import logging
import os
import math
import time
from urllib.request import Request, urlopen
from urllib.error import URLError
import json

log = logging.getLogger("embedder")

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/embed")
MODEL = "nomic-embed-text:v1.5"
BATCH_SIZE = 256  # texts per API call
TIMEOUT_S = 60
MAX_RETRIES = 3
RETRY_DELAY = 2.0  # seconds


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a list of texts via Ollama. Returns list of 768-dim vectors.

    Handles batching internally — callers can pass any size list.
    Raises on unrecoverable errors after retries.
    """
    if not texts:
        return []

    all_embeddings = []
    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i : i + BATCH_SIZE]
        embs = _embed_batch(batch)
        all_embeddings.extend(embs)
    return all_embeddings


def embed_text(text: str) -> list[float]:
    """Embed a single text. Returns 768-dim vector."""
    result = _embed_batch([text])
    return result[0]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def cosine_similarities_bulk(query_vec: list[float],
                              doc_vecs: list[list[float]]) -> list[float]:
    """Cosine similarity of one query vector against many document vectors.

    Optimised pure-Python path — avoids per-call overhead of cosine_similarity.
    """
    norm_q = math.sqrt(sum(x * x for x in query_vec))
    if norm_q == 0:
        return [0.0] * len(doc_vecs)

    scores = []
    for doc in doc_vecs:
        dot = sum(q * d for q, d in zip(query_vec, doc))
        norm_d = math.sqrt(sum(d * d for d in doc))
        if norm_d == 0:
            scores.append(0.0)
        else:
            scores.append(dot / (norm_q * norm_d))
    return scores


def _embed_batch(texts: list[str]) -> list[list[float]]:
    """Call Ollama API for a single batch with retries."""
    # Prefix for nomic-embed-text retrieval mode
    prefixed = [f"search_document: {t}" for t in texts]
    payload = json.dumps({"model": MODEL, "input": prefixed}).encode()

    for attempt in range(MAX_RETRIES):
        try:
            req = Request(
                OLLAMA_URL,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=TIMEOUT_S) as resp:
                data = json.loads(resp.read())
            embeddings = data["embeddings"]
            if len(embeddings) != len(texts):
                raise ValueError(
                    f"Expected {len(texts)} embeddings, got {len(embeddings)}"
                )
            return embeddings
        except (URLError, TimeoutError, OSError, ValueError) as e:
            if attempt < MAX_RETRIES - 1:
                log.warning(
                    "embed batch attempt %d failed (%s), retrying in %.1fs",
                    attempt + 1, e, RETRY_DELAY,
                )
                time.sleep(RETRY_DELAY * (attempt + 1))
            else:
                raise RuntimeError(
                    f"Embedding failed after {MAX_RETRIES} attempts: {e}"
                ) from e


def embed_query(text: str) -> list[float]:
    """Embed a query text (uses search_query prefix for nomic-embed-text)."""
    payload = json.dumps({
        "model": MODEL,
        "input": [f"search_query: {text}"],
    }).encode()

    for attempt in range(MAX_RETRIES):
        try:
            req = Request(
                OLLAMA_URL,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=TIMEOUT_S) as resp:
                data = json.loads(resp.read())
            return data["embeddings"][0]
        except (URLError, TimeoutError, OSError) as e:
            if attempt < MAX_RETRIES - 1:
                log.warning("embed query attempt %d failed, retrying", attempt + 1)
                time.sleep(RETRY_DELAY * (attempt + 1))
            else:
                raise RuntimeError(
                    f"Query embedding failed after {MAX_RETRIES} attempts: {e}"
                ) from e
