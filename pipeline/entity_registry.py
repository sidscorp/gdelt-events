# -*- coding: utf-8 -*-
"""Entity spine: one row per real-world company, fused from SEC filers + FDA establishments,
with a tiered alias table that maps news-side organisation strings onto those entities.

WHY THIS EXISTS
    The news<->company link used to be `fda_match_cache` name matching, whose `stripped` tier is
    noise (UNESCO->Olympus, 'Patterson' the flooring firm). Every downstream surface that wants
    "what happened to company X" needs a link it can trust. This module is that link.

TIERS (revised 2026-08-29 -- see the DOMAIN note below; the original spec's domain tiers are
not implementable, there is no domain data anywhere in the fleet)

    T1_NAME_DISTINCT  exact normalised-name equality, name has >=2 tokens.   auto-join, 0.95
    T1_NAME_SINGLE    exact normalised-name equality, name is ONE token AND the entity passes
                      the liveness test.                                      auto-join, 0.90
    T2_SUCCESSOR      curated successor alias (GOOGLE -> Alphabet Inc.).      auto-join, 0.90
    T3_DEAD_FILER     one-token name whose only entity fails liveness.        NEVER auto-join
    T3_PARTIAL        shared bare word / substring.                           NEVER auto-join

    "Liveness": the entity is currently exchange-listed OR has an SEC filing period on/after
    LIVENESS_CUTOFF. Both halves are needed. Recency alone drops foreign private issuers, which
    file 20-F and therefore have no rows in `snapshots` at all (Shell, Prudential, Canon,
    Shimadzu, Sysmex, Nihon Kohden). Exchange alone keeps dead shells that still carry a ticker
    (MORGAN GROUP HOLDING CO, MGHL, last filed 2023).

    The liveness test is what makes single-token names safe. Measured 2026-08-29 it rejects
    exactly the false joins found by hand -- TESCO CORP (the mentions are the UK grocer, which
    is not an SEC filer), MORGAN GROUP HOLDING CO (mentions are JPMorgan/Morgan Stanley),
    "Alphabet Holding Company, Inc." (a shell, not Google's Alphabet), Aurum Inc., MERIDIAN CO
    LTD -- while keeping Apple, Oracle, Chevron, Visa, Shell, Canon.

DOMAIN FIELDS ARE EMPTY ON PURPOSE
    `domains` is in the schema because the plan specifies it and because it is the right second
    signal if a source ever appears. Today no source exists: SEC `companies` is
    (cik, ticker, name, updated_at, sic, sic_description, exchange, fiscal_year_end) and
    `fda_companies` is (owner_operator_number, firm_name, site_count, product_count,
    device_classes, medical_specialties). GKG's domain columns are the *publisher's* domain, not
    the subject company's. Do not "fix" the empty column by populating it from GKG.

NEGATIVE RESULT -- do not retry
    Sector/SIC compatibility as a match discriminator. Of the 260 SEC<->FDA name joins, ~26 pair
    a non-medical SIC with an FDA establishment and every one inspected is correct (Sony,
    Ricoh, Stericycle, TE Connectivity, Thermo Fisher, Procter & Gamble, ...). Industrials are
    legitimately FDA-registered. A sector gate loses ~10% of correct joins and catches nothing.

WRITE DISCIPLINE (pipeline rule, has caused real incidents)
    Everything is computed on a READ-ONLY connection and staged in memory; there is exactly one
    short write burst at the end. Never hold the write connection across the GKG scan.

WHY A SEPARATE DATABASE FILE
    The registry is written to `data/entities.duckdb`, not into `gdelt.duckdb`. DuckDB allows
    many cross-process readers OR one writer, so writing these tables into the main database
    means stopping the live dashboard for the duration -- a deliberate outage of gdeltmonitor.com
    every time the spine is rebuilt. The registry is small, derived, and fully rebuildable, so it
    does not belong behind that lock. This is the same reasoning that put `sec.db` in SQLite.
    Readers join across the two with ATTACH, which costs nothing:

        ATTACH 'data/entities.duckdb' AS ent (READ_ONLY);
        SELECT ... FROM gkg g JOIN ent.entity_alias a ON ... ;

    Rebuilds are therefore safe at any time, with the dashboard serving.

USAGE
    python pipeline/entity_registry.py --build --dry-run     # compute + report, write nothing
    python pipeline/entity_registry.py --build               # compute + one write burst
    python pipeline/entity_registry.py --build --gkg-sample 300000
    python pipeline/entity_registry.py --audit-sample 200 --out data/entity_audit.tsv
"""
from __future__ import annotations

