"""DuckDB table definitions and CSV reader configuration for GDELT v2."""

import duckdb

# ── Column Definitions ──────────────────────────────────────────────────────
# Sourced from GDELT codebooks and verified against actual data in
# data-exploration-march-2026/analyze_gdelt.py

EVENTS_COLUMNS = {
    "GlobalEventID": "BIGINT",
    "Day": "INTEGER",
    "MonthYear": "INTEGER",
    "Year": "INTEGER",
    "FractionDate": "DOUBLE",
    "Actor1Code": "VARCHAR",
    "Actor1Name": "VARCHAR",
    "Actor1CountryCode": "VARCHAR",
    "Actor1KnownGroupCode": "VARCHAR",
    "Actor1EthnicCode": "VARCHAR",
    "Actor1Religion1Code": "VARCHAR",
    "Actor1Religion2Code": "VARCHAR",
    "Actor1Type1Code": "VARCHAR",
    "Actor1Type2Code": "VARCHAR",
    "Actor1Type3Code": "VARCHAR",
    "Actor2Code": "VARCHAR",
    "Actor2Name": "VARCHAR",
    "Actor2CountryCode": "VARCHAR",
    "Actor2KnownGroupCode": "VARCHAR",
    "Actor2EthnicCode": "VARCHAR",
    "Actor2Religion1Code": "VARCHAR",
    "Actor2Religion2Code": "VARCHAR",
    "Actor2Type1Code": "VARCHAR",
    "Actor2Type2Code": "VARCHAR",
    "Actor2Type3Code": "VARCHAR",
    "IsRootEvent": "INTEGER",
    "EventCode": "VARCHAR",
    "EventBaseCode": "VARCHAR",
    "EventRootCode": "VARCHAR",
    "QuadClass": "INTEGER",
    "GoldsteinScale": "DOUBLE",
    "NumMentions": "INTEGER",
    "NumSources": "INTEGER",
    "NumArticles": "INTEGER",
    "AvgTone": "DOUBLE",
    "Actor1Geo_Type": "INTEGER",
    "Actor1Geo_Fullname": "VARCHAR",
    "Actor1Geo_CountryCode": "VARCHAR",
    "Actor1Geo_ADM1Code": "VARCHAR",
    "Actor1Geo_ADM2Code": "VARCHAR",
    "Actor1Geo_Lat": "DOUBLE",
    "Actor1Geo_Long": "DOUBLE",
    "Actor1Geo_FeatureID": "VARCHAR",
    "Actor2Geo_Type": "INTEGER",
    "Actor2Geo_Fullname": "VARCHAR",
    "Actor2Geo_CountryCode": "VARCHAR",
    "Actor2Geo_ADM1Code": "VARCHAR",
    "Actor2Geo_ADM2Code": "VARCHAR",
    "Actor2Geo_Lat": "DOUBLE",
    "Actor2Geo_Long": "DOUBLE",
    "Actor2Geo_FeatureID": "VARCHAR",
    "ActionGeo_Type": "INTEGER",
    "ActionGeo_Fullname": "VARCHAR",
    "ActionGeo_CountryCode": "VARCHAR",
    "ActionGeo_ADM1Code": "VARCHAR",
    "ActionGeo_ADM2Code": "VARCHAR",
    "ActionGeo_Lat": "DOUBLE",
    "ActionGeo_Long": "DOUBLE",
    "ActionGeo_FeatureID": "VARCHAR",
    "DATEADDED": "BIGINT",
    "SOURCEURL": "VARCHAR",
}

MENTIONS_COLUMNS = {
    "GlobalEventID": "BIGINT",
    "EventTimeDate": "BIGINT",
    "MentionTimeDate": "BIGINT",
    "MentionType": "INTEGER",
    "MentionSourceName": "VARCHAR",
    "MentionIdentifier": "VARCHAR",
    "SentenceID": "INTEGER",
    "Actor1CharOffset": "INTEGER",
    "Actor2CharOffset": "INTEGER",
    "ActionCharOffset": "INTEGER",
    "InRawText": "INTEGER",
    "Confidence": "INTEGER",
    "MentionDocLen": "INTEGER",
    "MentionDocTone": "DOUBLE",
    "MentionDocTranslationInfo": "VARCHAR",
    "Extras": "VARCHAR",
}

