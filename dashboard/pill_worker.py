"""Background worker that processes custom pill backfill jobs.

Runs as a daemon thread started at boot in serve.py. Polls the
pill_jobs table every 5 seconds for queued jobs, then streams through
English GAL rows with an Aho-Corasick automaton built from the pill's
keywords.
"""

import json
import logging
import re
import sqlite3
import struct
import sys
import time
import threading
from pathlib import Path

import ahocorasick
import duckdb

# Add pipeline to path for embedder import
_PIPELINE_DIR = str(Path(__file__).resolve().parent.parent / "pipeline")
if _PIPELINE_DIR not in sys.path:
    sys.path.insert(0, _PIPELINE_DIR)

log = logging.getLogger("pill_worker")

from _paths import USERS_DB_PATH as USERS_DB, DB_PATH as GDELT_DB
CHUNK_SIZE = 50_000
_WORD_RE = re.compile(r"\w")

# Estimated total English GAL rows (used for progress %). Updated on
# each run from a quick count.
_ESTIMATED_GAL_EN = 7_600_000


def _user_db():
    con = sqlite3.connect(str(USERS_DB))
    con.row_factory = sqlite3.Row
    return con


def _update_job(pill_id, **fields):
    con = _user_db()
    sets = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [pill_id]
    con.execute(f"UPDATE pill_jobs SET {sets} WHERE pill_id = ? AND status IN ('queued','running')", vals)
    con.commit()
    con.close()


def _update_pill(pill_id, **fields):
    con = _user_db()
    sets = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [pill_id]
    con.execute(f"UPDATE custom_pills SET {sets} WHERE id = ?", vals)
    con.commit()
    con.close()


def _build_automaton(keywords):
    A = ahocorasick.Automaton()
    for kw in keywords:
        low = kw.lower().strip()
        if low:
            A.add_word(low, low)
    A.make_automaton()
    return A


def _keyword_scan(automaton, text):
    if not text:
        return None
    low = text.lower()
    n = len(low)
    for end_idx, kw in automaton.iter(low):
        start = end_idx - len(kw) + 1
        if start > 0 and _WORD_RE.match(low[start - 1]):
            continue
        if end_idx + 1 < n and _WORD_RE.match(low[end_idx + 1]):
            continue
        return kw
    return None


