"""Dashboard API regression tests driven by `golden_queries.json`.

Run with:
    pytest tests/test_queries.py -v
    BASE=http://localhost:8015 pytest tests/test_queries.py -v

Each query in golden_queries.json becomes one parametrized test case. Cases
assert shape (articles key present, source echoed), latency (<= max_latency_s),
and result-count bounds (min_results, max_results).
"""
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

BASE = os.environ.get("BASE", "https://gdelt.snambiar.com")
CASES_FILE = Path(__file__).parent / "golden_queries.json"
CASES = json.loads(CASES_FILE.read_text())["queries"]


def _ids(case):
    return case["id"]


UA = "gdelt-dashboard-tests/1.0"


def _get_json(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _fetch(params, timeout):
    qs = urllib.parse.urlencode(params)
    url = f"{BASE}/api/articles?{qs}"
    t0 = time.perf_counter()
    data = _get_json(url, timeout)
    return data, time.perf_counter() - t0


@pytest.mark.parametrize("case", CASES, ids=_ids)
def test_golden_query(case):
    params = case["params"]
    max_latency = case.get("max_latency_s", 5.0)
    data, elapsed = _fetch(params, timeout=max_latency + 3)

    assert "articles" in data, f"missing 'articles' key: {data}"
    assert "total" in data, f"missing 'total' key: {data}"
    assert isinstance(data["articles"], list), f"articles not a list: {type(data['articles'])}"

    assert elapsed <= max_latency, (
        f"latency {elapsed:.2f}s > max {max_latency}s"
    )

    if "min_results" in case:
        assert data["total"] >= case["min_results"], (
            f"only {data['total']} results (expected >= {case['min_results']})"
        )
    if "max_results" in case:
        assert data["total"] <= case["max_results"], (
            f"got {data['total']} results (expected <= {case['max_results']})"
        )

    expected_shape = case.get("response_shape")
    if expected_shape:
        for k, v in expected_shape.items():
            assert data.get(k) == v, f"expected {k}={v!r}, got {data.get(k)!r}"


def test_stats_endpoint():
    """Smoke: /api/stats returns per-source counts."""
    data = _get_json(f"{BASE}/api/stats", timeout=10)
    assert data.get("total_articles", 0) > 0
    assert data.get("gal_articles", 0) > 0
    assert data.get("gkg_articles", 0) > 0
    assert data["total_articles"] == data["gal_articles"] + data["gkg_articles"]


def test_gal_facets_endpoint():
    """Smoke: /api/gal_facets returns language + outlet lists."""
    data = _get_json(f"{BASE}/api/gal_facets", timeout=15)
    assert "languages" in data
    assert len(data["languages"]) >= 10, f"only {len(data['languages'])} languages"
    # Languages must have the shape we expect in the frontend
    first = data["languages"][0]
    assert "code" in first and "name" in first and "count" in first
    assert "top_outlets" in data
    assert len(data["top_outlets"]) >= 20


def test_views_endpoint():
    """Smoke: /api/views returns the registered view list with default_hours."""
    data = _get_json(f"{BASE}/api/views", timeout=10)
    assert "views" in data and len(data["views"]) >= 1
    fda = next((v for v in data["views"] if v["id"] == "fda-medical-devices"), None)
    assert fda is not None, "fda-medical-devices view missing"
    assert fda.get("default_hours") == 24
    assert fda.get("kind") == "fda_match"
