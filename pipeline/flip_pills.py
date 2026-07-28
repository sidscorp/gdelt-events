"""Promote judged shadow categories (<cat>__v2 -> <cat>, gal side only) — safely.

Two modes:

  merge    (default) Insert v2 rows whose article has no live tag for the pill;
           delete nothing. This reproduces normal healthy-day behaviour —
           keyword tags plus judge-approved semantic additions — for a window
           the live judge missed (e.g. the 2026-07-15..26 Ollama outage).

  replace  Window-scoped swap: delete live gal rows INSIDE the span the v2 set
           actually covers, then rename the v2 set in. The window becomes
           judge-only — markedly stricter than neighbouring days. Rows outside
           the window are never touched. (The previous version of this script
           deleted the pill's ENTIRE live gal history, including days the v2
           set never covered — that is the bug this rewrite removes.)

Dry-run by default: prints the per-pill plan against a read-only connection,
safe while the dashboard is serving. Pass --apply to write. Every row that is
deleted or consumed is first copied into `article_tags_flip_backup` with a
flip_id + reason, so any flip can be reconstructed or rolled back.

    python -m pipeline.flip_pills [--pills a,b] [--mode merge|replace] [--apply]
"""

import argparse
import logging
import time
from datetime import datetime, timezone

import duckdb

from .config import DB_PATH
from .loader import _open_connection
from . import pill_scorer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("flip_pills")

COLS = "article_id, source_type, category, matched_via, matched_detail, crawled_at"

BACKUP_DDL = (
    "CREATE TABLE IF NOT EXISTS article_tags_flip_backup ("
    " article_id VARCHAR, source_type VARCHAR, category VARCHAR,"
    " matched_via VARCHAR, matched_detail VARCHAR, crawled_at BIGINT,"
    " flip_id VARCHAR, reason VARCHAR)"
)


def _read_con(retries: int = 30):
    last = None
    for _ in range(retries):
        try:
            con = duckdb.connect(str(DB_PATH), read_only=True)
            con.execute("SET threads = 2")
            con.execute("SET memory_limit = '2GB'")
            # Read-only conns have no valid spill dir on Windows (db.py gotcha).
            con.execute(f"SET temp_directory='{(DB_PATH.parent / 'duckdb_tmp').as_posix()}'")
            return con
        except duckdb.IOException as e:
            last = e
            time.sleep(2)
    raise RuntimeError(f"could not open read-only connection: {last}")


def _new_additions_sql(v2: str, cat: str):
    """v2 rows whose article has no live gal tag for the pill."""
    return (
        f"SELECT {COLS} FROM article_tags v WHERE v.category = ? "
        "AND NOT EXISTS (SELECT 1 FROM article_tags l WHERE l.category = ? "
        "AND l.source_type = 'gal' AND l.article_id = v.article_id)"
    ), [v2, cat]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pills", help="comma-separated pill keys (default: all curated+fda)")
    parser.add_argument("--mode", choices=("merge", "replace"), default="merge")
    parser.add_argument("--apply", action="store_true",
                        help="actually write (default: dry-run print of the plan)")
    args = parser.parse_args()

    pills = [p for p in pill_scorer.load_pill_defs() if p["kind"] != "custom"]
    if args.pills:
        keep = {p.strip() for p in args.pills.split(",")}
        pills = [p for p in pills if p["key"] in keep]
    if not pills:
        raise SystemExit("no pills selected")

    flip_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    t0 = time.time()
    con = _open_connection(DB_PATH) if args.apply else _read_con()
    try:
        if args.apply:
            con.execute(BACKUP_DDL)
        for p in pills:
            cat, v2 = p["key"], p["key"] + "__v2"
            n_v2, lo, hi = con.execute(
                "SELECT count(*), min(crawled_at), max(crawled_at) "
                "FROM article_tags WHERE category = ?", [v2]
            ).fetchone()
            if not n_v2:
                log.warning("%s: no v2 rows — skipping", cat)
                continue

            if args.mode == "merge":
                sql, params = _new_additions_sql(v2, cat)
                n_new = con.execute(f"SELECT count(*) FROM ({sql})", params).fetchone()[0]
                log.info("%s: %d of %d v2 rows are new additions (window %d..%d)%s",
                         cat, n_new, n_v2, lo, hi, "" if args.apply else "  [dry-run]")
                if not args.apply:
                    continue
                # Preserve the full judged set before consuming it.
                con.execute(
                    f"INSERT INTO article_tags_flip_backup SELECT {COLS}, ?, 'v2_consumed' "
                    "FROM article_tags WHERE category = ?", [flip_id, v2])
                con.execute(
                    f"INSERT INTO article_tags ({COLS}) "
                    "SELECT article_id, source_type, ?, matched_via, matched_detail, crawled_at "
                    f"FROM ({sql})", [cat] + params)
                con.execute("DELETE FROM article_tags WHERE category = ?", [v2])

            else:  # replace — scoped to the span the v2 set actually covers
                n_live_in, n_live_out = con.execute(
                    "SELECT count(*) FILTER (crawled_at BETWEEN ? AND ?), "
                    "       count(*) FILTER (crawled_at NOT BETWEEN ? AND ?) "
                    "FROM article_tags WHERE category = ? AND source_type = 'gal'",
                    [lo, hi, lo, hi, cat]).fetchone()
                log.info("%s: replace %d live in-window rows with %d judged rows "
                         "(window %d..%d; %d out-of-window rows untouched)%s",
                         cat, n_live_in, n_v2, lo, hi, n_live_out,
                         "" if args.apply else "  [dry-run]")
                if not args.apply:
                    continue
                con.execute(
                    f"INSERT INTO article_tags_flip_backup SELECT {COLS}, ?, 'live_replaced' "
                    "FROM article_tags WHERE category = ? AND source_type = 'gal' "
                    "AND crawled_at BETWEEN ? AND ?", [flip_id, cat, lo, hi])
                con.execute(
                    "DELETE FROM article_tags WHERE category = ? AND source_type = 'gal' "
                    "AND crawled_at BETWEEN ? AND ?", [cat, lo, hi])
                con.execute(
                    "UPDATE article_tags SET category = ? WHERE category = ?", [cat, v2])

        if args.apply:
            con.execute("CHECKPOINT")
    finally:
        con.close()

    if args.apply:
        log.info("flip %s done in %.1fs — rollback data in article_tags_flip_backup "
                 "(flip_id=%s)", flip_id, time.time() - t0, flip_id)
    else:
        log.info("dry-run done in %.1fs — nothing written; add --apply to execute",
                 time.time() - t0)


if __name__ == "__main__":
    main()
