#!/usr/bin/env python3
"""Driving models in closed loop inside comma's worldmodel-4B, in lockstep, same noise.

Consolidated from the 2026-10-27 deep dive's closed_loop_cmp.py (itself from the G55 project's
closed_loop_wm.py). Unchanged from it: every (policy x car) episode of one (window, seed, condition)
is generated as one world-model batch, each row's initial noise drawn from a generator seeded by
(seed, step), so every policy sees the same noise at the same step and a model run alone pairs with
a baseline run earlier; each policy is warmed on 24 recorded frames before the window plus the 9
history frames; 36 frames at 5 Hz (7.2 s).

Sources of windows:
  --segments DIR [--segment NAME ...]   comma's public segments (fcamera/ecamera.hevc + rlog), native
                                        cameras and calibration; with --cereal
  --plugin FILE --plugin-args JSON      a module exposing drives(args) -> [Drive] and optionally
                                        make_car(name, v0); used for private drives
Conditions: nominal; disturb (an extra 2 deg/s yaw for 1 s: 2 deg off heading); offcentre (an extra
yaw-rate pulse out and back over 2 s that would move an uncorrected car 0.5 m sideways with no net
heading). Side alternates with the seed. Car: `ideal` (comma's rl/env.py Physics) or plugin cars.

    python wm_closed_loop.py --segments R --cereal C --policy name=model.onnx ... --seeds 0,1 --out out.json
"""
from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import math
import sys
import time
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import op011 as op  # noqa: E402
import runner  # noqa: E402
import views as V  # noqa: E402

WARM_FRAMES = 24
N = 36
WINDOW = 9 + N + 5
SIGN = np.array([1.0, -1.0, -1.0])     # (x, y left, yaw left) -> the world model's device frame
DISTURB_FRAMES, DISTURB_DEG_S = 5, 2.0
OFFCENTRE_M, OFFCENTRE_FRAMES = 0.5, 10
CHUNK = 4
MIN_SPEED = 5.0
SUBSTEPS, DT = 4, 0.05
STATE_ATTRS = ("img_q", "big_img_q", "desire_q", "feat_q", "state", "prev_desire", "prev_feat",
               "prev_accel", "prev_curvature")