import argparse
import collections
import json
import logging
import os
import re
import sqlite3
import sys
import time

log = logging.getLogger("entity_registry")

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(HERE, "data")
DUCK = os.path.join(DATA, "gdelt.duckdb")      # read-only source (gkg)
ENTDB = os.path.join(DATA, "entities.duckdb")  # write target -- see WHY A SEPARATE FILE
SECDB = os.path.join(DATA, "sec.db")
TMPDIR = os.path.join(DATA, "duckdb_tmp")

LIVENESS_CUTOFF = "2025-01-01"
GKG_MIN_MENTIONS = 2          # a one-off org string is not evidence
BUILD_VERSION = 1

# --------------------------------------------------------------------------- normalisation

LEGAL_SUFFIXES = {
    "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "COMPANIES", "LLC", "LLP",
    "LP", "LTD", "LIMITED", "PLC", "GMBH", "AG", "SA", "NV", "BV", "AB", "AS", "OY", "OYJ",
    "SPA", "SRL", "PTY", "PTE", "KK", "KGAA", "KG", "SE", "ASA", "APS", "SAS", "CV", "HOLDING",
    "HOLDINGS", "GROUP", "GRP", "TRUST", "THE", "PBC", "LLLP", "ULC", "AND", "OF", "SASU",
    "SPZOO", "ZOO", "DAC", "NPC", "EURL", "SCA", "SNC", "SARL",
}
# SEC appends state-of-incorporation and vintage markers: "FOO CORP /DE/", "BAR INC /NEW/".
_STATE_TAG = re.compile(r"[/\\][A-Z]{2,4}[/\\]?\s*$")
_PUNCT = re.compile(r"[^A-Z0-9]+")


def normalise(name):
    """Company name -> comparison key. Idempotent; returns '' for unusable input."""
    if not name:
        return ""
    s = name.upper().strip().replace("&", " AND ")
    for _ in range(3):                       # "CORP /DE/ /NEW/" needs more than one pass
        s2 = _STATE_TAG.sub("", s).strip()
        if s2 == s:
            break
        s = s2
    s = _PUNCT.sub(" ", s).strip()
    toks = s.split()
    while toks and toks[-1] in LEGAL_SUFFIXES:
        toks.pop()
    while toks and toks[0] == "THE":
        toks.pop(0)
    return " ".join(toks)


# --------------------------------------------------------------------------- successor map
# Hand-curated. These are normalised news names whose only SEC row is a dead filer, but whose
# mentions are real and high-volume. Without this they are correctly blocked as T3_DEAD_FILER
# and the product loses its single largest org (GOOGLE, ~4.7k mentions per 300k GKG rows).
# Each entry is (normalised alias -> ticker of the surviving entity). Extend deliberately:
# every line here is an assertion that today's news use of the name means that company.
SUCCESSORS = {
    "GOOGLE": "GOOGL",          # Google Inc. last filed 2015; Alphabet Inc. is the filer
    "LINKEDIN": "MSFT",         # acquired by Microsoft 2016
    "RAYTHEON": "RTX",          # Raytheon Co -> RTX Corp
    "SPRINT": "TMUS",           # merged into T-Mobile US 2020
    "TIME WARNER": "WBD",       # -> WarnerMedia -> Warner Bros. Discovery
    "TWENTY FIRST CENTURY FOX": "DIS",
    "MONSANTO": None,           # Bayer AG, not an SEC filer -> stays blocked, recorded here
    "TWITTER": None,            # X Corp is private -> intentionally no target
    "SEARS": None,
    "SAFEWAY": None,
    "MCAFEE": None,
    "DIRECTV": None,
}


# --------------------------------------------------------------------------- alias guards
# Both of these come out of the 200-row hand audit on 2026-08-29 (see docs/AUDIT-200 in the
# review folder). They were applied AFTER that measurement, so the 98.5% link precision reported
# for this phase is not circular.

