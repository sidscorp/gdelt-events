"""Structural non-article detection — shared by the clusterer and the dashboard.

Deliberately dependency-free (stdlib `re` only) so the Flask process can import
it. The clusterer's own module cannot be imported from the dashboard: it runs
`logging.basicConfig` at import time, which would attach a root stderr handler
and force INFO globally in a process that configures only a named logger with
propagate=False — plus it pulls in numpy and duckdb.

WHAT THIS IS FOR, and what it is deliberately NOT for.

This catches pages that are not articles at all: bot walls, paywall
interstitials, error pages, section indexes, job postings, evergreen listicles.
They contain no event — no actor, nothing that happened — so there is nothing
for a briefing to say about them beyond restating that they exist. They also
cluster beautifully (near-identical boilerplate across sites), so the event
clusterer amplifies exactly the wrong thing: "Making sure you're not a bot!"
was a 14-member cluster and reached a global briefing as prior coverage.

It is NOT a relevance or importance filter. Low-salience *real* news — a High
Court ruling in Allahabad, a fatal crash near Middlesbrough — must pass
through. Deciding what deserves a reader's attention is the editor stage's job
(briefing._select_events), and keeping the odd-but-real story in the pool is
the point. When in doubt, let it through: a false positive here silently
deletes a real story, while a false negative merely gives the editor one more
thing to pass over.
"""

import re

_WS = re.compile(r"\s+")

# Bot walls, paywalls, consent gates and error interstitials. Substring match
# against the whitespace-normalized lowercase title.
_JUNK_TITLES = (
    "client challenge", "just a moment", "are you a robot", "access denied",
    "attention required", "403 forbidden", "page not found", "404 not found",
    "bot verification", "verifying you are human", "one moment please",
    "robot or human", "please verify you are a human", "security check",
    "access to this page has been denied", "site maintenance", "are you human",
    # Observed in production candidate pools since the original list was written.
    # "making sure you're not a bot" is the one that mattered: it formed a
    # 14-member cluster and was handed to a briefing as previously-covered news.
    "making sure you're not a bot", "checking your browser",
    "please wait while we verify", "verify you are human",
    "enable javascript", "javascript is disabled", "please enable cookies",
    "subscribe to continue", "subscribe to read", "this content is not available",
    "log in or sign up", "sign in to continue", "your session has expired",
    "too many requests", "service unavailable", "site temporarily unavailable",
)

# Section indexes and nav pages: "Food and drink - Hull Live", "Sport | BBC".
# Anchored to the whole title so a headline merely *containing* one of these
# words is untouched. The separator forms are how outlets brand index pages.
#
# 'opinion' and 'comment' are deliberately NOT here: outlets prefix real
# columns with them ("COMMENT | Ageing society: M'sia must act now"), and an
# opinion index page is far rarer than an opinion piece.
_SECTION_INDEX = re.compile(
    r"^(news|sport|sports|business|politics|lifestyle|life|"
    r"food(?: and drink)?|drink|travel|health|education|entertainment|showbiz|"
    r"culture|arts|technology|tech|science|weather|obituaries|jobs|classifieds|"
    r"property|motors|homepage|home page|latest news|top stories|local news)"
    r"(\s*[\|\-–—:]\s*.{1,40})?$",
    re.I,
)

# Job postings. Only unambiguous hiring cues. 'apply now' and 'position
# available' were tried and removed: they fire on real coverage of hiring
# programmes ("Ladakh Youth Internship 2026: 2,000 paid government
# internships ... apply now"), which is a news story about a policy.
_JOB_POSTING = re.compile(
    r"\b(now hiring|we are hiring|we're hiring|job vacanc(y|ies)|job opening|"
    r"recruitment incentive|full[- ]time position|part[- ]time position|"
    r"employment opportunit(y|ies)|join our team)\b",
    re.I,
)

# A bot-wall phrase only means a bot wall when it is essentially the WHOLE
# title. "Access Denied: What the banking crackdown on sex workers says about
# us" is a real article that happens to open with one of these strings.
_INTERSTITIAL_MAX_CHARS = 60

# Evergreen listicles and explainers with no event: "10 Best...", "Here's Why
# That Matters", "Everything You Need to Know About...".
_EVERGREEN = re.compile(
    r"^\s*(top\s+)?\d{1,2}\s+(best|worst|things|ways|tips|reasons|facts|"
    r"products|gifts|deals|stocks|places)\b"
    # "...about X" is the pure-explainer form. "Everything you need to know AS
    # the Blood Moon eclipse hits UK skies" is pegged to an event, so it stays.
    r"|^\s*(everything|all) you need to know about\b"
    r"|^\s*here's (what|why|how) "
    r"|\b(a beginner's guide|the ultimate guide|buying guide)\b"
    # Trailing self-referential explainer tag, e.g. "...2 Strict Tests. Here's
    # Why That Matters." Anchored to the end and requiring the demonstrative,
    # so a genuine "Here's why the Fed cut rates" headline is untouched.
    r"|[.!?]\s*here's (why|what|how) (that|this|it) matters\.?\s*$",
    re.I,
)

# Titles arrive with typographic punctuation far more often than ASCII, and
# every apostrophe in the patterns above is ASCII. Normalizing first is what
# makes "Making sure you're not a bot!" (curly) match its list entry.
_PUNCT_FOLD = {0x2018: "'", 0x2019: "'", 0x201C: '"', 0x201D: '"',
               0x2013: "-", 0x2014: "-", 0x2011: "-", 0x00A0: " "}


def is_junk_title(title: str | None) -> bool:
    """True if the title indicates a page that is not a news article.

    Conservative by design — see the module docstring. Callers should treat a
    True as "drop before ranking"; everything else is a candidate.
    """
    if not title:
        return True
    normalized = _WS.sub(" ", title.translate(_PUNCT_FOLD)).strip()
    t = normalized.lower()
    if len(t) < 6:
        return True
    if len(t) <= _INTERSTITIAL_MAX_CHARS and any(j in t for j in _JUNK_TITLES):
        return True
    # Structural patterns run against the normalized original-case string: they
    # are already case-insensitive and some depend on punctuation.
    return bool(
        _SECTION_INDEX.match(normalized)
        or _JOB_POSTING.search(normalized)
        or _EVERGREEN.search(normalized)
    )
