"""Bulk-embed all English GAL articles for FAISS-ready vector storage.

Designed to run on rainbow-boi where Ollama is local. Resumable — saves
progress after each chunk so it picks up where it left off if interrupted.

Usage:
    python bulk_embed.py                # full run (resume if prior progress)
    python bulk_embed.py --export-only  # just export articles from DuckDB
    python bulk_embed.py --embed-only   # just embed from existing export
    python bulk_embed.py --status       # show progress

Output (in data/embeddings/):
    articles.csv       - exported articles (url, crawled_at, title, description)
    vectors.npy        - float32 array, shape (N, 768), memory-mapped
    urls.txt           - one URL per line, same order as vectors
    progress.json      - resumption state
"""

import csv
import json
import logging
import os
import sys
import time
from pathlib import Path

import numpy as np

# Add pipeline dir for embedder import
sys.path.insert(0, str(Path(__file__).resolve().parent))
from embedder import embed_texts

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("bulk_embed")

BASE_DIR = Path(__file__).resolve().parent.parent / "data" / "embeddings"
ARTICLES_CSV = BASE_DIR / "articles.csv"
VECTORS_NPY = BASE_DIR / "vectors.npy"
URLS_TXT = BASE_DIR / "urls.txt"
PROGRESS_JSON = BASE_DIR / "progress.json"

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "gdelt.duckdb"

EMBED_DIM = 768
BATCH_SIZE = 256       # texts per Ollama call
SAVE_EVERY = 10_000    # save progress every N articles


def export_articles():
    """Export all English GAL articles to CSV for offline embedding."""
    import duckdb

    BASE_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Exporting English GAL articles to %s", ARTICLES_CSV)

    con = duckdb.connect(str(DB_PATH), read_only=True)
    con.execute("SET threads = 4")

    total = con.execute(
        "SELECT count(*) FROM gal WHERE language = 'en'"
    ).fetchone()[0]
    log.info("Total English articles: %s", f"{total:,}")

    t0 = time.time()
    with open(ARTICLES_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["url", "crawled_at", "title", "description"])

        cursor_ts = 0
        exported = 0
        chunk_size = 100_000

        while True:
            rows = con.execute(
                "SELECT url, crawled_at, title, description FROM gal "
                "WHERE language = 'en' AND crawled_at > ? "
                "ORDER BY crawled_at LIMIT ?",
                [cursor_ts, chunk_size],
            ).fetchall()
            if not rows:
                break

            for url, crawled_at, title, desc in rows:
                writer.writerow([url, crawled_at, title or "", desc or ""])
                exported += 1

            cursor_ts = rows[-1][1]
            log.info("  exported %s / %s (%.0f%%)",
                     f"{exported:,}", f"{total:,}",
                     exported / total * 100)

            if len(rows) < chunk_size:
                break

    con.close()
    elapsed = time.time() - t0
    log.info("Export complete: %s articles in %.1fs", f"{exported:,}", elapsed)
    return exported


