"""Load GDELT zip files into DuckDB."""

import logging
import tempfile
import time
import zipfile
from pathlib import Path

import duckdb

from .config import DB_PATH
from .downloader import FILENAME_RE
from .schema import TABLE_MAP, create_tables, read_csv_sql, select_columns

log = logging.getLogger(__name__)

# Commit every N files to bound memory and let WAL flush.
# Smaller = lower memory, more disk churn. Larger = faster but more RAM.
BATCH_COMMIT_SIZE = 50

# Cap DuckDB memory. rainbow-boi has 48 GB total; 8 GB leaves plenty for
# Frigate + Ollama + other workloads.
DUCKDB_MEMORY_LIMIT = "8GB"


def classify_file(path: Path) -> tuple[str, str] | None:
    """Extract (file_type, timestamp) from a zip filename. Returns None if unrecognized."""
    m = FILENAME_RE.match(path.name)
    if not m:
        return None
    return m.group(2).lower(), m.group(1)


def is_loaded(con: duckdb.DuckDBPyConnection, filename: str) -> bool:
    """Check if a file has already been ingested."""
    result = con.execute(
        "SELECT 1 FROM _ingest_log WHERE filename = ?", [filename]
    ).fetchone()
    return result is not None


def _configure(con: duckdb.DuckDBPyConnection) -> None:
    """Apply memory limits and other pragmas."""
    con.execute(f"SET memory_limit = '{DUCKDB_MEMORY_LIMIT}'")
    con.execute("SET threads = 2")  # Keep CPU usage modest on the shared box
    # Give CTAS / GROUP BY / QUALIFY a writable scratch dir (Windows default
    # is blank and fails on operations that need to spill).
    from .config import DATA_DIR
    tmp = DATA_DIR / "duckdb_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    con.execute(f"PRAGMA temp_directory='{tmp.as_posix()}'")


def _open_connection(db_path: Path, max_retries: int = 20) -> duckdb.DuckDBPyConnection:
    """Open DuckDB for write, retrying persistently on lock conflicts from readers.

    During bulk load, the dashboard's read-only connections can briefly collide
    with the writer when we reopen between batches. Retry for up to ~60 seconds
    before giving up.
    """
    last_err = None
    for attempt in range(max_retries):
        try:
            con = duckdb.connect(str(db_path))
            _configure(con)
            create_tables(con)
            return con
        except duckdb.IOException as e:
            last_err = e
            if attempt < max_retries - 1:
                wait = min(5, 1 + attempt * 0.5)
                log.warning("DB lock conflict, retrying in %.1fs (attempt %d/%d)", wait, attempt + 1, max_retries)
                time.sleep(wait)
    raise last_err


def load_file(con: duckdb.DuckDBPyConnection, zip_path: Path, tmp_dir: str) -> int:
    """Load a single zip file into the appropriate DuckDB table. Returns row count."""
    info = classify_file(zip_path)
    if not info:
        log.warning("Skipping unrecognized file: %s", zip_path.name)
        return 0

    file_type, timestamp = info
    table_name = TABLE_MAP[file_type][0]

    if is_loaded(con, zip_path.name):
        return 0

    # Extract CSV from zip archive (DuckDB doesn't support zip, only gzip)
    with zipfile.ZipFile(zip_path) as zf:
        inner_name = zf.namelist()[0]
        zf.extract(inner_name, tmp_dir)
        csv_path = Path(tmp_dir) / inner_name

    try:
        csv_sql = read_csv_sql(str(csv_path), file_type)
        cols = select_columns(file_type)

        # Single INSERT with subquery counts + inserts in one pass.
        result = con.execute(
            f"INSERT INTO {table_name} SELECT {cols} FROM {csv_sql} RETURNING 1"
        ).fetchall()
        row_count = len(result)

        con.execute(
            "INSERT INTO _ingest_log (filename, file_type, batch_timestamp, row_count) "
            "VALUES (?, ?, ?, ?)",
            [zip_path.name, file_type, int(timestamp), row_count],
        )

        return row_count
    finally:
        csv_path.unlink(missing_ok=True)


def load_batch(zip_files: list[Path], db_path: Path | None = None) -> dict:
    """Load multiple zip files into DuckDB with periodic commits to bound memory.

    Opens a fresh connection every BATCH_COMMIT_SIZE files, which forces
    DuckDB to flush its WAL and release buffers. This keeps RAM under control
    when loading thousands of files in one backfill run.
    """
    db_path = db_path or DB_PATH
    summary = {"events": 0, "mentions": 0, "gkg": 0, "skipped": 0, "errors": 0}
    total = len(zip_files)

    # Sort so we process chronologically and in batches of adjacent files
    sorted_files = sorted(zip_files)

    with tempfile.TemporaryDirectory(prefix="gdelt_") as tmp_dir:
        con = _open_connection(db_path)
        processed = 0

        try:
            for i, zip_path in enumerate(sorted_files, start=1):
                try:
                    info = classify_file(zip_path)
                    if not info:
                        summary["skipped"] += 1
                        continue
                    file_type = info[0]
                    table_name = TABLE_MAP[file_type][0]
                    rows = load_file(con, zip_path, tmp_dir)
                    summary[table_name] += rows
                    processed += 1
                except Exception:
                    log.exception("Error loading %s", zip_path.name)
                    summary["errors"] += 1

                # Periodic checkpoint: close and reopen to flush memory
                if i % BATCH_COMMIT_SIZE == 0:
                    con.execute("CHECKPOINT")
                    con.close()
                    log.info(
                        "Progress: %d/%d loaded (events=%d, mentions=%d, gkg=%d, errors=%d)",
                        i, total, summary["events"], summary["mentions"],
                        summary["gkg"], summary["errors"],
                    )
                    con = _open_connection(db_path)
        finally:
            try:
                con.execute("CHECKPOINT")
                con.close()
            except Exception:
                pass

    return summary