def _run_backfill(pill_id, keywords, scan_description):
    category = f"custom_{pill_id}"
    automaton = _build_automaton(keywords)
    t0 = time.time()

    _update_job(pill_id, status="running", started_at=time.strftime("%Y-%m-%d %H:%M:%S"))

    try:
        con = duckdb.connect(str(GDELT_DB))
        con.execute("SET threads = 4")
    except Exception as e:
        _update_job(pill_id, status="failed", error_message=str(e))
        return

    try:
        # Clear any prior tags for this pill
        con.execute("DELETE FROM article_tags WHERE category = ?", [category])

        scanned = matched = 0
        offset = 0

        while True:
            chunk = con.execute(
                "SELECT url, crawled_at, title, description FROM gal "
                "WHERE language = 'en' "
                "ORDER BY crawled_at LIMIT ? OFFSET ?",
                [CHUNK_SIZE, offset],
            ).fetchall()
            if not chunk:
                break

            tags = []
            for url, crawled_at, title, description in chunk:
                scanned += 1
                text = title or ""
                if scan_description and description:
                    text += " " + description
                hit = _keyword_scan(automaton, text)
                if hit:
                    matched += 1
                    tags.append((url, "gal", category, "keyword", hit, crawled_at))

            if tags:
                con.executemany(
                    "INSERT INTO article_tags "
                    "(article_id, source_type, category, matched_via, matched_detail, crawled_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    tags,
                )

            pct = min(99, int(scanned / _ESTIMATED_GAL_EN * 100))
            _update_job(pill_id, progress_pct=pct, rows_scanned=scanned, rows_matched=matched)

            if len(chunk) < CHUNK_SIZE:
                break
            offset += CHUNK_SIZE

        # Set watermark in DuckDB tag_state so incremental tagger
        # doesn't re-scan from scratch for this custom pill.
        max_ts = con.execute(
            "SELECT max(crawled_at) FROM article_tags WHERE category = ?",
            [category],
        ).fetchone()[0] or 0
        if max_ts:
            con.execute(
                "INSERT OR REPLACE INTO tag_state "
                "(category, source_type, last_crawled_at, updated_at) "
                "VALUES (?, 'gal', ?, current_timestamp)",
                [category, max_ts],
            )

        con.execute("CHECKPOINT")
        elapsed = round(time.time() - t0, 1)

        _update_job(
            pill_id, status="completed", progress_pct=100,
            rows_scanned=scanned, rows_matched=matched,
            completed_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            elapsed_seconds=elapsed,
        )
        _update_pill(
            pill_id, article_count=matched,
            last_built_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        log.info("pill %s: scanned=%d matched=%d elapsed=%.1fs", pill_id, scanned, matched, elapsed)

    except Exception as e:
        log.exception("pill %s backfill failed", pill_id)
        _update_job(pill_id, status="failed", error_message=str(e)[:500])
    finally:
        try:
            con.close()
        except Exception:
            pass


SEMANTIC_BATCH = 256  # texts per Ollama embedding call
SEMANTIC_CHUNK = 20_000  # DuckDB rows per cursor fetch


def _unpack_embedding(blob: bytes) -> list[float]:
    """Unpack a description embedding stored as packed float32s."""
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def _is_cancelled(pill_id):
    """Check if the pill has been deleted or job cancelled."""
    con = _user_db()
    row = con.execute(
        "SELECT status FROM pill_jobs WHERE pill_id = ? ORDER BY id DESC LIMIT 1",
        (pill_id,),
    ).fetchone()
    con.close()
    if not row:
        return True  # pill_jobs row deleted
    return row["status"] not in ("queued", "running")


def _run_semantic_backfill(pill_id, description_embedding_blob, threshold, scan_days=60):
    """Backfill a semantic pill from the EMBEDDING STORE (pre-computed vectors,
    numpy dot products) instead of re-embedding every article via Ollama —
    minutes instead of hours, and covers the store's full 60-day window.

    Uses a single write connection (same as keyword pills) — DuckDB within
    one process allows concurrent read+write connections.
    """
    import numpy as np
    import embedding_store

    category = f"custom_{pill_id}"
    qvec = np.asarray(_unpack_embedding(description_embedding_blob), dtype="float32")
    qvec = qvec / (np.linalg.norm(qvec) or 1.0)
    t0 = time.time()

    _update_job(pill_id, status="running", started_at=time.strftime("%Y-%m-%d %H:%M:%S"))

    try:
        con = duckdb.connect(str(GDELT_DB))
        con.execute("SET threads = 2")
    except Exception as e:
        _update_job(pill_id, status="failed", error_message=str(e))
        return

    try:
        con.execute("DELETE FROM article_tags WHERE category = ?", [category])

        from datetime import datetime, timedelta
        cutoff_ts = int((datetime.utcnow() - timedelta(days=scan_days))
                        .strftime("%Y%m%d%H%M%S"))
        est_rows = embedding_store.active_count() or 1
        log.info("semantic pill %s: store-backed, scan_days=%d threshold=%.2f est_rows=%d",
                 pill_id, scan_days, threshold, est_rows)

        scanned = matched = 0
        for urls, vectors, _last_row in embedding_store.iter_active_chunks(chunk_rows=100_000):
            if _is_cancelled(pill_id):
                log.info("semantic pill %s: cancelled at %d rows", pill_id, scanned)
                break
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            scores = (vectors / norms) @ qvec
            hit_idx = np.nonzero(scores >= threshold)[0]
            scanned += len(urls)

            if len(hit_idx):
                hit_urls = [urls[i] for i in hit_idx]
                hit_score = {urls[i]: float(scores[i]) for i in hit_idx}
                # crawled_at lookup (gal_recent first, then gal), scan_days-filtered
                crawled: dict[str, int] = {}
                for tbl in ("gal_recent", "gal"):
                    missing = [u for u in hit_urls if u not in crawled]
                    if not missing:
                        break
                    for j in range(0, len(missing), 400):
                        part = missing[j:j + 400]
                        ph = ",".join(["?"] * len(part))
                        try:
                            for u, ts in con.execute(
                                f"SELECT url, max(crawled_at) FROM {tbl} "
                                f"WHERE url IN ({ph}) GROUP BY url", part,
                            ).fetchall():
                                crawled[u] = ts
                        except Exception:
                            break  # gal_recent may be absent
                tags = [
                    (u, "gal", category, "semantic", f"{hit_score[u]:.3f}", crawled[u])
                    for u in hit_urls
                    if crawled.get(u) and crawled[u] >= cutoff_ts
                ]
                if tags:
                    matched += len(tags)
                    con.executemany(
                        "INSERT INTO article_tags "
                        "(article_id, source_type, category, matched_via, "
                        "matched_detail, crawled_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        tags,
                    )

            pct = min(99, int(scanned / est_rows * 100))
            _update_job(pill_id, progress_pct=pct, rows_scanned=scanned, rows_matched=matched)

        # Set watermark
        max_ts = con.execute(
            "SELECT max(crawled_at) FROM article_tags WHERE category = ?",
            [category],
        ).fetchone()[0] or 0
        if max_ts:
            con.execute(
                "INSERT OR REPLACE INTO tag_state "
                "(category, source_type, last_crawled_at, updated_at) "
                "VALUES (?, 'gal', ?, current_timestamp)",
                [category, max_ts],
            )

        con.execute("CHECKPOINT")
        elapsed = round(time.time() - t0, 1)

        _update_job(
            pill_id, status="completed", progress_pct=100,
            rows_scanned=scanned, rows_matched=matched,
            completed_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            elapsed_seconds=elapsed,
        )
        _update_pill(
            pill_id, article_count=matched,
            last_built_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        log.info(
            "semantic pill %s: scanned=%d matched=%d threshold=%.2f elapsed=%.1fs",
            pill_id, scanned, matched, threshold, elapsed,
        )

    except Exception as e:
        log.exception("semantic pill %s backfill failed", pill_id)
        _update_job(pill_id, status="failed", error_message=str(e)[:500])
    finally:
        try:
            con.close()
        except Exception:
            pass


def _poll_loop():
    while True:
        try:
            con = _user_db()
            row = con.execute(
                "SELECT pj.pill_id, cp.keywords_json, cp.scan_description, "
                "cp.pill_type, cp.description_embedding, cp.similarity_threshold, cp.scan_days "
                "FROM pill_jobs pj "
                "JOIN custom_pills cp ON cp.id = pj.pill_id "
                "WHERE pj.status = 'queued' "
                "ORDER BY pj.id LIMIT 1"
            ).fetchone()
            con.close()

            if row:
                pill_id = row["pill_id"]
                pill_type = row["pill_type"] or "keyword"

                if pill_type == "semantic":
                    threshold = row["similarity_threshold"] or 0.55
                    scan_days = row["scan_days"] or 7
                    log.info("starting semantic backfill for pill %s (threshold=%.2f, days=%d)",
                             pill_id, threshold, scan_days)
                    _run_semantic_backfill(pill_id, row["description_embedding"],
                                           threshold, scan_days)
                else:
                    keywords = json.loads(row["keywords_json"])
                    scan_desc = bool(row["scan_description"])
                    log.info("starting backfill for pill %s (%d keywords)", pill_id, len(keywords))
                    _run_backfill(pill_id, keywords, scan_desc)
            else:
                time.sleep(5)
        except Exception:
            log.exception("pill worker error")
            time.sleep(10)


def start_worker():
    t = threading.Thread(target=_poll_loop, name="pill-worker", daemon=True)
    t.start()
    log.info("pill worker thread started")
    return t
