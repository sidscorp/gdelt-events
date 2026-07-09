"""Flip judged shadow categories live: <cat>__v2 -> <cat> (gal side only).

One brief write session. GKG-side legacy rows are untouched (unused by the
UI). Run AFTER v2 evaluation passes and TOGETHER with deploying views.py +
pill_scorer.SUFFIX="" + dashboard restart.

    python -m pipeline.flip_pills [--pills a,b]
"""

import argparse
import logging
import time

from .config import DB_PATH
from .loader import _open_connection
from . import pill_scorer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("flip_pills")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pills", help="comma-separated pill keys (default: all curated+fda)")
    args = parser.parse_args()

    pills = [p for p in pill_scorer.load_pill_defs() if p["kind"] != "custom"]
    if args.pills:
        keep = {p.strip() for p in args.pills.split(",")}
        pills = [p for p in pills if p["key"] in keep]

    t0 = time.time()
    con = _open_connection(DB_PATH)
    try:
        for p in pills:
            cat, v2 = p["key"], p["key"] + "__v2"
            n_v2 = con.execute(
                "SELECT count(*) FROM article_tags WHERE category = ?", [v2]
            ).fetchone()[0]
            if not n_v2:
                log.warning("%s: no v2 rows — skipping flip for this pill", cat)
                continue
            n_old = con.execute(
                "SELECT count(*) FROM article_tags WHERE category = ? AND source_type='gal'",
                [cat],
            ).fetchone()[0]
            con.execute(
                "DELETE FROM article_tags WHERE category = ? AND source_type='gal'", [cat])
            con.execute(
                "UPDATE article_tags SET category = ? WHERE category = ?", [cat, v2])
            log.info("%s: %d old gal rows replaced by %d judged rows", cat, n_old, n_v2)
        con.execute("CHECKPOINT")
    finally:
        con.close()
    log.info("flip done in %.1fs", time.time() - t0)


if __name__ == "__main__":
    main()
