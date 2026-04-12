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
import time
import threading
from pathlib import Path

import ahocorasick
import duckdb

log = logging.getLogger("pill_worker")

USERS_DB = Path(__file__).resolve().parent.parent / "data" / "users.db"
GDELT_DB = Path(__file__).resolve().parent.parent / "data" / "gdelt.duckdb"
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


def _poll_loop():
    while True:
        try:
            con = _user_db()
            row = con.execute(
                "SELECT pj.pill_id, cp.keywords_json, cp.scan_description "
                "FROM pill_jobs pj "
                "JOIN custom_pills cp ON cp.id = pj.pill_id "
                "WHERE pj.status = 'queued' "
                "ORDER BY pj.id LIMIT 1"
            ).fetchone()
            con.close()

            if row:
                pill_id = row["pill_id"]
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
