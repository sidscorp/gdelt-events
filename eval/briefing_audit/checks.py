"""Deterministic checks — free, exact, and deliberately covering what Jev is
documented to be weak at (numbers, counting, dates, citation bookkeeping)."""
from __future__ import annotations

import re

from .parse import Source, Unit

# Syndication/aggregator hosts: a story known ONLY through one of these has no
# original reporting behind it in the briefing's inputs.
AGGREGATORS = {"article.wn.com", "wn.com", "msn.com", "news.google.com", "newsbreak.com",
               "flipboard.com", "ground.news", "yahoo.com", "news.yahoo.com", "headtopics.com",
               "newsnow.co.uk", "inkl.com", "dailyhunt.in", "newsbreakapp.com"}

# A source hedging and a sentence NOT hedging is the H-1B pattern
# ("US Signals Total Shutdown" -> "has moved to shut down").
_HEDGE = re.compile(
    r"\b(signal(?:s|ed|ing)?|may|might|could|reportedly|considering|consider(?:s)?|plans?|planning|"
    r"propos(?:es|ed)|weighs?|mulls?|expected to|set to|likely|possible|possibly|potential|threatens?|"
    r"seeks?|aims? to|poised to|preparing|would)\b", re.I)  # modality only: facts like "probe" don't hedge
_NUM = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(?:\s*(%|percent|per cent))?")
_WORD = re.compile(r"[a-z][a-z'\-]{2,}")
_STOP = set("the and for with that this from have has had its are was were will would could into over under about after "
            "amid their they them than then also more most such which while where when what who whom whose been being "
            "said says say new one two may might can not but out off per via".split())


def host(url: str) -> str:
    m = re.match(r"https?://(?:www\.)?([^/]+)", url or "")
    return (m.group(1) if m else "").lower()


def _numbers(text: str) -> set[str]:
    out = set()
    for num, _pct in _NUM.findall(text or ""):
        v = num.replace(",", "")
        if re.fullmatch(r"(19|20)\d\d", v):  # years: too often implicit in sources
            continue
        out.add(v.rstrip("0").rstrip(".") if "." in v else v)
    return out


def tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOP}


def source_text(s: Source) -> str:
    return f"{s.title} {s.description}"


def best_sources(unit: Unit, sources: dict[int, Source], k: int = 5) -> list[int]:
    """For an uncited sentence: the chosen sources it most overlaps with."""
    t = tokens(unit.text)
    scored = sorted(((len(t & tokens(source_text(s))) / (len(t) or 1), n)
                     for n, s in sources.items() if s.chosen), reverse=True)
    return [n for score, n in scored[:k] if score > 0]


def check_unit(unit: Unit, sources: dict[int, Source]) -> list[str]:
    flags = []
    cited = [sources[n] for n in unit.cites if n in sources]
    if unit.section in ("summary", "highlight") and not unit.cites:
        flags.append("uncited_summary" if unit.section == "summary" else "uncited_highlight")
    for n in unit.cites:
        if n not in sources:
            flags.append(f"cite_missing:{n}")
        elif not sources[n].chosen:
            flags.append(f"cite_not_chosen:{n}")
    basis = cited or [sources[n] for n in best_sources(unit, sources)]
    if basis:
        src_text = " ".join(source_text(s) for s in basis)
        missing = _numbers(unit.text) - _numbers(src_text)
        if missing:
            flags.append("number_not_in_sources:" + ",".join(sorted(missing)))
        if _HEDGE.search(src_text) and not _HEDGE.search(unit.text):
            flags.append("possible_escalation")
    return flags


def lead_check(units: list[Unit], sources: dict[int, Source]) -> dict:
    """What the briefing leads with, and whether anything solid backs it."""
    lead = next((u for u in units if u.section == "summary"), None)
    if not lead:
        return {"lead_idx": None}
    ns = lead.cites or best_sources(lead, sources, k=1)
    s = sources.get(ns[0]) if ns else None
    if not s:
        return {"lead_idx": lead.idx, "lead_source": None}
    outlet_host = host(s.link) if s.link.startswith("http") else (s.outlet or "").lower()
    return {
        "lead_idx": lead.idx, "lead_source": s.n, "lead_cited": bool(lead.cites),
        "lead_n_outlets": s.n_sources, "lead_outlet": s.outlet,
        "lead_single_source": s.n_sources <= 1,
        "lead_aggregator": any(outlet_host == a or outlet_host.endswith("." + a) or (s.outlet or "").lower() == a for a in AGGREGATORS),
    }
