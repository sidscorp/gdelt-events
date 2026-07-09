"""Importance scoring for events / feed cards.

A composite "how much does this event matter right now" score, inspired by the
M4ESTRO paper *Indicators of External Disruptions in Supply Chains* (Bardas,
Kefalakis & Soldatos, 2025). That work defines a Hazard/Disruption Relevance
Score from three ingredients: coverage **density/prominence** (how broadly an
event is reported), **temporal concentration** (a burst of coverage in a short
window), and sentiment/severity — fused into a single relevance value.

Here we implement the first two axes plus a recency decay, using signals we
already materialize:

  * coverage  = log1p(n_sources)          — clusters.size (distinct outlets on a story)
  * velocity  = n_sources / span_hours    — burst: coverage per hour of the event's life
  * recency   = exp(-age_hours / TAU)      — keeps the ranking live, not just historical

Each axis is min-max normalized *within the candidate set for a window/view*
(so the score is relative to what's competing for attention right now), then
combined with fixed weights. Tone/severity is intentionally deferred (tone is a
GKG field and the reading feed is GAL-only) — IMP_W_SEVERITY leaves a slot.

Pure functions — no DB, no Flask — so both the feed (articles.py) and the AI
briefing (briefing.py) can rank the *same* way and stay coherent.
"""

import math
from datetime import datetime

# Composite weights. Coverage dominates (it's the paper's core relevance signal);
# velocity rewards fast-breaking bursts; recency keeps the feed feeling live.
IMP_W_COVERAGE = 0.5
IMP_W_VELOCITY = 0.2
IMP_W_RECENCY = 0.3
IMP_W_SEVERITY = 0.0  # reserved for a future tone/sentiment axis (needs GKG join)

IMP_TAU_HOURS = 12.0  # recency e-fold time: a 12h-old event keeps ~37% of its recency weight
IMP_MIN_SPAN_H = 0.5  # floor on an event's span so a fresh singleton doesn't get infinite velocity


def _ts_to_dt(ts):
    """Decode a GDELT YYYYMMDDHHMMSS int/str into a naive UTC datetime, or None.

    Tolerates shorter zero-padded forms and junk gracefully (returns None)."""
    if not ts:
        return None
    try:
        s = str(int(ts)).zfill(14)
        return datetime(int(s[0:4]), int(s[4:6]), int(s[6:8]),
                        int(s[8:10]), int(s[10:12]), int(s[12:14]))
    except (ValueError, TypeError):
        return None


def _hours_between(a, b):
    """Absolute hours between two datetimes; 0.0 if either is missing."""
    if not a or not b:
        return 0.0
    return abs((b - a).total_seconds()) / 3600.0


def _min_max(vals):
    """Min-max normalize a list to [0,1]. All-equal (or empty) -> all zeros,
    so a constant axis contributes nothing to the ranking rather than dominating."""
    if not vals:
        return vals
    lo, hi = min(vals), max(vals)
    if hi <= lo:
        return [0.0] * len(vals)
    span = hi - lo
    return [(v - lo) / span for v in vals]


def compute_importance(events, now=None):
    """Annotate each event dict with an ``_imp`` score in [0,1] and return the
    list sorted by it (descending, most important first).

    Each event may carry:
        n_sources    int   distinct sources covering the story (default 1)
        first_seen   int   YYYYMMDDHHMMSS of earliest coverage (clusters)
        latest_seen  int   YYYYMMDDHHMMSS of newest coverage (clusters)
        crawled_at   int   fallback recency timestamp for un-clustered singletons

    Missing temporal fields degrade gracefully: a singleton with no span gets
    velocity from the IMP_MIN_SPAN_H floor, and recency falls back to crawled_at.
    Normalization is relative to the passed-in candidate pool.
    """
    if not events:
        return events
    now = now or datetime.utcnow()

    coverage_raw, velocity_raw, recency_raw = [], [], []
    for e in events:
        n = e.get("n_sources") or 1
        try:
            n = max(1, int(n))
        except (ValueError, TypeError):
            n = 1
        first = _ts_to_dt(e.get("first_seen"))
        latest = _ts_to_dt(e.get("latest_seen")) or _ts_to_dt(e.get("crawled_at"))
        span_h = _hours_between(first, latest)

        coverage_raw.append(math.log1p(n))
        velocity_raw.append(n / max(span_h, IMP_MIN_SPAN_H))
        age_h = _hours_between(latest, now) if latest else 0.0
        recency_raw.append(math.exp(-age_h / IMP_TAU_HOURS))

    cov = _min_max(coverage_raw)
    vel = _min_max(velocity_raw)
    rec = _min_max(recency_raw)

    for e, c, v, r in zip(events, cov, vel, rec):
        e["_imp"] = round(
            IMP_W_COVERAGE * c + IMP_W_VELOCITY * v + IMP_W_RECENCY * r, 6
        )

    events.sort(key=lambda e: e.get("_imp", 0.0), reverse=True)
    return events
