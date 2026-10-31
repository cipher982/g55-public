"""Rankings for the G-Wagon eval:

1. G55 composite score (`g55_composite`, `composite_from`): combines the four G55 evaluation pillars on a
   0–100 scale (higher is better; 100 = best model on that pillar, 0 = worst):
     - Closed-loop path keeping (40%): nominal distance at 2 s in worldmodel-4B (25% median + 15% mean)
     - Closed-loop recovery (25%): 2 s distance after a 2° heading error (12.5%) and 0.5 m offset (12.5%)
     - Open-loop tracking (20%): mean lateral error on recorded G55 drives (across all scored models)
     - Closed-loop smoothness (15%): RMS requested lateral jerk in nominal closed loop

2. Tiers (`write_tiers`, `order`): the only order the site prints. Whole drives are resampled with
   replacement (TIER_REPS times, the same drives for every model and test that share them), every
   statistic, the min-max scaling and the composite are recomputed, and a model sits in a higher tier
   only when it beat every model in the lower tiers in at least TIER_P of the resamples.
   `rankings/tiers.json` holds aggregates only (values, rank intervals, tiers, pairwise shares).

3. The check (`check_site`): every order the built site prints must be one the resamples support. It
   fails on a tier claim the pairwise shares do not support, and on place numbers, medals or leader
   wording anywhere a reader can see them. `site/build.py` runs it and refuses to write a failing site.

4. Single-section closed-loop tables (`rank`, `scores`, `write_truck`): median and mean distance from
   the recorded path at 2 s with a drive-clustered 95% interval of the median and paired differences.

    uv run python -m geval.ranking                                  # from eval/; the public table
    uv run python -m geval.ranking --section truck_closed_loop      # the published G55 closed-loop table
    uv run python -m geval.ranking --section composite              # the G55 composite score and tiers
    uv run python -m geval.ranking --check site/_site               # the check, on a built site
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from html.parser import HTMLParser
from pathlib import Path

import numpy as np

from . import stats
from .reproduce import read_csv

ROOT = Path(__file__).resolve().parents[1]
SECTION = "public_closed_loop"
TRUCK = "truck_closed_loop"
KEY = "xtrack_2s"

# Pillar weights for the G55 composite score (sum = 1.00).
COMPOSITE_WEIGHTS = {
    "path": 0.40,        # 25% nominal CL 2 s median + 15% nominal CL 2 s mean
    "recovery": 0.25,    # 12.5% 2° heading 2 s median + 12.5% 0.5 m offset 2 s median
    "open_loop": 0.20,   # 20% G55 open-loop lateral error (lat_mae mean across all scored models)
    "smoothness": 0.15,  # 15% G55 closed-loop lateral jerk RMS (lat_jerk mean)
}


def _win_rates(vals: dict) -> dict[str, float]:
    """Per model, fraction of head-to-head comparisons won across shared units (lower is better; tie = 0.5)."""
    models = sorted(vals)
    if len(models) < 2:
        return {m: 0.5 for m in models}
    wins = {m: 0.0 for m in models}
    totals = {m: 0 for m in models}
    for i, a in enumerate(models):
        va = vals[a]
        for b in models[i + 1:]:
            vb = vals[b]
            for u in set(va) & set(vb):
                xa, xb = va[u], vb[u]
                if stats.finite(xa) and stats.finite(xb):
                    totals[a] += 1
                    totals[b] += 1
                    if xa < xb:
                        wins[a] += 1.0
                    elif xb < xa:
                        wins[b] += 1.0
                    else:
                        wins[a] += 0.5
                        wins[b] += 0.5
    return {m: (wins[m] / totals[m] if totals[m] else 0.5) for m in models}


def rank(vals: dict, cluster_of) -> dict:
    """vals: {model: {unit: value}}. Medians, means, intervals, head-to-head win rates, ordinal ranks,
    and every ordered pair's difference."""
    out = {}
    win_rate = _win_rates(vals)
    for m, v in vals.items():
        mp = stats.median_pool(v, cluster_of)
        pp = stats.pool(v, cluster_of)
        out[m] = {
            "median": mp["median"],
            "mean": pp["value"],
            "ci": mp["ci"],
            "n": mp["n"],
            "clusters": mp["clusters"],
            "win_rate": win_rate.get(m, 0.5),
        }
    pairs = {(a, b): stats.median_diff(vals[a], vals[b], cluster_of) for a in vals for b in vals if a != b}
    paired_mean = {(a, b): stats.paired(vals[a], vals[b], "lower", cluster_of) for a in vals for b in vals if a != b}
    ordered = sorted(
        out,
        key=lambda m: (
            round(out[m]["median"] or 999.0, 4),
            round(out[m]["mean"] or 999.0, 4),
            -round(out[m]["win_rate"], 4),
            m,
        ),
    )
    for idx, m in enumerate(ordered, 1):
        out[m]["rank"] = idx
        out[m]["separated_below"] = sum(
            1 for o in vals
            if o != m and paired_mean[(o, m)] and paired_mean[(o, m)].get("ci")
            and paired_mean[(o, m)]["ci"][1] < 0
        )
    return {"models": out, "pairs": pairs, "paired_mean": paired_mean}


