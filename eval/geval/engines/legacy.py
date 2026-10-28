"""openpilot's driving models from 0.9.5 (2023-11) to 0.11.1 (2026-05): the host side of each release's modeld.

Vendored from github.com/commaai/openpilot (MIT) at the release tags named below. One model version is a
bundle of its warp, its input queues, its output parser and the code that turns outputs into a steering and
acceleration command; each "era" here is one such contract, read from that release's
selfdrive/modeld/{modeld.py, constants.py, parse_model_outputs.py, fill_model_msg.py},
selfdrive/modeld/models/commonmodel.cc, selfdrive/modeld/transforms/{transform.cl, loadyuv.cl},
common/mat.h, common/transformations/model.py and selfdrive/controls/lib/{drive_helpers.py,
lateral_planner.py}.

  era        releases        graph inputs (besides the two camera views)                command
  A_latsol   0.9.5           desire[100,8], traffic, lat_planner_state[4], nav[256+150],  lateral planner on the
                             features_buffer[99,512] (20 Hz, host-kept)                   model's lat_planner_solution,
                                                                                          get_lag_adjusted_curvature
  B_curv     0.9.6, 0.9.7    desire[100,8], traffic, lateral_control_params[2],           desired_curvature head
                             prev_desired_curv[100,1], (0.9.6: nav), features_buffer[99]
  C_split    0.9.8, 0.9.9    vision; policy: desire[25,8] (5 Hz, max-pooled), traffic,    desired_curvature head
                             lateral_control_params, prev_desired_curv[25], features[25]
  D_plan     0.10.0..0.11.1  vision; policy: desire(_pulse)[25,8], traffic, features[25]  modeld's get_action_from_model:
                                                                                          curvature and acceleration
                                                                                          from the plan at action_t

Common to all eras: both road cameras warped by the OpenCL bilinear warp (transform.cl, OpenCV fixed-point
weights) into the 512x256 medmodel (narrow) and sbigmodel (wide) frames with the narrow calibration only (no
wide-from-device correction), packed as 6 channels of 128x256 (loadyuv.cl), two frames per view: the previous
20 Hz frame (eras A, B; float16) or the frame 4 steps back (C, D; uint8). The desire is a rising-edge pulse of
step()'s `desire`; the replays pass none (no lane change requested, as the lineup's replay), legacy_validate.py
passes what the reference's DesireHelper chose. Navigation inputs are zero (navigation off). Before 0.9.5 the steering
command came from the acados lateral MPC in lateral_planner.py, not from the model or modeld, so 0.9.4 and
earlier are not run. Before 0.10.0 the acceleration command came from the longitudinal MPC (acc or blended
mode) in longitudinal_planner.py, so eras A to C have no acceleration command here.
"""
from __future__ import annotations

import numpy as np

# ---------------------------------------------------------------- constants.py (identical in every era)
IDX_N = 33
T_IDXS = np.array([10.0 * (i / 32) ** 2 for i in range(IDX_N)])
X_IDXS = np.array([192.0 * (i / 32) ** 2 for i in range(IDX_N)])
DESIRE_LEN, FEATURE_LEN, PLAN_WIDTH = 8, 512, 15
PLAN_MHP_N, PLAN_MHP_SELECTION, LEAD_MHP_N, LEAD_MHP_SELECTION = 5, 1, 2, 3
LEAD_TRAJ_LEN, LEAD_WIDTH, NUM_LANE_LINES, LANE_LINES_WIDTH = 6, 4, 4, 2
LAT_PLANNER_SOLUTION_WIDTH, DESIRED_CURV_WIDTH, POSE_WIDTH = 4, 1, 6
DT_MDL = 0.05
POSITION, VELOCITY, ACCELERATION, T_FROM_CURRENT_EULER, ORIENTATION_RATE = (slice(0, 3), slice(3, 6), slice(6, 9),
                                                                            slice(9, 12), slice(12, 15))
MODEL_W, MODEL_H = 512, 256
MEDMODEL_CY = 47.6

# ---------------------------------------------------------------- common/transformations/model.py
view_frame_from_device_frame = np.array([[0., 0., 1.], [1., 0., 0.], [0., 1., 0.]]).T
medmodel_intrinsics = np.array([[910.0, 0, 256.0], [0, 910.0, MEDMODEL_CY], [0, 0, 1]])
sbigmodel_intrinsics = np.array([[455.0, 0, 256.0], [0, 455.0, 0.5 * (256 + MEDMODEL_CY)], [0, 0, 1]])