MIN_SINGLE_TOKEN_LEN = 3
# A one-token alias shorter than this never auto-joins. GKG emits 2-character org strings from
# acronyms and name fragments; "SU" (26 mentions) was joining to SU Group Holdings Ltd. The
# correct 3-character names in the audit sample -- RXO, DHT, IDT -- are unaffected.

ALIAS_BLOCKLIST = {
    "DOVER": "town in Delaware/Kent/New Hampshire and a port; the place dominates the mentions",
    "GLOBAL ENTERTAINMENT": "generic noun phrase joining to a shell filer with no exchange",
    "SU": "2-character acronym fragment (also caught by MIN_SINGLE_TOKEN_LEN)",
}


# --------------------------------------------------------------------------- loading

def load_sec(path=SECDB):
    """SEC filers + ticker lists + liveness inputs. Read-only URI connection."""
    con = sqlite3.connect("file:" + path + "?mode=ro", uri=True)
    try:
        companies = con.execute(
            "SELECT cik, name, ticker, sic, sic_description, exchange "
            "FROM companies WHERE name IS NOT NULL"
        ).fetchall()
        tickers = collections.defaultdict(list)
        for tic, cik, is_primary in con.execute(
            "SELECT ticker, cik, is_primary FROM tickers WHERE ticker IS NOT NULL"
        ):
            tickers[cik].append((tic, is_primary))
        last_period = dict(con.execute(
            "SELECT cik, MAX(period_end) FROM snapshots GROUP BY cik"
        ).fetchall())
    finally:
        con.close()
    return companies, tickers, last_period


def open_duck_ro(path=DUCK):
    import duckdb
    con = duckdb.connect(path, read_only=True)
    # Without an explicit temp_directory a spill fails with '\\.tmp invalid' and poisons every
    # later query on the connection. This is a documented db.py gotcha; do not drop it.
    os.makedirs(TMPDIR, exist_ok=True)
    con.execute("SET temp_directory='%s'" % TMPDIR.replace("\\", "/"))
    return con


def load_fda(con):
    return con.execute(
        "SELECT owner_operator_number, firm_name, device_classes, medical_specialties "
        "FROM fda_companies WHERE firm_name IS NOT NULL"
    ).fetchall()


def scan_gkg_orgs(con, sample=None, chunk=50000):
    """Normalised GKG organisation -> mention count.

    Streamed in chunks: the column is ~3.6M rows of ';'-delimited strings and materialising it
    whole is needless pressure on a connection the live dashboard is sharing. GKG rows are known
    to contain corrupt/misaligned records, so parsing stays defensive -- split, strip, length-cap,
    never trust position.
    """
    q = ("SELECT V1ORGANIZATIONS FROM gkg "
         "WHERE V1ORGANIZATIONS IS NOT NULL AND V1ORGANIZATIONS <> ''")
    if sample:
        q += " USING SAMPLE %d ROWS" % int(sample)
    cur = con.execute(q)
    counts = collections.Counter()
    rows = 0
    while True:
        batch = cur.fetchmany(chunk)
        if not batch:
            break
        rows += len(batch)
        for (blob,) in batch:
            for org in blob.split(";"):
                org = org.strip()
                if not org or len(org) > 80:
                    continue
                key = normalise(org)
                if key:
                    counts[key] += 1
        if rows % (chunk * 10) == 0:
            log.info("gkg scan: %d rows, %d distinct orgs", rows, len(counts))
    log.info("gkg scan done: %d rows, %d distinct normalised orgs", rows, len(counts))
    return counts, rows


# --------------------------------------------------------------------------- build

class Entity(object):
    __slots__ = ("entity_id", "canonical_name", "norm_name", "cik", "tickers", "exchange",
                 "sic", "sic_description", "fda_firm_ids", "device_classes",
                 "medical_specialties", "alt_names", "last_filing", "live", "sources",
                 "match_tier", "confidence")

    def __init__(self, **kw):
        for s in self.__slots__:
            setattr(self, s, kw.get(s))


def is_live(exchange, last_period):
    """Currently exchange-listed OR filed on/after the cutoff. See module docstring."""
    return bool(exchange) or (last_period or "") >= LIVENESS_CUTOFF