def scores(root: Path) -> dict:
    """{model id: {"median", "ci", "n", "clusters", "rank"}} for every model with a public closed-loop
    CSV, and {(a, b): median_diff(a, b)} for every ordered pair of them."""
    vals, drives = {}, {}
    for p in sorted((root / "results").glob("*.json")):
        sec = json.loads(p.read_text())["sections"].get(SECTION) or {}
        if sec.get("status") == "ok" and sec.get("per_unit_file"):
            v, d = read_csv(root / sec["per_unit_file"])
            vals[p.stem] = v[KEY]
            drives.update(d)
    return rank(vals, drives.get)


def write_truck(work: Path, root: Path) -> Path | None:
    """From the private unit store: rankings/truck_closed_loop.json, aggregates only."""
    from . import results, store
    unit_dir = work / "units" / TRUCK
    if not unit_dir.is_dir():
        return None
    key = next(m[4] for m in results.S.SECTIONS[TRUCK]["metrics"] if m[0] == KEY)
    vals = {p.stem: {u: v for u, v in store.values(store.load(work, TRUCK, p.stem), key).items()}
            for p in sorted(unit_dir.glob("*.json"))}
    vals = {m: v for m, v in vals.items() if any(stats.finite(x) for x in v.values())}
    r = rank(vals, results.cluster_fn(TRUCK, None))
    doc = {"section": TRUCK, "key": KEY, "unit": "m",
           "models": {m: {k: stats.rounded(v) if k in ("median", "mean", "ci", "win_rate") else v
                          for k, v in s.items()}
                      for m, s in r["models"].items()}}
    p = root / "rankings" / f"{TRUCK}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, indent=1) + "\n")
    return p


def truck_scores(root: Path) -> dict | None:
    """The published G55 closed-loop ranking, or None before `geval build` has written one."""
    p = root / "rankings" / f"{TRUCK}.json"
    return json.loads(p.read_text()) if p.exists() else None


def _metric_val(m: dict, section: str, key: str, stat: str = "value") -> float | None:
    s = m.get("sections", {}).get(section)
    if not s or s.get("status") != "ok":
        return None
    for x in s.get("metrics", []):
        if x.get("key") == key:
            v = x.get(stat)
            return float(v) if v is not None else (float(x["value"]) if x.get("value") is not None else None)
    return None


def _norm_0_100(val: float | None, vals: list[float]) -> float | None:
    if val is None or not vals:
        return None
    lo, hi = min(vals), max(vals)
    if hi <= lo:
        return 100.0
    return 100.0 * (hi - val) / (hi - lo)


