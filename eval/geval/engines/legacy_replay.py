"""Replay historical openpilot driving models (0.9.5 to 0.11.1, engines/legacy.py) on one of comma's public segments.

  python legacy_replay.py <segment_dir> <out.npz> <cereal_dir> <model> [<model> ...]

A <model> is `name=supercombo.onnx` or `name=vision.onnx+policy.onnx`. Same inputs and output format as
route_replay.py (so geval.openloop.route_unit scores it), with three differences that follow each release:
the command is that release's (legacy.LegacyModel._command: the lateral planner on lat_planner_solution in
0.9.5, the desired_curvature head in 0.9.6 to 0.9.9, curvature and acceleration from the plan in 0.10+),
the wide view uses the narrow calibration only, as those releases did, and each model's own action_t is
stored as `action_t__<name>` (before 0.10 the acceleration is NaN: the MPC planned it, no model command).

`act__<name>` is [lateral acceleration (curvature x max(v, 1)^2), acceleration]; `raw__<name>` holds the
heads the scorer reads in the single-hypothesis layout (legacy.canonical), slices in meta.
"""
import json
import os
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import legacy as L  # noqa: E402
import route_replay as R  # noqa: E402
from legacy_ort import load  # noqa: E402


def run(seg, out, cereal, specs, provider="cpu"):
    seg = Path(seg)
    lg = R.read_log(seg, Path(cereal))
    w, h, f_n, f_w = R.geometry(lg)
    first = next(R.frames(seg / "fcamera.hevc"))
    if first.size != w * h * 3 // 2:
        raise SystemExit(f"{seg}: sensor {lg['sensor']} but frame of {first.size} bytes")
    fingerprint, steer_delay, long_delay = lg["car_params"]
    models = {}
    for spec in specs:
        name, backends = load(spec, provider)
        models[name] = L.LegacyModel(backends, w, h, R.K(f_n, w, h), R.K(f_w, w, h), lg["calib"], steer_delay, long_delay)
    t_all = lg["road"][:, 1] / 1e9
    ct = lg["car"][:, 0] / 1e9
    v_all = np.interp(t_all, ct, lg["car"][:, 1])
    acts = {k: [] for k in models}
    raws = {k: [] for k in models}
    slices = {}
    n = 0
    for narrow, wide in zip(R.frames(seg / "fcamera.hevc"), R.frames(seg / "ecamera.hevc")):
        if n >= len(t_all):
            break
        v = float(max(v_all[n], 0.0))
        views = next(iter(models.values())).warp(narrow, wide)     # one calibration, one warp: shared
        for k, m in models.items():
            outs, cmd = m.step(narrow, wide, v, views)
            if outs is None:
                acts[k].append(np.full(2, np.nan, np.float32))
                raws[k].append(None)
                continue
            vec, sl = L.canonical(outs, L.raw_slices(m))
            slices[k] = sl
            lat = cmd["curvature"] * max(v, 1.0) ** 2
            acts[k].append(np.array([lat, np.nan if cmd["accel"] is None else cmd["accel"]], np.float32))
            raws[k].append(vec)
        n += 1
    for k in models:
        width = next(len(r) for r in raws[k] if r is not None)
        raws[k] = [r if r is not None else np.full(width, np.nan, np.float32) for r in raws[k]]
    t = t_all[:n]
    ot = lg["odo"][:, 0] / 1e9
    tmp = Path(str(out) + ".part.npz")
    shared_t = models[next(iter(models))].action_t
    np.savez(tmp, t=t, v=np.interp(t, ct, lg["car"][:, 1]), a=np.interp(t, ct, lg["car"][:, 2]),
             yaw=np.interp(t, ot, lg["odo"][:, 1]), action_t=np.array(shared_t),
             models=np.array(json.dumps(list(models))), meta=np.array(json.dumps(
                 {"segment": seg.name, "fingerprint": fingerprint, "sensor": lg["sensor"], "calib": lg["calib"],
                  "engine": "legacy_replay", "provider": provider,
                  "eras": {k: m.era for k, m in models.items()},
                  "sha256": {k: [b.sha256 for b in m.backends] for k, m in models.items()},
                  "l2_norms_fp32": {k: [b.l2_norms_fixed for b in m.backends] for k, m in models.items()},
                  "action_t": {k: list(m.action_t) for k, m in models.items()},
                  "slices": slices})),
             **{f"action_t__{k}": np.array(m.action_t) for k, m in models.items()},
             **{f"act__{k}": np.array(v) for k, v in acts.items()},
             **{f"raw__{k}": np.array(v) for k, v in raws.items()})
    tmp.rename(out)
    print(json.dumps({"out": str(out), "frames": n, "fingerprint": fingerprint, "sensor": lg["sensor"],
                      "eras": {k: m.era for k, m in models.items()}}))


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:], os.environ.get("GEVAL_ORT_PROVIDER", "cpu"))
