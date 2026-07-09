"""Judged pill backfill — builds shadow (`__v2`) categories over a recent window.

Candidates per pill (keyword tags + FDA name matches + loose semantic net from
the embedding store) are judged by cerebras-fast (pipeline/pill_judge.py);
only approved articles get tagged. ~$1-2 and ~1-2h for the default 14 days.

OUTAGE-FREE: scan/score/judge phases hold NO write connection (read-only
lookups only); all writes land in one brief burst at the end.

    python -m pipeline.rescore_pills [--days 14] [--pills a,b] [--suffix __v2]
"""

import argparse
import logging
import time
from datetime import datetime, timedelta

import duckdb
import numpy as np
import pandas as pd

from .config import DB_PATH
from .loader import _open_connection
from . import embedding_store
from . import pill_scorer
from . import pill_judge

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("rescore_pills")

CHUNK = 200_000
SEM_NET_CAP = 2000  # max semantic-net candidates per pill for the backfill


def _read_con(retries: int = 30):
    last = None
    for _ in range(retries):
        try:
            con = duckdb.connect(str(DB_PATH), read_only=True)
            con.execute("SET threads = 2")
            con.execute("SET memory_limit = '4GB'")
            # Read-only conns have no valid default spill dir on Windows —
            # same gotcha (and fix) as dashboard/db.py::get_db. Without this,
            # hitting the memory limit fails with 'cannot create \\.tmp'.
            con.execute(f"SET temp_directory='{(DB_PATH.parent / 'duckdb_tmp').as_posix()}'")
            return con
        except duckdb.IOException as e:
            last = e
            time.sleep(2)
    raise RuntimeError(f"could not open read-only connection: {last}")


def main():
    parser = argparse.ArgumentParser(description="Judged pill backfill into shadow categories")
    parser.add_argument("--suffix", default="__v2")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--pills", help="comma-separated pill keys")
    args = parser.parse_args()

    pills = [p for p in pill_scorer.load_pill_defs() if p["kind"] != "custom"]
    if args.pills:
        keep = {p.strip() for p in args.pills.split(",")}
        pills = [p for p in pills if p["key"] in keep]
    if not pills:
        raise SystemExit("no pills to rescore")
    cutoff = int((datetime.utcnow() - timedelta(days=args.days)).strftime("%Y%m%d%H%M%S"))
    log.info("judged backfill: %d pills, %d days (cutoff %d) -> suffix '%s'",
             len(pills), args.days, cutoff, args.suffix)

    pill_vecs = pill_scorer.get_pill_vectors(pills)
    keys = [p["key"] for p in pills]

    # ---- candidates from keyword tags + fda (read-only) -------------------
    con = _read_con()
    try:
        cand: dict[str, dict] = {k: {} for k in keys}  # key -> {url: score|None}
        for p in pills:
            if p["kind"] != "curated":
                continue
            for (u,) in con.execute(
                "SELECT DISTINCT article_id FROM article_tags "
                "WHERE category = ? AND source_type='gal' AND crawled_at >= ?",
                [p["key"], cutoff],
            ).fetchall():
                cand[p["key"]][u] = None
        for p in pills:
            if p["kind"] != "fda":
                continue
            for (u,) in con.execute(
                "SELECT DISTINCT article_id FROM fda_match_cache "
                "WHERE source_type='gal' AND crawled_at >= ? "
                "AND match_type IN ('legal','contextual','stripped')",
                [cutoff],
            ).fetchall():
                cand[p["key"]][u] = None
    finally:
        con.close()
    log.info("keyword/fda candidates: %s", {k: len(v) for k, v in cand.items()})

    # ---- semantic net from the store (no DB connection) -------------------
    t0 = time.time()
    net: dict[str, list] = {k: [] for k in keys}
    for urls, vectors, _ in embedding_store.iter_active_chunks(chunk_rows=CHUNK):
        scores = pill_scorer._score_matrix(vectors, pill_vecs, keys)
        for j, k in enumerate(keys):
            s = scores[:, j]
            for i in np.nonzero(s >= pill_scorer.SEM_NET)[0]:
                net[k].append((float(s[i]), urls[i]))
    for p in pills:
        k = p["key"]
        if p["kind"] == "fda":
            continue  # fda candidates come only from name matches
        top = sorted(net[k], reverse=True)[:SEM_NET_CAP]
        for sc, u in top:
            if u not in cand[k]:
                cand[k][u] = sc
    log.info("semantic net done in %.0fs: %s", time.time() - t0,
             {k: len(v) for k, v in cand.items()})

    # ---- titles + crawled_at, window filter (read-only) -------------------
    all_urls = sorted({u for d in cand.values() for u in d})
    con = _read_con()
    try:
        titles = pill_scorer._titles_for(con, all_urls)
        crawled = pill_scorer._crawled_at_for(con, all_urls)
    finally:
        con.close()
    log.info("lookups: %d urls, %d titled, %d dated",
             len(all_urls), len(titles), len(crawled))
    if all_urls and (len(crawled) < len(all_urls) * 0.5 or len(titles) < len(all_urls) * 0.3):
        raise RuntimeError(
            f"lookup coverage implausibly low (titled={len(titles)}, dated={len(crawled)} "
            f"of {len(all_urls)}) — refusing to write a gutted backfill; see warnings above")

    # ---- judge (no DB connection; gateway calls) ---------------------------
    frames = []
    for p in pills:
        k = p["key"]
        items = [{"url": u, "title": titles[u][0], "desc": titles[u][1]}
                 for u in cand[k]
                 if u in titles and crawled.get(u) and crawled[u] >= cutoff]
        log.info("%s: judging %d candidates...", k, len(items))
        t1 = time.time()
        verdicts = pill_judge.judge(k + args.suffix, items) or {}
        accept = {"relevant"} if p.get("strict") else pill_judge.ACCEPT
        approved = [u for u, v in verdicts.items() if v in accept]
        rows = [(u, "gal", k + args.suffix, "judge",
                 verdicts[u] + (f"|{cand[k][u]:.3f}" if cand[k].get(u) else ""),
                 crawled[u]) for u in approved]
        if rows:
            frames.append(pd.DataFrame(rows, columns=[
                "article_id", "source_type", "category",
                "matched_via", "matched_detail", "crawled_at"]))
        log.info("%s: %d/%d approved (%.0fs)", k, len(approved), len(items), time.time() - t1)
    staged = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    # ---- ONE brief write burst ---------------------------------------------
    log.info("write phase: %d rows", len(staged))
    t2 = time.time()
    wcon = _open_connection(DB_PATH)
    try:
        for p in pills:
            wcon.execute("DELETE FROM article_tags WHERE category = ?",
                         [p["key"] + args.suffix])
        if len(staged):
            wcon.register("_staged_tags", staged)
            wcon.execute(
                "INSERT INTO article_tags (article_id, source_type, category, "
                "matched_via, matched_detail, crawled_at) "
                "SELECT * FROM _staged_tags"
            )
            wcon.unregister("_staged_tags")
        wcon.execute("CHECKPOINT")
    finally:
        wcon.close()
    log.info("write phase done in %.1fs", time.time() - t2)


if __name__ == "__main__":
    main()
