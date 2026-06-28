"""Schema for the materialized event-clustering tables.

Lives in the GDELT DuckDB alongside article_tags. Written only by
build_clusters.py (a scheduled, lock-guarded writer process); read by the
dashboard via read-only connections.

  clusters         one row per multi-article event (size >= 2)
  cluster_members  url -> cluster_id (only members of materialized clusters)
  cluster_state    single-row watermark (last_clustered_at)

Singletons are NOT stored: an article with no cluster_members row is an
implicit singleton. This keeps the tables small and the read-path lookup cheap.
"""


def create_cluster_tables(con):
    """Create cluster tables + indexes if absent. Safe to call repeatedly."""
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS clusters (
            cluster_id    VARCHAR PRIMARY KEY,
            rep_url       VARCHAR,
            title         VARCHAR,
            image         VARCHAR,
            centroid      BLOB,        -- mean of member unit vectors (768 float32)
            title_fp      VARCHAR,     -- normalized-title fingerprint of the seed
            size          INTEGER,
            first_seen    BIGINT,      -- min crawled_at (YYYYMMDDHHMMSS)
            latest_seen   BIGINT,      -- max crawled_at
            members_json  VARCHAR,     -- denormalized variant snapshot (persists post-prune)
            status        VARCHAR DEFAULT 'active',
            created_at    TIMESTAMP DEFAULT current_timestamp,
            updated_at    TIMESTAMP DEFAULT current_timestamp
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS cluster_members (
            article_url VARCHAR PRIMARY KEY,
            cluster_id  VARCHAR,
            similarity  REAL,
            added_at    BIGINT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS cluster_state (
            id                INTEGER PRIMARY KEY,
            last_clustered_at BIGINT,
            updated_at        TIMESTAMP DEFAULT current_timestamp
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_cluster_members_cid ON cluster_members(cluster_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_clusters_latest ON clusters(latest_seen)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_clusters_fp ON clusters(title_fp)")
