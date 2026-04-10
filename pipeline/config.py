"""GDELT v2 pipeline configuration."""

from pathlib import Path

# ── URLs ─────────────────────────────────────────────────────────────────────
MASTER_FILE_URL = "http://data.gdeltproject.org/gdeltv2/masterfilelist.txt"
LAST_UPDATE_URL = "http://data.gdeltproject.org/gdeltv2/lastupdate.txt"

# ── Paths ────────────────────────────────────────────────────────────────────
REPO_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
DB_PATH = DATA_DIR / "gdelt.duckdb"
LOG_DIR = DATA_DIR / "logs"
LOCK_FILE = DATA_DIR / ".ingest.lock"

# ── Retention ────────────────────────────────────────────────────────────────
RETENTION_DAYS = 60  # 2 months rolling window

# ── Download ─────────────────────────────────────────────────────────────────
DOWNLOAD_TIMEOUT = 30  # seconds per file
MAX_RETRIES = 3
BACKOFF_BASE = 2  # seconds, doubled each retry
BACKFILL_WORKERS = 4  # parallel download threads

# ── Disk safety ──────────────────────────────────────────────────────────────
MIN_FREE_GB = 10  # skip downloads if disk free drops below this
