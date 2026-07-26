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

BASE = os.environ.get("BASE", "https://gdeltmonitor.com")
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
    devices = next((v for v in data["views"] if v["id"] == "medical-devices"), None)
    assert devices is not None, "medical-devices view missing"
    assert devices.get("default_hours") == 24
    assert devices.get("kind") == "tag_match"


# -----------------------------------------------------------------------
# Data-level acceptance tests — validate actual article content, not just
# HTTP status codes.
# -----------------------------------------------------------------------

def _fetch_articles(params, n=20):
    params["per_page"] = n
    qs = urllib.parse.urlencode(params)
    return _get_json(f"{BASE}/api/articles?{qs}", timeout=15)


class TestArticleRelevance:
    """Verify that returned articles contain the expected keywords."""

    def test_supply_chain_titles_contain_keywords(self):
        data = _fetch_articles({"source": "gal", "view": "supply-chain-alerts", "hours": 168})
        assert data["total"] > 0
        keywords = {
            "recall", "shortage", "disruption", "supply chain", "tariff",
            "sanction", "bankruptcy", "flood", "earthquake", "explosion",
            "hurricane", "layoff", "cyberattack", "embargo", "blockade",
        }
        hits = 0
        for a in data["articles"]:
            text = ((a.get("title") or "") + " " + (a.get("description") or "")).lower()
            if any(kw in text for kw in keywords):
                hits += 1
        precision = hits / len(data["articles"]) if data["articles"] else 0
        assert precision >= 0.7, (
            f"supply chain precision {precision:.0%} < 70% "
            f"({hits}/{len(data['articles'])} articles contain expected keywords)"
        )

    def test_medical_devices_titles_contain_device_terms(self):
        data = _fetch_articles({"source": "gal", "view": "medical-devices", "hours": 168})
        if data["total"] == 0:
            pytest.skip("no medical device articles in window")
        terms = {
            "mri", "ct scan", "ultrasound", "pacemaker", "catheter",
            "implant", "ventilator", "dialysis", "prosthetic", "endoscope",
            "surgical robot", "x-ray", "defibrillator", "stent",
        }
        hits = 0
        for a in data["articles"]:
            text = ((a.get("title") or "") + " " + (a.get("description") or "")).lower()
            if any(t in text for t in terms):
                hits += 1
        precision = hits / len(data["articles"]) if data["articles"] else 0
        assert precision >= 0.6, (
            f"medical devices precision {precision:.0%} < 60%"
        )

class TestSortOrder:
    """Verify sort parameter actually changes article ordering."""

    def test_newest_first_is_default(self):
        data = _fetch_articles({"source": "gal", "hours": 6})
        if len(data["articles"]) < 2:
            pytest.skip("not enough articles")
        times = [a.get("time_ago", "") for a in data["articles"][:5]]
        assert times == sorted(times), f"not sorted newest-first: {times}"

    def test_oldest_reverses_order(self):
        newest = _fetch_articles({"source": "gal", "view": "supply-chain-alerts", "hours": 72})
        oldest = _fetch_articles({"source": "gal", "view": "supply-chain-alerts", "hours": 72, "sort": "oldest"})
        if len(newest["articles"]) < 2 or len(oldest["articles"]) < 2:
            pytest.skip("not enough articles")
        assert newest["articles"][0]["time_ago"] != oldest["articles"][0]["time_ago"], (
            "newest and oldest should show different first articles"
        )


class TestFilterStacking:
    """Verify filters apply correctly on top of view pills."""

    def test_language_filter_on_pill_view(self):
        data = _fetch_articles({
            "source": "gal", "view": "medical-devices",
            "hours": 168, "language": "en",
        })
        for a in data["articles"]:
            assert a.get("language") == "en", (
                f"article {a.get('id', '?')[:30]} has language={a.get('language')}"
            )

    def test_domain_filter_returns_matching_domains(self):
        data = _fetch_articles({
            "source": "gal", "hours": 24, "domain": "yahoo",
        })
        if data["total"] == 0:
            pytest.skip("no yahoo articles in 24h")
        for a in data["articles"]:
            assert "yahoo" in (a.get("source") or "").lower(), (
                f"article source {a.get('source')} doesn't contain 'yahoo'"
            )


class TestAuthEndpoints:
    """Verify auth routes respond correctly."""

    def test_login_page_accessible(self):
        data = _get_json(f"{BASE}/api/views", timeout=10)
        assert data.get("authenticated") is False

    def test_about_page_returns_200(self):
        req = urllib.request.Request(f"{BASE}/about", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=10) as r:
            assert r.status == 200
            body = r.read().decode()
            assert "Siddhartha Nambiar" in body
            assert "GDELT" in body

    def test_pill_info_returns_keywords(self):
        data = _get_json(f"{BASE}/api/pill_info/supply-chain-alerts", timeout=10)
        assert len(data.get("keywords", [])) >= 30
        assert "tariff" in data["keywords"]
