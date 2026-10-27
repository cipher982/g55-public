"""Replay comma driving models on one of comma's public segments, both cameras, native intrinsics.

  python route_replay.py <segment_dir> <out.npz> <cereal_dir> <model.onnx> [...]

<segment_dir> holds fcamera.hevc, ecamera.hevc and rlog.zst or rlog.bz2 from
https://commadataci.blob.core.windows.net/openpilotci/<dongle>/<route>/<segment>/. <cereal_dir> is
a checkout of commaai/cereal's schema (log.capnp) with opendbc beside it (car.capnp imports). Every
model steps on the same decoded frames in lockstep and keeps its full output vector (raw__<model>,
slices in meta) through runner.ModelState on ONNX Runtime (CUDA unless GEVAL_ORT_PROVIDER=cpu).
Truth: carState vEgo and aEgo, and yaw rate from cameraOdometry (the recording device's own pose
model; these cars log carState.yawRate as 0). The wide view uses the device's wideFromDeviceEuler,
inverted to first order.

From the 2026-10-27 deep dive's route_residual.py (unchanged replay; arguments instead of paths).
"""
import bz2
import io
import json
import os
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import runner  # noqa: E402
from ort_backend import OnnxRuntimeBackend  # noqa: E402

CAMERAS = {  # openpilot common/transformations/camera.py: (width, height, narrow focal, wide focal)
    "os04c10": (1344, 760, 1522.0 * 3 / 4, 567.0 / 4 * 3),
    "ar0231": (1928, 1208, 2648.0, 567.0), "ox03c10": (1928, 1208, 2648.0, 567.0),
}
PROVIDER = os.environ.get("GEVAL_ORT_PROVIDER", os.environ.get("G55_ORT_PROVIDER", "cuda"))


def K(f, w, h):
    return np.array([[f, 0, w / 2], [0, f, h / 2], [0, 0, 1.0]])


def read_log(seg: Path, cereal: Path):
    import capnp
    import zstandard
    capnp.remove_import_hook()
    log = capnp.load(str(cereal / "log.capnp"), imports=[str(cereal), str(cereal.parent / "opendbc")])
    path = next(p for p in (seg / "rlog.zst", seg / "rlog.bz2") if p.exists())
    raw = path.read_bytes()
    data = (zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw)).read()
            if path.suffix == ".zst" else bz2.decompress(raw))
    road, car, odo, calib, wide_euler, sensor, cp = [], [], [], None, None, None, None
    for e in log.Event.read_multiple_bytes(data):
        w = e.which()
        if w == "narrowRoadCameraState":
            road.append((e.narrowRoadCameraState.frameId, e.narrowRoadCameraState.timestampSof))
            sensor = sensor or str(e.narrowRoadCameraState.sensor)
        elif w == "carState":
            car.append((e.logMonoTime, e.carState.vEgo, e.carState.aEgo))
        elif w == "cameraOdometry":
            odo.append((e.logMonoTime, e.cameraOdometry.rot[2]))
        elif w == "extrinsicsCalibration" and len(e.extrinsicsCalibration.rpyCalib):
            calib = list(e.extrinsicsCalibration.rpyCalib)
            wide_euler = list(e.extrinsicsCalibration.wideFromDeviceEuler) or None
        elif w == "carParams" and cp is None:
            cp = (str(e.carParams.carFingerprint), float(e.carParams.steerActuatorDelay),
                  float(e.carParams.longitudinalActuatorDelay))
    # 2021 logs carry no wideFromDeviceEuler: the wide view then assumes it is aligned (meta records it).
    wide_missing = wide_euler is None
    wide_euler = wide_euler or [0.0, 0.0, 0.0]
    return {"wide_missing": wide_missing, "road": np.array(road, float), "car": np.array(car, float),
            "odo": np.array(odo, float), "calib": calib, "wide_euler": wide_euler, "sensor": sensor, "car_params": cp}


