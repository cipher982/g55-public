"""Closed-loop episode metrics against the recorded path (ported from the deep dive's summarize_cl.py).

Per episode (5 Hz, 36 generated frames = 7.2 s), against the recorded driver's path for the same
window (integrated from speed and yaw rate, extended through the 5 anchor frames):
  absx_<t>s      |cross-track| distance to the recorded polyline at 1, 2, 4, 7.2 s
  along_<t>s     arc length reached minus the arc length the driver reached at the same time (+ = ahead)
  fail_1m        |cross-track| ever > 1.0 m
  rms_lat_jerk   d(curvature * v^2)/dt of the requested action, RMS
  rms_long_jerk  d(accel)/dt of the requested action, RMS
  rms_speed_err  car speed minus recorded speed, RMS
A window's value is the mean over its seeds; windows are the unit of evidence.
"""
from __future__ import annotations

from collections import defaultdict

import numpy as np

DT = 0.2
TIMES = (1, 2, 4, 7.2)


def densify(ref, step=0.05):
    pts = np.vstack([[0.0, 0.0, 0.0], np.asarray(ref)])
    out = [pts[0]]
    for a, b in zip(pts[:-1], pts[1:]):
        n = max(1, int(np.ceil(np.hypot(*(b[:2] - a[:2])) / step)))
        for t in np.arange(1, n + 1) / n:
            out.append(a + t * (b - a))
    out = np.array(out)
    seg = np.hypot(*np.diff(out[:, :2], axis=0).T)
    return out, np.concatenate([[0.0], np.cumsum(seg)])


def episode_metrics(ep, ref, rec_speed):
    dense, arc = densify(ref)
    path = np.asarray(ep["path"])
    n = len(path)
    ref_arc_t = np.interp(np.arange(1, n + 1), np.arange(0, len(ref) + 1),
                          np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(np.vstack([[0, 0, 0], ref])[:, :2], axis=0).T))]))
    xt, al = [], []
    for x, y, _psi in path:
        d2 = (dense[:, 0] - x) ** 2 + (dense[:, 1] - y) ** 2
        i = int(np.argmin(d2))
        j = min(i + 1, len(dense) - 1)
        i0 = max(j - 1, 0)
        tx, ty = dense[j, 0] - dense[i0, 0], dense[j, 1] - dense[i0, 1]
        nrm = np.hypot(tx, ty) or 1.0
        cross = (tx * (y - dense[i, 1]) - ty * (x - dense[i, 0])) / nrm
        xt.append(np.sign(cross) * np.sqrt(d2[i]) if abs(cross) > 1e-9 else 0.0)
        al.append(arc[i])
    xt = np.array(xt)
    along = np.array(al) - ref_arc_t
    acts = ep["actions"]
    curv = np.array([a["curvature"] for a in acts])
    acc = np.array([a["accel"] for a in acts])
    spd = np.array([a["speed"] for a in acts])
    lat = curv * spd ** 2
    speed_err = spd - np.asarray(rec_speed[:n])
    k = {t: int(round(t / DT)) - 1 for t in TIMES}
    m = {f"absx_{t:g}s": float(abs(xt[k[t]])) for t in TIMES}
    m.update({f"along_{t:g}s": float(along[k[t]]) for t in TIMES})
    m.update({
        "fail_1m": float(np.max(np.abs(xt)) > 1.0),
        "rms_speed_err": float(np.sqrt(np.mean(speed_err ** 2))),
        "rms_lat_jerk": float(np.sqrt(np.mean((np.diff(lat) / DT) ** 2))),
        "rms_long_jerk": float(np.sqrt(np.mean((np.diff(acc) / DT) ** 2))),
        "min_speed": float(np.min(spd)),
    })
    if ep.get("side"):
        rel = ep["side"] * xt                    # + = still on the side it was pushed to
        m.update({f"pushed_side_{t:g}s": float(rel[k[t]]) for t in TIMES})
    m["along_series"] = along.tolist()
    return m


def window_units(windows, policy, car, cond, seeds=None, metric_keys=None):
    """{window_id: {metric: mean over seeds}} for one (policy, car, condition)."""
    out = defaultdict(lambda: defaultdict(list))
    for w in windows:
        wid = f"{w['drive']}:{w['start']}"
        for ep in w["episodes"]:
            if ep["policy"] != policy or ep["car"] != car or ep["cond"] != cond:
                continue
            if seeds is not None and ep["seed"] not in seeds:
                continue
            m = episode_metrics(ep, w["ref"], w["rec_speed"])
            for key, val in m.items():
                if key == "along_series" or (metric_keys and key not in metric_keys):
                    continue
                out[wid][key].append(val)
    return {wid: {k: float(np.mean(v)) for k, v in ms.items()} for wid, ms in out.items()}
