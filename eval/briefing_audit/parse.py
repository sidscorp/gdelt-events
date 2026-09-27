"""Turn a stored briefing into auditable units.

A unit is one sentence with the citations that back it. Briefing sentences
already carry ``[n]`` markers, so no LLM claim-extraction step is needed: the
writer's own citations say which sources each sentence rests on.

Sources come from what the writer ACTUALLY saw: ``meta_json.prompt`` lists the
editor-chosen stories as ``N. [tags] Title — description``. ``sources_json``
only has titles, so descriptions are recovered from the prompt.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# Same normalisation the dashboard applies (dashboard/briefing.py _CITE_*):
# models write [3], 【3】 or ［3］; fold them all to [3].
_CITE_BRACKETS = re.compile(r"[【［\[]{1,2}\s*(\d+(?:\s*[,，、]\s*\d+)*)\s*[】］\]]{1,2}")
_CITE = re.compile(r"\[(\d+)\]")
_SOURCE_LINE = re.compile(r"^(\d+)\. \[([^\]]*)\] (.*)$")
_BULLET = re.compile(r"^\s*[-*•]\s+")
_LABEL = re.compile(r"^\*\*(.+?)\*\*\s*[–—:-]?\s*")
_SECTION = {
    "executive summary": "summary", "summary": "summary", "overview": "summary",
    "key highlights": "highlight", "highlights": "highlight",
    "what to watch": "watch", "quieter but notable": "quieter",
}
# Sentence boundary: ., ! or ? then space then a capital/quote/digit. Common
# abbreviations are protected first so "U.S. Department" stays one sentence.
_ABBREV = re.compile(r"\b(U\.S|U\.K|U\.N|E\.U|Mr|Mrs|Ms|Dr|St|No|vs|Inc|Ltd|Co|Corp|Rs|Jr|Sr|Gen|Sen|Rep|Gov|Lt|Col|approx|e\.g|i\.e)\.", re.I)
_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[\"“‘'(A-Z0-9])")


@dataclass
class Source:
    n: int
    title: str
    description: str
    outlet: str
    n_sources: int
    link: str
    chosen: bool


@dataclass
class Unit:
    idx: int
    section: str          # summary | highlight | watch | quieter | other
    text: str             # sentence without citation markers
    cites: list[int] = field(default_factory=list)
    label: str | None = None  # bold highlight label, if any


def normalize_cites(text: str) -> str:
    return _CITE_BRACKETS.sub(lambda m: "".join(f"[{n.strip()}]" for n in re.split(r"[,，、]", m.group(1))), text or "")


def parse_sources(sources_json: str | None, prompt: str | None) -> dict[int, Source]:
    """n -> Source, merging sources_json (outlet, counts, chosen) with the
    descriptions the writer saw in the prompt."""
    meta = {}
    for s in json.loads(sources_json or "[]"):
        try:
            meta[int(s["n"])] = s
        except (KeyError, TypeError, ValueError):
            continue
    out: dict[int, Source] = {}
    for line in (prompt or "").split("Numbered sources:", 1)[-1].splitlines():
        m = _SOURCE_LINE.match(line.strip())
        if not m:
            continue
        n, rest = int(m.group(1)), m.group(3)
        s = meta.get(n, {})
        title = (s.get("title") or "").strip()
        if title and rest.startswith(title):
            desc = rest[len(title):].lstrip(" —-").strip()
        else:
            title, _, desc = rest.partition(" — ")
        out[n] = Source(n, title.strip(), desc.strip(), s.get("outlet") or "", int(s.get("n_sources") or 1),
                        s.get("link") or "", bool(s.get("chosen", True)))
    for n, s in meta.items():  # chosen sources absent from the prompt text still count
        out.setdefault(n, Source(n, s.get("title") or "", "", s.get("outlet") or "", int(s.get("n_sources") or 1),
                                 s.get("link") or "", bool(s.get("chosen"))))
    return out


def _sentences(text: str) -> list[str]:
    protected = _ABBREV.sub(lambda m: m.group(0).replace(".", "§"), text)
    return [s.replace("§", ".").strip() for s in _SPLIT.split(protected) if s.strip()]


def _strip_cites(s: str) -> tuple[str, list[int]]:
    cites = [int(n) for n in _CITE.findall(s)]
    return re.sub(r"\s*\[\d+\]", "", s).strip(), cites


def parse_briefing(text: str) -> list[Unit]:
    """Split a briefing into sentence units, section-tagged, with citations.
    In a highlight bullet the writer puts citations once at the end, so a
    sentence with none of its own inherits the bullet's citations."""
    units: list[Unit] = []
    # Text before any labelled section is the lede — some briefings omit the
    # "**Executive Summary**" label but the opening paragraph is still the summary.
    section = "summary"
    for raw in normalize_cites(text).splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):  # markdown heading: a section switch or the title, never a claim
            section = _SECTION.get(line.lstrip("#").lower().strip("*: "), section)
            continue
        low = line.lower().strip("*: ")
        head = _LABEL.match(line)
        if head and head.group(1).lower().strip(": ") in _SECTION:
            section = _SECTION[head.group(1).lower().strip(": ")]
            line = line[head.end():].strip()
            if not line:
                continue
        elif low in _SECTION:
            section = _SECTION[low]
            continue
        label = None
        is_bullet = bool(_BULLET.match(line))
        if is_bullet:
            line = _BULLET.sub("", line)
            lm = _LABEL.match(line)
            if lm:
                label, line = lm.group(1).strip(), line[lm.end():]
            if section in ("other", "summary"):
                section = "highlight"
        sents = _sentences(line)
        bullet_cites = [int(n) for n in _CITE.findall(line)]
        for s in sents:
            body, cites = _strip_cites(s)
            if len(body) < 12:  # stray fragments like a lone citation
                continue
            if not cites and is_bullet:
                cites = bullet_cites
            units.append(Unit(len(units), section, body, cites, label))
    return units
