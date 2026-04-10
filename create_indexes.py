"""Create indexes on GKG table for fast dashboard queries.

DuckDB's columnar format usually doesn't need indexes — it uses zone maps
on sorted data. But for ad-hoc range queries on V1DATE plus string LIKE
filters on persons/orgs/themes, explicit indexes help significantly.
"""

import time
import duckdb
from pipeline.config import DB_PATH

con = duckdb.connect(str(DB_PATH))
con.execute("SET memory_limit = '8GB'")
con.execute("SET threads = 4")

indexes = [
    ("gkg", "V1DATE", "idx_gkg_date"),
    ("gkg", "V2SOURCECOMMONNAME", "idx_gkg_source"),
    ("events", "DATEADDED", "idx_events_dateadded"),
    ("events", "ActionGeo_CountryCode", "idx_events_country"),
    ("mentions", "MentionTimeDate", "idx_mentions_time"),
]

for table, col, name in indexes:
    print(f"Creating {name} on {table}({col})...")
    start = time.time()
    try:
        con.execute(f'CREATE INDEX IF NOT EXISTS {name} ON {table}("{col}")')
        print(f"  done in {time.time() - start:.1f}s")
    except Exception as e:
        print(f"  error: {e}")

# ORDER BY V1DATE DESC via sort would also help but is expensive; skip for now
print("\nDB size after indexing:")
import os
size = os.path.getsize(DB_PATH) / (1024 ** 3)
print(f"  {size:.2f} GB")
con.close()
