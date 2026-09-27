# -*- coding: utf-8 -*-
"""Build the dev data slice: a small, recent copy of the prod corpus for the :8016 instance.

WHY THIS FILE EXISTS
    `scripts/rebuild_dev_slice.ps1` has invoked this script since it was written, but the script
    itself was never committed and existed nowhere on disk (`git log --all -- ` on the path is
    empty). Every rebuild therefore failed with rc=2 and the dev slice silently froze at
    2026-07-19..07-26. Written 2026-08-29 to close PLAN-100X Phase 0.

WHAT IT DOES
    Copies the last DEV_DAYS (default 7) days of prod into the dev database, plus the small
    reference tables in full. Prod is opened READ-ONLY and is safe to leave serving; only the dev
    instance must be stopped, because DuckDB is single-writer and the running dev dashboard holds
    the dev database open. `rebuild_dev_slice.ps1` stops it before calling this.

    The slice is built into a NEW file and swapped in at the end. Rebuilding in place with
    CREATE OR REPLACE grows the file every run -- DuckDB does not return freed pages to the OS --
    so a weekly rebuild would quietly bloat the dev disk.

SLICING KEY: crawled_at, NOT published_at
    `gal.published_at` carries values from 2025 to 2026-12 because publishers lie in their
    metadata; slicing on it produces a slice with a random tail and no coherent window.
    `crawled_at` is our own ingest clock and is monotonic. This is also what `gal_recent` is
    maintained on -- its own published_at range spans 2025-08..2026-12 while it holds ~8 days of
    crawls -- so slicing on crawled_at reproduces prod's real shape, garbage timestamps included.

TABLES DELIBERATELY NOT COPIED
    `events` (3.6M rows) and `mentions` (9.7M rows) are ingested but never read: there is no
    `FROM events` or `FROM mentions` anywhere in dashboard/ or pipeline/ (checked 2026-08-29).
    Copying them would triple the rebuild for data nothing queries. `_ingest_log`,
    `_gal_ingest_log` and `article_tags_flip_backup` are operational tables for jobs that do not
    run against dev.

WHY gal_recent MATTERS AND WAS MISSING
    `dashboard/articles.py::_has_gal_recent` silently routes around the table when it is absent.
    The old dev slice had no `gal_recent`, so dev exercised a *different* query path than prod --
    a dev-first check could pass on code that fails in production. It is copied now.

USAGE
    DEV_DAYS=7 python scripts/build_dev_snapshot.py          # normally via rebuild_dev_slice.ps1
    python scripts/build_dev_snapshot.py --days 3 --dry-run
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import shutil
import sys
import time

log = logging.getLogger("build_dev_snapshot")

PROD_ROOT = r"C:\Users\siddh\Code_Library\gdelt-events"
DEV_ROOT = r"C:\Users\siddh\Code_Library\gdelt-events-dev"
PROD_DATA = os.path.join(PROD_ROOT, "data")
DEV_DATA = os.path.join(DEV_ROOT, "data")
PROD_DB = os.path.join(PROD_DATA, "gdelt.duckdb")
DEV_DB = os.path.join(DEV_DATA, "gdelt.duckdb")

DEFAULT_DAYS = 7

# (table, time column) -- None means copy the whole table, it is small reference data.
SLICED = [
    ("gal", "crawled_at"),
    ("gal_recent", "crawled_at"),
    ("gkg", "V1DATE"),
    ("article_tags", "crawled_at"),
]
WHOLE = [
    "fda_companies",
    "fda_regulatory_events",
    "fda_match_cache",
    "fda_match_state",
    "tag_state",
    "cluster_state",
]
# Copied as files rather than sliced: small, and not time-series.
SIDECARS = ["sec.db", "entities.duckdb"]

# Tables that must come out non-empty, or the slice is not usable and the run fails.
MUST_BE_NONEMPTY = ["gal", "gal_recent", "gkg", "article_tags", "fda_companies"]


def cutoff_stamp(days):
    """YYYYMMDDHHMMSS as a BIGINT, `days` before now. Matches the corpus' timestamp encoding."""
    t = dt.datetime.now() - dt.timedelta(days=days)
    return int(t.strftime("%Y%m%d%H%M%S"))


def attach_prod(con):
    """ATTACH prod read-only. Fails loudly if ingest currently holds the write lock."""
    try:
        con.execute("ATTACH '%s' AS prod (READ_ONLY)" % PROD_DB.replace("\\", "/"))
    except Exception as e:
        raise SystemExit(
            "cannot attach prod read-only: %s\n"
            "If GDELT-Ingest is mid-cycle it holds the write lock; wait and re-run." % e)


