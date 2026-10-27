"""Closed-loop rankings: the median distance from the recorded path at 2 s, with a drive-clustered 95%
interval of the median. Two models are separated when the interval of the difference of their medians,
on the units both have, excludes zero. A model's rank is 1 + the number of models separated from it with
a lower median, so models that are not separated can share a rank. No interval below 10 drives
(stats.MIN_CLUSTERS), so below that every model shares rank 1.

Public closed loop (comma's segments, comma's ideal car): computed from the published results and
per-unit CSVs, so a reader gets the same numbers as the site.

G55 closed loop: the window values stay private, so `geval build` on cube computes it from the unit store
and writes rankings/truck_closed_loop.json (per model: median, interval, windows, drives, rank; no
window ids). The site ranks by it once it has intervals.

    uv run python -m geval.ranking                                  # from eval/; the public table
    uv run python -m geval.ranking --root DIR                       # a directory holding results/ and per_unit/
    uv run python -m geval.ranking --section truck_closed_loop      # the published G55 file
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import stats
from .reproduce import read_csv

ROOT = Path(__file__).resolve().parents[1]
SECTION = "public_closed_loop"
TRUCK = "truck_closed_loop"
KEY = "xtrack_2s"


def rank(vals: dict, cluster_of) -> dict:
    """vals: {model: {unit: value}}. Medians, intervals, ranks and every ordered pair's difference."""
    out = {m: stats.median_pool(v, cluster_of) for m, v in vals.items()}
    pairs = {(a, b): stats.median_diff(vals[a], vals[b], cluster_of) for a in vals for b in vals if a != b}
    for m in out:
        out[m]["rank"] = 1 + sum(1 for o in vals if o != m and pairs[(o, m)] and pairs[(o, m)]["ci"]
                                 and pairs[(o, m)]["ci"][1] < 0)
    return {"models": out, "pairs": pairs}


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
           "models": {m: {k: stats.rounded(v) if k in ("median", "ci") else v for k, v in s.items()}
                      for m, s in r["models"].items()}}
    p = root / "rankings" / f"{TRUCK}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, indent=1) + "\n")
    return p


def truck_scores(root: Path) -> dict | None:
    """The published G55 closed-loop ranking, or None before `geval build` has written one."""
    p = root / "rankings" / f"{TRUCK}.json"
    return json.loads(p.read_text()) if p.exists() else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--section", default=SECTION, choices=[SECTION, TRUCK])
    a = ap.parse_args(argv)
    if a.section == TRUCK:
        t = truck_scores(a.root)
        for m, s in sorted((t or {"models": {}})["models"].items(), key=lambda kv: (kv[1]["rank"], kv[1]["median"])):
            ci = "no interval" if not s["ci"] else f"[{s['ci'][0]:.4f}, {s['ci'][1]:.4f}]"
            print(f"{s['rank']:>2}  {m:<16} median {s['median']:.4f} m {ci}  {s['n']} windows, {s['clusters']} drives")
        return 0
    r = scores(a.root)
    for m, s in sorted(r["models"].items(), key=lambda kv: (kv[1]["rank"], kv[1]["median"])):
        ci = "no interval" if not s["ci"] else f"[{s['ci'][0]:.4f}, {s['ci'][1]:.4f}]"
        print(f"{s['rank']:>2}  {m:<16} median {s['median']:.4f} m {ci}  {s['n']} segments, {s['clusters']} drives")
    for (a, b), d in sorted(r["pairs"].items()):
        if a < b and d:
            ci = "no interval" if not d["ci"] else f"[{d['ci'][0]:+.4f}, {d['ci'][1]:+.4f}]"
            print(f"    {a} minus {b}: {d['delta']:+.4f} m {ci}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
