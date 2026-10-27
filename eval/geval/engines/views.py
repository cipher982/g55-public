"""comma's world-model views from the G55's cameras, ego motion from CAN, and the image metrics.

Pure NumPy/OpenCV (no torch): everything here runs and is tested on any host.

The world model (commaai/worldmodel-4B over commaai/vit-ae-2x-f8c32) sees two calibrated
256x128 RGB views, concatenated on the channel axis (128x256x6):
  narrow: openpilot's medmodel frame at half scale, focal 455, principal point (128, 23.8)
  wide:   openpilot's sbigmodel frame at half scale, focal 227.5, principal point (128, 75.9)
(comma's rl/env.py and the autoencoder's example.py). Each view is a warp of a camera image
into that frame; here the narrow AR0234 supplies the narrow view and, either, the real
fisheye or the same narrow camera (synthesized wide) supplies the wide view.

Frames, in metres and radians: openpilot's device frame is x forward, y right, z down. The
world model's pose input sign is not documented; `relative_pose` returns x forward, y left,
yaw left (positive for a left turn) and the caller multiplies y and yaw by the sign it uses.
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))   # op011 sits beside this file here
import op011 as op  # noqa: E402

VIEW_W, VIEW_H = 256, 128
FPS = 5
FRAME_SKIP = 4                       # 20 Hz recording -> the world model's 5 Hz
COMMA_HEIGHT_M = 1.22                # the height comma's views assume (calibrationd's initializer)
G55_HEIGHT_M = 1.78                  # the windshield cameras, measured
_HALF = np.diag([2.0, 2.0, 1.0])     # a 256x128 view pixel -> openpilot's 512x256 model pixel


def view_matrix(rotation: np.ndarray, K: np.ndarray, wide: bool, road_plane=None) -> np.ndarray:
    """Undistorted camera pixel <- 256x128 view pixel, for a device_from_calib rotation."""
    return op.get_warp_matrix_from_rotation(rotation, np.asarray(K, float), wide, road_plane) @ _HALF


def view_maps(M: np.ndarray, K: np.ndarray, dist, model: str = "pinhole") -> tuple[np.ndarray, np.ndarray]:
    """cv2.remap tables (map_x, map_y) for one 256x128 view through the calibrated lens.

    `M` maps a view pixel to the undistorted (pinhole) camera pixel; the lens model then maps
    that ray to the raw image: OpenCV radial-tangential (k1, k2, p1, p2[, k3]) for "pinhole",
    the four-coefficient OpenCV fisheye for "fisheye" (as runner.Warp does)."""
    x, y = np.meshgrid(np.arange(VIEW_W, dtype=np.float64), np.arange(VIEW_H, dtype=np.float64))
    p = np.einsum("ij,jhw->ihw", M, np.stack([x, y, np.ones_like(x)]))
    u, v = p[0] / p[2], p[1] / p[2]
    K = np.asarray(K, float)
    yn = (v - K[1, 2]) / K[1, 1]
    xn = (u - K[0, 2] - K[0, 1] * yn) / K[0, 0]
    if model == "fisheye":
        k1, k2, k3, k4 = (np.zeros(4) if dist is None else np.asarray(dist, float).reshape(-1)[:4])
        r = np.hypot(xn, yn)
        theta = np.arctan(r)
        t2 = theta * theta
        theta_d = theta * (1 + k1 * t2 + k2 * t2**2 + k3 * t2**3 + k4 * t2**4)
        f = np.divide(theta_d, r, out=np.ones_like(r), where=r > 1e-12)
        xd, yd = xn * f, yn * f
    elif model == "pinhole":
        d = np.zeros(5) if dist is None else np.asarray(dist, float).reshape(-1)
        k1, k2, p1, p2 = d[:4]
        k3 = d[4] if len(d) > 4 else 0.0
        r2 = xn * xn + yn * yn
        radial = 1 + k1 * r2 + k2 * r2 * r2 + k3 * r2**3
        xd = xn * radial + 2 * p1 * xn * yn + p2 * (r2 + 2 * xn * xn)
        yd = yn * radial + p1 * (r2 + 2 * yn * yn) + 2 * p2 * xn * yn
    else:
        raise ValueError(f"unsupported camera model: {model}")
    ud = K[0, 0] * xd + K[0, 1] * yd + K[0, 2]
    vd = K[1, 1] * yd + K[1, 2]
    return ud.astype(np.float32), vd.astype(np.float32)


class ViewMaker:
    """Both world-model views for one drive's cameras.

    `wide_source` is "fisheye" (the real wide camera through `wide_camera`, a camera.json dict
    with K, dist, model and device_from_wide_euler_rad) or "narrow" (the wide view synthesized
    from the narrow camera, as on 2026-10-12). `road_plane=(source_h, target_h)` views both
    cameras' road plane from comma's height; None keeps the rotation-only warp."""

    def __init__(self, K, dist, rpy_rad, wide_source="fisheye", wide_camera=None,
                 road_plane=(G55_HEIGHT_M, COMMA_HEIGHT_M)):
        R = op.rot_from_euler(np.asarray(rpy_rad, float))
        self.narrow = view_maps(view_matrix(R, K, False, road_plane), K, dist, "pinhole")
        if wide_source == "narrow":
            self.wide = view_maps(view_matrix(R, K, True, road_plane), K, dist, "pinhole")
        elif wide_source == "fisheye":
            wide_K = np.asarray(wide_camera["K"], float)
            R_wide = R @ op.rot_from_euler(np.asarray(wide_camera.get("device_from_wide_euler_rad", (0, 0, 0)), float))
            self.wide = view_maps(view_matrix(R_wide, wide_K, True, road_plane), wide_K, wide_camera.get("dist"),
                                  wide_camera.get("model", "pinhole"))
        else:
            raise ValueError(f"wide_source must be 'fisheye' or 'narrow', not {wide_source!r}")
        self.wide_source = wide_source

    @staticmethod
    def _warp(bgr: np.ndarray, maps) -> np.ndarray:
        view = cv2.remap(bgr, maps[0], maps[1], cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        return cv2.cvtColor(view, cv2.COLOR_BGR2RGB)

    def __call__(self, narrow_bgr: np.ndarray, wide_bgr: np.ndarray | None = None) -> np.ndarray:
        """Upright BGR camera frames -> one 128x256x6 RGB world-model frame (narrow, wide)."""
        wide_src = narrow_bgr if self.wide_source == "narrow" else wide_bgr
        return np.concatenate([self._warp(narrow_bgr, self.narrow), self._warp(wide_src, self.wide)], axis=-1)


def relative_pose(mono_ns: np.ndarray, v_mps: np.ndarray, yaw_dps: np.ndarray, i0: int, i1: int) -> np.ndarray:
    """Pose of 20 Hz sample i1 in the frame of sample i0: (dx forward, dy left, dyaw left), integrated
    sample by sample from true speed and CAN yaw rate (+left, deg/s)."""
    x = y = psi = 0.0
    for i in range(i0, i1):
        dt = (int(mono_ns[i + 1]) - int(mono_ns[i])) / 1e9
        v = 0.5 * (float(v_mps[i]) + float(v_mps[i + 1]))
        w = np.radians(0.5 * (float(yaw_dps[i]) + float(yaw_dps[i + 1])))
        mid = psi + 0.5 * w * dt
        x += v * dt * np.cos(mid)
        y += v * dt * np.sin(mid)
        psi += w * dt
    return np.array([x, y, psi])


def step_poses(mono_ns, v_mps, yaw_dps, track_indices) -> np.ndarray:
    """(n-1, 3) relative poses between consecutive 5 Hz frames given their 20 Hz track indices."""
    return np.stack([relative_pose(mono_ns, v_mps, yaw_dps, int(a), int(b))
                     for a, b in zip(track_indices[:-1], track_indices[1:])])


def perturbed_pose(pose: np.ndarray, extra_yaw_rate_rad_s: float, dt: float = 1 / FPS) -> np.ndarray:
    """A 5 Hz step's relative pose with an extra constant yaw rate (+left), same distance travelled."""
    dx, dy, dyaw = pose
    d = float(np.hypot(dx, dy))
    yaw = dyaw + extra_yaw_rate_rad_s * dt
    return np.array([d * np.cos(yaw / 2), d * np.sin(yaw / 2), yaw])