def composite_from(v: dict) -> dict[str, tuple[float, dict]]:
    """v: {statistic: {model: value}} with the statistics cl_med, cl_mean, hdg, off, ol, jerk (lower is
    better for each). For every model with a closed-loop median: (score 0-100, pillars). Each statistic is
    min-max scaled across the models that have it; a model's pillars are weighted by COMPOSITE_WEIGHTS over
    the pillars it has. The site and the resamples both score with this function."""
    def norm(name, mid):
        col = v.get(name) or {}
        return _norm_0_100(col.get(mid), [x for x in col.values() if x is not None])

    out = {}
    for mid, med in (v.get("cl_med") or {}).items():
        if med is None:
            continue
        s_med, s_mean = norm("cl_med", mid), norm("cl_mean", mid)
        s_path = 0.625 * s_med + 0.375 * s_mean if s_mean is not None else s_med
        s_hdg, s_off = norm("hdg", mid), norm("off", mid)
        if s_hdg is not None and s_off is not None:
            s_rec = 0.5 * (s_hdg + s_off)
        else:
            s_rec = s_hdg if s_hdg is not None else s_off
        pillars = {"path": s_path, "recovery": s_rec, "open_loop": norm("ol", mid), "smoothness": norm("jerk", mid)}
        avail = [(p, COMPOSITE_WEIGHTS[k]) for k, p in pillars.items() if p is not None]
        w_sum = sum(w for _, w in avail)
        out[mid] = (sum(p * w for p, w in avail) / w_sum, pillars)
    return out


def g55_composite(models: list[dict], truck_rk: dict | None = None) -> dict[str, dict]:
    """The G55 composite score (0–100) and pillar sub-scores for every model in `models` that has a G55
    closed-loop median, plus the raw values behind them. No ranks: the order the site prints is the tiers
    (`write_tiers`)."""
    trk = (truck_rk or {}).get("models", truck_rk or {})

    def cl_med(m):
        mid = m["model"]["id"]
        if mid in trk and trk[mid].get("median") is not None:
            return float(trk[mid]["median"])
        return _metric_val(m, "truck_closed_loop", "xtrack_2s", "median")

    def cl_mean(m):
        mid = m["model"]["id"]
        if mid in trk and trk[mid].get("mean") is not None:
            return float(trk[mid]["mean"])
        return _metric_val(m, "truck_closed_loop", "xtrack_2s", "value")

    raw = {m["model"]["id"]: {
        "cl_med": cl_med(m), "cl_mean": cl_mean(m),
        "hdg": _metric_val(m, "truck_recovery", "heading_xtrack_2s", "median"),
        "off": _metric_val(m, "truck_recovery", "offcentre_xtrack_2s", "median"),
        "ol": _metric_val(m, "truck_open_loop", "lat_mae", "value"),
        "jerk": _metric_val(m, "truck_closed_loop", "lat_jerk", "value"),
    } for m in models}
    scored = composite_from({k: {mid: r[k] for mid, r in raw.items() if r[k] is not None} for k in STATS})

    out: dict[str, dict] = {}
    for mid, r in raw.items():
        comp, pillars = scored.get(mid, (None, {k: None for k in COMPOSITE_WEIGHTS}))
        out[mid] = {
            "score": round(comp, 1) if comp is not None else None,
            "score_raw": comp,
            "pillars": {k: (round(v, 1) if v is not None else None) for k, v in pillars.items()},
            "cl_median": r["cl_med"],
            "cl_mean": r["cl_mean"],
            "hdg_median": r["hdg"],
            "off_median": r["off"],
            "ol_mae": r["ol"],
            "cl_jerk": r["jerk"],
            "win_rate": trk.get(mid, {}).get("win_rate"),
        }
    return out


# ---------------------------------------------------------------- tiers: the order the resamples support

TIER_REPS = 2000
TIER_SEED = 0
TIER_P = 0.95         # a higher tier beat every model below it in at least this share of resamples
TIERS_FILE = "tiers.json"

# The composite's statistics: (section, store key, statistic). Lower is better for each.
STATS = {
    "cl_med": ("truck_closed_loop", "absx_2s", "median"),
    "cl_mean": ("truck_closed_loop", "absx_2s", "mean"),
    "hdg": ("truck_recovery", "heading:absx_2s", "median"),
    "off": ("truck_recovery", "offcentre:absx_2s", "median"),
    "ol": ("truck_open_loop", "lat_mae", "mean"),
    "jerk": ("truck_closed_loop", "rms_lat_jerk", "mean"),
}
# The tests the site orders models by: test -> (label, statistic or None for the composite, better).
TESTS = {
    "composite": ("G55 score", None, "higher"),
    "cl2s": ("Closed loop at 2 s", "cl_med", "lower"),
    "heading": ("2° heading at 2 s", "hdg", "lower"),
    "offset": ("0.5 m offset at 2 s", "off", "lower"),
    "jerk": ("Closed-loop lateral jerk", "jerk", "lower"),
    "openloop": ("Open-loop lateral error", "ol", "lower"),
}
# Sections whose windows come from the same drives draw the same drives in a resample.
NAMESPACE = {"truck_closed_loop": "closed loop and recovery", "truck_recovery": "closed loop and recovery",
             "truck_open_loop": "open loop"}


