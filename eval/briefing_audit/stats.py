"""Uncertainty, so a difference is only reported when it survives the noise.

The previous harness reported a 0.16 gap on a saturated 5-point scale from n=1
per cell, with no variance at all. Everything here exists so that cannot recur:
means come with intervals, and comparisons come with a verdict on whether the
interval excludes zero.

Stdlib only, matching the rest of the estate's pipeline code.
"""
from __future__ import annotations

import math
import random
from statistics import mean, stdev

# Two-tailed t at 95% by degrees of freedom. Small-n intervals are much wider
# than the normal approximation and n=5 is a realistic budget here, so using
# 1.96 throughout would overstate confidence exactly where it matters.
_T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
        8: 2.306, 9: 2.262, 10: 2.228, 12: 2.179, 15: 2.131, 20: 2.086,
        25: 2.060, 30: 2.042, 40: 2.021, 60: 2.000, 120: 1.980}


def _t95(df: int) -> float:
    if df <= 0:
        return float("inf")
    for k in sorted(_T95):
        if df <= k:
            return _T95[k]
    return 1.96


def summarise(values: list[float]) -> dict:
    """mean, sd and a 95% CI on the mean."""
    vals = [v for v in values if v is not None]
    n = len(vals)
    if n == 0:
        return {"n": 0, "mean": None, "sd": None, "ci_low": None, "ci_high": None}
    m = mean(vals)
    if n == 1:
        return {"n": 1, "mean": round(m, 3), "sd": None, "ci_low": None, "ci_high": None}
    sd = stdev(vals)
    half = _t95(n - 1) * sd / math.sqrt(n)
    return {"n": n, "mean": round(m, 3), "sd": round(sd, 3),
            "ci_low": round(m - half, 3), "ci_high": round(m + half, 3)}


def diff_ci(a: list[float], b: list[float], iters: int = 5000, seed: int = 7) -> dict:
    """Bootstrap CI for mean(a) - mean(b), and whether it excludes zero.

    Bootstrap rather than a t-test because these are bounded 1-5 rubric scores,
    heavily tied, and nowhere near normal.
    """
    a = [v for v in a if v is not None]
    b = [v for v in b if v is not None]
    if len(a) < 2 or len(b) < 2:
        return {"diff": None, "ci_low": None, "ci_high": None, "significant": False}
    rng = random.Random(seed)
    obs = mean(a) - mean(b)
    diffs = []
    for _ in range(iters):
        ra = [a[rng.randrange(len(a))] for _ in a]
        rb = [b[rng.randrange(len(b))] for _ in b]
        diffs.append(mean(ra) - mean(rb))
    diffs.sort()
    lo = diffs[int(0.025 * iters)]
    hi = diffs[int(0.975 * iters) - 1]
    return {"diff": round(obs, 3), "ci_low": round(lo, 3), "ci_high": round(hi, 3),
            "significant": bool(lo > 0 or hi < 0)}


def wilson(successes: int, n: int, z: float = 1.96) -> dict:
    """Wilson score interval for a win rate.

    Used for the pairwise head-to-head. Wilson rather than the normal
    approximation because win rates land near 0 or 1 often enough that the naive
    interval runs outside [0,1] and stops meaning anything.
    """
    if n == 0:
        return {"rate": None, "ci_low": None, "ci_high": None, "significant": False}
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    lo, hi = max(0.0, centre - half), min(1.0, centre + half)
    return {"rate": round(p, 3), "ci_low": round(lo, 3), "ci_high": round(hi, 3),
            "significant": bool(lo > 0.5 or hi < 0.5)}
