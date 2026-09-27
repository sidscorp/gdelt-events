import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.sec_context import filing_rows_from_submissions, filing_url, safe_business_extract
from pipeline.sec_ingest import filing_index_entries


def test_filing_url_removes_accession_dashes():
    assert filing_url(320193, "0000320193-24-000123", "a.htm").endswith("/320193/000032019324000123/a.htm")


def test_submissions_rows_keep_provenance():
    data = {"filings": {"recent": {"form": ["10-K", "8-K"], "accessionNumber": ["0001-24-000001", "x"], "filingDate": ["2024-02-01", "2024-02-02"], "reportDate": ["2023-12-31", ""], "primaryDocument": ["annual.htm", ""]}}}
    rows = filing_rows_from_submissions(12, data)
    assert len(rows) == 1 and rows[0]["report_period"] == "2023-12-31"
    assert rows[0]["filing_url"].endswith("/12/000124000001/annual.htm")


def test_business_extract_is_bounded_and_stops_at_risk_factors():
    doc = "<h1>Item 1. Business</h1><p>" + "We sell useful things. " * 40 + "</p><h1>Item 1A. Risk Factors</h1><p>Never include this.</p>"
    text = safe_business_extract(doc, limit=180)
    assert text and len(text) <= 181 and "Never include" not in text and text.endswith("…")


def test_business_extract_falls_back_cleanly():
    assert safe_business_extract("<p>Nothing headed here</p>") is None


def test_daily_index_parser_uses_archive_cik_not_company_name_digits():
    text = "10-Q  3M COMPANY  66740  2024-04-30  edgar/data/66740/000006674024000012/x.htm\n8-K ACME 1 edgar/data/1/x"
    assert filing_index_entries(text) == [{"cik": 66740, "form": "10-Q", "archive_path": "edgar/data/66740/000006674024000012/x.htm"}]
