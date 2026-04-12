#!/usr/bin/env python3
"""
Import FDA companies and build the FDA match cache.

Usage:
    python gdelt_fda_backfill.py --import-companies --csv data/fda_companies.csv
    python gdelt_fda_backfill.py --initial-match
    python gdelt_fda_backfill.py --full --csv data/fda_companies.csv
"""

import argparse
import logging
import time
from pathlib import Path

from pipeline.fda_matcher import import_companies, initial_match

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)
log = logging.getLogger("gdelt_fda_backfill")


def main():
    parser = argparse.ArgumentParser(description="FDA company import + match backfill")
    parser.add_argument("--csv", type=str, default="data/fda_companies.csv",
                        help="Path to fda_companies.csv")
    parser.add_argument("--import-companies", action="store_true",
                        help="Import the CSV into the fda_companies table")
    parser.add_argument("--initial-match", action="store_true",
                        help="Run a full match against all GKG + GAL rows")
    parser.add_argument("--full", action="store_true",
                        help="Shortcut: import + initial match")
    args = parser.parse_args()

    if args.full:
        args.import_companies = True
        args.initial_match = True

    if not (args.import_companies or args.initial_match):
        parser.error("Nothing to do. Pass --import-companies, --initial-match, or --full.")

    if args.import_companies:
        csv_path = Path(args.csv)
        if not csv_path.exists():
            parser.error(f"CSV not found: {csv_path}")
        log.info("Importing FDA companies from %s ...", csv_path)
        t0 = time.time()
        n = import_companies(csv_path)
        log.info("Imported %d companies in %.2fs", n, time.time() - t0)

    if args.initial_match:
        log.info("Running initial match ...")
        t0 = time.time()
        summary = initial_match()
        log.info("Initial match complete in %.1fs", time.time() - t0)
        for k, v in summary.items():
            log.info("  %s: %s", k, v)


if __name__ == "__main__":
    main()