def resample(data: dict, spec: dict, cluster_of: dict, reps: int = TIER_REPS, seed: int = TIER_SEED):
    """data: {statistic: {model: {unit: value}}}; spec: {statistic: (namespace, "median" | "mean")};
    cluster_of: {namespace: unit -> drive}. Point values and `reps` drive resamples of every statistic.
    Each namespace's drives are drawn once per resample, so every model and statistic in it sees the same
    drives (paired). Returns ({stat: {model: value}}, {stat: {model: array}}, {namespace: (units, drives)})."""
    units = defaultdict(set)
    for name, (ns, _) in spec.items():
        for vals in data.get(name, {}).values():
            units[ns] |= {u for u, x in vals.items() if stats.finite(x)}
    groups = {}
    for ns in sorted(units):
        g = defaultdict(list)
        for u in sorted(units[ns]):
            g[cluster_of[ns](u)].append(u)
        groups[ns] = [g[c] for c in sorted(g)]
    rng = np.random.default_rng(seed)
    draws = {ns: rng.integers(0, len(groups[ns]), size=(reps, len(groups[ns]))) for ns in sorted(groups)}
    point, boot = {}, {}
    for name, (ns, how) in spec.items():
        f = np.median if how == "median" else np.mean
        point[name], boot[name] = {}, {}
        for mid, vals in sorted(data.get(name, {}).items()):
            arr = [np.array([float(vals[u]) for u in g if u in vals and stats.finite(vals[u])]) for g in groups[ns]]
            allv = np.concatenate(arr) if arr else np.array([])
            if not allv.size:
                continue
            point[name][mid] = float(f(allv))
            b = np.full(reps, np.nan)
            for r, row in enumerate(draws[ns]):
                x = np.concatenate([arr[i] for i in row])
                if x.size:
                    b[r] = f(x)
            boot[name][mid] = b
    return point, boot, {ns: (sum(len(g) for g in gs), len(gs)) for ns, gs in groups.items()}


def order(point: dict, boot: dict, better: str, p_sep: float = TIER_P) -> dict:
    """point: {model: value}; boot: {model: array of resampled values}. Per model its 95% rank interval
    and tier, and for every pair the share of resamples in which one beat the other (ties count half).
    Tiers: models sorted by point value; a cut between two places is kept only when every model above it
    beat every model below it in at least p_sep of the resamples."""
    models = sorted(point, key=lambda m: (-point[m] if better == "higher" else point[m], m))
    sign = -1.0 if better == "higher" else 1.0
    s = sign * np.vstack([boot[m] for m in models])          # lower is better on every row
    s = s[:, np.isfinite(s).all(axis=0)]
    n, reps = s.shape
    beats = {a: {b: round(float(np.mean(s[i] < s[j]) + 0.5 * np.mean(s[i] == s[j])), 4)
                 for j, b in enumerate(models) if j != i} for i, a in enumerate(models)}
    ranks = s.argsort(axis=0).argsort(axis=0) + 1
    cuts = [k for k in range(1, n)
            if all(beats[models[i]][models[j]] >= p_sep for i in range(k) for j in range(k, n))]
    tier_of, t = {}, 1
    for k, m in enumerate(models):
        if k in cuts:
            t += 1
        tier_of[m] = t
    return {
        "better": better, "resamples": int(reps),
        "models": {m: {"value": stats.rounded(point[m]),
                       "rank_ci": [int(np.percentile(ranks[i], 2.5)), int(np.percentile(ranks[i], 97.5))],
                       "tier": tier_of[m]} for i, m in enumerate(models)},
        "tiers": [[m for m in models if tier_of[m] == k] for k in range(1, t + 1)],
        "beats": beats,
    }