def build(gkg_sample=None):
    t0 = time.time()
    companies, tickers, last_period = load_sec()
    con = open_duck_ro()
    try:
        fda = load_fda(con)
        gkg_counts, gkg_rows = scan_gkg_orgs(con, sample=gkg_sample)
    finally:
        con.close()          # read-only work is over BEFORE anything is written

    # ---- SEC entities, one per cik; a normalised name may map to several
    by_norm = collections.defaultdict(list)
    entities = {}
    for cik, name, ticker, sic, sic_desc, exchange in companies:
        n = normalise(name)
        if not n:
            continue
        tics = [t for t, _ in sorted(tickers.get(cik, []), key=lambda x: -(x[1] or 0))]
        if ticker and ticker not in tics:
            tics.insert(0, ticker)
        e = Entity(entity_id="sec:%d" % cik, canonical_name=name, norm_name=n, cik=cik,
                   tickers=tics, exchange=exchange, sic=sic, sic_description=sic_desc,
                   fda_firm_ids=[], device_classes=None, medical_specialties=None,
                   alt_names=[], last_filing=last_period.get(cik),
                   live=is_live(exchange, last_period.get(cik)), sources=["sec"],
                   match_tier=None, confidence=1.0)
        entities[e.entity_id] = e
        by_norm[n].append(e)

    def pick(cands):
        """Among entities sharing a normalised name: live wins, then most recent filer, then
        lowest CIK. FDA-only entities have cik None and rank last, so a real filer always beats
        a bare establishment record."""
        if not cands:
            return None
        live = [c for c in cands if c.live] or cands
        return sorted(live,
                      key=lambda c: ((c.last_filing or ""), -(c.cik or 10 ** 12)),
                      reverse=True)[0]

    # ---- fuse FDA establishments onto SEC entities by exact normalised name (T1)
    fused = 0
    fda_only = 0
    for oon, firm, dev, spec in fda:
        n = normalise(firm)
        if not n:
            continue
        target = pick(by_norm.get(n))
        if target is not None:
            target.fda_firm_ids.append(oon)
            if firm != target.canonical_name and firm not in target.alt_names:
                target.alt_names.append(firm)
            target.device_classes = target.device_classes or dev
            target.medical_specialties = target.medical_specialties or spec
            if "fda" not in target.sources:
                target.sources.append("fda")
            if target.cik is not None:
                # match_tier describes an SEC<->FDA fusion. Two FDA establishments merging onto
                # one FDA-only entity is not that, and must not be labelled as one.
                target.match_tier = ("T1_NAME_DISTINCT" if len(n.split()) > 1
                                     else "T1_NAME_SINGLE")
                fused += 1
        else:
            eid = "fda:%d" % oon
            e = Entity(entity_id=eid, canonical_name=firm, norm_name=n, cik=None, tickers=[],
                       exchange=None, sic=None, sic_description=None, fda_firm_ids=[oon],
                       device_classes=dev, medical_specialties=spec, alt_names=[],
                       last_filing=None, live=False, sources=["fda"], match_tier=None,
                       confidence=1.0)
            entities[eid] = e
            by_norm[n].append(e)
            fda_only += 1

    # ---- ticker index, for the successor map
    by_ticker = {}
    for e in entities.values():
        for t in (e.tickers or []):
            by_ticker.setdefault(t, e)

    # ---- aliases: the news-side edge
    aliases = []
    seen = set()

    def emit(alias, norm_alias, entity_id, source, tier, auto, conf, mentions=0):
        k = (norm_alias, entity_id, source)
        if k in seen:
            return
        seen.add(k)
        aliases.append(dict(entity_id=entity_id, alias=alias, norm_alias=norm_alias,
                            alias_source=source, tier=tier, auto_join=auto,
                            confidence=conf, gkg_mentions=mentions))

    for e in entities.values():
        emit(e.canonical_name, e.norm_name, e.entity_id,
             "sec_name" if e.cik else "fda_firm", "T1_NAME_DISTINCT", True, 1.0)
        for alt in e.alt_names:
            emit(alt, normalise(alt), e.entity_id, "fda_firm", "T1_NAME_DISTINCT", True, 1.0)

    stats = collections.Counter()
    for norm_org, mentions in gkg_counts.items():
        if mentions < GKG_MIN_MENTIONS:
            continue
        if norm_org in ALIAS_BLOCKLIST:
            stats["blocklisted"] += 1
            continue
        if len(norm_org.split()) == 1 and len(norm_org) < MIN_SINGLE_TOKEN_LEN:
            stats["too_short"] += 1
            continue
        cands = by_norm.get(norm_org)
        if not cands:
            if norm_org in SUCCESSORS:
                tgt = SUCCESSORS[norm_org]
                ent = by_ticker.get(tgt) if tgt else None
                if ent is not None:
                    emit(norm_org, norm_org, ent.entity_id, "gkg_org", "T2_SUCCESSOR",
                         True, 0.90, mentions)
                    stats["T2_SUCCESSOR"] += 1
                else:
                    stats["successor_no_target"] += 1
            continue
        target = pick(cands)
        multi = len(norm_org.split()) > 1
        if norm_org in SUCCESSORS:
            tgt = SUCCESSORS[norm_org]
            ent = by_ticker.get(tgt) if tgt else None
            if ent is not None:
                emit(norm_org, norm_org, ent.entity_id, "gkg_org", "T2_SUCCESSOR",
                     True, 0.90, mentions)
                stats["T2_SUCCESSOR"] += 1
            else:
                emit(norm_org, norm_org, target.entity_id, "gkg_org", "T3_DEAD_FILER",
                     False, 0.30, mentions)
                stats["T3_DEAD_FILER"] += 1
            continue
        if multi:
            emit(norm_org, norm_org, target.entity_id, "gkg_org", "T1_NAME_DISTINCT",
                 True, 0.95, mentions)
            stats["T1_NAME_DISTINCT"] += 1
        elif target.live:
            emit(norm_org, norm_org, target.entity_id, "gkg_org", "T1_NAME_SINGLE",
                 True, 0.90, mentions)
            stats["T1_NAME_SINGLE"] += 1
        else:
            emit(norm_org, norm_org, target.entity_id, "gkg_org", "T3_DEAD_FILER",
                 False, 0.30, mentions)
            stats["T3_DEAD_FILER"] += 1

    report = {
        "build_version": BUILD_VERSION,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "elapsed_s": round(time.time() - t0, 1),
        "sec_companies": len(companies),
        "fda_establishments": len(fda),
        "gkg_rows_scanned": gkg_rows,
        "gkg_sample": gkg_sample,
        "gkg_distinct_orgs": len(gkg_counts),
        "entities_total": len(entities),
        "entities_sec": sum(1 for e in entities.values() if e.cik),
        "entities_fda_only": fda_only,
        "entities_with_both": sum(1 for e in entities.values() if e.cik and e.fda_firm_ids),
        "entities_live": sum(1 for e in entities.values() if e.live),
        "fda_fused": fused,
        "aliases_total": len(aliases),
        "aliases_auto_join": sum(1 for a in aliases if a["auto_join"]),
        "gkg_edges": dict(stats),
    }
    return entities, aliases, report


