#!/usr/bin/env python3
"""
GDELT v2 Pipeline Status — quick diagnostic dashboard.

Usage:
    python gdelt_status.py
"""

import shutil
from datetime import datetime
from pathlib import Path

from pipeline.config import DATA_DIR, DB_PATH, GAL_RAW_DIR, RAW_DIR


def main():
    if not DB_PATH.exists():
        print("Database not initialized. Run gdelt_backfill.py or gdelt_ingest.py first.")
        return

    import duckdb
    con = duckdb.connect(str(DB_PATH), read_only=True)

    db_size_gb = DB_PATH.stat().st_size / (1024 ** 3)

    raw_files = list(RAW_DIR.glob("*.zip")) if RAW_DIR.exists() else []
    raw_size_gb = sum(f.stat().st_size for f in raw_files) / (1024 ** 3)

    gal_files = list(GAL_RAW_DIR.glob("*.gal.json.gz")) if GAL_RAW_DIR.exists() else []
    gal_raw_size_gb = sum(f.stat().st_size for f in gal_files) / (1024 ** 3)

    disk = shutil.disk_usage(str(DATA_DIR))
    free_gb = disk.free / (1024 ** 3)

    print(f"\n{'='*55}")
    print(f"  GDELT Pipeline Status")
    print(f"{'='*55}")
    print(f"  Database : {DB_PATH}")
    print(f"  DB size  : {db_size_gb:.2f} GB")
    print(f"  Raw zips : {len(raw_files):,} files ({raw_size_gb:.2f} GB)")
    print(f"  Raw GAL  : {len(gal_files):,} files ({gal_raw_size_gb:.2f} GB)")
    print(f"  Disk free: {free_gb:.1f} GB")
    print()

    for table, ts_col in [
        ("events", "DATEADDED"),
        ("mentions", "MentionTimeDate"),
        ("gkg", "V1DATE"),
        ("gal", "crawled_at"),
    ]:
        try:
            row = con.execute(
                f'SELECT count(*), min("{ts_col}"), max("{ts_col}") FROM {table}'
            ).fetchone()
            count, min_ts, max_ts = row
            print(f"  {table:>8}: {count:>12,} rows  ({min_ts} — {max_ts})")
        except Exception as e:
            print(f"  {table:>8}: error — {e}")

    print()

    # Latest ingest info
    try:
        row = con.execute(
            "SELECT max(loaded_at), count(*) FROM _ingest_log"
        ).fetchone()
        last_loaded, total_files = row
        print(f"  Files ingested: {total_files:,}")
        print(f"  Last ingest   : {last_loaded}")
        if last_loaded:
            age = datetime.now() - last_loaded
            minutes = age.total_seconds() / 60
            status = "OK" if minutes < 30 else "STALE" if minutes < 120 else "WARNING"
            print(f"  Data age      : {int(minutes)} min ({status})")
    except Exception:
        print("  Ingest log: not available")

    print(f"{'='*55}\n")
    con.close()


if __name__ == "__main__":
    main()
