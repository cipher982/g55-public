"""Assemble results/<model-id>.json (SCHEMA.md) and the public per-unit CSVs from the unit store."""
from __future__ import annotations

import csv
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import SUITE_VERSION, sections as S, stats, store

DATA = Path(__file__).resolve().parent / "data"
REFERENCE = "lebowski_877m"


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


def timeline_key(m: dict) -> tuple:
    """The site's timeline order: the date the weights were first public (openpilot master or
    Hugging Face), then the id. A later release of the same weights does not move a model."""
    return (m.get("first_public") or m["source"]["date"], m["id"])


def baselines(model_id: str, reg: dict) -> list:
    """lebowski, then the previous model on the timeline: the model first public before this one."""
    me = reg[model_id]
    out = [] if model_id == REFERENCE else [REFERENCE]
    prior = [m for m in reg.values() if m["id"] != model_id and timeline_key(m) < timeline_key(me)]
    if prior:
        prev = max(prior, key=timeline_key)["id"]
        if prev not in out:
            out.append(prev)
    return out


def section_baselines(work: Path, name: str, model_id: str, reg: dict) -> list:
    """The baselines of one section: lebowski, and the latest model before this one on the timeline
    that has scores in this section (the timeline's previous model when it has them)."""
    def scored(mid):
        e = store.load(work, name, mid)
        return bool(e and e["units"] and e["meta"].get("status") != "failed")
    me = reg[model_id]
    out = [REFERENCE] if model_id != REFERENCE and scored(REFERENCE) else []
    prior = sorted((m for m in reg.values() if m["id"] != model_id and timeline_key(m) < timeline_key(me)),
                   key=timeline_key, reverse=True)
    prev = next((m["id"] for m in prior if scored(m["id"])), None)
    if prev and prev not in out:
        out.append(prev)
    return out


def drive_of_segment(segment: str) -> str:
    """A public segment's drive: its route without the segment number (dongle/time)."""
    route = segments().get(segment, {}).get("route", "")
    return route.rsplit("/", 1)[0] if route else segment


def truck_drives(cfg) -> dict:
    """{window id: drive} for the truck open-loop windows, from the private settings."""
    return {w["id"]: w.get("narrow", w["id"]).split("-")[0] for w in cfg.get("truck_open_loop", {}).get("windows", [])}


def cluster_fn(name: str, drives: dict | None):
    """Which units share a drive. Public: the route. Truck closed loop and recovery: window ids are
    "<drive>:<start>" (a braking-lead window "brake:<drive>:<start>"). Truck open loop: the map."""
    if name.startswith("public_"):
        return drive_of_segment
    if name == "truck_open_loop":
        return lambda u: (drives or {}).get(u, u)
    return lambda u: u.split(":")[-2] if ":" in u else u


def censor(key: str, vals: dict) -> dict:
    """Drop values a metric cannot resolve (sections.CENSORED: a best lag at the edge of its search)."""
    edges = S.CENSORED.get(key)
    if not edges:
        return vals
    return {u: (None if stats.finite(v) and any(abs(float(v) - e) < 1e-9 for e in edges) else v)
            for u, v in vals.items()}


