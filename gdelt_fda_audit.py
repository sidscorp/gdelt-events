#!/usr/bin/env python3
"""Audit tools for the FDA Medical Devices view.

Usage:
    python gdelt_fda_audit.py --top-names 30
    python gdelt_fda_audit.py --sample 20
    python gdelt_fda_audit.py --by-specialty
"""

import argparse
import duckdb
from pipeline.config import DB_PATH


def top_names(con, n: int) -> None:
    print(f"=== Top {n} matched FDA company names by hit count ===")
    print(f"{'count':>8}  {'prod_cnt':>8}  {'specialty':<30}  name")
    print(f"{'-'*8}  {'-'*8}  {'-'*30}  {'-'*40}")
    rows = con.execute("""
        SELECT fmc.matched_name,
               count(*) as n,
               any_value(fc.product_count) as product_count,
               any_value(fc.medical_specialties) as specialty
        FROM fda_match_cache fmc
        LEFT JOIN fda_companies fc ON fc.firm_name = fmc.matched_name
        GROUP BY fmc.matched_name
        ORDER BY n DESC
        LIMIT ?
    """, [n]).fetchall()
    for name, cnt, pc, spec in rows:
        spec_s = (spec or "")[:30]
        print(f"{cnt:>8,}  {pc or 0:>8}  {spec_s:<30}  {name}")


def sample_matches(con, n: int) -> None:
    print(f"=== {n} random matched GKG articles ===\n")
    # DuckDB's USING SAMPLE requires a literal count; just inline it safely.
    n_int = int(n)
    rows = con.execute(f"""
        SELECT fmc.matched_name,
               fmc.medical_specialties,
               gkg."V2SOURCECOMMONNAME",
               regexp_extract(gkg."V2EXTRASXML", '<PAGE_TITLE>(.*?)</PAGE_TITLE>', 1) as title,
               substr(gkg."V2ENHANCEDORGANIZATIONS", 1, 200) as orgs_preview,
               fmc.crawled_at
        FROM fda_match_cache fmc
        INNER JOIN gkg ON gkg."GKGRECORDID" = fmc.article_id
        WHERE fmc.source_type = 'gkg'
        ORDER BY random()
        LIMIT {n_int}
    """).fetchall()
    for i, (name, spec, source, title, orgs, ts) in enumerate(rows, 1):
        print(f"[{i}] matched={name!r}  specialty={(spec or '-')[:30]}")
        print(f"    [{ts}] {source}: {(title or '')[:110]}")
        print(f"    orgs: {orgs or '(empty)'}")
        print()

    print(f"\n=== {n} random matched GAL articles ===\n")
    try:
        rows = con.execute(f"""
            SELECT fmc.matched_name,
                   fmc.medical_specialties,
                   gal.domain,
                   gal.title,
                   fmc.crawled_at
            FROM fda_match_cache fmc
            INNER JOIN gal ON gal.url = fmc.article_id
            WHERE fmc.source_type = 'gal'
            ORDER BY random()
            LIMIT {n_int}
        """).fetchall()
        for i, (name, spec, domain, title, ts) in enumerate(rows, 1):
            print(f"[{i}] matched={name!r}  specialty={(spec or '-')[:30]}")
            print(f"    [{ts}] {domain}: {(title or '')[:110]}")
            print()
    except Exception as e:
        print(f"  (no GAL matches yet or error: {e})")


def by_specialty(con) -> None:
    print("=== Match count by medical_specialties ===\n")
    rows = con.execute("""
        SELECT
            COALESCE(NULLIF(TRIM(medical_specialties), ''), '(unknown)') as spec,
            source_type,
            count(*) as n
        FROM fda_match_cache
        GROUP BY spec, source_type
        ORDER BY n DESC
    """).fetchall()
    cur_spec = None
    for spec, src, n in rows:
        if spec != cur_spec:
            print(f"\n{spec}")
            cur_spec = spec
        print(f"  {src}: {n:,}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--top-names", type=int, help="Show top N matched company names")
    parser.add_argument("--sample", type=int, help="Show N random matched articles")
    parser.add_argument("--by-specialty", action="store_true", help="Show match counts grouped by specialty")
    args = parser.parse_args()

    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        if args.top_names:
            top_names(con, args.top_names)
        if args.sample:
            sample_matches(con, args.sample)
        if args.by_specialty:
            by_specialty(con)
        if not (args.top_names or args.sample or args.by_specialty):
            parser.print_help()
    finally:
        con.close()


if __name__ == "__main__":
    main()
