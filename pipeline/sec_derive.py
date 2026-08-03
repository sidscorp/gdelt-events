"""Derive the facts that let a page say something, from the facts SEC filed.

Runs after each ingest. Everything here is arithmetic over `snapshots` — growth
rates, margins, streaks, where profit actually came from, and where a company
sits against its own history and its industry.

This exists so the observations layer never has to compute anything at render
time and never has to invent anything: if a number is not in `derived`, no
sentence on the page is allowed to state it.

    python -m pipeline.sec_derive --data-dir <dir>
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import sec_schema  # noqa: E402

log = logging.getLogger("sec_derive")

# A quarter is only comparable to the same quarter a year earlier; comparing Q4
# retail to Q2 retail is noise, not signal.
GROWTH_WINDOW = 8          # quarters considered for "fastest in N"
MIN_SECTOR_PEERS = 5       # below this a percentile is not worth stating


def _pct_change(now, then):
    if now is None or then is None or then == 0:
        return None
    return (now - then) / abs(then)


def _percentile(sorted_vals: list[float], v: float) -> float | None:
    """Fraction of peers at or below v. Simple rank, no interpolation."""
    if not sorted_vals:
        return None
    below = sum(1 for x in sorted_vals if x <= v)
    return below / len(sorted_vals)


def _quantiles(vals: list[float]) -> tuple[float, float, float]:
    s = sorted(vals)
    def q(p):
        if not s:
            return None
        i = min(int(p * (len(s) - 1) + 0.5), len(s) - 1)
        return s[i]
    return q(0.25), q(0.50), q(0.75)


def compute_for_company(rows: list[dict]) -> list[dict]:
    """rows = that company's snapshots, newest first. Returns derived rows."""
    by_key = {(r["fp"], r["fy"]): r for r in rows}
    ordered = sorted(rows, key=lambda r: r["period_end"])   # oldest first
    out: list[dict] = []

    # Growth history, so "fastest in N quarters" can be answered.
    growth_hist: list[tuple[str, float]] = []
    for r in ordered:
        prior = by_key.get((r["fp"], (r["fy"] or 0) - 1))
        g = _pct_change(r.get("revenue"), prior.get("revenue") if prior else None)
        if g is not None:
            growth_hist.append((r["period_end"], g))

    for idx, r in enumerate(ordered):
        prior_y = by_key.get((r["fp"], (r["fy"] or 0) - 1))
        prev_q = ordered[idx - 1] if idx else None
        rev, ni, oi = r.get("revenue"), r.get("net_income"), r.get("operating_income")
        gp, cl, ca = r.get("gross_profit"), r.get("current_liabilities"), r.get("current_assets")

        d: dict = {"cik": r["cik"], "period_end": r["period_end"], "fp": r["fp"]}
        d["revenue_yoy"] = _pct_change(rev, prior_y.get("revenue") if prior_y else None)
        d["revenue_qoq"] = _pct_change(rev, prev_q.get("revenue") if prev_q else None)
        d["net_income_yoy"] = _pct_change(ni, prior_y.get("net_income") if prior_y else None)
        d["operating_income_yoy"] = _pct_change(
            oi, prior_y.get("operating_income") if prior_y else None)

        d["gross_margin"] = gp / rev if rev and gp is not None else None
        d["operating_margin"] = oi / rev if rev and oi is not None else None
        d["net_margin"] = ni / rev if rev and ni is not None else None

        # Margin moves are stated in percentage points, not percent-of-percent.
        if prior_y and rev and prior_y.get("revenue"):
            po = (prior_y.get("operating_income") / prior_y["revenue"]
                  if prior_y.get("operating_income") is not None else None)
            pn = (prior_y.get("net_income") / prior_y["revenue"]
                  if prior_y.get("net_income") is not None else None)
            d["operating_margin_yoy_pp"] = (
                (d["operating_margin"] - po) * 100 if None not in (d["operating_margin"], po) else None)
            d["net_margin_yoy_pp"] = (
                (d["net_margin"] - pn) * 100 if None not in (d["net_margin"], pn) else None)

        # Return on equity and assets. These are how a bank is actually judged -
        # revenue and gross profit are tagged by 24.8% and 0.2% of bank filings
        # respectively, while equity and assets are tagged by 98% and 94%.
        # NOTE: for a quarter this is the QUARTER'S return, not an annualised
        # one. It is deliberately not multiplied by four; the page says which
        # period it covers and silently annualising would overstate it.
        eq, at = r.get("stockholders_equity"), r.get("total_assets")
        if ni is not None and eq:
            d["return_on_equity"] = ni / eq
        if ni is not None and at:
            d["return_on_assets"] = ni / at

        # Where profit actually came from. GOOGL Q2 FY2026: operating income
        # $40.77B but net income $112.19B, because $97.98B arrived from
        # non-operating gains. A table of totals hides exactly this.
        if ni is not None and oi is not None:
            d["nonop_income"] = ni - oi
            pretax = abs(oi) + abs(ni - oi)
            d["nonop_share_pretax"] = (ni - oi) / pretax if pretax else None

        # Where this quarter's growth ranks among the recent window.
        if d["revenue_yoy"] is not None:
            window = [g for pe, g in growth_hist if pe <= r["period_end"]][-GROWTH_WINDOW:]
            if len(window) >= 3:
                d["rev_growth_rank_n"] = len(window)
                d["rev_growth_is_best"] = int(d["revenue_yoy"] >= max(window))
                d["rev_growth_is_worst"] = int(d["revenue_yoy"] <= min(window))

        # Consecutive year-over-year revenue declines up to and including this one.
        streak = 0
        for pe, g in reversed([(pe, g) for pe, g in growth_hist if pe <= r["period_end"]]):
            if g < 0:
                streak += 1
            else:
                break
        d["decline_streak"] = streak

        out.append(d)
    return out


