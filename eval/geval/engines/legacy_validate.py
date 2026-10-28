"""Check engines/legacy.py against comma's own model replay: the modelV2 that each release's modeld wrote in CI.

  python legacy_validate.py <segment_dir> <ref_log> <cereal_dir> <model spec> [--frames N] [--json out.json]

<segment_dir> is the CI route segment the release's selfdrive/test/process_replay/model_replay.py replays
(fcamera.hevc, ecamera.hevc, rlog), <ref_log> that release's reference output: for 0.9.5 to 0.9.7
`<route>_model_tici_<model_replay_ref_commit>.bz2` on comma's openpilotci blob store, for 0.9.8 and later the
file `<route>_model_tici_master.zst` on commaai/ci-artifacts branch model_replay_master at a commit uploaded
while the model was on master. The references were made on a comma device (tici) by that release's modeld.

The inputs are what model_replay fed modeld: the first N frame pairs, the first liveCalibration only, no
lateralPlan (so desire 0 in 0.9.5 to 0.9.7, whose desire came from the lateral planner; from 0.9.8 modeld's
own DesireHelper runs on the logged carState and carControl, so lane changes are requested: the desire fed here
is the one the reference's lane change state implies), carState only where that release's process_replay config publishes it (0.9.8 and
later; before, v_ego is 0 in lateral_control_params), no liveDelay (so 0.10+ uses lateralDelay 0 + 0.1 s).
Compared per frame: plan position, velocity, orientation rate, lane line 1, the 0.9.5 lateral planner
solution, and action.desiredCurvature / desiredAcceleration where the release published them.
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import legacy as L  # noqa: E402
import route_replay as R  # noqa: E402
from legacy_ort import load  # noqa: E402


def events(path, cereal):
    import bz2
    import io
    import capnp
    import zstandard
    capnp.remove_import_hook()
    log = capnp.load(str(Path(cereal) / "log.capnp"), imports=[str(cereal), str(Path(cereal).parent / "opendbc")])
    raw = Path(path).read_bytes()
    data = (zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw)).read() if str(path).endswith(".zst")
            else bz2.decompress(raw))
    return list(log.Event.read_multiple_bytes(data))


def ref_fields(ev):
    out = []
    for e in ev:
        if e.which() != "modelV2":
            continue
        m = e.modelV2
        row = {"pos_x": list(m.position.x), "pos_y": list(m.position.y), "vel_x": list(m.velocity.x),
               "yawrate": list(m.orientationRate.z), "lane1_y": list(m.laneLines[1].y) if len(m.laneLines) > 1 else [],
               "curv": m.action.desiredCurvature, "accel": m.action.desiredAcceleration}
        try:
            sol = m.lateralPlannerSolutionDEPRECATED
            row["sol_yaw"], row["sol_yawrate"] = list(sol.yaw), list(sol.yawRate)
        except Exception:
            pass
        try:
            row["lc"] = (str(m.meta.laneChangeState), str(m.meta.laneChangeDirection))
        except Exception:
            row["lc"] = ("off", "none")
        out.append(row)
    return out


# selfdrive/controls/lib/desire_helper.py DESIRES: log.Desire index by (lane change direction, state)
LC_DESIRE = {("left", "laneChangeStarting"): 3, ("left", "laneChangeFinishing"): 3,
             ("right", "laneChangeStarting"): 4, ("right", "laneChangeFinishing"): 4}


def ref_desires(ref):
    """The desire modeld fed at each reference frame: DesireHelper updates after each model run, so frame r ran
    with the desire of frame r-1's lane change state (in the reference's meta)."""
    out = [0]
    for r in ref[:-1]:
        state, direction = r["lc"]
        out.append(LC_DESIRE.get((direction, state), 0))
    return out


def ours(outs, cmd):
    p = outs["plan"][0]
    row = {"pos_x": p[:, 0], "pos_y": p[:, 1], "vel_x": p[:, 3], "yawrate": p[:, 14],
           "lane1_y": outs["lane_lines"][0][1, :, 0], "curv": cmd["curvature"], "accel": cmd["accel"]}
    if "lat_planner_solution" in outs:
        row["sol_yaw"], row["sol_yawrate"] = outs["lat_planner_solution"][0][:, 2], outs["lat_planner_solution"][0][:, 3]
    return row


def compare(ref, mine, keys):
    rep = {}
    for k in keys:
        if any(k not in r for r in ref) or any(k not in m for m in mine):
            continue
        a = np.array([np.asarray(r[k], float).reshape(-1) for r in ref])
        b = np.array([np.asarray(m[k], float).reshape(-1) for m in mine])
        if a.size == 0 or a.shape != b.shape:
            continue
        d = b - a
        per_frame = np.sqrt(np.nanmean(d.reshape(len(d), -1) ** 2, axis=1))
        rep[k] = {"per_frame_rms": [round(float(x), 5) for x in per_frame],
                  "max_abs": float(np.nanmax(np.abs(d))), "rms_diff": float(np.sqrt(np.nanmean(d ** 2))),
                  "rms_ref": float(np.sqrt(np.nanmean(a ** 2))),
                  "corr": float(np.corrcoef(a.reshape(-1), b.reshape(-1))[0, 1]) if np.std(a) > 0 and np.std(b) > 0 else None}
    return rep


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("segment"); ap.add_argument("ref"); ap.add_argument("cereal"); ap.add_argument("model")
    ap.add_argument("--frames", type=int, default=0); ap.add_argument("--json")
    ap.add_argument("--provider", default="cpu")
    ap.add_argument("--dump", help="npz of every compared field, ours and the reference, per frame")
    a = ap.parse_args()
    seg = Path(a.segment)
    ev = events(next(p for p in (seg / "rlog.zst", seg / "rlog.bz2") if p.exists()), a.cereal)
    ref = ref_fields(events(a.ref, a.cereal))
    n = a.frames or len(ref)
    ref = ref[:n]
    cal = next(e.extrinsicsCalibration for e in ev if e.which() == "extrinsicsCalibration" and len(e.extrinsicsCalibration.rpyCalib))
    cp = next(e.carParams for e in ev if e.which() == "carParams")
    road = sorted((e.logMonoTime, e.narrowRoadCameraState.frameId, str(e.narrowRoadCameraState.sensor))
                  for e in ev if e.which() == "narrowRoadCameraState")
    car = sorted((e.logMonoTime, e.carState.vEgo) for e in ev if e.which() == "carState")
    lg = {"sensor": road[0][2]}
    w, h, f_n, f_w = R.geometry(lg)
    name, backends = load(a.model, a.provider)
    m = L.LegacyModel(backends, w, h, R.K(f_n, w, h), R.K(f_w, w, h), list(cal.rpyCalib),
                      cp.steerActuatorDelay, cp.longitudinalActuatorDelay, lateral_delay=0.0)
    uses_car_state = m.era in ("C_split", "D_plan")
    ct = np.array([c[0] for c in car]); cv = np.array([c[1] for c in car])
    mine = []
    desires = ref_desires(ref) if m.era in ("C_split", "D_plan") else [0] * n
    pairs = zip(R.frames(seg / "fcamera.hevc"), R.frames(seg / "ecamera.hevc"))
    # The reference's first modelV2 is the second frame pair's, and that frame ran with empty history (its
    # outputs match a cold start on frame 1, not frame 0 then 1): modeld never received the first pair.
    next(pairs)
    for i, (narrow, wide) in enumerate(pairs, start=1):
        if i > n:
            break
        if uses_car_state:
            j = np.searchsorted(ct, road[i][0], side="right") - 1   # frame i's camera state
            v = max(float(cv[j]), 0.0) if j >= 0 else 0.0
        else:
            v = 0.0
        outs, cmd = m.step(narrow, wide, v, desire=desires[i - 1])
        mine.append(ours(outs, cmd))
    keys = ["pos_x", "pos_y", "vel_x", "yawrate", "lane1_y", "sol_yaw", "sol_yawrate"]
    keys += ["curv"] if m.era != "A_latsol" else []
    keys += ["accel"] if m.era == "D_plan" else []
    rep = {"lane_change_frames": int(sum(d > 0 for d in desires)), "model": name, "era": m.era, "ref": Path(a.ref).name, "frames": n, "sensor": lg["sensor"],
           "sha256": [b.sha256 for b in backends], "provider": a.provider,
           "l2_norms_fp32": [b.l2_norms_fixed for b in backends], "fields": compare(ref, mine, keys)}
    print(json.dumps(rep, indent=1))
    if a.dump:
        np.savez(a.dump, **{f"{w}__{k}": np.array([np.asarray(r[k], float).reshape(-1) for r in rows])
                            for w, rows in (("ref", ref), ("ours", mine)) for k in keys
                            if all(k in r and r[k] is not None for r in rows)})
    if a.json:
        Path(a.json).write_text(json.dumps(rep, indent=1))


if __name__ == "__main__":
    main()