def from_bgr(bgr):
    h, w = bgr.shape[:2]
    i420 = cv2.cvtColor(bgr, cv2.COLOR_BGR2YUV_I420).reshape(-1)
    out = np.empty(w * h * 3 // 2, np.uint8)
    out[:w * h] = i420[:w * h]
    out[w * h::2] = i420[w * h:w * h * 5 // 4]
    out[w * h + 1::2] = i420[w * h * 5 // 4:]
    return out


def to_nv(frame6):
    return [from_bgr(cv2.cvtColor(cv2.resize(frame6[..., c:c + 3], (512, 256), interpolation=cv2.INTER_LINEAR),
                                  cv2.COLOR_RGB2BGR)) for c in (0, 3)]


class OnnxPolicy:
    def __init__(self, name, onnx):
        from ort_backend import OnnxRuntimeBackend
        self.name = name
        self.backend = OnnxRuntimeBackend(Path(onnx))
        self.slices = runner.output_slices(str(onnx))
        wide = {"K": op.sbigmodel_intrinsics.tolist(), "width": 512, "height": 256, "model": "pinhole"}
        self.state = runner.ModelState(self.backend, self.slices, 512, 256, op.medmodel_intrinsics, (0.0, 0.0, 0.0),
                                       wide_camera=wide)
        self.contract = "graph_state" if self.state.graph_state else "host_history"

    def reset(self):
        self.state.reset()

    def snapshot(self):
        return copy.deepcopy({a: getattr(self.state, a) for a in STATE_ATTRS})

    def restore(self, snap):
        for a, v in copy.deepcopy(snap).items():
            setattr(self.state, a, v)

    def act(self, nv, v_ego):
        outs = action = None
        for _ in range(4):
            outs, action = self.state.step(nv[0], v_ego=v_ego, wide_nv12=nv[1])
        if outs is None:
            return 0.0, 0.0, {"nonfinite": True}
        accel, curvature, _stop = action
        return -float(curvature or 0.0), float(accel), {"raw_lat_accel_right": float(outs["action"][0, 0])}


class Straight:
    name, contract = "straight", "control"

    def reset(self):
        pass

    def snapshot(self):
        return None

    def restore(self, snap):
        pass

    def act(self, nv, v_ego):
        return 0.0, 0.0, {}


class IdealCar:
    """comma's rl/env.py Physics, at 5 Hz: the request is the motion."""

    def __init__(self, speed):
        self.speed = float(speed)

    def step(self, curvature, accel):
        self.speed = max(self.speed + accel * SUBSTEPS * DT, 0.0)
        yaw = self.speed * curvature * SUBSTEPS * DT
        d = self.speed * SUBSTEPS * DT
        return np.array([d * math.cos(yaw / 2), d * math.sin(yaw / 2), yaw])


def integrate(poses):
    out, x, y, psi = [], 0.0, 0.0, 0.0
    for dx, dy, dyaw in poses:
        x += dx * np.cos(psi) - dy * np.sin(psi)
        y += dx * np.sin(psi) + dy * np.cos(psi)
        psi += dyaw
        out.append((x, y, psi))
    return np.array(out)


# ---------------------------------------------------------------- comma's public segments
class CommaSegment:
    """One public segment as a drive: 20 Hz narrow frames, carState speed, cameraOdometry yaw rate.

    Views are comma's own cameras warped into the world model's two views with the device's own
    calibration (no height change: comma's views assume its own mounting)."""

    def __init__(self, seg_dir: Path, cereal: Path):
        from route_replay import CAMERAS, K, read_log
        self.name = seg_dir.name
        self.dir = seg_dir
        lg = read_log(seg_dir, cereal)
        w, h, f_n, f_w = CAMERAS.get(lg["sensor"], CAMERAS["ar0231"])
        self.mono = lg["road"][:, 1].astype(np.int64)
        t = self.mono / 1e9
        ct, ot = lg["car"][:, 0] / 1e9, lg["odo"][:, 0] / 1e9
        self.v = np.interp(t, ct, lg["car"][:, 1])
        # cameraOdometry rot is in the device frame (z down): + is a right turn. Yaw here is +left, deg/s.
        self.yaw = -np.degrees(np.interp(t, ot, lg["odo"][:, 1]))
        self.maker = V.ViewMaker(K(f_n, w, h), None, np.asarray(lg["calib"], float), "fisheye",
                                 {"K": K(f_w, w, h).tolist(), "model": "pinhole",
                                  "device_from_wide_euler_rad": [-a for a in lg["wide_euler"]]}, road_plane=None)
        self.night = False
        self._views = {}

    def valid_starts(self):
        span = (WINDOW - 1) * V.FRAME_SKIP
        lo = V.FRAME_SKIP * WARM_FRAMES
        out = []
        for s in range(lo, len(self.mono) - span - 1, V.FRAME_SKIP * 5):
            if self.v[s - lo:s + span + 1].min() >= MIN_SPEED:
                out.append(s)
        return out

    def turn(self, s):
        a, b = s + 9 * V.FRAME_SKIP, s + 45 * V.FRAME_SKIP
        return float(np.sum(self.yaw[a:b]) * 0.05)

    def load_views(self, starts):
        need = set()
        for s in starts:
            need |= {int(i) for i in s - V.FRAME_SKIP * WARM_FRAMES + V.FRAME_SKIP * np.arange(WARM_FRAMES)}
            need |= {int(i) for i in s + V.FRAME_SKIP * np.arange(WINDOW)}
        import av
        with av.open(str(self.dir / "fcamera.hevc")) as cn, av.open(str(self.dir / "ecamera.hevc")) as cw:
            for i, (fn, fw) in enumerate(zip(cn.decode(video=0), cw.decode(video=0))):
                if i > max(need):
                    break
                if i in need:
                    self._views[i] = self.maker(fn.to_ndarray(format="bgr24"), fw.to_ndarray(format="bgr24"))

    def window(self, s):
        idx = s + V.FRAME_SKIP * np.arange(WINDOW)
        return {"track_indices": idx, "poses_left": V.step_poses(self.mono, self.v, self.yaw, idx),
                "views": np.stack([self._views[int(i)] for i in idx])}

    def warm_views(self, s):
        idx = s - V.FRAME_SKIP * WARM_FRAMES + V.FRAME_SKIP * np.arange(WARM_FRAMES)
        return [(self._views[int(i)], float(self.v[int(i)])) for i in idx if int(i) in self._views]


def pick_middle(drive, per):
    starts = drive.valid_starts()
    if not starts:
        return []
    mid = len(drive.mono) // 2 - (WINDOW * V.FRAME_SKIP) // 2
    order = sorted(starts, key=lambda s: abs(s - mid))
    chosen = []
    for s in order:
        if all(abs(s - c) >= WINDOW * V.FRAME_SKIP for c in chosen):
            chosen.append(s)
        if len(chosen) >= per:
            break
    return sorted(chosen)


# ---------------------------------------------------------------- the loop (closed_loop_cmp.py)
def predict_batch(rt, future, hists, poses, fidx0, steps, cfg, seed, k):
    import torch
    from wm_runtime import CONTEXT_FRAMES, FUTURE_FRAMES
    B = hists.shape[0]
    dev = rt.device
    lat = torch.cat((future[None].expand(B, -1, -1, -1, -1), hists, torch.zeros_like(hists[:, :1])), dim=1)
    lat = lat.to(dev, torch.bfloat16).contiguous()
    g = torch.Generator(device=dev).manual_seed(seed * 1000 + k)
    noise = torch.randn(lat.shape[2:], generator=g, device=dev, dtype=torch.float32).to(torch.bfloat16)
    lat[:, -1] = noise
    T = lat.shape[1]
    position = lat.new_zeros((B, T, 3))
    euler = torch.zeros_like(position)
    pose_mask = torch.ones((B, T), dtype=torch.int64, device=dev)
    p = torch.as_tensor(np.asarray(poses, np.float32), device=dev)
    position[:, -1, 0], position[:, -1, 1] = p[:, 0].to(position.dtype), p[:, 1].to(position.dtype)
    euler[:, -1, 2] = p[:, 2].to(euler.dtype)
    pose_mask[:, -1] = 0
    fidxs = torch.cat((torch.arange(fidx0, fidx0 + FUTURE_FRAMES), torch.arange(CONTEXT_FRAMES)))[None].expand(B, -1).to(dev)
    with torch.inference_mode():
        out = rt.model.generate(latents=lat, augments_pos_ref_augment=position, ref_augment_from_augments_euler=euler,
                                pose_mask=pose_mask, fidxs=fidxs, steps=steps, num_prefill_frames=T - 1,
                                dtype=torch.bfloat16, inference_schedule="linear", cfg=cfg, kv_cache_dtype="bfloat16")
    return out["latents"][:, 0]


def run_group(rt, policies, snaps, init_actions, cars, make_car, w, latents, d, seed, cond, steps, cfg):
    import torch
    from wm_runtime import HISTORY_FRAMES
    history, future = latents[:9], latents[9 + N: 14 + N]
    v0 = float(d.v[w["track_indices"][8]])
    agents = [{"policy": pol, "car_name": c, "car": make_car(c, v0), "act": init_actions[pol.name], "poses": [],
               "actions": [], "snap": snaps[pol.name]} for pol in policies for c in cars]
    B = len(agents)
    hists = history[None].repeat(B, 1, 1, 1, 1).clone()
    side = 1.0 if seed % 2 == 0 else -1.0
    omega_off = OFFCENTRE_M / max(v0, 1.0)      # rad/s: 5 frames out, 5 back -> v0 * omega lateral metres
    t0 = time.perf_counter()
    t_wm = t_pol = 0.0
    for k in range(N):
        poses = []
        for a in agents:
            pose = a["car"].step(a["act"][0], a["act"][1])
            if cond == "disturb" and k < DISTURB_FRAMES:
                pose = V.perturbed_pose(pose, side * np.radians(DISTURB_DEG_S))
            if cond == "offcentre" and k < OFFCENTRE_FRAMES:
                pose = V.perturbed_pose(pose, side * omega_off * (1.0 if k < OFFCENTRE_FRAMES // 2 else -1.0))
            a["poses"].append(pose)
            poses.append(pose * SIGN)
        ta = time.perf_counter()
        P = np.array(poses)
        lat = torch.cat([predict_batch(rt, future, hists[c:c + CHUNK], P[c:c + CHUNK], HISTORY_FRAMES + N - k, steps, cfg,
                                       seed, k) for c in range(0, B, CHUNK)])
        hists = torch.roll(hists, -1, dims=1)
        hists[:, -1] = lat
        frames = rt.decode(lat)
        tb = time.perf_counter()
        t_wm += tb - ta
        for i, a in enumerate(agents):
            pol = a["policy"]
            if a["snap"] is not None:
                pol.restore(a["snap"])
            curvature, accel, info = pol.act(to_nv(frames[i]), a["car"].speed)
            a["snap"] = pol.snapshot()
            a["act"] = (curvature, accel)
            a["actions"].append({"curvature": curvature, "accel": accel, "speed": a["car"].speed, **info})
        t_pol += time.perf_counter() - tb
    out = [{"policy": a["policy"].name, "contract": a["policy"].contract, "car": a["car_name"], "seed": seed,
            "cond": cond, "side": side if cond in ("disturb", "offcentre") else 0.0,
            "path": integrate(np.array(a["poses"])).tolist(), "actions": a["actions"], "speed_end": a["car"].speed}
           for a in agents]
    timing = {"batch": B, "peak_gb": torch.cuda.max_memory_allocated() / 1e9 if torch.cuda.is_available() else 0.0,
              "sec_total": time.perf_counter() - t0, "sec_wm_per_step": t_wm / N, "sec_policy_per_step": t_pol / N}
    return out, timing


def warm(policies, d, w, s):
    warm_views = [(to_nv(f), v) for f, v in d.warm_views(s)]
    hist_views = [(to_nv(w["views"][k]), float(d.v[w["track_indices"][k]])) for k in range(9)]
    snaps, acts = {}, {}
    for pol in policies:
        pol.reset()
        for nv, v in warm_views:
            pol.act(nv, v)
        act = (0.0, 0.0)
        for nv, v in hist_views:
            c, a_, _ = pol.act(nv, v)
            act = (c, a_)
        snaps[pol.name] = pol.snapshot()
        acts[pol.name] = act
    return snaps, acts, len(warm_views)


def load_plugin(path):
    spec = importlib.util.spec_from_file_location("geval_plugin", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--segments", type=Path)
    p.add_argument("--segment", action="append", default=[])
    p.add_argument("--cereal", type=Path)
    p.add_argument("--per-segment", type=int, default=1)
    p.add_argument("--plugin", type=Path)
    p.add_argument("--plugin-args", default="{}")
    p.add_argument("--policy", action="append", required=True, help="name=path.onnx, or 'straight'")
    p.add_argument("--cars", default="ideal")
    p.add_argument("--seeds", default="0,1")
    p.add_argument("--conds", default="nominal")
    p.add_argument("--steps", type=int, default=5)
    p.add_argument("--cfg", type=float, default=2.0)
    p.add_argument("--wm-chunk", type=int, default=4)
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args()
    global CHUNK
    CHUNK = a.wm_chunk
    make_car = lambda name, v0: IdealCar(v0)  # noqa: E731
    todo = []
    if a.plugin:
        plug = load_plugin(a.plugin)
        for d, s in plug.drives(json.loads(a.plugin_args)):
            todo.append((d, s))
        if hasattr(plug, "make_car"):
            make_car = lambda name, v0: IdealCar(v0) if name == "ideal" else plug.make_car(name, v0)  # noqa: E731
    else:
        names = a.segment or sorted(x.name for x in a.segments.iterdir() if (x / "ecamera.hevc").exists())
        for name in names:
            try:
                d = CommaSegment(a.segments / name, a.cereal)
            except Exception as e:
                print(json.dumps({"segment": name, "skipped": f"{type(e).__name__}: {e}"}), flush=True)
                continue
            starts = pick_middle(d, a.per_segment)
            if not starts:
                print(json.dumps({"segment": name, "skipped": "no 10 s stretch above 5 m/s"}), flush=True)
                continue
            d.load_views(starts)
            todo += [(d, s) for s in starts]
    from wm_runtime import WorldModelRuntime
    rt = WorldModelRuntime()
    print(json.dumps({"wm_loaded_s": round(rt.load_s, 1), "windows": len(todo)}), flush=True)
    policies = [Straight() if spec == "straight" else OnnxPolicy(*spec.split("=", 1)) for spec in a.policy]
    cars, seeds, conds = a.cars.split(","), [int(x) for x in a.seeds.split(",")], a.conds.split(",")
    result = {"args": {k: str(v) for k, v in vars(a).items() if k != "plugin_args"}, "windows": []}
    done = set()
    if a.out.exists():                        # resume: keep finished windows
        result = json.loads(a.out.read_text())
        done = {f"{w['drive']}:{w['start']}" for w in result["windows"]}
    t0 = time.perf_counter()
    for d, s in todo:
        if f"{d.name}:{s}" in done:
            continue
        w = d.window(s)
        latents = rt.encode(w["views"])
        snaps, acts, nwarm = warm(policies, d, w, s)
        ref = integrate(w["poses_left"][8:49])
        rec_speed = [float(d.v[int(i)]) for i in w["track_indices"][9:50]]
        win = {"drive": d.name, "start": int(s), "turn_deg": d.turn(s), "v0": float(d.v[w["track_indices"][8]]),
               "night": bool(getattr(d, "night", False)), "warm_frames": nwarm, "ref": ref.tolist(),
               "rec_speed": rec_speed, "episodes": [], "timing": []}
        for seed in seeds:
            for cond in conds:
                eps, timing = run_group(rt, policies, snaps, acts, cars, make_car, w, latents, d, seed, cond, a.steps, a.cfg)
                win["episodes"] += eps
                win["timing"].append(timing)
                dev = {f"{e['policy']}/{e['car']}": round(float(np.array(e["path"])[19, 1] - ref[19, 1]), 2) for e in eps}
                print(json.dumps({"drive": d.name, "start": int(s), "seed": seed, "cond": cond,
                                  "sec": round(timing["sec_total"], 1), "lat_dev_4s": dev}), flush=True)
        result["windows"].append(win)
        tmp = a.out.with_suffix(".part")
        tmp.write_text(json.dumps(result, default=float))
        tmp.rename(a.out)
    print(json.dumps({"elapsed_s": round(time.perf_counter() - t0, 1)}), flush=True)


if __name__ == "__main__":
    main()
