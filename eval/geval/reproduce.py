"""Recompute every public section's values, intervals and paired deltas from the published per-unit
CSVs and compare them with the results files.

    uv run python -m geval.reproduce            # from eval/; exit 1 on any difference
    uv run python -m geval.reproduce --root DIR # a directory holding results/ and per_unit/

Uses only the CSVs and geval.stats (the same code that wrote the results), so a difference means
the published files disagree with each other.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from . import stats

ROOT = Path(__file__).resolve().parents[1]


def read_csv(path: Path):
    """{metric: {segment: value or None}}, {segment: drive}."""
    with open(path) as f:
        rows = list(csv.DictReader(f))
    drives = {r["segment"]: r["drive"] for r in rows}
    keys = [k for k in rows[0] if k not in ("segment", "route", "drive", "car")] if rows else []
    return {k: {r["segment"]: (float(r[k]) if r[k] != "" else None) for r in rows} for k in keys}, drives


def check(root: Path) -> list[str]:
    bad, checked = [], 0
    docs = {p.stem: json.loads(p.read_text()) for p in sorted((root / "results").glob("*.json"))}
    for mid, doc in docs.items():
        for name, sec in doc["sections"].items():
            if sec["status"] != "ok" or not sec.get("per_unit_file"):
                continue
            vals, drives = read_csv(root / sec["per_unit_file"])
            for m in sec["metrics"]:
                v = vals[m["key"]]
                p = stats.rounded(stats.pool(v, drives.get))
                for f in ("value", "median", "ci", "n", "clusters"):
                    checked += 1
                    if p[f] != m.get(f):
                        bad.append(f"{mid} {name} {m['key']} {f}: published {m.get(f)} recomputed {p[f]}")
                for b, pub in m["paired"].items():
                    bsec = docs.get(b, {}).get("sections", {}).get(name, {})
                    if not bsec.get("per_unit_file"):
                        bad.append(f"{mid} {name} {m['key']} vs {b}: no baseline CSV")
                        continue
                    bv, bd = read_csv(root / bsec["per_unit_file"])
                    r = stats.rounded(stats.paired(v, bv[m["key"]], m["better"], {**bd, **drives}.get))
                    for f, x in pub.items():
                        checked += 1
                        if r.get(f) != x:
                            bad.append(f"{mid} {name} {m['key']} vs {b} {f}: published {x} recomputed {r.get(f)}")
    print(f"{checked} published numbers checked, {len(bad)} differ")
    return bad


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=ROOT)
    bad = check(ap.parse_args(argv).root)
    for line in bad:
        print(line)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
