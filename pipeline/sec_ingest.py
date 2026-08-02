"""Collect SEC company financials into a local store, so pages serve instantly.

Freshness model - the point of the whole design:

  Financial facts change ONLY when a company files, and SEC publishes exactly who
  filed each day. So this never polls and never guesses:

    --backfill    one 1.4 GB companyfacts.zip covering every filer. Run once.
    --daily       read the ~1.1 MB daily filing index, take the CIKs that filed a
                  10-K/10-Q, refetch just those. On 2026-07-31 that was 181 of
                  5,782 filings - about 20 seconds of work at SEC's rate limit.

  Result: at most one business day stale, and every refresh is targeted.

SEC asks for a descriptive User-Agent and <=10 requests/second; both are enforced
here. See https://www.sec.gov/os/accessing-edgar-data
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import sys
import time
import urllib.request
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.sec_normalize import build_snapshots  # noqa: E402
from pipeline import sec_schema  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
DATA_DIR = REPO / "data"
LOG_DIR = DATA_DIR / "logs"

UA = "gdeltmonitor.com sidd@snambiar.com"
BULK_URL = "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"
SUBMISSIONS_ZIP = "https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip"
DAILY_IDX = "https://www.sec.gov/Archives/edgar/daily-index/{yr}/QTR{q}/form.{ymd}.idx"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

RATE_LIMIT_S = 0.12          # ~8 req/s, under SEC's 10/s ceiling
PERIODS_KEPT = 24            # ~6 years of quarters

log = logging.getLogger("sec_ingest")


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(LOG_DIR / "sec_ingest.log", encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)],
        force=True,   # embed_new_articles' import-time basicConfig would win otherwise
    )


_last_req = 0.0


def _get(url: str, binary: bool = False):
    global _last_req
    wait = RATE_LIMIT_S - (time.time() - _last_req)
    if wait > 0:
        time.sleep(wait)
    # No Accept-Encoding: urllib does not transparently decompress, so asking for
    # gzip just yields bytes that fail to parse. The bulk file is a zip already.
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=180) as r:
        raw = r.read()
    _last_req = time.time()
    return raw if binary else raw.decode("utf-8", errors="replace")


def load_ticker_map() -> dict[int, tuple[str, str]]:
    """cik -> (ticker, name). One request; changes rarely."""
    data = json.loads(_get(TICKERS_URL))
    out: dict[int, tuple[str, str]] = {}
    for row in data.values():
        cik = int(row["cik_str"])
        # Multiple share classes map to one CIK; first wins (they are ordered by
        # rank, so the primary listing comes first).
        out.setdefault(cik, (row.get("ticker", ""), row.get("title", "")))
    return out


def _store(con, cik: int, facts: dict, tickers: dict, ts: str) -> int:
    rows = build_snapshots(facts, limit=PERIODS_KEPT)
    if not rows:
        return 0
    ticker, name = tickers.get(cik, ("", facts.get("entityName", "")))
    sec_schema.upsert_company(con, cik, ticker or None,
                              name or facts.get("entityName"), ts)
    return sec_schema.upsert_snapshots(con, cik, rows)


def backfill(con, tickers: dict, limit: int | None = None) -> tuple[int, int]:
    """One bulk zip instead of 10,414 requests. Streams entry by entry so the
    1.4 GB never has to be held in memory or unpacked to disk."""
    log.info("downloading companyfacts.zip (~1.4 GB) ...")
    blob = _get(BULK_URL, binary=True)
    log.info("downloaded %.1f MB; normalizing", len(blob) / 1e6)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

    companies = rows = 0
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = [n for n in z.namelist() if n.startswith("CIK") and n.endswith(".json")]
        if limit:
            names = names[:limit]
        log.info("%d filers in archive", len(names))
        for i, name in enumerate(names, 1):
            try:
                cik = int(name[3:-5])
                with z.open(name) as fh:
                    facts = json.load(fh)
                n = _store(con, cik, facts, tickers, ts)
                if n:
                    companies += 1
                    rows += n
            except Exception as e:      # one malformed filer must not stop the run
                log.debug("skip %s: %s", name, e)
            if i % 500 == 0:
                con.commit()
                log.info("  %d/%d filers, %d rows", i, len(names), rows)
    con.commit()
    return companies, rows



def submissions(con, limit: int | None = None) -> tuple[int, int]:
    """Company profiles from SEC's bulk submissions archive.

    companyfacts has the numbers but not the industry, the exchange, or the full
    ticker list. Without SIC there is no peer group, and a figure with no peer
    group is a number rather than a judgement. Without every ticker, GOOG resolves
    to nothing while GOOGL works.

    Streams the archive; the per-CIK files also contain a long filing history we
    do not need, so only the header fields are read.
    """
    log.info("downloading submissions.zip (~1.6 GB) ...")
    blob = _get(SUBMISSIONS_ZIP, binary=True)
    log.info("downloaded %.1f MB; reading profiles", len(blob) / 1e6)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

    companies = tick = 0
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        # Per-company files are CIK##########.json; the -submissions-### shards are
        # continuation pages of the filing history and carry no profile fields.
        names = [n for n in z.namelist()
                 if n.startswith("CIK") and n.endswith(".json") and "-submissions-" not in n]
        if limit:
            names = names[:limit]
        log.info("%d profiles in archive", len(names))
        for i, name in enumerate(names, 1):
            try:
                with z.open(name) as fh:
                    d = json.load(fh)
                cik = int(d.get("cik") or name[3:-5])
                tk = [t for t in (d.get("tickers") or []) if t]
                ex = (d.get("exchanges") or [None])[0]
                sec_schema.upsert_profile(
                    con, cik, d.get("name"), d.get("sic"), d.get("sicDescription"),
                    ex, d.get("fiscalYearEnd"), tk, ts)
                companies += 1
                tick += len(tk)
            except Exception as e:
                log.debug("skip %s: %s", name, e)
            if i % 2000 == 0:
                con.commit()
                log.info("  %d/%d profiles, %d tickers", i, len(names), tick)
    con.commit()

    # The archive contains every entity that ever filed ANYTHING - overwhelmingly
    # individuals filing Forms 3/4/5 (961,471 of 979,405 on the first run). They
    # have no financial statements and no ticker, and left in place they bury
    # real companies in search results. Keep only filers we can actually show.
    pruned = con.execute(
        "DELETE FROM companies WHERE cik NOT IN (SELECT cik FROM snapshots) "
        "AND cik NOT IN (SELECT cik FROM tickers)").rowcount
    con.commit()
    log.info("pruned %d profile-only filers (no financials, no ticker)", pruned)
    kept = con.execute("SELECT count(*) FROM companies").fetchone()[0]
    log.info("companies retained: %d", kept)
    return kept, tick


def ciks_that_filed(day: date) -> set[int]:
    """CIKs with a 10-K/10-Q in the daily index - the change feed."""
    url = DAILY_IDX.format(yr=day.year, q=(day.month - 1) // 3 + 1,
                           ymd=day.strftime("%Y%m%d"))
    try:
        text = _get(url)
    except Exception as e:
        log.warning("no daily index for %s (%s)", day, e)
        return set()
    out: set[int] = set()
    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0] not in ("10-K", "10-Q", "10-K/A", "10-Q/A"):
            continue
        # Take the CIK from the edgar/data/<CIK>/... path, not from the first bare
        # integer on the line: company names contain digits ("3M", "1-800-FLOWERS"),
        # and that naive scan was pulling CIK 4 out of a company name.
        path = parts[-1]
        if "edgar/data/" in path:
            try:
                out.add(int(path.split("edgar/data/")[1].split("/")[0]))
            except (IndexError, ValueError):
                continue
    return out


def daily(con, tickers: dict, days_back: int = 1) -> tuple[int, int]:
    targets: set[int] = set()
    for d in range(days_back):
        targets |= ciks_that_filed(date.today() - timedelta(days=d + 1))
    log.info("%d filers filed a 10-K/10-Q in the last %dd", len(targets), days_back)

    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    companies = rows = 0
    for cik in sorted(targets):
        try:
            facts = json.loads(_get(FACTS_URL.format(cik=cik)))
            n = _store(con, cik, facts, tickers, ts)
            if n:
                companies += 1
                rows += n
        except Exception as e:
            log.warning("cik %s failed: %s", cik, e)
    con.commit()
    return companies, rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true", help="full bulk rebuild")
    ap.add_argument("--daily", action="store_true", help="refresh only recent filers")
    ap.add_argument("--submissions", action="store_true",
                    help="refresh company profiles (SIC, exchange, all tickers)")
    ap.add_argument("--days-back", type=int, default=1)
    ap.add_argument("--limit", type=int, help="backfill only N filers (testing)")
    ap.add_argument("--data-dir", default=str(DATA_DIR))
    args = ap.parse_args()
    if not (args.backfill or args.daily or args.submissions):
        ap.error("choose --backfill, --daily or --submissions")

    _setup_logging()
    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    con = sec_schema.connect(data_dir)
    sec_schema.create(con)

    t0 = time.time()
    mode = ("backfill" if args.backfill else
            "submissions" if args.submissions else "daily")
    try:
        if args.submissions:
            companies, rows = submissions(con, limit=args.limit)
        else:
            tickers = load_ticker_map()
            if args.backfill:
                companies, rows = backfill(con, tickers, limit=args.limit)
            else:
                companies, rows = daily(con, tickers, days_back=args.days_back)
        note = "ok"
    except Exception as e:
        companies = rows = 0
        note = f"error: {e}"
        log.exception("ingest failed")

    elapsed = time.time() - t0
    con.execute("INSERT INTO ingest_log (ts, mode, ciks_touched, rows_written, "
                "elapsed_s, note) VALUES (?,?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), mode,
                 companies, rows, round(elapsed, 1), note))
    # Serve layer keys its cache on this, the same way the feed keys on data_version.
    sec_schema.set_meta(con, "data_version",
                        datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"))
    con.commit()
    con.close()

    log.info("%s: %d companies, %d rows in %.1fs (%s)", mode, companies, rows, elapsed, note)
    return 0 if note == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
