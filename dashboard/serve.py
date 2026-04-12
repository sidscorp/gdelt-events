"""Windows-compatible WSGI server for the GDELT dashboard.

Uses waitress (pure-Python, works on Windows) instead of gunicorn (Unix-only).
Starts the pill backfill worker thread at boot.
"""

import logging

from waitress import serve

from app import app
from pill_worker import start_worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

if __name__ == "__main__":
    start_worker()
    serve(app, host="0.0.0.0", port=8015, threads=4)