def code_sha() -> str:
    here = Path(__file__).resolve().parent
    try:
        return subprocess.run(["git", "-C", str(here), "rev-parse", "--short=12", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip()
    except Exception:
        f = here / "_code_sha.txt"
        return f.read_text().strip() if f.exists() else "unknown"


def public_counts(units_with_data: set) -> str:
    """Counted from the data: units with a score, the drives and cars they come from."""
    segs = segments()
    drives = {drive_of_segment(u) for u in units_with_data}
    cars = {segs[u]["car"] for u in units_with_data if u in segs}
    return f"{len(units_with_data)} of the {len(segs)} listed segments have a score, from {len(drives)} drives and {len(cars)} cars."


def section(work: Path, name: str, model_id: str, reg: dict, per_unit_root: Path | None, drives: dict | None = None):
    spec = S.SECTIONS[name]
    entry = store.load(work, name, model_id)
    if entry is None or not entry["units"]:
        return {"status": "not_run", "unit_of_evidence": spec["unit"], "n": 0, "condition": S.CONDITIONS[name],
                "baselines": [], "metrics": [], "per_unit_file": None}
    if entry["meta"].get("status") == "failed":
        return {"status": "failed", "unit_of_evidence": spec["unit"], "n": 0, "condition": S.CONDITIONS[name],
                "baselines": [], "metrics": [], "per_unit_file": None}
    bases = section_baselines(work, name, model_id, reg)
    base_entries = {b: store.load(work, name, b) for b in bases}
    cl = cluster_fn(name, drives)
    metrics, units_with_data = [], set()
    for key, label, unit, better, src in spec["metrics"]:
        vals = censor(key, store.values(entry, src))
        units_with_data |= {u for u, v in vals.items() if stats.finite(v)}
        p = stats.pool(vals, cl)
        if p["n"] == 0:
            continue
        paired = {}
        for b, be in base_entries.items():
            r = stats.paired(vals, censor(key, store.values(be, src)), better, cl) if be else None
            if r:
                paired[b] = stats.rounded(r)
        metrics.append({"key": key, "label": label, "unit": unit, "better": better,
                        "value": stats.rounded(p["value"]), "median": stats.rounded(p["median"]),
                        "ci": stats.rounded(p["ci"]), "n": p["n"], "clusters": p["clusters"], "paired": paired})
    groups = entry["meta"].get("groups") or {}
    by_group = {g: group_metrics(spec, entry, base_entries, cl, {u for u, x in groups.items() if x == g})
                for g in sorted(set(groups.values()))} if groups else None
    cond = S.CONDITIONS[name]
    if spec["public"]:
        cond = f"{cond} {public_counts(units_with_data)}"
    elif entry["meta"].get("condition"):
        cond = f"{cond} {entry['meta']['condition']}"
    per_unit = None
    if spec["public"] and per_unit_root is not None:
        per_unit = f"per_unit/{model_id}/{name}.csv"
        write_csv(per_unit_root / model_id / f"{name}.csv", entry, spec)
    n_drives = len({cl(u) for u in units_with_data})
    out = {"status": "ok", "unit_of_evidence": spec["unit"], "n": len(units_with_data), "clusters": n_drives,
           "condition": cond, "baselines": bases, "metrics": metrics, "per_unit_file": per_unit}
    if by_group:
        out["by_group"] = by_group
    return out


def group_metrics(spec, entry, base_entries, cl, units):
    """One group's pooled metrics (a truck camera setup): the same pooling and pairing over its units only."""
    ms = []
    for key, label, unit, better, src in spec["metrics"]:
        vals = {u: v for u, v in censor(key, store.values(entry, src)).items() if u in units}
        p = stats.pool(vals, cl)
        if p["n"] == 0:
            continue
        paired = {}
        for b, be in base_entries.items():
            bv = {u: v for u, v in censor(key, store.values(be, src)).items() if u in units} if be else {}
            r = stats.paired(vals, bv, better, cl) if bv else None
            if r:
                paired[b] = stats.rounded(r)
        ms.append({"key": key, "value": stats.rounded(p["value"]), "median": stats.rounded(p["median"]),
                   "ci": stats.rounded(p["ci"]), "n": p["n"], "clusters": p["clusters"], "paired": paired})
    return {"n": len(units), "clusters": len({cl(u) for u in units}), "metrics": ms}


def write_csv(path: Path, entry: dict, spec: dict):
    """One row per unit, every value at full precision (repr), so the intervals reproduce exactly.
    An empty cell is no value, including a censored one (sections.CENSORED)."""
    segs = segments()
    cols = {m[0]: censor(m[0], store.values(entry, m[4])) for m in spec["metrics"]}
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["segment", "route", "drive", "car"] + list(cols))
        for unit in sorted(entry["units"]):
            s = segs.get(unit, {})
            row = [unit, s.get("route", ""), drive_of_segment(unit), s.get("car", "")]
            for vals in cols.values():
                v = vals.get(unit)
                row.append(repr(float(v)) if stats.finite(v) else "")
            w.writerow(row)


def build(work: Path, model_id: str, arch: dict, out_dir: Path, per_unit_root: Path, suite: str = SUITE_VERSION,
          scored_date: str | None = None, limits_extra: list | None = None, sha256: str | None = None,
          drives: dict | None = None):
    reg = registry(work)
    me = reg[model_id]
    bases = baselines(model_id, reg)
    secs = {name: section(work, name, model_id, reg, per_unit_root, drives) for name in S.SECTIONS}
    limits = list(S.LIMITS_COMMON)
    limits += [S.LIMITS[n] for n, s in secs.items() if s["status"] == "ok" and n in S.LIMITS]
    limits += me.get("limits", [])          # the model's own (data/models.json), e.g. a historical model's confounds
    limits += limits_extra or []
    if me["id"] == "96244b1f_12800":
        limits.append("This model takes a navigation map and route; every run here feeds zeros (no route), "
                      "which is not how it would be used with navigation.")
    doc = {
        "schema": 1,
        "model": {
            "id": model_id, "name": me.get("name"), "sha256": sha256 or me.get("sha256"),
            "source": me["source"], "first_public": me.get("first_public") or me["source"]["date"],
            "on_master": me.get("on_master"), "params_m": arch.get("params_m"), "gmac": arch.get("gmac"),
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