def frames(path, fmt="nv12"):
    import av
    with av.open(str(path)) as c:
        for f in c.decode(video=0):
            yield f.to_ndarray(format=fmt).reshape(-1) if fmt == "nv12" else f.to_ndarray(format=fmt)


def geometry(lg):
    # Older comma three logs record the sensor as "unknown"; ar0231 and ox03c10 share one geometry.
    return CAMERAS.get(lg["sensor"], CAMERAS["ar0231"])


def action_t_of(lg):
    # modeld's action_t: lateral delay + 0.075 s, longitudinal actuator delay + 0.375 s;
    # these logs carry no live lateral delay, so carParams' steerActuatorDelay stands in.
    _, steer_delay, long_delay = lg["car_params"]
    return (steer_delay + 0.075, long_delay + 0.375)


def run(seg, out, cereal, models):
    seg = Path(seg)
    lg = read_log(seg, Path(cereal))
    w, h, f_n, f_w = geometry(lg)
    first = next(frames(seg / "fcamera.hevc"))
    if first.size != w * h * 3 // 2:
        raise SystemExit(f"{seg}: sensor {lg['sensor']} but frame of {first.size} bytes")
    fingerprint = lg["car_params"][0]
    action_t = action_t_of(lg)
    states = {}
    for m in models:
        backend = OnnxRuntimeBackend(Path(m), PROVIDER)
        states[Path(m).stem] = runner.ModelState(
            backend, runner.output_slices(m), w, h, K(f_n, w, h), lg["calib"],
            wide_camera={"K": K(f_w, w, h).tolist(), "width": w, "height": h},
            device_from_wide_euler=[-a for a in lg["wide_euler"]])
    slices = {Path(m).stem: runner.output_slices(m) for m in models}
    # A frame the model drops is stored as NaNs of that model's output width.
    width = {k: max(sl.stop for sl in v.values() if sl.stop is not None) for k, v in slices.items()}
    acts, raws = {k: [] for k in states}, {k: [] for k in states}
    n = 0
    for narrow, wide in zip(frames(seg / "fcamera.hevc"), frames(seg / "ecamera.hevc")):
        if n >= len(lg["road"]):
            break
        for k, st in states.items():
            outs, _ = st.step(narrow, action_t=action_t, wide_nv12=wide)
            a = slices[k]["action"]
            raw = (outs["raw"] if outs is not None else
                   np.full(len(raws[k][-1]) if raws[k] else width[k], np.nan, np.float32))
            acts[k].append(raw[a])
            raws[k].append(raw.astype(np.float32))
        n += 1
    t = lg["road"][:n, 1] / 1e9
    ct, ot = lg["car"][:, 0] / 1e9, lg["odo"][:, 0] / 1e9
    tmp = Path(str(out) + ".part.npz")
    np.savez(tmp, t=t, v=np.interp(t, ct, lg["car"][:, 1]), a=np.interp(t, ct, lg["car"][:, 2]),
             yaw=np.interp(t, ot, lg["odo"][:, 1]), action_t=np.array(action_t),
             models=np.array(json.dumps(list(acts))), meta=np.array(json.dumps(
                 {"segment": seg.name, "fingerprint": fingerprint, "sensor": lg["sensor"], "calib": lg["calib"],
                  "wide_from_device_euler": lg["wide_euler"], "wide_from_device_missing": lg["wide_missing"],
                  "action_t": list(action_t), "provider": PROVIDER,
                  "slices": {k: {n_: [sl.start, sl.stop] for n_, sl in v.items()} for k, v in slices.items()}})),
             **{f"act__{k}": np.array(v) for k, v in acts.items()},
             **{f"raw__{k}": np.array(v) for k, v in raws.items()})
    tmp.rename(out)
    print(json.dumps({"out": str(out), "frames": n, "fingerprint": fingerprint, "sensor": lg["sensor"],
                      "action_t": action_t}))


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:])
