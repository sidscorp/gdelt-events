"""Windows-compatible WSGI server for the GDELT dashboard.

Uses waitress (pure-Python, works on Windows) instead of gunicorn (Unix-only).
Starts the pill backfill worker thread at boot.

Usage:
    python serve.py              # production on port 8015
    python serve.py --port 8016  # preprod on port 8016
"""

import argparse
import logging
import os

from waitress import serve

from app import app
from pill_worker import start_worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

if __name__ == "__main__":
    default_port = int(os.environ.get("GDELT_DASH_PORT") or 8015)
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=default_port)
    args = parser.parse_args()

    # GDELT_DISABLE_WORKERS=1 runs a read-only-ish instance without the pill
    # backfill writer thread (not needed for some dev/preview scenarios).
    if not os.environ.get("GDELT_DISABLE_WORKERS"):
        start_worker()
    serve(app, host="0.0.0.0", port=args.port, threads=4)
