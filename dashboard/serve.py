"""Windows-compatible WSGI server for the GDELT dashboard.

Uses waitress (pure-Python, works on Windows) instead of gunicorn (Unix-only).
"""

from waitress import serve

from app import app

if __name__ == "__main__":
    serve(app, host="0.0.0.0", port=8015, threads=4)
