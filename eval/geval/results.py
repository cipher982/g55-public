"""Assemble results/<model-id>.json (SCHEMA.md) and the public per-unit CSVs from the unit store."""
from __future__ import annotations

import csv
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import SUITE_VERSION, sections as S, stats, store

DATA = Path(__file__).resolve().parent / "data"


def registry(work: Path | None = None) -> dict:
    """Known models (data/models.json) plus any model `geval run` resolved (<work>/models/<id>.json)."""
    reg = {m["id"]: m for m in json.loads((DATA / "models.json").read_text())["models"]}
    if work is not None:
        for p in sorted((Path(work) / "models").glob("*.json")):
            rec = json.loads(p.read_text())
            if "registry" in rec and rec["id"] not in reg:
                reg[rec["id"]] = rec["registry"]
    return reg


def segments() -> dict:
    with open(DATA / "segments.tsv") as f:
        return {r["segment"]: r for r in csv.DictReader(f, delimiter="\t")}


def baselines(model_id: str, reg: dict) -> list:
    """lebowski, then the previous model on the site's timeline: the scored model with the latest
    model.source.date before this one's (ties broken by id)."""
    me = reg[model_id]
    out = [] if model_id == "lebowski_877m" else ["lebowski_877m"]
    key = lambda m: (m["source"]["date"], m["id"])  # noqa: E731
    prior = [m for m in reg.values() if m["id"] != model_id and key(m) < key(me)]
    if prior:
        prev = max(prior, key=key)["id"]
        if prev not in out:
            out.append(prev)
    return out


def code_sha() -> str:
    here = Path(__file__).resolve().parent
    try:
        return subprocess.run(["git", "-C", str(here), "rev-parse", "--short=12", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip()
    except Exception:
        f = here / "_code_sha.txt"
        return f.read_text().strip() if f.exists() else "unknown"


def section(work: Path, name: str, model_id: str, bases: list, per_unit_root: Path | None, extra_condition: str = ""):
    spec = S.SECTIONS[name]
    entry = store.load(work, name, model_id)
    if entry is None or not entry["units"]:
        return {"status": "not_run", "unit_of_evidence": spec["unit"], "n": 0, "condition": S.CONDITIONS[name],
                "metrics": [], "per_unit_file": None}
    if entry["meta"].get("status") == "failed":
        return {"status": "failed", "unit_of_evidence": spec["unit"], "n": 0, "condition": S.CONDITIONS[name],
                "metrics": [], "per_unit_file": None}
    base_entries = {b: store.load(work, name, b) for b in bases}
    metrics, units_with_data = [], set()
    for key, label, unit, better, src in spec["metrics"]:
        vals = store.values(entry, src)
        units_with_data |= {u for u, v in vals.items() if stats.finite(v)}
        p = stats.pool(vals.values())
        if p["n"] == 0:
            continue
        paired = {}
        for b, be in base_entries.items():
            r = stats.paired(vals, store.values(be, src), better) if be else None
            if r:
                paired[b] = stats.rounded(r)
        metrics.append({"key": key, "label": label, "unit": unit, "better": better,
                        "value": stats.rounded(p["value"]), "ci": stats.rounded(p["ci"]), "n": p["n"], "paired": paired})
    cond = S.CONDITIONS[name]
    meta_cond = entry["meta"].get("condition")
    if meta_cond:
        cond = f"{cond} {meta_cond}"
    if extra_condition:
        cond = f"{cond} {extra_condition}"
    per_unit = None
    if spec["public"] and per_unit_root is not None:
        per_unit = f"per_unit/{model_id}/{name}.csv"
        write_csv(per_unit_root / model_id / f"{name}.csv", entry, spec)
    return {"status": "ok", "unit_of_evidence": spec["unit"], "n": len(units_with_data), "condition": cond,
            "metrics": metrics, "per_unit_file": per_unit}


def write_csv(path: Path, entry: dict, spec: dict):
    segs = segments()
    keys = [m[4] for m in spec["metrics"]]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["segment", "route", "car"] + [m[0] for m in spec["metrics"]])
        for unit in sorted(entry["units"]):
            m = entry["units"][unit]
            s = segs.get(unit, {})
            row = [unit, s.get("route", ""), s.get("car", "")]
            for k in keys:
                v = m.get(k)
                row.append("" if v is None else f"{v:.6g}")
            w.writerow(row)


def build(work: Path, model_id: str, arch: dict, out_dir: Path, per_unit_root: Path, suite: str = SUITE_VERSION,
          scored_date: str | None = None, limits_extra: list | None = None, sha256: str | None = None):
    reg = registry(work)
    me = reg[model_id]
    bases = baselines(model_id, reg)
    secs = {name: section(work, name, model_id, bases, per_unit_root) for name in S.SECTIONS}
    limits = list(S.LIMITS_COMMON)
    limits += [S.LIMITS[n] for n, s in secs.items() if s["status"] == "ok" and n in S.LIMITS]
    limits += limits_extra or []
    if me["id"] == "96244b1f_12800":
        limits.append("This model takes a navigation map and route; every run here feeds zeros (no route), "
                      "which is not how it would be used with navigation.")
    doc = {
        "schema": 1,
        "model": {
            "id": model_id, "name": me.get("name"), "sha256": sha256 or me.get("sha256"),
            "source": me["source"], "params_m": arch.get("params_m"), "gmac": arch.get("gmac"),
            "family": me.get("family"), "notes": me.get("notes"),
        },
        "scored": {"date": scored_date or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "suite": suite, "code": code_sha()},
        "baselines": bases,
        "sections": secs,
        "stills": [],
        "limits": limits,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{model_id}.json"
    p.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
    return p
