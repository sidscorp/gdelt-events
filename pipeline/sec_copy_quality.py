"""Deterministic copy contract for the SEC experience; no model is involved."""
from __future__ import annotations

import re
import html
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CopyIssue:
    code: str
    detail: str


_FORBIDDEN = re.compile(r"\b(?:buy|sell|hold|outperform|underperform|price target|fair value)\b", re.I)
_FORECAST = re.compile(r"\b(?:will (?:rise|fall|grow|decline)|expected to|likely to)\b", re.I)
_MOJIBAKE = re.compile(r"[\ufffd]|(?:Ã.|Â.|â..)")
_VAGUE = re.compile(r"\b(?:significant|meaningful|strong|weak|healthy|concerning)\b", re.I)
_SENTENCES = re.compile(r"(?<=[.!?])\s+")


def check_text(text: str, *, label: str = "copy") -> list[CopyIssue]:
    """Check portable copy rules without trying to judge writing subjectively."""
    issues: list[CopyIssue] = []
    if _MOJIBAKE.search(text):
        issues.append(CopyIssue("encoding", f"{label} contains malformed encoding"))
    # Templates are source code: evaluate their human-facing words, not tag or
    # Jinja syntax which would make one minified line look like a paragraph.
    visible = html.unescape(re.sub(r"\{%.*?%\}|\{\{.*?\}\}", " ", text, flags=re.S))
    visible = re.sub(r"<[^>]+>", " ", visible)
    visible = re.sub(r"\s+", " ", visible).strip()
    for sentence in _SENTENCES.split(visible):
        if len(sentence.split()) > 42:
            issues.append(CopyIssue("long-sentence", f"{label} has a sentence over 42 words"))
            break
    blocks = re.split(r"</(?:p|li|dd|figcaption|h[1-6])>", text, flags=re.I)
    for paragraph in blocks:
        paragraph = re.sub(r"\{%.*?%\}|\{\{.*?\}\}|<[^>]+>", " ", paragraph, flags=re.S)
        if len(paragraph.split()) > 140:
            issues.append(CopyIssue("dense-paragraph", f"{label} has a paragraph over 140 words"))
            break
    if _FORBIDDEN.search(visible):
        issues.append(CopyIssue("recommendation", f"{label} uses recommendation language"))
    if _FORECAST.search(visible):
        issues.append(CopyIssue("forecast", f"{label} uses forecast language"))
    if _VAGUE.search(visible):
        issues.append(CopyIssue("vague-term", f"{label} uses an unsupported vague term"))
    return issues


def check_observations(items) -> list[CopyIssue]:
    """Every emitted claim must identify a filing-derived comparison basis."""
    issues: list[CopyIssue] = []
    for item in items:
        issues.extend(check_text(item.text, label=f"observation:{item.kind}"))
        if not getattr(item, "basis", ""):
            issues.append(CopyIssue("claim-basis", f"observation:{item.kind} has no comparison basis"))
    return issues


def check_mirrors(root: Path) -> list[CopyIssue]:
    pipe, dash = root / "pipeline/sec_explain.py", root / "dashboard/sec_explain.py"
    return [] if pipe.read_bytes() == dash.read_bytes() else [CopyIssue("mirrored-copy", "SEC explanation modules differ")]