# All 27 columns as they appear in the CSV file (positional order matters)
_GKG_CSV_COLUMNS = {
    "GKGRECORDID": "VARCHAR",
    "V1DATE": "BIGINT",
    "V2SOURCECOLLECTIONIDENTIFIER": "INTEGER",
    "V2SOURCECOMMONNAME": "VARCHAR",
    "V2DOCUMENTIDENTIFIER": "VARCHAR",
    "V1COUNTS": "VARCHAR",
    "V2ENHANCEDCOUNTS": "VARCHAR",
    "V1THEMES": "VARCHAR",
    "V2ENHANCEDTHEMES": "VARCHAR",
    "V1LOCATIONS": "VARCHAR",
    "V2ENHANCEDLOCATIONS": "VARCHAR",
    "V1PERSONS": "VARCHAR",
    "V2ENHANCEDPERSONS": "VARCHAR",
    "V1ORGANIZATIONS": "VARCHAR",
    "V2ENHANCEDORGANIZATIONS": "VARCHAR",
    "V15TONE": "VARCHAR",
    "V2ENHANCEDDATES": "VARCHAR",
    "V2GCAM": "VARCHAR",  # read from CSV but excluded from table
    "V2SHARINGIMAGE": "VARCHAR",
    "V2RELATEDIMAGES": "VARCHAR",
    "V2SOCIALIMAGEEMBEDS": "VARCHAR",
    "V2SOCIALVIDEOEMBEDS": "VARCHAR",
    "V2QUOTATIONS": "VARCHAR",
    "V2ALLNAMES": "VARCHAR",
    "V2AMOUNTS": "VARCHAR",
    "V2TRANSLATIONINFO": "VARCHAR",
    "V2EXTRASXML": "VARCHAR",
}

# V2GCAM omitted from stored table — 2,230 content analysis dimensions,
# ~9 KB/row, rarely queried. Dropping it reduces GKG storage by ~90%.
# Raw zips still contain it if needed.
GKG_COLUMNS = {k: v for k, v in _GKG_CSV_COLUMNS.items() if k != "V2GCAM"}

# Columns to exclude when loading GKG from CSV into the table
_GKG_EXCLUDED = {"V2GCAM"}

TABLE_MAP = {
    "export": ("events", EVENTS_COLUMNS),
    "mentions": ("mentions", MENTIONS_COLUMNS),
    "gkg": ("gkg", GKG_COLUMNS),
}

# CSV column specs (used for read_csv — must match file layout exactly)
_CSV_COLUMNS = {
    "export": EVENTS_COLUMNS,
    "mentions": MENTIONS_COLUMNS,
    "gkg": _GKG_CSV_COLUMNS,
}


def create_tables(con: duckdb.DuckDBPyConnection) -> None:
    """Create all GDELT tables and the ingest log if they don't exist."""
    for table_name, columns in [
        ("events", EVENTS_COLUMNS),
        ("mentions", MENTIONS_COLUMNS),
        ("gkg", GKG_COLUMNS),
    ]:
        col_defs = ", ".join(f'"{k}" {v}' for k, v in columns.items())
        con.execute(f"CREATE TABLE IF NOT EXISTS {table_name} ({col_defs})")

    con.execute("""
        CREATE TABLE IF NOT EXISTS _ingest_log (
            filename VARCHAR PRIMARY KEY,
            file_type VARCHAR NOT NULL,
            batch_timestamp BIGINT NOT NULL,
            loaded_at TIMESTAMP DEFAULT current_timestamp,
            row_count INTEGER
        )
    """)

    create_gal_tables(con)


def create_gal_tables(con: duckdb.DuckDBPyConnection) -> None:
    """Create the GAL article table and its ingest log.

    GAL is the GDELT Global Article List — broader article coverage than GKG
    but without entity extraction. One row per article, keyed by URL.
    """
    con.execute("""
        CREATE TABLE IF NOT EXISTS gal (
            url VARCHAR PRIMARY KEY,
            crawled_at BIGINT,       -- YYYYMMDDHHMMSS from the GAL filename (when GDELT saw it)
            published_at BIGINT,     -- YYYYMMDDHHMMSS from the article's metadata (may be wrong)
            domain VARCHAR,
            outlet_name VARCHAR,
            outlet_logo VARCHAR,
            outlet_twitter VARCHAR,
            title VARCHAR,
            image VARCHAR,
            description VARCHAR,
            language VARCHAR,
            author VARCHAR,
            loaded_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_gal_crawled ON gal(crawled_at)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_gal_domain ON gal(domain)")

    con.execute("""
        CREATE TABLE IF NOT EXISTS _gal_ingest_log (
            filename VARCHAR PRIMARY KEY,
            batch_timestamp BIGINT,
            loaded_at TIMESTAMP DEFAULT current_timestamp,
            row_count INTEGER
        )
    """)


def read_csv_sql(csv_path: str, file_type: str) -> str:
    """Build the read_csv SQL fragment for a given file type and extracted CSV path."""
    csv_columns = _CSV_COLUMNS[file_type]
    col_spec = ", ".join(f"'{k}': '{v}'" for k, v in csv_columns.items())
    return (
        f"read_csv('{csv_path}', "
        f"sep='\\t', header=false, "
        f"columns={{{col_spec}}}, "
        f"null_padding=true, "
        f"quote='', "
        f"ignore_errors=true)"
    )


def select_columns(file_type: str) -> str:
    """Return the SELECT column list for a file type (excludes dropped columns)."""
    if file_type == "gkg":
        return ", ".join(f'"{k}"' for k in _GKG_CSV_COLUMNS if k not in _GKG_EXCLUDED)
    # Events and mentions: all columns
    csv_columns = _CSV_COLUMNS[file_type]
    return ", ".join(f'"{k}"' for k in csv_columns)