# --------------------------------------------------------------------------- write

DDL = [
    """CREATE OR REPLACE TABLE entity_registry (
        entity_id VARCHAR, canonical_name VARCHAR, norm_name VARCHAR,
        cik BIGINT, tickers VARCHAR[], exchange VARCHAR,
        sic VARCHAR, sic_description VARCHAR,
        fda_firm_ids BIGINT[], device_classes VARCHAR, medical_specialties VARCHAR,
        domains VARCHAR[], alt_names VARCHAR[],
        last_filing VARCHAR, live BOOLEAN, sources VARCHAR[],
        match_tier VARCHAR, confidence DOUBLE, built_at TIMESTAMP)""",
    """CREATE OR REPLACE TABLE entity_alias (
        entity_id VARCHAR, alias VARCHAR, norm_alias VARCHAR, alias_source VARCHAR,
        tier VARCHAR, auto_join BOOLEAN, confidence DOUBLE, gkg_mentions BIGINT,
        built_at TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS entity_build_log (
        built_at TIMESTAMP, build_version INTEGER, report_json VARCHAR)""",
]


def write(entities, aliases, report, path=ENTDB):
    """One short write burst into the registry's own database file (see WHY A SEPARATE DATABASE
    FILE). The read-only connection to gdelt.duckdb is already closed by now."""
    import duckdb
    ts = report["built_at"]
    ent_rows = [(e.entity_id, e.canonical_name, e.norm_name, e.cik, e.tickers, e.exchange,
                 e.sic, e.sic_description, e.fda_firm_ids, e.device_classes,
                 e.medical_specialties, [], e.alt_names, e.last_filing, bool(e.live),
                 e.sources, e.match_tier, e.confidence, ts)
                for e in entities.values()]
    ali_rows = [(a["entity_id"], a["alias"], a["norm_alias"], a["alias_source"], a["tier"],
                 bool(a["auto_join"]), a["confidence"], a["gkg_mentions"], ts) for a in aliases]
    con = duckdb.connect(path)
    try:
        for stmt in DDL:
            con.execute(stmt)
        con.executemany(
            "INSERT INTO entity_registry VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ent_rows)
        con.executemany(
            "INSERT INTO entity_alias VALUES (?,?,?,?,?,?,?,?,?)", ali_rows)
        con.execute("INSERT INTO entity_build_log VALUES (?,?,?)",
                    (ts, BUILD_VERSION, json.dumps(report)))
        con.execute("CREATE INDEX IF NOT EXISTS ix_alias_norm ON entity_alias(norm_alias)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_entity_id ON entity_registry(entity_id)")
    finally:
        con.close()
    log.info("wrote %d entities, %d aliases", len(ent_rows), len(ali_rows))


# --------------------------------------------------------------------------- audit

def audit_sample(n, out, seed=1, path=ENTDB):
    """Random sample of AUTO-JOINED edges for the hand audit that gates this phase.

    Sampled uniformly over auto-join alias rows that carry news evidence (gkg_mentions > 0) --
    those are the joins the product actually acts on. Identity aliases (an entity's own SEC/FDA
    name) are excluded: auditing 'ABBOTT LABORATORIES -> ABBOTT LABORATORIES' measures nothing.
    """
    import random
    con = open_duck_ro(path)
    try:
        rows = con.execute(
            "SELECT a.norm_alias, a.tier, a.confidence, a.gkg_mentions, "
            "       r.canonical_name, r.tickers, r.exchange, r.sic_description, "
            "       r.last_filing, r.fda_firm_ids "
            "FROM entity_alias a JOIN entity_registry r USING (entity_id) "
            "WHERE a.auto_join AND a.gkg_mentions > 0"
        ).fetchall()
    finally:
        con.close()
    random.seed(seed)
    pick = random.sample(rows, min(n, len(rows)))
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("verdict\tnews_org\ttier\tconf\tmentions\tentity\ttickers\texchange\tsic\t"
                 "last_filing\tn_fda_sites\n")
        for r in pick:
            fh.write("\t".join(["", str(r[0]), str(r[1]), "%.2f" % r[2], str(r[3]),
                                str(r[4]), ",".join(r[5] or []), str(r[6] or ""),
                                str(r[7] or ""), str(r[8] or ""),
                                str(len(r[9] or []))]) + "\n")
    log.info("wrote %d audit rows to %s (population %d)", len(pick), out, len(rows))
    return len(pick), len(rows)


# --------------------------------------------------------------------------- cli

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="compute + report, write nothing")
    ap.add_argument("--gkg-sample", type=int, default=None)
    ap.add_argument("--audit-sample", type=int, default=0)
    ap.add_argument("--out", default=os.path.join(DATA, "entity_audit.tsv"))
    args = ap.parse_args(argv)

    # force=True: importing anything from embed_new_articles configures root first and silently
    # swallows these handlers otherwise (long job, 0-byte log).
    logging.basicConfig(level=logging.INFO, force=True,
                        format="%(asctime)s %(levelname)s %(message)s")

    if args.build:
        entities, aliases, report = build(gkg_sample=args.gkg_sample)
        print(json.dumps(report, indent=2))
        if args.dry_run:
            log.info("dry run: nothing written")
        else:
            write(entities, aliases, report)
    if args.audit_sample:
        got, pop = audit_sample(args.audit_sample, args.out)
        print("audit sample: %d rows of %d auto-join news edges -> %s" % (got, pop, args.out))
    if not args.build and not args.audit_sample:
        ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