def build(days, dry_run=False):
    import duckdb

    cutoff = cutoff_stamp(days)
    log.info("slice window: %d days, crawled_at >= %d", days, cutoff)

    tmp_db = DEV_DB + ".build"
    for stale in (tmp_db, tmp_db + ".wal"):
        if os.path.exists(stale):
            os.remove(stale)

    if dry_run:
        con = duckdb.connect(PROD_DB, read_only=True)
        con.execute("SET temp_directory='%s'" % os.path.join(PROD_DATA, "duckdb_tmp").replace("\\", "/"))
        for t, col in SLICED:
            n = con.execute("SELECT count(*) FROM %s WHERE %s >= ?" % (t, col), [cutoff]).fetchone()[0]
            log.info("  would copy %-14s %10d rows", t, n)
        for t in WHOLE:
            n = con.execute("SELECT count(*) FROM %s" % t).fetchone()[0]
            log.info("  would copy %-14s %10d rows (whole)", t, n)
        con.close()
        return 0

    t0 = time.time()
    os.makedirs(os.path.join(DEV_DATA, "duckdb_tmp"), exist_ok=True)
    con = duckdb.connect(tmp_db)
    counts = {}
    try:
        con.execute("SET temp_directory='%s'" % os.path.join(DEV_DATA, "duckdb_tmp").replace("\\", "/"))
        attach_prod(con)

        for t, col in SLICED:
            con.execute("CREATE TABLE %s AS SELECT * FROM prod.%s WHERE %s >= %d" % (t, t, col, cutoff))
            counts[t] = con.execute("SELECT count(*) FROM %s" % t).fetchone()[0]
            log.info("  %-14s %10d rows", t, counts[t])

        # Clusters need referential integrity: take the recent clusters, then only the members
        # that belong to them. Slicing both independently leaves members pointing at nothing.
        con.execute("CREATE TABLE clusters AS SELECT * FROM prod.clusters WHERE latest_seen >= %d" % cutoff)
        counts["clusters"] = con.execute("SELECT count(*) FROM clusters").fetchone()[0]
        con.execute("CREATE TABLE cluster_members AS SELECT m.* FROM prod.cluster_members m "
                    "SEMI JOIN clusters c ON m.cluster_id = c.cluster_id")
        counts["cluster_members"] = con.execute("SELECT count(*) FROM cluster_members").fetchone()[0]
        log.info("  %-14s %10d rows", "clusters", counts["clusters"])
        log.info("  %-14s %10d rows", "cluster_members", counts["cluster_members"])

        for t in WHOLE:
            con.execute("CREATE TABLE %s AS SELECT * FROM prod.%s" % (t, t))
            counts[t] = con.execute("SELECT count(*) FROM %s" % t).fetchone()[0]
            log.info("  %-14s %10d rows (whole)", t, counts[t])

        con.execute("DETACH prod")
    finally:
        con.close()

    empty = [t for t in MUST_BE_NONEMPTY if counts.get(t, 0) == 0]
    if empty:
        os.remove(tmp_db)
        log.error("REFUSING to swap in an unusable slice: empty %s", ", ".join(empty))
        return 2

    # Swap. The dev dashboard must already be stopped by rebuild_dev_slice.ps1.
    backup = DEV_DB + ".prev"
    try:
        if os.path.exists(backup):
            os.remove(backup)
        if os.path.exists(DEV_DB):
            os.replace(DEV_DB, backup)
        os.replace(tmp_db, DEV_DB)
    except OSError as e:
        log.error("swap failed (is the dev dashboard still running and holding %s?): %s", DEV_DB, e)
        return 3
    log.info("swapped in new slice; previous kept at %s", os.path.basename(backup))

    for name in SIDECARS:
        src = os.path.join(PROD_DATA, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(DEV_DATA, name))
            log.info("  copied sidecar %s (%.1f MB)", name, os.path.getsize(src) / 1e6)
        else:
            log.warning("  sidecar %s not present in prod, skipped", name)

    # Report the real window the slice ended up with, not the one we asked for.
    con = duckdb.connect(DEV_DB, read_only=True)
    try:
        for t, col in (("gal", "crawled_at"), ("gkg", "V1DATE")):
            lo, hi = con.execute("SELECT min(%s), max(%s) FROM %s" % (col, col, t)).fetchone()
            log.info("  %s.%s window: %s .. %s", t, col, lo, hi)
    finally:
        con.close()

    log.info("done in %.1fs, %d rows total", time.time() - t0, sum(counts.values()))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=int(os.environ.get("DEV_DAYS", DEFAULT_DAYS)))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    # force=True: a basicConfig elsewhere in the import graph otherwise wins and this long job
    # logs to a stdout the scheduled task discards, leaving a 0-byte log.
    logging.basicConfig(level=logging.INFO, force=True,
                        format="%(asctime)s %(levelname)s %(message)s")
    return build(args.days, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