def rot_from_euler(rpy):
    cx, sx = np.cos(rpy[0]), np.sin(rpy[0]); cy, sy = np.cos(rpy[1]), np.sin(rpy[1]); cz, sz = np.cos(rpy[2]), np.sin(rpy[2])
    return (np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]]) @ np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
            @ np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]]))


def _calib_from(model_intrinsics):
    return np.linalg.inv((model_intrinsics @ np.hstack((view_frame_from_device_frame, [[0], [0], [0]])))[:, :3])


def get_warp_matrix(device_from_calib_euler, intrinsics, bigmodel_frame):
    calib_from_model = _calib_from(sbigmodel_intrinsics if bigmodel_frame else medmodel_intrinsics)
    return intrinsics @ view_frame_from_device_frame @ rot_from_euler(device_from_calib_euler) @ calib_from_model


# ---------------------------------------------------------------- common/mat.h, transform.cl, loadyuv.cl
def transform_scale_buffer(M, s):
    """in_pt = (transform(out_pt/s + 0.5) - 0.5) * s, in float32 as mat3 is."""
    out = np.array([[1 / s, 0, 0.5], [0, 1 / s, 0.5], [0, 0, 1]], np.float32)
    inn = np.array([[s, 0, -0.5 * s], [0, s, -0.5 * s], [0, 0, 1]], np.float32)
    return (inn @ (M.astype(np.float32) @ out)).astype(np.float32)


def _sat_short(x):
    return np.clip(x, -32768, 32767)


class ClWarp:
    """transform.cl's warpPerspective as gather tables: per output pixel four source indices and four
    integer weights (INTER_BITS 5, INTER_REMAP_COEF_BITS 15, short saturation as in the kernel)."""

    def __init__(self, M, dst_w, dst_h, src_w, src_h, row_stride, px_stride, offset):
        M = np.asarray(M, np.float32).reshape(3, 3)
        dy, dx = np.mgrid[0:dst_h, 0:dst_w].astype(np.float32)
        X0 = M[0, 0] * dx + M[0, 1] * dy + M[0, 2]
        Y0 = M[1, 0] * dx + M[1, 1] * dy + M[1, 2]
        W = M[2, 0] * dx + M[2, 1] * dy + M[2, 2]
        W = np.where(W != 0, np.float32(32.0) / np.where(W != 0, W, 1), np.float32(0)).astype(np.float32)
        X = np.rint((X0 * W).astype(np.float32)).astype(np.int64)
        Y = np.rint((Y0 * W).astype(np.float32)).astype(np.int64)
        sx, sy = _sat_short(X >> 5), _sat_short(Y >> 5)
        x0, x1 = np.clip(sx, 0, src_w - 1), np.clip(sx + 1, 0, src_w - 1)
        y0, y1 = np.clip(sy, 0, src_h - 1), np.clip(sy + 1, 0, src_h - 1)
        idx = lambda yy, xx: yy * row_stride + offset + xx * px_stride  # noqa: E731
        self.i = np.stack([idx(y0, x0), idx(y0, x1), idx(y1, x0), idx(y1, x1)]).reshape(4, -1)
        tabx = (np.float32(1 / 32) * (X & 31)).astype(np.float32)
        taby = (np.float32(1 / 32) * (Y & 31)).astype(np.float32)
        one = np.float32(1)
        sc = np.float32(32768)
        w = [(one - taby) * (one - tabx), (one - taby) * tabx, taby * (one - tabx), taby * tabx]
        self.w = np.stack([_sat_short(np.rint((x * sc).astype(np.float32))) for x in w]).reshape(4, -1).astype(np.int64)
        self.shape = (dst_h, dst_w)

    def __call__(self, buf):
        v = buf[self.i].astype(np.int64)
        val = (v * self.w).sum(0)
        return np.clip((val + (1 << 14)) >> 15, 0, 255).astype(np.uint8).reshape(self.shape)


