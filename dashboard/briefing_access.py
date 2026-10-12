"""Briefing request validation and non-browser worker authorization."""
import hmac
import os
import re

from _paths import DATA_DIR

TOKEN_PATH = DATA_DIR / ".briefing_worker_token"
ALLOWED_HOURS = frozenset((3, 6, 24, 72, 168, 720))


def parse_key(args):
    for name in ("view", "hours", "stream", "refresh", "prewarm"):
        if len(args.getlist(name)) > 1:
            raise ValueError("Duplicate briefing parameter")
    view = args.get("view", "").strip()
    if view and not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", view):
        raise ValueError("Invalid briefing view")
    try:
        hours = int(args.get("hours", "24"))
    except (ValueError, TypeError):
        raise ValueError("Invalid briefing window") from None
    if hours not in ALLOWED_HOURS:
        raise ValueError("Unsupported briefing window")
    for name in ("stream", "refresh", "prewarm"):
        if args.get(name, "0") not in ("0", "1"):
            raise ValueError("Invalid briefing flag")
    return view, hours


def worker_authorized(headers):
    expected = os.environ.get("GDELT_BRIEFING_WORKER_TOKEN", "").strip()
    if not expected:
        try:
            expected = TOKEN_PATH.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            return False
    supplied = headers.get("X-GDELT-Briefing-Token", "")
    return bool(expected) and hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))
