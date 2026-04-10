"""Load GDELT zip files into DuckDB."""

import logging
import tempfile
import zipfile
from pathlib import Path

import duckdb

from .config import DB_PATH
from .downloader import FILENAME_RE
from .schema import TABLE_MAP, create_tables, read_csv_sql, select_columns

log = logging.getLogger(__name__)


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


def load_file(con: duckdb.DuckDBPyConnection, zip_path: Path, tmp_dir: str) -> int:
    """Load a single zip file into the appropriate DuckDB table. Returns row count."""
    info = classify_file(zip_path)
    if not info:
        log.warning("Skipping unrecognized file: %s", zip_path.name)
        return 0

    file_type, timestamp = info
    table_name = TABLE_MAP[file_type][0]

    if is_loaded(con, zip_path.name):
        log.debug("Already loaded: %s", zip_path.name)
        return 0

    # Extract CSV from zip archive (DuckDB doesn't support zip, only gzip)
    with zipfile.ZipFile(zip_path) as zf:
        inner_name = zf.namelist()[0]
        zf.extract(inner_name, tmp_dir)
        csv_path = Path(tmp_dir) / inner_name

    try:
        csv_sql = read_csv_sql(str(csv_path), file_type)
        cols = select_columns(file_type)
        count_result = con.execute(f"SELECT count(*) FROM {csv_sql}").fetchone()
        row_count = count_result[0] if count_result else 0

        con.execute(f"INSERT INTO {table_name} SELECT {cols} FROM {csv_sql}")

        con.execute(
            "INSERT INTO _ingest_log (filename, file_type, batch_timestamp, row_count) "
            "VALUES (?, ?, ?, ?)",
            [zip_path.name, file_type, int(timestamp), row_count],
        )

        return row_count
    finally:
        csv_path.unlink(missing_ok=True)


def load_batch(zip_files: list[Path], db_path: Path | None = None) -> dict:
    """Load multiple zip files into DuckDB. Returns {table_name: rows_inserted}."""
    db_path = db_path or DB_PATH
    con = duckdb.connect(str(db_path))
    create_tables(con)

    summary = {"events": 0, "mentions": 0, "gkg": 0, "skipped": 0, "errors": 0}

    with tempfile.TemporaryDirectory(prefix="gdelt_") as tmp_dir:
        for zip_path in sorted(zip_files):
            try:
                info = classify_file(zip_path)
                if not info:
                    summary["skipped"] += 1
                    continue
                file_type = info[0]
                table_name = TABLE_MAP[file_type][0]
                rows = load_file(con, zip_path, tmp_dir)
                summary[table_name] += rows
            except Exception:
                log.exception("Error loading %s", zip_path.name)
                summary["errors"] += 1

    con.close()
    return summary
