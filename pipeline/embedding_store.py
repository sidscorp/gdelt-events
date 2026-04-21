"""Unified embedding store for GAL article vectors.

Architecture:
  - SQLite manifest: maps url -> row_index, tracks status (active/pruned)
  - Append-only vectors file: vectors.bin (raw float32, 768 dims per row)
  - FAISS index: rebuilt periodically from active vectors

Design goals:
  - Atomic appends (one article at a time, crash-safe)
  - Easy gap detection (find URLs in GAL not yet embedded)
  - Easy pruning (find URLs in store no longer in GAL)
  - Fast FAISS rebuild from active subset

The store is the single source of truth for embeddings. The chunked
vectors_NNN.npy files from the initial bulk job will be migrated into
this store on first use.
"""

import logging
import sqlite3
import struct
import sys
import threading
import time
from pathlib import Path

import numpy as np

log = logging.getLogger("embedding_store")

BASE_DIR = Path(__file__).resolve().parent.parent / "data" / "embeddings"
MANIFEST_DB = BASE_DIR / "manifest.db"
VECTORS_BIN = BASE_DIR / "vectors.bin"
FAISS_INDEX = BASE_DIR / "articles.faiss"

EMBED_DIM = 768
ROW_BYTES = EMBED_DIM * 4  # float32

_lock = threading.Lock()


def _conn():
    """Open SQLite manifest connection."""
    con = sqlite3.connect(str(MANIFEST_DB), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def init():
    """Initialize manifest and vectors file."""
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    con = _conn()
    con.execute("""
        CREATE TABLE IF NOT EXISTS embeddings (
            url TEXT PRIMARY KEY,
            row_index INTEGER NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'active',
            embedded_at TEXT DEFAULT (datetime('now'))
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_status ON embeddings(status)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_row ON embeddings(row_index)")
    con.commit()
    con.close()

    if not VECTORS_BIN.exists():
        VECTORS_BIN.touch()


def total_rows() -> int:
    """Total rows in vectors.bin (including pruned slots)."""
    if not VECTORS_BIN.exists():
        return 0
    return VECTORS_BIN.stat().st_size // ROW_BYTES


def active_count() -> int:
    """Number of active (non-pruned) embeddings."""
    con = _conn()
    n = con.execute("SELECT count(*) FROM embeddings WHERE status='active'").fetchone()[0]
    con.close()
    return n


def has_url(url: str) -> bool:
    """Check if URL is already embedded (any status)."""
    con = _conn()
    row = con.execute("SELECT 1 FROM embeddings WHERE url=?", (url,)).fetchone()
    con.close()
    return row is not None


def get_missing_urls(urls: list[str]) -> list[str]:
    """Of the given URLs, return those NOT yet embedded."""
    if not urls:
        return []
    con = _conn()
    # Use temp table for efficient bulk check
    con.execute("CREATE TEMP TABLE _check (url TEXT PRIMARY KEY)")
    con.executemany("INSERT OR IGNORE INTO _check VALUES (?)", [(u,) for u in urls])
    rows = con.execute(
        "SELECT c.url FROM _check c LEFT JOIN embeddings e ON e.url = c.url "
        "WHERE e.url IS NULL"
    ).fetchall()
    con.close()
    return [r[0] for r in rows]


def append_embeddings(items: list[tuple[str, list[float]]]):
    """Append (url, vector) pairs. Allocates new row_indexes.

    Crash-safe: appends to vectors.bin first, then commits manifest.
    If we crash between append and manifest commit, the row is "leaked"
    (occupies space but no manifest entry) — harmless, will be reclaimed
    on next compaction.
    """
    if not items:
        return

    with _lock:
        con = _conn()
        try:
            # Allocate row indexes starting from current end
            start_row = total_rows()

            # Append all vectors to the binary file
            with open(VECTORS_BIN, "ab") as f:
                for _, vec in items:
                    arr = np.asarray(vec, dtype="float32")
                    if arr.shape != (EMBED_DIM,):
                        raise ValueError(f"Bad vector shape: {arr.shape}")
                    f.write(arr.tobytes())

            # Now register in manifest
            rows = [(url, start_row + i) for i, (url, _) in enumerate(items)]
            con.executemany(
                "INSERT OR REPLACE INTO embeddings (url, row_index, status, embedded_at) "
                "VALUES (?, ?, 'active', datetime('now'))",
                rows,
            )
            con.commit()
        finally:
            con.close()


def mark_pruned(urls: list[str]) -> int:
    """Mark URLs as pruned (vectors stay in file, will be reclaimed on rebuild)."""
    if not urls:
        return 0
    con = _conn()
    cur = con.executemany(
        "UPDATE embeddings SET status='pruned' WHERE url=? AND status='active'",
        [(u,) for u in urls],
    )
    n = cur.rowcount
    con.commit()
    con.close()
    return n


def get_active_vectors_and_urls():
    """Return (vectors numpy array, urls list) for all ACTIVE embeddings.
    Used to rebuild the FAISS index. May be slow / use lots of memory.
    """
    con = _conn()
    rows = con.execute(
        "SELECT url, row_index FROM embeddings WHERE status='active' ORDER BY row_index"
    ).fetchall()
    con.close()

    if not rows:
        return np.zeros((0, EMBED_DIM), dtype="float32"), []

    n = len(rows)
    vectors = np.zeros((n, EMBED_DIM), dtype="float32")
    urls = []

    with open(VECTORS_BIN, "rb") as f:
        for i, row in enumerate(rows):
            urls.append(row["url"])
            f.seek(row["row_index"] * ROW_BYTES)
            data = f.read(ROW_BYTES)
            vectors[i] = np.frombuffer(data, dtype="float32")

    return vectors, urls


def get_vector_at(row_index: int) -> np.ndarray | None:
    """Read a single vector by row index."""
    if row_index < 0 or row_index >= total_rows():
        return None
    with open(VECTORS_BIN, "rb") as f:
        f.seek(row_index * ROW_BYTES)
        data = f.read(ROW_BYTES)
    return np.frombuffer(data, dtype="float32").copy()


def stats() -> dict:
    """Quick stats for the store."""
    con = _conn()
    counts = dict(con.execute(
        "SELECT status, count(*) FROM embeddings GROUP BY status"
    ).fetchall())
    con.close()
    return {
        "total_rows": total_rows(),
        "active": counts.get("active", 0),
        "pruned": counts.get("pruned", 0),
        "vectors_file_mb": VECTORS_BIN.stat().st_size / 1e6 if VECTORS_BIN.exists() else 0,
    }
