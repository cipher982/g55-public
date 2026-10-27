"""Means over units with percentile bootstrap intervals, and paired differences on shared units.

The unit of evidence (a public segment, a truck window) is what the bootstrap resamples. Every call
seeds its own generator, so the same inputs always give the same interval.
"""
from __future__ import annotations

import math

import numpy as np

BOOT = 4000
SEED = 0


def finite(x) -> bool:
    return x is not None and isinstance(x, (int, float, np.floating, np.integer)) and math.isfinite(float(x))


def boot_ci(values, reps: int = BOOT):
    v = np.asarray([float(x) for x in values if finite(x)], float)
    if len(v) < 2:
        return None
    rng = np.random.default_rng(SEED)
    means = v[rng.integers(0, len(v), size=(reps, len(v)))].mean(axis=1)
    return [float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))]


def pool(values):
    v = [float(x) for x in values if finite(x)]
    return {"value": float(np.mean(v)) if v else None, "ci": boot_ci(v), "n": len(v)}


def better_than(a: float, b: float, better: str) -> bool:
    if better == "lower":
        return a < b
    if better == "higher":
        return a > b
    if better == "near1":
        return abs(a - 1.0) < abs(b - 1.0)
    raise ValueError(better)


def paired(model: dict, base: dict, better: str):
    """model, base: {unit: value}. Delta is model minus baseline on units both have."""
    shared = [u for u in model if u in base and finite(model[u]) and finite(base[u])]
    if not shared:
        return None
    d = [float(model[u]) - float(base[u]) for u in shared]
    wins = sum(better_than(float(model[u]), float(base[u]), better) for u in shared)
    return {"delta": float(np.mean(d)), "ci": boot_ci(d), "n": len(shared), "wins": int(wins)}


def rounded(x, digits: int = 4):
    if isinstance(x, dict):
        return {k: rounded(v, digits) for k, v in x.items()}
    if isinstance(x, list):
        return [rounded(v, digits) for v in x]
    if isinstance(x, float):
        return None if not math.isfinite(x) else float(f"{x:.{digits}g}")
    return x
