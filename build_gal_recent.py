"""One-time (re-runnable) builder for gal_recent — a rolling ~8-day mirror of
the gal table that the dashboard routes window-bounded queries to (see
dashboard/articles.py::_gal_table / RECENT_HOURS=168). Scanning ~3M rows
instead of ~24M is what makes cold pill/search queries fast.

Run with the dashboard and GDELT-Ingest STOPPED (single-writer DuckDB) —
prep_rebuild.ps1 / restart_dash.ps1 patterns apply. Safe to re-run: CREATE
TABLE IF NOT EXISTS + idempotent backfill (skips if already populated for the
current window; use --force to rebuild anyway).

Usage:
    python build_gal_recent.py [--force]
"""

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta

from pipeline.config import DATA_DIR, DB_PATH
from pipeline.loader import _open_connection
from pipeline.gal_loader import GAL_RECENT_KEEP_DAYS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("build_gal_recent")

GAL_RECENT_SCHEMA = """
    CREATE TABLE IF NOT EXISTS gal_recent (
        url TEXT,
        crawled_at BIGINT,
        published_at BIGINT,
        domain TEXT,
        outlet_name TEXT,
        outlet_logo TEXT,
        outlet_twitter TEXT,
        title TEXT,
        image TEXT,
        description TEXT,
        language TEXT,
        author TEXT
    )
"""


def build(force: bool = False) -> None:
    con = _open_connection(DB_PATH)
    try:
        con.execute(GAL_RECENT_SCHEMA)
        existing = con.execute("SELECT count(*) FROM gal_recent").fetchone()[0]
        if existing and not force:
            log.info("gal_recent already has %d rows — skipping (use --force to rebuild)", existing)
            return
        if force and existing:
            log.info("Force rebuild: clearing existing %d rows", existing)
            con.execute("DELETE FROM gal_recent")

        cutoff = int((datetime.utcnow() - timedelta(days=GAL_RECENT_KEEP_DAYS))
                     .strftime("%Y%m%d%H%M%S"))
        t0 = time.time()
        con.execute(
            "INSERT INTO gal_recent (url, crawled_at, published_at, domain, "
            "outlet_name, outlet_logo, outlet_twitter, title, image, "
            "description, language, author) "
            "SELECT url, crawled_at, published_at, domain, "
            "outlet_name, outlet_logo, outlet_twitter, title, image, "
            "description, language, author FROM gal WHERE crawled_at >= ?",
            [cutoff],
        )
        count = con.execute("SELECT count(*) FROM gal_recent").fetchone()[0]
        log.info("Backfilled %d rows (cutoff=%d) in %.1fs", count, cutoff, time.time() - t0)

        log.info("Creating indexes...")
        con.execute("CREATE INDEX IF NOT EXISTS idx_gal_recent_crawled ON gal_recent(crawled_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_gal_recent_url ON gal_recent(url)")
        con.execute("CHECKPOINT")
        log.info("Done.")
    finally:
        con.close()
    # New table in place -> bust the dashboard's feed/stats caches so the
    # speedup is visible immediately instead of waiting for the next ingest.
    try:
        (DATA_DIR / "data_version.txt").write_text(str(int(time.time())))
    except Exception:
        log.exception("Failed to write data_version.txt (non-fatal)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build/backfill the gal_recent hot-window table")
    parser.add_argument("--force", action="store_true", help="Rebuild even if already populated")
    args = parser.parse_args()
    try:
        build(force=args.force)
    except Exception:
        log.exception("build_gal_recent failed")
        sys.exit(1)
