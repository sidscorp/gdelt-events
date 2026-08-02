"""GDELT News Dashboard — Tufte-inspired breaking news viewer."""

import json
import logging
import logging.handlers
import time

from flask import Flask, request, g

from models import init_user_db
from auth import init_auth

from _paths import DB_PATH, LOG_DIR
LOG_DIR.mkdir(parents=True, exist_ok=True)
(DB_PATH.parent / "duckdb_tmp").mkdir(parents=True, exist_ok=True)


app = Flask(__name__)
app.jinja_env.filters["from_json"] = json.loads
init_user_db()
init_auth(app)

# Dedicated request logger — captures per-request timing so backend slowness
# is visible without guessing. Rotating file handler caps disk usage.
req_log = logging.getLogger("dashboard.requests")
if not req_log.handlers:
    req_log.setLevel(logging.INFO)
    _h = logging.handlers.RotatingFileHandler(
        LOG_DIR / "dashboard.log", maxBytes=5 * 1024 * 1024, backupCount=3,
        encoding="utf-8",
    )
    _h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    req_log.addHandler(_h)
    req_log.propagate = False


@app.before_request
def _req_start():
    g._req_t0 = time.perf_counter()
    g._req_phases = {}


@app.after_request
def _req_end(resp):
    # Always revalidate HTML so deployed UI changes show on a normal reload
    # (no hard-refresh needed). The page is tiny + gzipped, so this is cheap.
    if resp.headers.get("Content-Type", "").startswith("text/html"):
        resp.headers["Cache-Control"] = "no-cache"
    t0 = getattr(g, "_req_t0", None)
    if t0 is None:
        return resp
    dt = time.perf_counter() - t0
    phases = getattr(g, "_req_phases", {}) or {}
    phase_s = " ".join(f"{k}={int(v*1000)}ms" for k, v in phases.items())
    qs = request.query_string.decode("ascii", "replace")[:120]
    try:
        req_log.info(
            "%s %s?%s -> %s in %.2fs%s",
            request.method, request.path, qs, resp.status_code, dt,
            f"  ({phase_s})" if phase_s else "",
        )
    except Exception:
        pass
    return resp


# ---------------------------------------------------------------------------
# Blueprint registration
# ---------------------------------------------------------------------------
from routes.pages import bp as pages_bp
from routes.auth import bp as auth_bp
from routes.api_feed import bp as api_feed_bp
from routes.api_briefing import bp as api_briefing_bp
from routes.api_pills import bp as api_pills_bp
from routes.sec_analysis import bp as sec_bp

app.register_blueprint(pages_bp)
app.register_blueprint(auth_bp)
app.register_blueprint(api_feed_bp)
app.register_blueprint(api_briefing_bp)
app.register_blueprint(api_pills_bp)
app.register_blueprint(sec_bp)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8015, debug=True)