# ---- metrics -------------------------------------------------------------------------------------

def psnr(a: np.ndarray, b: np.ndarray) -> float:
    mse = float(np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2))
    return float("inf") if mse == 0 else 10 * np.log10(255.0**2 / mse)


def ssim(a: np.ndarray, b: np.ndarray) -> float:
    """Mean SSIM over channels (Wang et al. 2004: 11x11 Gaussian window, sigma 1.5, K1 0.01, K2 0.03)."""
    a = a.astype(np.float64)
    b = b.astype(np.float64)
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    blur = lambda z: cv2.GaussianBlur(z, (11, 11), 1.5)  # noqa: E731
    out = []
    for ch in range(a.shape[-1]):
        x, y = a[..., ch], b[..., ch]
        mx, my = blur(x), blur(y)
        sxx, syy, sxy = blur(x * x) - mx * mx, blur(y * y) - my * my, blur(x * y) - mx * my
        s = ((2 * mx * my + c1) * (2 * sxy + c2)) / ((mx * mx + my * my + c1) * (sxx + syy + c2))
        out.append(s[5:-5, 5:-5].mean())
    return float(np.mean(out))


def horizontal_shift(reference_rgb: np.ndarray, moved_rgb: np.ndarray) -> float:
    """How far `moved` sits to the right of `reference`, in pixels (phase correlation, Hann window).
    A left yaw moves the far scene right in the image: +f * dyaw pixels."""
    g0 = cv2.cvtColor(reference_rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
    g1 = cv2.cvtColor(moved_rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
    window = cv2.createHanningWindow(g0.shape[::-1], cv2.CV_32F)
    (dx, _dy), _response = cv2.phaseCorrelate(g0, g1, window)
    return float(dx)


NARROW_FOCAL_PX = 455.0              # the narrow view's focal length at 256x128
WIDE_FOCAL_PX = 227.5