def tiers_from(data: dict, cluster_of: dict, reps: int = TIER_REPS, seed: int = TIER_SEED) -> dict:
    """Every test's tiers from the per-window values: data {statistic: {model: {unit: value}}} (STATS)."""
    spec = {name: (NAMESPACE[sec], how) for name, (sec, _, how) in STATS.items()}
    point, boot, counts = resample(data, spec, cluster_of, reps, seed)
    tests = {}
    for test, (label, stat, better) in TESTS.items():
        if stat is None:
            pc = {m: sc for m, (sc, _) in composite_from(point).items()}
            bc = {m: np.full(reps, np.nan) for m in pc}
            for r in range(reps):
                draw = {k: {m: float(b[r]) for m, b in boot[k].items() if np.isfinite(b[r])} for k in STATS}
                for m, (sc, _) in composite_from(draw).items():
                    if m in bc:
                        bc[m][r] = sc
            o = order(pc, bc, better)
        else:
            o = order(point[stat], boot[stat], better)
        tests[test] = {"label": label, **o}
    return {"reps": reps, "seed": seed, "p_separate": TIER_P,
            "drives": {ns: {"windows": u, "drives": d} for ns, (u, d) in sorted(counts.items())},
            "tests": tests}


def write_tiers(work: Path, root: Path, drives: dict | None = None) -> Path | None:
    """From the private unit store: rankings/tiers.json, aggregates only, for the models with a results
    file under `root` (the models the site shows)."""
    from . import results, store
    shown = {p.stem for p in (root / "results").glob("*.json")}
    data = {}
    for name, (sec, key, _) in STATS.items():
        data[name] = {}
        for p in sorted((work / "units" / sec).glob("*.json")):
            if p.stem in shown:
                vals = {u: float(x) for u, x in store.values(store.load(work, sec, p.stem), key).items() if stats.finite(x)}
                if vals:
                    data[name][p.stem] = vals
    if not data["cl_med"]:
        return None
    cluster_of = {NAMESPACE["truck_closed_loop"]: results.cluster_fn("truck_closed_loop", None),
                  NAMESPACE["truck_open_loop"]: results.cluster_fn("truck_open_loop", drives)}
    doc = tiers_from(data, cluster_of)
    p = root / "rankings" / TIERS_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, indent=1) + "\n")
    return p


def read_tiers(root: Path) -> dict | None:
    p = root / "rankings" / TIERS_FILE
    return json.loads(p.read_text()) if p.exists() else None


def public_tiers(root: Path) -> dict | None:
    """Tiers of comma's public segments in closed loop (median distance at 2 s), from the published CSVs,
    so anyone can recompute them."""
    vals, drives = {}, {}
    for p in sorted((root / "results").glob("*.json")):
        sec = json.loads(p.read_text())["sections"].get(SECTION) or {}
        if sec.get("status") == "ok" and sec.get("per_unit_file"):
            v, d = read_csv(root / sec["per_unit_file"])
            vals[p.stem] = {u: x for u, x in v[KEY].items() if stats.finite(x)}
            drives.update(d)
    if len(vals) < 2:
        return None
    point, boot, counts = resample({"x": vals}, {"x": ("public", "median")}, {"public": drives.get})
    return {"label": "Closed loop at 2 s, comma's public segments", **order(point["x"], boot["x"], "lower"),
            "drives": {"windows": counts["public"][0], "drives": counts["public"][1]}}


# ---------------------------------------------------------------- the check: no order the resamples do not support