def build_sector_stats(con) -> int:
    """Quartiles per (sic, period_end, metric), so a margin can be judged."""
    con.execute("DELETE FROM sector_stats")
    rows = con.execute("""
        SELECT c.sic, s.period_end, d.gross_margin, d.net_margin, d.revenue_yoy
        FROM derived d
        JOIN snapshots s ON s.cik=d.cik AND s.period_end=d.period_end AND s.fp=d.fp
        JOIN companies c ON c.cik = d.cik
        WHERE c.sic IS NOT NULL AND s.fp <> 'FY'
    """).fetchall()
    buckets: dict[tuple, dict[str, list]] = {}
    for sic, pe, gm, nm, rg in rows:
        b = buckets.setdefault((sic, pe), {"gross_margin": [], "net_margin": [], "revenue_yoy": []})
        for k, v in (("gross_margin", gm), ("net_margin", nm), ("revenue_yoy", rg)):
            # Guard against the long-tail rows that would drag a quartile around.
            if v is not None and -10 < v < 10:
                b[k].append(v)
    n = 0
    for (sic, pe), metrics in buckets.items():
        for metric, vals in metrics.items():
            if len(vals) < MIN_SECTOR_PEERS:
                continue
            p25, p50, p75 = _quantiles(vals)
            con.execute("INSERT OR REPLACE INTO sector_stats "
                        "(sic, period_end, metric, p25, p50, p75, n) VALUES (?,?,?,?,?,?,?)",
                        (sic, pe, metric, p25, p50, p75, len(vals)))
            n += 1
    con.commit()
    return n


def attach_sector_position(con) -> int:
    """Percentile of each company's margins within its industry that period."""
    peers: dict[tuple, list[float]] = {}
    for sic, pe, gm, nm in con.execute("""
            SELECT c.sic, d.period_end, d.gross_margin, d.net_margin
            FROM derived d JOIN companies c ON c.cik=d.cik
            WHERE c.sic IS NOT NULL"""):
        if gm is not None and -10 < gm < 10:
            peers.setdefault((sic, pe, "gm"), []).append(gm)
        if nm is not None and -10 < nm < 10:
            peers.setdefault((sic, pe, "nm"), []).append(nm)
    for k in peers:
        peers[k].sort()

    n = 0
    for cik, pe, fp, sic, gm, nm in con.execute("""
            SELECT d.cik, d.period_end, d.fp, c.sic, d.gross_margin, d.net_margin
            FROM derived d JOIN companies c ON c.cik=d.cik
            WHERE c.sic IS NOT NULL""").fetchall():
        g_peers = peers.get((sic, pe, "gm"), [])
        n_peers = peers.get((sic, pe, "nm"), [])
        gp = _percentile(g_peers, gm) if gm is not None and len(g_peers) >= MIN_SECTOR_PEERS else None
        np_ = _percentile(n_peers, nm) if nm is not None and len(n_peers) >= MIN_SECTOR_PEERS else None
        if gp is None and np_ is None:
            continue
        con.execute("UPDATE derived SET sector_gross_margin_pct=?, sector_net_margin_pct=?, "
                    "sector_peers=? WHERE cik=? AND period_end=? AND fp=?",
                    (gp, np_, max(len(g_peers), len(n_peers)), cik, pe, fp))
        n += 1
    con.commit()
    return n


def rebuild_fts(con) -> int:
    """Search index over names + tickers. Rebuilt wholesale: bulk ingest would
    otherwise fire per-row triggers 289k times."""
    con.execute("DELETE FROM companies_fts")
    con.execute("""
        INSERT INTO companies_fts (name, ticker, cik)
        SELECT c.name, COALESCE((SELECT group_concat(t.ticker, ' ') FROM tickers t
                                 WHERE t.cik = c.cik), c.ticker), c.cik
        FROM companies c WHERE c.name IS NOT NULL
    """)
    con.commit()
    return con.execute("SELECT count(*) FROM companies_fts").fetchone()[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(Path(__file__).resolve().parent.parent / "data"))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s",
                        force=True)

    con = sec_schema.connect(Path(args.data_dir))
    sec_schema.create(con)
    con.row_factory = __import__("sqlite3").Row

    ciks = [r[0] for r in con.execute("SELECT DISTINCT cik FROM snapshots")]
    log.info("deriving for %d companies", len(ciks))
    con.execute("DELETE FROM derived")
    written = 0
    for i, cik in enumerate(ciks, 1):
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM snapshots WHERE cik=? ORDER BY period_end DESC", (cik,))]
        for d in compute_for_company(rows):
            cols = [k for k in d if d[k] is not None]
            con.execute(f"INSERT OR REPLACE INTO derived ({','.join(cols)}) "
                        f"VALUES ({','.join('?' * len(cols))})", [d[k] for k in cols])
            written += 1
        if i % 2000 == 0:
            con.commit()
            log.info("  %d/%d companies", i, len(ciks))
    con.commit()
    log.info("derived rows: %d", written)

    log.info("sector stats: %d", build_sector_stats(con))
    log.info("sector positions: %d", attach_sector_position(con))
    log.info("fts rows: %d", rebuild_fts(con))
    sec_schema.set_meta(con, "derived_at",
                        datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"))
    con.commit()
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
