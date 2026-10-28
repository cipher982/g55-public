"""Open-loop behaviour metrics: one model's outputs against what the recording vehicle did next.

Ported from the 2026-10-27 deep dive's scorer (matrix_score.py: parse_fields, model_metrics) and
residual_effect.py (shift, rms_rate, best_lag), unchanged in what they compute. Per unit (a public
segment or a truck window), over moving frames (speed > 1 m/s) after a 100-frame warm-up:

  lateral truth   v * yaw rate, shifted by the model's own lateral action_t
  longitudinal    the vehicle's acceleration, shifted by the longitudinal action_t
  lat_mae, lat_corr, lat_gain (no-intercept slope), lat_rate_rms (smoothness), best lag
  lon_mae, lon_corr, lon_brake_mae (frames braking harder than 0.5 m/s2), lon_rate_rms
  v3_mae          plan speed at 3 s against the speed 3 s later
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent / "engines"))
import op011 as op  # noqa: E402

DT = 0.05
WARMUP_FRAMES = 100
TURN = 0.8
BRAKE = -0.5
FIELDS = ("v0", "v1", "v3", "w20", "lane_prob", "lead_prob", "lead_dist", "jerk0", "jerk_1s",
          "plan_yaw", "plan_v", "plan_ay", "plan_lon")


# ---------------------------------------------------------------- helpers (residual_effect.py)
def shift(x, frames):
    x = np.asarray(x, float)
    out = np.full_like(x, np.nan)
    if frames == 0:
        return x.copy()
    out[:-frames] = x[frames:]
    return out


def rms_rate(x, m):
    d = np.diff(x) / DT
    mm = m[1:] & m[:-1] & np.isfinite(d)
    return float(np.sqrt(np.mean(d[mm] ** 2))) if mm.sum() > 10 else math.nan


def best_lag(action, truth_now, scope, max_s=1.5):
    best = (None, -np.inf)
    for f in range(0, int(round(max_s / DT)) + 1):
        t = shift(truth_now, f)
        m = scope & np.isfinite(t) & np.isfinite(action)
        if m.sum() < 50:
            continue
        r = np.corrcoef(action[m], t[m])[0, 1]
        if r > best[1]:
            best = (round(f * DT, 3), float(r))
    return {"lag_s": best[0], "corr": best[1]}


def causal_accel(truth, times, window=10):
    """Strictly causal backward finite-difference acceleration smoothed over trailing `window` steps."""
    v, t = np.asarray(truth, float), np.asarray(times, float)
    n = len(v)
    inst = np.zeros(n, float)
    if n > 1:
        dv, dt = np.diff(v), np.diff(t)
        good = np.isfinite(dv) & np.isfinite(dt) & (dt > 0)
        inst[1:] = np.where(good, dv / np.where(good, dt, 1.0), 0.0)
    k = max(1, min(int(window), n))
    csum = np.concatenate(([0.0], np.cumsum(inst)))
    idx = np.arange(n)
    lo = np.where(idx == 0, 0, np.maximum(1, idx - k + 1))
    return (csum[idx + 1] - csum[lo]) / (idx - lo + 1)


# ---------------------------------------------------------------- parsing (matrix_score.py)
def interp_t(col, t):
    j = int(np.clip(np.searchsorted(op.T_IDXS, t, side="right") - 1, 0, len(op.T_IDXS) - 2))
    w = (t - op.T_IDXS[j]) / (op.T_IDXS[j + 1] - op.T_IDXS[j])
    return col[:, j] * (1 - w) + col[:, j + 1] * w


def parse_fields(raw, sl, action_t):
    n = len(raw)
    f = {k: np.full(n, np.nan) for k in FIELDS}
    notes = []

    def head(name):
        a, b = sl[name]
        return raw[:, a:b].astype(np.float64)

    def attempt(label, fn):
        try:
            fn()
        except Exception as e:  # a layout without this head
            notes.append(f"{label}: {type(e).__name__}: {e}")

    def plan_f():
        o = {"plan": head("plan")}
        op._mdn(o, "plan", out_N=0, out_shape=(op.IDX_N, op.PLAN_WIDTH))
        plan = o["plan"]
        vel, acc = plan[:, :, op.Plan.VELOCITY][:, :, 0], plan[:, :, op.Plan.ACCELERATION][:, :, 0]
        f["v0"] = vel[:, 0].copy()
        f["v1"] = np.array([np.interp(1.0, op.T_IDXS, r) for r in vel])
        f["v3"] = np.array([np.interp(3.0, op.T_IDXS, r) for r in vel])
        f["jerk0"] = (acc[:, 1] - acc[:, 0]) / float(op.T_IDXS[1] - op.T_IDXS[0])
        dt1 = np.diff(op.T_IDXS[:11])
        f["jerk_1s"] = np.sqrt(np.mean(((acc[:, 1:11] - acc[:, :10]) / dt1) ** 2, axis=1))
        f["plan_yaw"] = interp_t(plan[:, :, op.Plan.ORIENTATION_RATE][:, :, 2], action_t[0])
        f["plan_v"] = interp_t(vel, action_t[0])
        f["plan_ay"] = interp_t(plan[:, :, op.Plan.ACCELERATION][:, :, 1], action_t[0])
        f["plan_lon"] = interp_t(acc, action_t[1])

    def lane_f():
        o = {"lane_lines": head("lane_lines")}
        op._mdn(o, "lane_lines", out_N=0, out_shape=(op.NUM_LANE_LINES, op.IDX_N, op.LANE_LINES_WIDTH))
        ll = o["lane_lines"]
        f["w20"] = np.array([abs(np.interp(20.0, op.X_IDXS, r[2, :, 0]) - np.interp(20.0, op.X_IDXS, r[1, :, 0]))
                             for r in ll])

    def lane_prob_f():
        p = op.sigmoid(head("lane_lines_prob"))
        f["lane_prob"] = np.mean(p[:, 1::2][:, 1:3], axis=1)

    def lead_f():
        f["lead_prob"] = op.sigmoid(head("lead_prob"))[:, 0]
        o = {"lead": head("lead")}
        mhp = o["lead"].shape[1] != 2 * op.LEAD_MHP_SELECTION * op.LEAD_TRAJ_LEN * op.LEAD_WIDTH
        if mhp:
            op._mdn(o, "lead", op.LEAD_MHP_N, op.LEAD_MHP_SELECTION, (op.LEAD_TRAJ_LEN, op.LEAD_WIDTH))
        else:
            op._mdn(o, "lead", 0, 0, (op.LEAD_MHP_SELECTION, op.LEAD_TRAJ_LEN, op.LEAD_WIDTH))
        f["lead_dist"] = o["lead"][:, 0, 0, 0].copy()

    attempt("plan", plan_f)
    attempt("lane_lines", lane_f)
    attempt("lane_lines_prob", lane_prob_f)
    attempt("lead", lead_f)
    return f, notes


# ---------------------------------------------------------------- metrics (matrix_score.py)
def slope0(a, t):
    m = np.isfinite(a) & np.isfinite(t)
    d = np.sum(t[m] ** 2)
    return float(np.sum(a[m] * t[m]) / d) if m.sum() > 10 and d > 0 else math.nan


def corr(a, b):
    m = np.isfinite(a) & np.isfinite(b)
    if m.sum() < 20 or np.std(a[m]) == 0 or np.std(b[m]) == 0:
        return math.nan
    return float(np.corrcoef(a[m], b[m])[0, 1])


def mae(a, b):
    m = np.isfinite(a) & np.isfinite(b)
    return float(np.mean(np.abs(a[m] - b[m]))) if m.sum() > 10 else math.nan


def nanmean(x):
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    return float(x.mean()) if len(x) else math.nan


def lead_frames(action_t):
    return [int(math.floor(float(t) / DT + 0.5)) for t in action_t[:2]]


def model_metrics(S, lat, lon, f, sign=1.0):
    """S: {t, v, a, yaw, action_t}; lat/lon: the model's actions (lat in S's yaw sense after `sign`);
    f: parse_fields output. -> {metric: value}."""
    n = len(S["t"])
    post = np.arange(n) >= WARMUP_FRAMES
    moving = post & np.isfinite(S["v"]) & (S["v"] > 1.0)
    plan_ok = moving & (S["t"] + 3.0 <= S["t"][-1])
    t, v = S["t"], S["v"]
    lat_lead, lon_lead = lead_frames(S["action_t"])
    latraw = S["latraw"] if "latraw" in S else S["v"] * S["yaw"]
    lat_truth, lon_truth = shift(latraw, lat_lead), shift(S["a"], lon_lead)
    lat = sign * np.asarray(lat, float)
    lon = np.asarray(lon, float)
    r = {}
    lat_scope = moving & np.isfinite(lat_truth) & np.isfinite(lat)
    turns = lat_scope & (np.abs(lat_truth) > TURN)
    r["lat_gain"] = slope0(lat[lat_scope], lat_truth[lat_scope])
    r["lat_gain_turns"] = slope0(lat[turns], lat_truth[turns]) if turns.sum() >= 20 else math.nan
    r["lat_corr"] = corr(lat[lat_scope], lat_truth[lat_scope])
    r["lat_mae"] = mae(lat[lat_scope], lat_truth[lat_scope])
    bl = best_lag(lat, latraw, moving)
    r["lat_best_lag"] = bl["lag_s"] if bl["lag_s"] is not None else math.nan
    if bl["lag_s"] is not None:
        tb = shift(latraw, int(round(bl["lag_s"] / DT)))
        sc = moving & np.isfinite(tb) & np.isfinite(lat)
        r["lat_mae_bl"] = mae(lat[sc], tb[sc])
    else:
        r["lat_mae_bl"] = math.nan
    r["lat_rate_rms"] = rms_rate(lat, moving)
    lon_scope = moving & np.isfinite(lon_truth) & np.isfinite(lon)
    brake = lon_scope & (lon_truth < BRAKE)
    r["lon_gain"] = slope0(lon[lon_scope], lon_truth[lon_scope])
    r["lon_corr"] = corr(lon[lon_scope], lon_truth[lon_scope])
    r["lon_mae"] = mae(lon[lon_scope], lon_truth[lon_scope])
    r["lon_brake_mae"] = mae(lon[brake], lon_truth[brake]) if brake.sum() >= 20 else math.nan
    r["lon_rate_rms"] = rms_rate(lon, moving)
    f3 = np.interp(t + 3.0, t, v)
    ok3 = plan_ok & np.isfinite(f["v3"])
    r["v3_mae"] = float(np.mean(np.abs(f["v3"][ok3] - f3[ok3]))) if ok3.sum() > 10 else math.nan
    ok0 = moving & np.isfinite(f["v0"]) & (v != 0)
    r["v0_mae"] = float(np.mean(np.abs(f["v0"][ok0] / v[ok0] - 1))) if ok0.sum() > 10 else math.nan
    r["moving_frames"] = float(moving.sum())
    return r


# ---------------------------------------------------------------- loaders
def route_unit(npz_path, model, sign=1.0):
    """Metrics of `model` from a route_replay.py output (any group file that holds it)."""
    d = np.load(npz_path, allow_pickle=False)
    meta = json.loads(str(d["meta"].item()))
    S = {k: np.asarray(d[k]) for k in ("t", "v", "a", "yaw", "action_t")}
    if f"action_t__{model}" in d.files:      # engines/legacy_replay.py: each release's own action_t
        S["action_t"] = np.asarray(d[f"action_t__{model}"])
    act = np.asarray(d[f"act__{model}"], float)
    f, _ = parse_fields(np.asarray(d[f"raw__{model}"], np.float32), meta["slices"][model], S["action_t"])
    return model_metrics(S, act[:, 0], act[:, 1], f, sign), meta


def truck_unit(npz_path, condition):
    """Metrics from a truck replay output (the private replay_frames.py format): the model's lateral
    accel is -action[0] there, truth is the CAN speed and yaw rate."""
    d = np.load(npz_path, allow_pickle=False)
    keys = json.loads(str(d["condition_keys"].item()))
    sl = {k: tuple(v) for k, v in json.loads(str(d["slices"].item())).items()}
    raw = np.asarray(d[keys[condition]], np.float32)
    lo, hi = sl["action"]
    act = raw[:, lo:hi].astype(float)
    t = np.asarray(d["t_mono_ns"], float) / 1e9
    v = np.asarray(d["truth"], float)
    S = {"t": t, "v": v, "a": causal_accel(v, t), "action_t": np.asarray(d["action_t"], float),
         "latraw": np.asarray(d["v_true"], float) * np.deg2rad(np.asarray(d["yaw_dps"], float))}
    f, _ = parse_fields(raw, sl, S["action_t"])
    return model_metrics(S, -act[:, 0], act[:, 1], f, 1.0)