# Text a reader sees that claims a place. Changelog entries (inside data-history) describe past methods.
RANK_TEXT = [
    (re.compile("[\U0001F947\U0001F948\U0001F949\U0001F3C6]"), "medal"),
    (re.compile(r"(?<![\w&/#])#\d{1,2}(?![\d\w])"), "place number"),
    (re.compile(r"\b(?:rank(?:s|ed)?|place[sd]?)\s+(?:#?\d|first|second|third|last|top)", re.IGNORECASE), "rank or place"),
    (re.compile(r"\b(?:1st|2nd|3rd|[4-9]th)\s+(?:place|of|on|overall)\b", re.IGNORECASE), "ordinal place"),
    (re.compile(r"\b(?:podium|leads|leader|fastest|slowest)\b", re.IGNORECASE), "leader wording"),
    (re.compile(r"\btop\s+(?:\d|three|five|model|shipped)", re.IGNORECASE), "top-N wording"),
    (re.compile(r"\b(?:best|worst)\s*:", re.IGNORECASE), "best or worst label"),
]
TEXT_ATTRS = ("aria-label", "title", "data-tip", "alt", "content")
TIER_TEXT = re.compile(r"\btiers?\s+\d", re.IGNORECASE)
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class _Page(HTMLParser):
    """Visible text, reader-facing attributes and tier claims: an element with `data-tier-<test>` and
    `data-model` (one model) or `data-models` (space-separated). Text that names a tier ("tier 2") must sit
    inside such an element, so every tier a reader sees is a claim the check verifies."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.skip, self.claimed = [], 0, 0
        self.text, self.claims, self.loose = [], [], []

    def _see(self, text: str, claimed: bool):
        self.text.append(text)
        if not claimed:
            self.loose += [text[max(0, mo.start() - 40): mo.end() + 40] for mo in TIER_TEXT.finditer(text)]

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        hide = tag in ("script", "style") or "data-history" in a
        mids = (a.get("data-models") or a.get("data-model") or "").split()
        mine = [(k[len("data-tier-"):], v) for k, v in a.items() if k.startswith("data-tier-") and v not in (None, "", "-")]
        claim = bool(mids and mine)
        if tag not in VOID:
            self.stack.append((hide, claim))
            self.skip += hide
            self.claimed += claim
        if self.skip:
            return
        for k in TEXT_ATTRS:
            if a.get(k):
                self._see(a[k], claim or self.claimed > 0)
        if claim:
            self.claims += [(test, m, v) for test, v in mine for m in mids]
        elif mine:
            self.claims.append((mine[0][0], "(no data-model)", mine[0][1]))

    def handle_endtag(self, tag):
        if tag in VOID or not self.stack:
            return
        hide, claim = self.stack.pop()
        self.skip -= hide
        self.claimed -= claim

    def handle_data(self, data):
        if not self.skip:
            self._see(data, self.claimed > 0)


def check_claims(claims: list, tests: dict) -> list[str]:
    """claims: [(test, model, tier)]. A model claims one tier per test; for every two models in different
    tiers, the one in the better tier must have beaten the other in at least the file's p_separate share of
    the resamples."""
    problems, by_test = [], defaultdict(dict)
    for test, mid, tier in claims:
        t = tests.get(test)
        if t is None:
            problems.append(f"{test}: tier claim for {mid}, but no resamples for this test")
            continue
        if mid not in t["beats"]:
            problems.append(f"{test}: tier claim for {mid}, which has no resamples")
            continue
        try:
            tier = int(tier)
        except ValueError:
            problems.append(f"{test}: {mid} claims tier {tier!r}")
            continue
        old = by_test[test].setdefault(mid, tier)
        if old != tier:
            problems.append(f"{test}: {mid} is printed in tier {old} and in tier {tier}")
    for test, tiers in by_test.items():
        t = tests[test]
        p_sep = t.get("p_separate", TIER_P)
        for a, ta in tiers.items():
            for b, tb in tiers.items():
                if ta < tb and t["beats"][a][b] < p_sep:
                    problems.append(f"{test}: {a} (tier {ta}) above {b} (tier {tb}), but it beat it in only "
                                    f"{t['beats'][a][b]:.3f} of {t['resamples']} resamples (needs {p_sep})")
    return problems


def check_site(site: Path, tests: dict) -> list[str]:
    """Every problem with the order a built site prints: place wording or medals in any page, script or
    stylesheet a reader gets, and tier claims the resamples do not support. `tests`: {test: order(...)}."""
    problems, claims = [], []
    if not any(site.rglob("*.html")):
        return [f"{site}: no HTML pages to check"]
    for f in sorted(site.rglob("*")):
        if f.suffix not in (".html", ".js", ".css") or not f.is_file():
            continue
        src = f.read_text(errors="replace")
        rel = f.relative_to(site)
        if f.suffix == ".html":
            pg = _Page()
            pg.feed(src)
            text = "\n".join(pg.text)
            claims += pg.claims
            problems += [f"{rel}: tier text outside a verified claim in ...{x.strip()}..." for x in pg.loose]
        else:
            text = src
        pats = RANK_TEXT if f.suffix == ".html" else RANK_TEXT[:1]   # scripts and styles: medals
        for rx, what in pats:
            for mo in rx.finditer(text):
                ctx = text[max(0, mo.start() - 40): mo.end() + 40].replace("\n", " ")
                problems.append(f"{rel}: {what} {mo.group(0)!r} in ...{ctx.strip()}...")
    return problems + check_claims(claims, tests)


def site_tests(root: Path) -> dict:
    """Every test the site may print tiers for: the G55 tests from rankings/tiers.json and comma's public
    closed loop from the CSVs."""
    doc = read_tiers(root) or {"tests": {}}
    tests = {k: {**v, "p_separate": doc.get("p_separate", TIER_P)} for k, v in doc["tests"].items()}
    pub = public_tiers(root)
    if pub:
        tests["public_cl2s"] = {**pub, "p_separate": TIER_P}
    return tests


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--section", default=SECTION, choices=[SECTION, TRUCK, "composite"])
    ap.add_argument("--check", type=Path, metavar="SITE", help="check a built site (site/_site) against the resamples")
    a = ap.parse_args(argv)
    if a.check:
        tests = site_tests(a.root)
        problems = check_site(a.check, tests)
        for p in problems:
            print(f"FAIL {p}")
        print(f"{len(problems)} problems; tests with resamples: {', '.join(sorted(tests)) or 'none'}")
        return 1 if problems else 0
    if a.section == "composite":
        docs = [json.loads(p.read_text()) for p in sorted((a.root / "results").glob("*.json"))]
        comp = g55_composite(docs, truck_scores(a.root))
        tc = ((read_tiers(a.root) or {}).get("tests") or {}).get("composite", {"models": {}})
        rows = sorted((kv for kv in comp.items() if kv[1].get("score") is not None),
                      key=lambda kv: (tc["models"].get(kv[0], {}).get("tier", 99), -kv[1]["score_raw"]))
        for m, s in rows:
            p, t = s["pillars"], tc["models"].get(m, {})
            print(f"tier {t.get('tier', '?')}  {m:<16} G55 score {s['score']:5.1f}  rank interval {t.get('rank_ci')}  "
                  f"[path {p['path']:4.0f} | rec {p['recovery']:4.0f} | ol {p['open_loop']:4.0f} | smooth {p['smoothness']:4.0f}]  "
                  f"CL 2s {s['cl_median']*100:4.1f} cm")
        return 0
    if a.section == TRUCK:
        t = truck_scores(a.root)
        for m, s in sorted((t or {"models": {}})["models"].items(), key=lambda kv: (kv[1]["rank"], kv[1]["median"])):
            ci = "no interval" if not s["ci"] else f"[{s['ci'][0]:.4f}, {s['ci'][1]:.4f}]"
            wr = f"  win_rate {s['win_rate']*100:4.1f}%" if s.get("win_rate") is not None else ""
            print(f"{s['rank']:>2}  {m:<16} median {s['median']:.4f} m {ci}{wr}  {s['n']} windows, {s['clusters']} drives")
        return 0
    r = scores(a.root)
    for m, s in sorted(r["models"].items(), key=lambda kv: (kv[1]["rank"], kv[1]["median"])):
        ci = "no interval" if not s["ci"] else f"[{s['ci'][0]:.4f}, {s['ci'][1]:.4f}]"
        wr = f"  win_rate {s['win_rate']*100:4.1f}%" if s.get("win_rate") is not None else ""
        print(f"{s['rank']:>2}  {m:<16} median {s['median']:.4f} m {ci}{wr}  {s['n']} segments, {s['clusters']} drives")
    for (a, b), d in sorted(r["pairs"].items()):
        if a < b and d:
            ci = "no interval" if not d["ci"] else f"[{d['ci'][0]:+.4f}, {d['ci'][1]:+.4f}]"
            print(f"    {a} minus {b}: {d['delta']:+.4f} m {ci}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
