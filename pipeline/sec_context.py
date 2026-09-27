"""SEC filing provenance and short, attributed Business-section extracts."""
from __future__ import annotations

import html
import re
from typing import Iterable

ARCHIVES = "https://www.sec.gov/Archives/edgar/data"


def filing_url(cik: int, accession: str, primary_document: str | None = None) -> str:
    """Direct EDGAR document URL; the accession is stored with dashes."""
    root = f"{ARCHIVES}/{int(cik)}/{accession.replace('-', '')}"
    return f"{root}/{primary_document}" if primary_document else f"{root}/"


def filing_rows_from_submissions(cik: int, payload: dict) -> list[dict]:
    """Normalize SEC submissions ``recent`` arrays into stored filing rows."""
    recent = payload.get("filings", {}).get("recent", {})
    out = []
    for i, form in enumerate(recent.get("form", [])):
        if form not in ("10-K", "10-Q", "10-K/A", "10-Q/A"):
            continue
        accession = (recent.get("accessionNumber", [""])[i] or "")
        if not accession:
            continue
        primary = (recent.get("primaryDocument", [""])[i] or "")
        out.append({
            "cik": cik, "accession": accession, "form": form,
            "filing_date": (recent.get("filingDate", [None])[i]),
            "report_period": (recent.get("reportDate", [None])[i]),
            "primary_document": primary or None,
            "filing_url": filing_url(cik, accession, primary or None),
        })
    return out


def safe_business_extract(document: str, limit: int = 1_200) -> str | None:
    """Return a bounded Item 1 extract, never pretending it is a summary.

    EDGAR documents vary wildly. This intentionally permissive parser only strips
    markup after locating the Item 1 heading, and stops at Item 1A/2.
    """
    plain = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", document)
    plain = re.sub(r"(?s)<[^>]+>", " ", plain)
    plain = html.unescape(plain).replace("\xa0", " ")
    plain = re.sub(r"\s+", " ", plain).strip()
    start = re.search(r"\bITEM\s+1\s*[.:-]?\s*(?:BUSINESS)?\b", plain, re.I)
    if not start:
        return None
    end = re.search(r"\bITEM\s+(?:1A|2)\s*[.:-]", plain[start.end():], re.I)
    text = plain[start.end(): start.end() + end.start() if end else None].strip(" .:-")
    if len(text) < 80:
        return None
    if len(text) <= limit:
        return text
    cut = text.rfind(". ", 0, limit)
    return text[:cut + 1 if cut > limit // 2 else limit].rstrip() + "…"
