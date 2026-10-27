"""Means over units with cluster-bootstrap intervals, and paired differences on shared units.

Units (a public segment, a truck window) are grouped into clusters, the independent draws: the
drive (route) a public segment was cut from, the recorded drive a truck window comes from. The
bootstrap resamples whole clusters and recomputes the mean over all units in the draw, so three
adjacent minutes of one drive count as one draw, not three.

Below MIN_CLUSTERS clusters there is no interval. Simulated coverage of the nominal 95% percentile
interval of a mean: 79% at 4 draws, 88% at 8, 90% at 10 for normal data, and 69% / 79% / 80% for
skewed (lognormal) data; at 19 to 23 draws it is about 93% (normal) and 85 to 87% (skewed).

Every call orders units and clusters by id and seeds its own generator, so the same values give the
same interval whatever order they arrive in, and a reader can reproduce it from the per-unit CSVs.
"""
from __future__ import annotations

import math
from collections import defaultdict

import numpy as np

BOOT = 4000
SEED = 0
MIN_CLUSTERS = 10


def finite(x) -> bool:
    return x is not None and isinstance(x, (int, float, np.floating, np.integer)) and math.isfinite(float(x))


def _clusters(vals: dict, cluster_of) -> tuple[list, np.ndarray, np.ndarray]:
    """Sorted cluster ids, per-cluster sums and counts of the finite values in {unit: value}."""
    sums, counts = defaultdict(float), defaultdict(int)
    for u in sorted(vals):
        v = vals[u]
        if finite(v):
            c = cluster_of(u) if cluster_of else u
            sums[c] += float(v)
            counts[c] += 1
    ids = sorted(sums)
    return ids, np.array([sums[c] for c in ids]), np.array([counts[c] for c in ids], float)


def boot_ci(vals: dict, cluster_of=None, reps: int = BOOT):
    """95% percentile interval of the unit mean, resampling clusters; None below MIN_CLUSTERS."""
    ids, sums, counts = _clusters(vals, cluster_of)
    if len(ids) < MIN_CLUSTERS:
        return None
    rng = np.random.default_rng(SEED)
    idx = rng.integers(0, len(ids), size=(reps, len(ids)))
    means = sums[idx].sum(axis=1) / counts[idx].sum(axis=1)
    return [float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))]


def pool(vals: dict, cluster_of=None):
    """vals: {unit: value}. Mean, median and interval over the units with a finite value."""
    v = [float(vals[u]) for u in sorted(vals) if finite(vals[u])]
    ids, _, _ = _clusters(vals, cluster_of)
    return {"value": float(np.mean(v)) if v else None, "median": float(np.median(v)) if v else None,
            "ci": boot_ci(vals, cluster_of), "n": len(v), "clusters": len(ids)}


def _draws(vals: dict, cluster_of, reps: int):
    """Sorted units grouped by cluster, and the bootstrap draws of cluster indices; None below
    MIN_CLUSTERS."""
    groups = defaultdict(list)
    for u in sorted(vals):
        if finite(vals[u]):
            groups[cluster_of(u) if cluster_of else u].append(u)
    ids = sorted(groups)
    if len(ids) < MIN_CLUSTERS:
        return [groups[c] for c in ids], None
    rng = np.random.default_rng(SEED)
    return [groups[c] for c in ids], rng.integers(0, len(ids), size=(reps, len(ids)))


def median_pool(vals: dict, cluster_of=None, reps: int = BOOT):
    """Median over the units with a finite value, with a 95% percentile interval of the median that
    resamples clusters; no interval below MIN_CLUSTERS."""
    groups, idx = _draws(vals, cluster_of, reps)
    v = [float(vals[u]) for g in groups for u in g]
    ci = None
    if idx is not None:
        arr = [np.array([float(vals[u]) for u in g]) for g in groups]
        meds = [np.median(np.concatenate([arr[i] for i in row])) for row in idx]
        ci = [float(np.percentile(meds, 2.5)), float(np.percentile(meds, 97.5))]
    return {"median": float(np.median(v)) if v else None, "ci": ci, "n": len(v), "clusters": len(groups)}


def median_diff(model: dict, base: dict, cluster_of=None, reps: int = BOOT):
    """Median of model minus median of base, over the units both have, with a 95% interval from
    resampling clusters of shared units (both medians recomputed on each draw)."""
    shared = {u: 0.0 for u in model if u in base and finite(model[u]) and finite(base[u])}
    if not shared:
        return None
    groups, idx = _draws(shared, cluster_of, reps)
    a = [np.array([float(model[u]) for u in g]) for g in groups]
    b = [np.array([float(base[u]) for u in g]) for g in groups]
    d = float(np.median(np.concatenate(a)) - np.median(np.concatenate(b)))
    ci = None
    if idx is not None:
        ds = [np.median(np.concatenate([a[i] for i in row])) - np.median(np.concatenate([b[i] for i in row]))
              for row in idx]
        ci = [float(np.percentile(ds, 2.5)), float(np.percentile(ds, 97.5))]
    return {"delta": d, "ci": ci, "n": len(shared), "clusters": len(groups)}


def better_than(a: float, b: float, better: str) -> bool:
    if better == "lower":
        return a < b
    if better == "higher":
        return a > b
    if better == "near1":
        return abs(a - 1.0) < abs(b - 1.0)
    raise ValueError(better)


def paired(model: dict, base: dict, better: str, cluster_of=None):
    """model, base: {unit: value}. Per shared unit, model minus baseline; for a "near1" metric the
    difference of their distances from 1, so a negative delta is better there too. Also the units
    and clusters where the model was better, and the units where the two were equal."""
    shared = sorted(u for u in model if u in base and finite(model[u]) and finite(base[u]))
    if not shared:
        return None
    if better == "near1":
        d = {u: abs(float(model[u]) - 1.0) - abs(float(base[u]) - 1.0) for u in shared}
    else:
        d = {u: float(model[u]) - float(base[u]) for u in shared}
    wins = sum(better_than(float(model[u]), float(base[u]), better) for u in shared)
    ties = sum(float(model[u]) == float(base[u]) for u in shared)
    ids, sums, counts = _clusters(d, cluster_of)
    cmean = sums / counts
    gain = cmean < 0 if better in ("lower", "near1") else cmean > 0
    out = {"delta": float(np.mean(list(d.values()))), "median": float(np.median(list(d.values()))),
           "ci": boot_ci(d, cluster_of), "n": len(shared), "wins": int(wins), "ties": int(ties),
           "clusters": len(ids), "cluster_wins": int(gain.sum())}
    return out


def rounded(x, digits: int = 4):
    if isinstance(x, dict):
        return {k: rounded(v, digits) for k, v in x.items()}
    if isinstance(x, list):
        return [rounded(v, digits) for v in x]
    if isinstance(x, float):
        return None if not math.isfinite(x) else float(f"{x:.{digits}g}")
    return x
