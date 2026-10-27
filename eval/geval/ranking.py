"""The closed-loop ranking on the site's front page, computed from the published files.

Primary score: the median over comma's public segments of the distance from the recorded path at
2 s in the public closed loop (worldmodel-4B, comma's ideal car), with a drive-clustered 95%
interval of the median. Two models are separated when the interval of the difference of their
medians, on the segments both have, excludes zero. A model's rank is 1 + the number of models
separated from it with a lower median, so models that are not separated can share a rank.

    uv run python -m geval.ranking            # from eval/; prints the table
    uv run python -m geval.ranking --root DIR # a directory holding results/ and per_unit/

Reads only results/*.json and the per-unit CSVs, so a reader gets the same numbers as the site.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import stats
from .reproduce import read_csv

ROOT = Path(__file__).resolve().parents[1]
SECTION = "public_closed_loop"
KEY = "xtrack_2s"


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
    out = {m: stats.median_pool(v, drives.get) for m, v in vals.items()}
    pairs = {(a, b): stats.median_diff(vals[a], vals[b], drives.get) for a in vals for b in vals if a != b}
    for m in out:
        out[m]["rank"] = 1 + sum(1 for o in vals if o != m and pairs[(o, m)] and pairs[(o, m)]["ci"]
                                 and pairs[(o, m)]["ci"][1] < 0)
    return {"models": out, "pairs": pairs}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=ROOT)
    r = scores(ap.parse_args(argv).root)
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
