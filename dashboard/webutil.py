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
