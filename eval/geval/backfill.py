"""Regenerate the unit store and results files from the 2026-10-27 deep dive's outputs.

Sources (paths from the private config's [backfill] table):
  matrix_scores    the scorer's per-segment metrics on comma's public segments (public_open_loop)
  truck_npz_dirs   truck replay outputs <window>__<model>.npz (truck_open_loop, recomputed here)
  closed_loop      closed-loop episode files, in order (a later file wins for the same window, model,
                   condition and seed): nominal -> truck_closed_loop, disturb -> truck_recovery "heading"
  arch             parameter and MAC counts per model
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import numpy as np

from . import closedloop, openloop, results, sections as S, store


def arch_table(path: Path) -> dict:
    d = json.loads(Path(path).read_text())["models"]
    out = {}
    for k, m in d.items():
        out[k] = {"params_m": round(m["params"]["unique_learned"] / 1e6, 2), "gmac": round(m["macs"]["total"] / 1e9, 1)}
    return out


def arch_for(model_id: str, table: dict) -> dict:
    return table.get(model_id) or table.get(model_id.split("_")[0]) or {}


def public_open_loop(cfg, reg) -> list:
    d = json.loads(Path(cfg["backfill"]["matrix_scores"]).read_text())
    segs = results.segments()
    done = []
    for mid in reg:
        units = {}
        for seg, s in d["segments"].items():
            m = s["metrics"].get(mid)
            if m is None:
                continue
            units[seg] = {k[0]: m.get(k[0]) for k in S.OPEN}
        if units:
            moving = sum(1 for u in units.values() if u.get("lat_mae") is not None)
            cars = len({segs[u]["car"] for u, m in units.items() if u in segs and m.get("lat_mae") is not None})
            store.save(cfg.work, "public_open_loop", mid, units,
                       {"condition": f"{len(units)} segments, {moving} with movement from {cars} cars.",
                        "source": "deep dive 2026-10-27 scorer (matrix_scores.json)"})
            done.append(mid)
    return done


def truck_open_loop_one(cfg, mid, extra_dirs=()) -> bool:
    tol = cfg["truck_open_loop"]
    dirs = [Path(p) for p in cfg["backfill"].get("truck_npz_dirs", [])] + [Path(p) for p in extra_dirs]
    units = {}
    for w in tol["windows"]:
        hit = [d / f"{w['id']}__{mid}.npz" for d in dirs if (d / f"{w['id']}__{mid}.npz").exists()]
        if not hit:
            continue
        units[w["id"]] = openloop.truck_unit(hit[-1], tol["condition"])
    if not units:
        return False
    store.save(cfg.work, "truck_open_loop", mid, units, {"condition": tol.get("condition_note", "")})
    return True


def closed_loop_episodes(cfg):
    """{(policy_id, cond): {window: {seed: (episode, ref, rec_speed, night)}}} with later files winning."""
    bf = cfg["backfill"]
    names = bf["policy_names"]
    seeds = set(bf.get("seeds", [0, 1]))
    skip = set(bf.get("skip_windows", []))
    eps = defaultdict(lambda: defaultdict(dict))
    for f in bf["closed_loop_files"]:
        for w in json.loads(Path(f).read_text())["windows"]:
            wid = f"{w['drive']}:{w['start']}"
            if wid in skip:
                continue
            for ep in w["episodes"]:
                if ep["car"] != "g55" or ep["seed"] not in seeds or ep["policy"] not in names:
                    continue
                eps[(names[ep["policy"]], ep["cond"])][wid][ep["seed"]] = (ep, w["ref"], w["rec_speed"], w["night"])
    return eps


def window_means(by_window: dict) -> dict:
    out = {}
    for wid, by_seed in by_window.items():
        rows = [closedloop.episode_metrics(ep, ref, rs) for ep, ref, rs, _ in by_seed.values()]
        out[wid] = {k: float(np.mean([r[k] for r in rows])) for k in rows[0] if k != "along_series"}
    return out


def merge_recovery(cfg, mid, test: str, units: dict, note: str):
    entry = store.load(cfg.work, "truck_recovery", mid) or {"units": {}, "meta": {}}
    for wid, m in units.items():
        entry["units"].setdefault(wid, {})[test] = m
    notes = dict(entry["meta"].get("tests", {}))
    notes[test] = note
    tests = [t for t in ("heading", "offcentre", "brake") if t in notes]
    entry["meta"]["tests"] = notes
    entry["meta"]["condition"] = " ".join(notes[t] for t in tests)
    store.save(cfg.work, "truck_recovery", mid, entry["units"], entry["meta"])


def truck_closed_loop(cfg) -> list:
    eps = closed_loop_episodes(cfg)
    done = []
    for (mid, cond), by_window in sorted(eps.items()):
        units = window_means(by_window)
        night = sum(1 for w in by_window.values() if next(iter(w.values()))[3])
        desc = f"{len(units)} windows ({len(units) - night} day, {night} night)."
        if cond == "nominal":
            store.save(cfg.work, "truck_closed_loop", mid, units, {"condition": desc})
        elif cond == "disturb":
            merge_recovery(cfg, mid, "heading", units, f"{S.RECOVERY_TESTS['heading']} {desc}")
        done.append((mid, cond, len(units)))
    return done


def run(cfg, out_dir: Path, per_unit_root: Path, suite: str):
    reg = results.registry()
    log = {"public_open_loop": public_open_loop(cfg, reg)}
    log["truck_open_loop"] = [m for m in reg if truck_open_loop_one(cfg, m, [cfg.work / "out" / "truck_open_loop"])]
    log["truck_closed_loop"] = truck_closed_loop(cfg)
    arch = arch_table(cfg["backfill"]["arch"])
    written = []
    for mid in reg:
        written.append(str(results.build(cfg.work, mid, arch_for(mid, arch), out_dir, per_unit_root, suite=suite,
                                          drives=results.truck_drives(cfg))))
    return log, written
