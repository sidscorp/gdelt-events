"""Resolved data-directory paths for the dashboard.

All data paths derive from the GDELT_DATA_DIR environment variable when set
(used by the isolated dev instance to point at a separate 7-day-slice data
dir); otherwise they default to <repo>/data, matching production behavior
exactly. With the env var unset, behavior is identical to the old hardcoded
paths.
"""

import os
from pathlib import Path

_REPO_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("GDELT_DATA_DIR") or (_REPO_DIR / "data"))

DB_PATH = DATA_DIR / "gdelt.duckdb"
USERS_DB_PATH = DATA_DIR / "users.db"
LOG_DIR = DATA_DIR / "logs"
EMBEDDINGS_DIR = DATA_DIR / "embeddings"
OPENROUTER_KEY_PATH = DATA_DIR / ".openrouter_key"