class Frame:
    """One camera into the model's YUV layout: Y (512x256) and U, V (256x128) by the warp, then loadyuv's
    6 channels: Y even rows even cols, Y odd rows even cols, Y even rows odd cols, Y odd rows odd cols, U, V."""

    def __init__(self, M, cam_w, cam_h):
        M = np.asarray(M, np.float32)
        Muv = transform_scale_buffer(M, 0.5)
        uv_off = cam_w * cam_h
        self.y = ClWarp(M, MODEL_W, MODEL_H, cam_w, cam_h, cam_w, 1, 0)
        self.u = ClWarp(Muv, MODEL_W // 2, MODEL_H // 2, cam_w // 2, cam_h // 2, cam_w, 2, uv_off)
        self.v = ClWarp(Muv, MODEL_W // 2, MODEL_H // 2, cam_w // 2, cam_h // 2, cam_w, 2, uv_off + 1)

    def __call__(self, nv12):
        Y = self.y(nv12)
        return np.stack([Y[0::2, 0::2], Y[1::2, 0::2], Y[0::2, 1::2], Y[1::2, 1::2], self.u(nv12), self.v(nv12)])


# ---------------------------------------------------------------- parse_model_outputs.py (union of eras)
def safe_exp(x):
    return np.exp(np.clip(x, -np.inf, 11))


def sigmoid(x):
    return 1. / (1. + safe_exp(-x))


def softmax(x, axis=-1):
    x = x - np.max(x, axis=axis, keepdims=True)
    x = safe_exp(x)
    return x / np.sum(x, axis=axis, keepdims=True)


def parse_mdn(outs, name, in_N=0, out_N=1, out_shape=()):
    raw = outs[name]
    raw = raw.reshape((raw.shape[0], max(in_N, 1), -1))
    n = (raw.shape[2] - out_N) // 2
    mu, std = raw[:, :, :n].copy(), safe_exp(raw[:, :, n:2 * n])
    if in_N > 1:
        w = np.zeros((raw.shape[0], in_N, out_N), dtype=raw.dtype)
        for i in range(out_N):
            w[:, :, i - out_N] = softmax(raw[:, :, i - out_N], axis=-1)
        if out_N == 1:
            for f in range(w.shape[0]):
                o = np.argsort(w[f][:, 0])[::-1]
                w[f], mu[f], std[f] = w[f][o], mu[f][o], std[f][o]
        mu_f = np.zeros((raw.shape[0], out_N, n), dtype=raw.dtype); std_f = np.zeros_like(mu_f)
        for f in range(w.shape[0]):
            for h in range(out_N):
                o = np.argsort(w[f, :, h])[::-1]
                mu_f[f, h], std_f[f, h] = mu[f, o[0]], std[f, o[0]]
    else:
        mu_f, std_f = mu, std
    final = (raw.shape[0], out_N) + tuple(out_shape) if out_N > 1 else (raw.shape[0],) + tuple(out_shape)
    outs[name], outs[name + '_stds'] = mu_f.reshape(final), std_f.reshape(final)


def parse(outs):
    """Every head the scorer and the validation read; MHP or not is decided by width, as 0.10+ does."""
    p = outs['plan']
    if p.shape[1] == 2 * IDX_N * PLAN_WIDTH:
        parse_mdn(outs, 'plan', 0, 0, (IDX_N, PLAN_WIDTH))
    else:
        parse_mdn(outs, 'plan', PLAN_MHP_N, PLAN_MHP_SELECTION, (IDX_N, PLAN_WIDTH))
    parse_mdn(outs, 'lane_lines', 0, 0, (NUM_LANE_LINES, IDX_N, LANE_LINES_WIDTH))
    if 'sim_pose' in outs:
        parse_mdn(outs, 'sim_pose', 0, 0, (POSE_WIDTH,))
    if 'lat_planner_solution' in outs:
        parse_mdn(outs, 'lat_planner_solution', 0, 0, (IDX_N, LAT_PLANNER_SOLUTION_WIDTH))
    if 'desired_curvature' in outs:
        parse_mdn(outs, 'desired_curvature', 0, 0, (DESIRED_CURV_WIDTH,))
    return outs


# ---------------------------------------------------------------- drive_helpers.py
MIN_SPEED, MAX_LATERAL_JERK, MAX_VEL_ERR, CONTROL_N = 1.0, 5.0, 5.0, 17
LAT_SMOOTH_SECONDS, LONG_SMOOTH_SECONDS, MIN_LAT_CONTROL_SPEED = 0.1, 0.3, 0.3     # 0.10.x modeld.py


def get_lag_adjusted_curvature(steer_actuator_delay, v_ego, psis, curvatures):
    """0.9.5 drive_helpers (curvature_rates are all 0 there)."""
    v_ego = max(MIN_SPEED, v_ego)
    delay = steer_actuator_delay + .2
    current = curvatures[0]
    psi = np.interp(delay, T_IDXS[:CONTROL_N], psis)
    desired = 2 * (psi / (v_ego * delay)) - current
    max_rate = MAX_LATERAL_JERK / (v_ego ** 2)
    return float(np.clip(desired, current - max_rate * DT_MDL, current + max_rate * DT_MDL))


def smooth_value(val, prev_val, tau, dt=DT_MDL):
    alpha = 1 - np.exp(-dt / tau) if tau > 0 else 1
    return alpha * val + (1 - alpha) * prev_val


def get_accel_from_plan(speeds, accels, action_t, v_stopping=0.05):
    v_now, a_now = speeds[0], accels[0]
    v_target = np.interp(action_t, T_IDXS, speeds)
    a_target = 2 * (v_target - v_now) / action_t - a_now
    v_target_1s = np.interp(action_t + 1.0, T_IDXS, speeds)
    return float(a_target), bool(v_target < v_stopping and v_target_1s < v_stopping)


def get_curvature_from_plan(yaws, yaw_rates, v_ego, action_t):
    psi_target = np.interp(action_t, T_IDXS, yaws)
    v_ego = np.clip(v_ego, MIN_SPEED, np.inf)
    return float(2 * psi_target / (v_ego * action_t) - yaw_rates[0] / v_ego)


# ---------------------------------------------------------------- the models
def era_of(input_names, policy_input_names=None):
    if policy_input_names is None:
        if 'lat_planner_state' in input_names:
            return 'A_latsol'
        if 'lateral_control_params' in input_names:
            return 'B_curv'
        raise ValueError(f"unknown supercombo contract: {input_names}")
    if 'lateral_control_params' in policy_input_names:
        return 'C_split'
    return 'D_plan'


class LegacyModel:
    """One historical model on one camera pair. `backends`: (supercombo,) or (vision, policy), each with
    `input_shapes` {name: (shape, dtype)} and `output_slices`; called with a dict of inputs, returning the flat
    output vector.

    step() returns (outs, cmd): `outs` the parsed heads (modelV2's sources), `cmd` {curvature, accel,
    should_stop} where accel is None before 0.10 (the MPC planned it then)."""

    def __init__(self, backends, cam_w, cam_h, K_narrow, K_wide, rpy_calib, steer_actuator_delay,
                 long_actuator_delay, lateral_delay=None, rhd=False):
        self.backends = backends
        if len(backends) == 1:
            self.era = era_of(backends[0].input_shapes)
        else:
            self.era = era_of(backends[0].input_shapes, backends[1].input_shapes)
        self.steer_actuator_delay = float(steer_actuator_delay)
        self.long_delay = float(long_actuator_delay) + LONG_SMOOTH_SECONDS
        # 0.10+: liveDelay.lateralDelay, which lagd starts at steerActuatorDelay + 0.2 until it has learned one
        self.lat_delay = (self.steer_actuator_delay + 0.2 if lateral_delay is None else float(lateral_delay)) + LAT_SMOOTH_SECONDS
        self.traffic = np.zeros((1, 2), np.float32); self.traffic[0, int(rhd)] = 1
        self.set_calibration(cam_w, cam_h, K_narrow, K_wide, rpy_calib)
        self.reset()

    @property
    def action_t(self):
        """(lateral, longitudinal) seconds ahead that the command is for. Before 0.10 there is no acceleration
        command (the MPC planned it); the longitudinal time is then route_replay's (actuator delay + 0.375 s),
        used only to read the plan."""
        if self.era == 'D_plan':
            return (self.lat_delay + DT_MDL, self.long_delay + DT_MDL)
        return (self.steer_actuator_delay + 0.2, self.long_delay - LONG_SMOOTH_SECONDS + 0.375)

    def set_calibration(self, cam_w, cam_h, K_narrow, K_wide, rpy):
        rpy = np.asarray(rpy, float)
        self.narrow = Frame(get_warp_matrix(rpy, np.asarray(K_narrow, float), False).astype(np.float32), cam_w, cam_h)
        self.wide = Frame(get_warp_matrix(rpy, np.asarray(K_wide, float), True).astype(np.float32), cam_w, cam_h)

    def reset(self):
        k = 1 if self.era in ('A_latsol', 'B_curv') else 4      # previous frame, or TEMPORAL_SKIP frames back
        self.img_q = np.zeros((k + 1, 6, 128, 256), np.uint8)
        self.big_q = np.zeros_like(self.img_q)
        self.prev_action = (0.0, 0.0)                              # (curvature, accel), modeld's prev_action
        if self.era in ('A_latsol', 'B_curv'):
            sh = self.backends[0].input_shapes
            self.state = {n: np.zeros(s, np.float32) for n, (s, _) in sh.items() if n not in ('input_imgs', 'big_input_imgs')}
        else:
            self.full_features = np.zeros((100, FEATURE_LEN), np.float32)
            self.full_desire = np.zeros((100, DESIRE_LEN), np.float32)
            self.full_prev_curv = np.zeros((100, 1), np.float32)
        self.prev_desire = np.zeros(DESIRE_LEN, np.float32)

    @staticmethod
    def _shift(q, new):
        q[:-1] = q[1:]; q[-1] = new

    def warp(self, nv12, wide_nv12):
        """This frame's two views (6 x 128 x 256 each); every legacy model with the same calibration shares them."""
        return self.narrow(nv12), self.wide(wide_nv12)

    def _pulse(self, desire):
        """modeld's rising-edge pulse of the desire index (0: none; the 'none' slot is always zeroed)."""
        vec = np.zeros(DESIRE_LEN, np.float32)
        if 0 < desire < DESIRE_LEN:
            vec[desire] = 1
        pulse = np.where(vec - self.prev_desire > .99, vec, 0).astype(np.float32)
        self.prev_desire[:] = vec
        return pulse

    def step(self, nv12, wide_nv12, v_ego, views=None, desire=0):
        """One 20 Hz frame. `desire`: the log.Desire index modeld would feed (DesireHelper's; 0 = none)."""
        self.pulse = self._pulse(desire)
        narrow, wide = self.warp(nv12, wide_nv12) if views is None else views
        self._shift(self.img_q, narrow); self._shift(self.big_q, wide)
        img = np.concatenate([self.img_q[0], self.img_q[-1]])[None]
        big = np.concatenate([self.big_q[0], self.big_q[-1]])[None]
        if self.era in ('A_latsol', 'B_curv'):
            outs = self._step_supercombo(img, big, v_ego)
        else:
            outs = self._step_split(img, big, v_ego)
        if outs is None:
            return None, None
        return outs, self._command(outs, v_ego)

    # eras A, B: modeld.py ModelState.run of 0.9.5 / 0.9.6 / 0.9.7
    def _step_supercombo(self, img, big, v_ego):
        be, st = self.backends[0], self.state
        st['desire'][0, :-1] = st['desire'][0, 1:]                   # 100 x 8 history of pulses
        st['desire'][0, -1] = self.pulse
        st['traffic_convention'][:] = self.traffic
        if 'lateral_control_params' in st:
            st['lateral_control_params'][:] = [v_ego, self.steer_actuator_delay + .2]
        raw = be({'input_imgs': img.astype(np.float32), 'big_input_imgs': big.astype(np.float32), **st}).reshape(-1).astype(np.float32)
        if not np.isfinite(raw).all():
            return None
        outs = parse({k: raw[np.newaxis, s] for k, s in be.output_slices.items()})
        st['features_buffer'][0, :-1] = st['features_buffer'][0, 1:]
        st['features_buffer'][0, -1] = outs['hidden_state'][0]
        if self.era == 'A_latsol':
            sol = outs['lat_planner_solution'][0]
            st['lat_planner_state'][0, 2] = np.interp(DT_MDL, T_IDXS, sol[:, 2])
            st['lat_planner_state'][0, 3] = np.interp(DT_MDL, T_IDXS, sol[:, 3])
        else:
            st['prev_desired_curv'][0, :-1] = st['prev_desired_curv'][0, 1:]
            st['prev_desired_curv'][0, -1] = outs['desired_curvature'][0]
        outs['raw'] = raw
        return outs

    # eras C, D: modeld.py ModelState.run of 0.9.8 .. 0.11.1 (vision, then policy)
    def _step_split(self, img, big, v_ego):
        vis, pol = self.backends
        vnames = list(vis.input_shapes)
        vraw = vis({vnames[0]: img, vnames[1]: big}).reshape(-1).astype(np.float32)
        vouts = {k: vraw[np.newaxis, s] for k, s in vis.output_slices.items()}
        self._shift(self.full_desire, self.pulse)
        self._shift(self.full_features, vouts['hidden_state'][0])
        idx = slice(-1 - 4 * 24, None, 4)
        feed = {'traffic_convention': self.traffic, 'features_buffer': self.full_features[idx][None]}
        desire = self.full_desire.reshape(25, 4, DESIRE_LEN).max(1)[None]
        feed['desire_pulse' if 'desire_pulse' in pol.input_shapes else 'desire'] = desire
        if self.era == 'C_split':
            feed['lateral_control_params'] = np.array([[v_ego, self.steer_actuator_delay + .2]], np.float32)
            feed['prev_desired_curv'] = self.full_prev_curv[idx][None]
        praw = pol(feed).reshape(-1).astype(np.float32)
        if not (np.isfinite(vraw).all() and np.isfinite(praw).all()):
            return None
        outs = {k: praw[np.newaxis, s] for k, s in pol.output_slices.items()}
        for k, v in vouts.items():
            outs.setdefault(k, v)
        outs = parse(outs)
        if self.era == 'C_split':
            self._shift(self.full_prev_curv, outs['desired_curvature'][0])
        outs['raw'] = np.concatenate([vraw, praw])
        return outs

    def _command(self, outs, v_ego):
        if self.era == 'A_latsol':
            # lateral_planner.py 0.9.5 then controlsd's get_lag_adjusted_curvature
            plan, sol = outs['plan'][0], outs['lat_planner_solution'][0]
            speed_err = float(np.clip(outs['sim_pose'][0, 0] - v_ego, -MAX_VEL_ERR, MAX_VEL_ERR))
            v_plan0 = max(float(np.linalg.norm(plan[0, VELOCITY])) - speed_err, MIN_SPEED)
            curv = get_lag_adjusted_curvature(self.steer_actuator_delay, v_ego, sol[:CONTROL_N, 2], sol[:CONTROL_N, 3] / v_plan0)
            return {'curvature': curv, 'accel': None, 'should_stop': None}
        if self.era in ('B_curv', 'C_split'):
            return {'curvature': float(outs['desired_curvature'][0, 0]), 'accel': None, 'should_stop': None}
        # D_plan: 0.10.x modeld.get_action_from_model
        plan = outs['plan'][0]
        lat_t, long_t = self.action_t
        accel, stop = get_accel_from_plan(plan[:, VELOCITY][:, 0], plan[:, ACCELERATION][:, 0], long_t)
        accel = smooth_value(accel, self.prev_action[1], LONG_SMOOTH_SECONDS)
        curv = get_curvature_from_plan(plan[:, T_FROM_CURRENT_EULER][:, 2], plan[:, ORIENTATION_RATE][:, 2], v_ego, lat_t)
        curv = smooth_value(curv, self.prev_action[0], LAT_SMOOTH_SECONDS) if v_ego > MIN_LAT_CONTROL_SPEED else self.prev_action[0]
        self.prev_action = (curv, accel)
        return {'curvature': float(curv), 'accel': float(accel), 'should_stop': stop}


# ---------------------------------------------------------------- the scorer's view
CANON = {'plan': 2 * IDX_N * PLAN_WIDTH, 'lane_lines': 2 * NUM_LANE_LINES * IDX_N * LANE_LINES_WIDTH,
         'lane_lines_prob': 8}


def canonical(outs, slices):
    """The heads geval.openloop.parse_fields reads, in the single-hypothesis layout of 0.10+: the plan as the
    best hypothesis's [mean, log std]; lane lines, lead and their probabilities as the model's own raw values."""
    plan = np.concatenate([outs['plan'][0].reshape(-1), np.log(outs['plan_stds'][0]).reshape(-1)])
    lead = outs['raw'][slices['lead']]
    lead_prob = outs['raw'][slices['lead_prob']]
    lane = outs['raw'][slices['lane_lines']]
    lane_p = outs['raw'][slices['lane_lines_prob']]
    parts = [('plan', plan), ('lane_lines', lane), ('lane_lines_prob', lane_p), ('lead', lead), ('lead_prob', lead_prob)]
    vec, sl, a = [], {}, 0
    for name, v in parts:
        sl[name] = [a, a + len(v)]; a += len(v); vec.append(np.asarray(v, np.float32))
    return np.concatenate(vec), sl


def raw_slices(model):
    """Where each head sits in outs['raw'] (vision then policy for split models)."""
    if len(model.backends) == 1:
        return {k: slice(s.start, s.stop) for k, s in model.backends[0].output_slices.items()}
    vis, pol = model.backends
    nv = vis.output_size
    out = {k: slice(s.start, s.stop) for k, s in vis.output_slices.items() if s.stop is not None and s.stop > 0}
    for k, s in pol.output_slices.items():
        if k == 'pad':
            continue
        out[k] = slice(nv + s.start, nv + s.stop)
    return out
