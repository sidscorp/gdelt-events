"""Rebuild the GAL portion of fda_match_cache.

Usage: python rebuild_gal_fda.py --mode {strict|broad|full}
Run with dashboard stopped (writer needs exclusive DB access on Windows).
"""
import argparse
import logging
import time

from pipeline.config import DB_PATH
from pipeline.loader import _open_connection
from pipeline.schema import migrate_fda_match_cache
from pipeline.fda_matcher import (
    build_automaton,
    _build_context_automaton,
    _match_gal_stream,
    _set_last_ts,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rebuild_gal_fda")


MODE_TO_DELETE_TYPES = {
    "strict": ["legal"],
    "broad":  ["legal", "stripped"],
    "full":   ["legal", "stripped", "contextual"],
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode", choices=list(MODE_TO_DELETE_TYPES.keys()), default="full",
        help="Which match_type rows to delete + rebuild. Default: full.",
    )
    args = parser.parse_args()

    con = _open_connection(DB_PATH)
    con.execute("SET threads = 8")
    try:
        migrate_fda_match_cache(con)

        delete_types = MODE_TO_DELETE_TYPES[args.mode]
        placeholders = ",".join(["?"] * len(delete_types))

        before_total = con.execute(
            "SELECT count(*) FROM fda_match_cache WHERE source_type='gal'"
        ).fetchone()[0]
        before_kept = con.execute(
            f"SELECT count(*) FROM fda_match_cache "
            f"WHERE source_type='gal' AND match_type NOT IN ({placeholders})",
            delete_types,
        ).fetchone()[0]
        log.info(
            "mode=%s. GAL rows before: %d total (%d will be kept because their "
            "match_type is not in %s)",
            args.mode, before_total, before_kept, delete_types,
        )

        log.info("Deleting GAL rows where match_type IN %s ...", delete_types)
        con.execute(
            f"DELETE FROM fda_match_cache "
            f"WHERE source_type='gal' AND match_type IN ({placeholders})",
            delete_types,
        )
        con.execute("DELETE FROM fda_match_state WHERE source_type='gal'")

        include_stripped = args.mode in ("broad", "full")
        log.info(
            "Building Aho-Corasick automaton (gal, min_length=5, "
            "min_stripped_length=7, include_stripped=%s)...", include_stripped,
        )
        t0 = time.time()
        gal_automaton, specialty_map = build_automaton(
            con, min_length=5, include_stripped=include_stripped,
            min_stripped_length=7,
        )
        context_automaton = _build_context_automaton() if args.mode == "full" else None
        log.info("  built in %.1fs", time.time() - t0)

        log.info("Scanning GAL (title%s)...",
                 " + description context" if context_automaton else "")
        t0 = time.time()
        scanned, _matched, max_ts = _match_gal_stream(
            con, gal_automaton, specialty_map, since_ts=0,
            context_automaton=context_automaton,
        )
        elapsed = time.time() - t0
        log.info("  scanned %d rows in %.1fs", scanned, elapsed)
        _set_last_ts(con, "gal", max_ts)

        after_total = con.execute(
            "SELECT count(*) FROM fda_match_cache WHERE source_type='gal'"
        ).fetchone()[0]
        log.info("GAL rows after: %d (delta: %+d)",
                 after_total, after_total - before_total)

        log.info("Breakdown by match_type:")
        for mt, n in con.execute(
            "SELECT match_type, count(*) FROM fda_match_cache "
            "WHERE source_type='gal' GROUP BY match_type ORDER BY match_type"
        ).fetchall():
            log.info("  %-12s %d", mt, n)

        con.execute("CHECKPOINT")
    finally:
        con.close()


if __name__ == "__main__":
    main()