def embed_articles():
    """Read exported CSV, embed each article, save vectors to chunked numpy files.

    Vectors are stored in 500K-article chunks (~1.4GB each) to avoid
    Windows issues with large memory-mapped files. The final output is:
        vectors_000.npy, vectors_001.npy, ... (each 500K x 768 float32)
        urls.txt (one URL per line, matching vector order)
        progress.json (for resumability)
    """
    BASE_DIR.mkdir(parents=True, exist_ok=True)

    if not ARTICLES_CSV.exists():
        log.error("No articles.csv found. Run with --export-only first.")
        return

    # Count total lines using byte counting (fast, low memory)
    log.info("Counting articles...")
    total = 0
    with open(ARTICLES_CSV, "rb") as f:
        while True:
            buf = f.read(1024 * 1024)
            if not buf:
                break
            total += buf.count(b"\n")
    total -= 1  # minus header
    log.info("Total articles to embed: %s", f"{total:,}")

    # Load progress state for resumability
    start_line = 0
    if PROGRESS_JSON.exists():
        with open(PROGRESS_JSON) as f:
            progress = json.load(f)
        start_line = progress.get("lines_done", 0)
        if start_line > 0:
            log.info("Resuming from line %s", f"{start_line:,}")

    CHUNK_ROWS = 500_000  # rows per numpy chunk file (~1.4GB each)

    # In-memory buffer for current chunk
    chunk_id = start_line // CHUNK_ROWS
    chunk_offset = start_line % CHUNK_ROWS
    chunk_buf = np.zeros((CHUNK_ROWS, EMBED_DIM), dtype="float32")

    # If resuming mid-chunk, load the partial chunk
    if chunk_offset > 0:
        chunk_path = BASE_DIR / f"vectors_{chunk_id:03d}.npy"
        if chunk_path.exists():
            chunk_buf[:chunk_offset] = np.load(str(chunk_path))[:chunk_offset]
            log.info("Loaded partial chunk %d (%d rows)", chunk_id, chunk_offset)

    # Open URL file
    if start_line > 0:
        url_file = open(URLS_TXT, "a", encoding="utf-8")
    else:
        url_file = open(URLS_TXT, "w", encoding="utf-8")

    t0 = time.time()
    embedded = start_line
    batch_texts = []
    batch_urls = []
    errors = 0

    def _save_chunk():
        nonlocal chunk_id, chunk_offset, chunk_buf
        rows_in_chunk = chunk_offset
        if rows_in_chunk > 0:
            chunk_path = BASE_DIR / f"vectors_{chunk_id:03d}.npy"
            np.save(str(chunk_path), chunk_buf[:rows_in_chunk])
            log.info("Saved chunk %d (%d rows, %.1f MB)",
                     chunk_id, rows_in_chunk,
                     rows_in_chunk * EMBED_DIM * 4 / 1e6)

    def _process_batch():
        nonlocal embedded, errors, chunk_id, chunk_offset, chunk_buf
        if not batch_texts:
            return
        try:
            vecs = embed_texts(batch_texts)
        except Exception as e:
            errors += 1
            log.warning("Embed error at line %d: %s", embedded, e)
            vecs = [np.zeros(EMBED_DIM, dtype="float32")] * len(batch_texts)
            if errors > 50:
                raise RuntimeError("Too many embedding errors")

        for i, vec in enumerate(vecs):
            chunk_buf[chunk_offset] = vec
            url_file.write(batch_urls[i] + "\n")
            chunk_offset += 1
            embedded += 1

            # Chunk full — save and start new one
            if chunk_offset >= CHUNK_ROWS:
                _save_chunk()
                chunk_id += 1
                chunk_offset = 0
                chunk_buf = np.zeros((CHUNK_ROWS, EMBED_DIM), dtype="float32")

    with open(ARTICLES_CSV, encoding="utf-8") as f:
        reader = csv.DictReader(f)

        for line_num, row in enumerate(reader):
            if line_num < start_line:
                continue

            title = row.get("title", "")
            desc = row.get("description", "")
            text = title
            if desc:
                text += " " + desc
            text = text.strip()[:512]

            if not text:
                text = "empty"  # placeholder to keep alignment

            batch_texts.append(text)
            batch_urls.append(row["url"])

            if len(batch_texts) >= BATCH_SIZE:
                _process_batch()
                batch_texts = []
                batch_urls = []

                # Progress update
                if embedded % SAVE_EVERY < BATCH_SIZE:
                    elapsed = time.time() - t0
                    rate = (embedded - start_line) / elapsed if elapsed > 0 else 0
                    eta_hrs = (total - embedded) / rate / 3600 if rate > 0 else 0
                    log.info(
                        "  %s / %s (%.1f%%) | %.0f/sec | ETA %.1fh | errors=%d",
                        f"{embedded:,}", f"{total:,}",
                        embedded / total * 100,
                        rate, eta_hrs, errors,
                    )
                    # Save progress (but not the chunk — that's saved when full)
                    url_file.flush()
                    with open(PROGRESS_JSON, "w") as pf:
                        json.dump({
                            "lines_done": embedded,
                            "total": total,
                            "errors": errors,
                            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        }, pf)

    # Flush remaining batch
    _process_batch()

    # Save final partial chunk
    _save_chunk()

    url_file.close()
    with open(PROGRESS_JSON, "w") as pf:
        json.dump({
            "lines_done": embedded,
            "total": total,
            "errors": errors,
            "completed": True,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }, pf)

    elapsed = time.time() - t0
    log.info(
        "Embedding complete: %s articles in %.1f hours (%.0f/sec, %d errors)",
        f"{embedded:,}", elapsed / 3600,
        (embedded - start_line) / elapsed if elapsed > 0 else 0,
        errors,
    )


def show_status():
    """Show current progress."""
    if not PROGRESS_JSON.exists():
        print("No bulk embedding in progress.")
        return
    with open(PROGRESS_JSON) as f:
        p = json.load(f)
    done = p.get("lines_done", 0)
    total = p.get("total", 0)
    pct = done / total * 100 if total else 0
    print(f"Progress: {done:,} / {total:,} ({pct:.1f}%)")
    print(f"Errors: {p.get('errors', 0)}")
    print(f"Completed: {p.get('completed', False)}")
    print(f"Updated: {p.get('updated_at', '?')}")
    if VECTORS_NPY.exists():
        size_gb = VECTORS_NPY.stat().st_size / 1e9
        print(f"Vectors file: {size_gb:.1f} GB")


if __name__ == "__main__":
    args = sys.argv[1:]

    if "--status" in args:
        show_status()
    elif "--export-only" in args:
        export_articles()
    elif "--embed-only" in args:
        embed_articles()
    else:
        # Full run: export then embed
        export_articles()
        embed_articles()
