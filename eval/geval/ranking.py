"""Tiers, closed-loop tables and the order check for the G-Wagon eval site.

The site is a results report: one table per test, each model's value with its 95% interval, rows newest
release first. It prints no combined score and no places (the G55 composite score was retired on
2026-10-31, David: "just present our results and lay them out").

1. The check (`check_site`): `site/build.py` runs it and refuses to write a failing site. It fails on
   - place numbers, medals, leader wording or a combined score anywhere a reader can see them;
   - a list of models (`data-order="release"`) that is not newest release first, a model listed twice in
     one list, and two or more models listed outside such a list;
   - a tier claim (`data-tier-<test>`) the resamples do not support;
   - a link into the private code repositories (a 404 for every reader).

2. Tiers (`write_tiers`, `order`): whole drives resampled with replacement (TIER_REPS times, the same drives
   for every model and test that share them); a model sits in a higher tier only when it beat every model
   in the lower tiers in at least TIER_P of the resamples. `rankings/tiers.json` holds aggregates only
   (values, rank intervals, tiers, pairwise shares). The site prints no tiers now; the check verifies any
   it would.

3. Single-section closed-loop tables (`rank`, `scores`, `write_truck`): median and mean distance from
   the recorded path at 2 s with a drive-clustered 95% interval of the median and paired differences.

    uv run python -m geval.ranking                                  # from eval/; the public table
    uv run python -m geval.ranking --section truck_closed_loop      # the G55 closed-loop table
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


# ---------------------------------------------------------------- tiers: the order the resamples support

TIER_REPS = 2000
TIER_SEED = 0
TIER_P = 0.95         # a higher tier beat every model below it in at least this share of resamples
TIERS_FILE = "tiers.json"

# The statistics tiers are computed for: (section, store key, statistic). Lower is better for each.
STATS = {
    "cl_med": ("truck_closed_loop", "absx_2s", "median"),
    "hdg": ("truck_recovery", "heading:absx_2s", "median"),
    "off": ("truck_recovery", "offcentre:absx_2s", "median"),
    "ol": ("truck_open_loop", "lat_mae", "mean"),
    "jerk": ("truck_closed_loop", "rms_lat_jerk", "mean"),
}
# The tests with tiers: test -> (label, statistic, better).
TESTS = {
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
        tests[test] = {"label": label, **order(point[stat], boot[stat], better)}
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
    (re.compile(r"\b(?:G55|composite)\s+score\b|\b\d{1,3}(?:\.\d+)?\s*/\s*100\b", re.IGNORECASE), "combined score"),
]
TEXT_ATTRS = ("aria-label", "title", "data-tip", "alt", "content")
# The code repositories are private: a link into one is a 404 for every reader.
PRIVATE_LINKS = ("https://github.com/cipher982/",)
TIER_TEXT = re.compile(r"\btiers?\s+\d", re.IGNORECASE)
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class _Page(HTMLParser):
    """Visible text, reader-facing attributes, tier claims and lists of models.

    A tier claim is an element with `data-tier-<test>` and `data-model` (one model) or `data-models`
    (space-separated). Text that names a tier ("tier 2") must sit inside such an element, so every tier a
    reader sees is a claim the check verifies.

    A list of models is an element with `data-order` (the only order is "release": newest first by first
    public date). Each element with `data-model` inside it adds that model to the list, once per run of
    elements for the same model. Elements with `data-model` outside every list are collected in `free`."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.skip, self.claimed = [], 0, 0
        self.text, self.claims, self.loose = [], [], []
        self.open_lists, self.lists, self.free = [], [], []
        self.private = []

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
        listing = "data-order" in a and not self.skip and not hide
        if tag not in VOID:
            self.stack.append((hide, claim, listing))
            self.skip += hide
            self.claimed += claim
        if self.skip:
            return
        for k in ("href", "src"):
            if (a.get(k) or "").startswith(PRIVATE_LINKS):
                self.private.append(a[k])
        if listing:
            self.open_lists.append((a["data-order"], []))
        if a.get("data-model"):
            mid = a["data-model"]
            if self.open_lists:
                lst = self.open_lists[-1][1]
                if not lst or lst[-1] != mid:
                    lst.append(mid)
            else:
                self.free.append(mid)
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
        hide, claim, listing = self.stack.pop()
        self.skip -= hide
        self.claimed -= claim
        if listing and self.open_lists:
            self.lists.append(self.open_lists.pop())

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


def check_lists(lists: list, free: list, released: dict, where: str) -> list[str]:
    """lists: [(order, [model ids in page order])]; free: model ids listed outside any list; released:
    {model id: first public date}. Every list is newest release first, holds a model once, and names only
    models with a release date; a page lists at most one model outside a list."""
    problems = []
    for kind, mids in lists:
        if kind != "release":
            problems.append(f"{where}: a list ordered by {kind!r}; the only order is \"release\"")
            continue
        twice = sorted({m for m in mids if mids.count(m) > 1})
        if twice:
            problems.append(f"{where}: {', '.join(twice)} listed twice in one list")
        unknown = [m for m in mids if m not in released]
        if unknown:
            problems.append(f"{where}: no release date for {', '.join(unknown)}")
            continue
        for a, b in zip(mids, mids[1:]):
            if released[a] < released[b]:
                problems.append(f"{where}: {a} (first public {released[a]}) listed above {b} ({released[b]}): "
                                f"lists are newest release first")
    if len(set(free)) > 1:
        problems.append(f"{where}: {len(set(free))} models listed outside a release-ordered list "
                        f"({', '.join(sorted(set(free))[:5])}{', ...' if len(set(free)) > 5 else ''})")
    return problems


def release_dates(root: Path) -> dict:
    """{model id: first public date} from the results files under `root`."""
    out = {}
    for p in sorted((root / "results").glob("*.json")):
        m = json.loads(p.read_text())["model"]
        out[p.stem] = m.get("first_public") or m["source"]["date"]
    return out


def check_site(site: Path, tests: dict, released: dict | None = None) -> list[str]:
    """Every problem with the order a built site prints: place wording, medals or a combined score in any
    page, script or stylesheet a reader gets; lists of models that are not newest release first (`released`:
    {model id: first public date}; None: no dates, so any list fails); and tier claims the resamples do not
    support. `tests`: {test: order(...)}."""
    problems, claims = [], []
    released = released or {}
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
            problems += check_lists(pg.lists, pg.free, released, str(rel))
            problems += [f"{rel}: link into a private repository {x}" for x in pg.private]
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
    ap.add_argument("--section", default=SECTION, choices=[SECTION, TRUCK])
    ap.add_argument("--check", type=Path, metavar="SITE", help="check a built site (site/_site) against the resamples")
    a = ap.parse_args(argv)
    if a.check:
        tests = site_tests(a.root)
        problems = check_site(a.check, tests, release_dates(a.root))
        for p in problems:
            print(f"FAIL {p}")
        print(f"{len(problems)} problems; tests with resamples: {', '.join(sorted(tests)) or 'none'}")
        return 1 if problems else 0
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
