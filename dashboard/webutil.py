"""Small shared web/request helpers for the GDELT dashboard."""

import time

from flask import g


def _phase(name, fn):
    """Time a callable and record it into g._req_phases."""
    t0 = time.perf_counter()
    try:
        return fn()
    finally:
        g._req_phases[name] = time.perf_counter() - t0


# --- structural non-article filter -------------------------------------------
# Shared with the clusterer via pipeline/textfilters.py (stdlib-only, safe to
# import into Flask — unlike pipeline/build_clusters.py, which runs
# logging.basicConfig at import and would reconfigure the root logger here).
# Same repo-root sys.path dance as routes/api_feed.py.
#
# Degrades to a length check if the import fails, so a packaging problem
# softens the filter rather than emptying every feed.
def _load_is_junk_title():
    try:
        import importlib
        import sys
        from pathlib import Path
        repo_root = str(Path(__file__).resolve().parent.parent)
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        return importlib.import_module("pipeline.textfilters").is_junk_title
    except Exception:
        return lambda title: not title or len(title.strip()) < 6


is_junk_title = _load_is_junk_title()


def usable_title(title) -> bool:
    """True if a title is worth showing as a feed card / briefing candidate.

    Folds together the >10-char rule the feed already applied and the
    structural non-article filter. This is NOT a relevance judgement — a real
    but low-salience story must pass; deciding what deserves attention is the
    briefing editor's job.
    """
    t = (title or "").strip()
    return len(t) > 10 and not is_junk_title(t)
