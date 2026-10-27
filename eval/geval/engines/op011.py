"""The input/output contract of openpilot 0.11.2's big driving model, vendored from openpilot.

Source: github.com/commaai/openpilot, MIT licence, branch release-chestnut at
473eba53f280743d09c5bebc2d0613ea46883e02 (the 877M model's ONNX is at fa0c6876d3):
  selfdrive/modeld/constants.py, parse_model_outputs.py, compile_modeld.py (queues, warp),
  common/transformations/{camera,model,transformations}.py, controls/lib/drive_helpers.py.
Kept to what the 877M model needs. A model version is a bundle: these constants, slices,
the parser and the queue rules change with it, so this file is pinned to that commit.
"""
import numpy as np

# ---- constants.py -------------------------------------------------------------------
def _index(idx, max_val, max_idx=32): return max_val * ((idx / max_idx) ** 2)
IDX_N = 33
T_IDXS = np.array([_index(i, 10.0) for i in range(IDX_N)])
X_IDXS = np.array([_index(i, 192.0) for i in range(IDX_N)])
LEAD_T_IDXS = [0., 2., 4., 6., 8., 10.]
MODEL_RUN_FREQ, MODEL_CONTEXT_FREQ = 20, 5
FRAME_SKIP = MODEL_RUN_FREQ // MODEL_CONTEXT_FREQ        # 4: history is sampled at 5 Hz
DESIRE_LEN, POSE_WIDTH, WIDE_FROM_DEVICE_WIDTH = 8, 6, 3
LEAD_WIDTH, LANE_LINES_WIDTH, PLAN_WIDTH = 4, 2, 15
NUM_LANE_LINES, NUM_ROAD_EDGES = 4, 2
LEAD_TRAJ_LEN, DESIRE_PRED_LEN, DESIRE_PRED_WIDTH = 6, 4, 8
LEAD_MHP_N, LEAD_MHP_SELECTION = 2, 3

class Plan:
  POSITION = slice(0, 3); VELOCITY = slice(3, 6); ACCELERATION = slice(6, 9)
  T_FROM_CURRENT_EULER = slice(9, 12); ORIENTATION_RATE = slice(12, 15)

# ---- parse_model_outputs.py ---------------------------------------------------------
def safe_exp(x, out=None): return np.exp(np.clip(x, -np.inf, 11), out=out)
def sigmoid(x): return 1. / (1. + safe_exp(-x))
def softmax(x, axis=-1):
  x = x - np.max(x, axis=axis, keepdims=True); x = safe_exp(x)
  return x / np.sum(x, axis=axis, keepdims=True)

def _mdn(outs, name, in_N=0, out_N=1, out_shape=()):
  raw = outs[name]; raw = raw.reshape((raw.shape[0], max(in_N, 1), -1))
  n = (raw.shape[2] - out_N) // 2
  mu, std = raw[:, :, :n], safe_exp(raw[:, :, n:2 * n])
  if in_N > 1:
    w = np.zeros((raw.shape[0], in_N, out_N), dtype=raw.dtype)
    for i in range(out_N): w[:, :, i - out_N] = softmax(raw[:, :, i - out_N], axis=-1)
    if out_N == 1:
      for f in range(w.shape[0]):
        o = np.argsort(w[f][:, 0])[::-1]; w[f], mu[f], std[f] = w[f][o], mu[f][o], std[f][o]
    full = (raw.shape[0], in_N) + tuple(out_shape)
    outs[name + '_weights'], outs[name + '_hypotheses'], outs[name + '_stds_hypotheses'] = w, mu.reshape(full), std.reshape(full)
    mu_f = np.zeros((raw.shape[0], out_N, n), dtype=raw.dtype); std_f = np.zeros_like(mu_f)
    for f in range(w.shape[0]):
      for h in range(out_N):
        o = np.argsort(w[f, :, h])[::-1]; mu_f[f, h], std_f[f, h] = mu[f, o[0]], std[f, o[0]]
  else:
    mu_f, std_f = mu, std
  final = (raw.shape[0], out_N) + tuple(out_shape) if out_N > 1 else (raw.shape[0],) + tuple(out_shape)
  outs[name], outs[name + '_stds'] = mu_f.reshape(final), std_f.reshape(final)

def parse_outputs(outs):
  _mdn(outs, 'pose', out_N=0, out_shape=(POSE_WIDTH,))
  _mdn(outs, 'wide_from_device_euler', out_N=0, out_shape=(WIDE_FROM_DEVICE_WIDTH,))
  _mdn(outs, 'road_transform', out_N=0, out_shape=(POSE_WIDTH,))
  _mdn(outs, 'lane_lines', out_N=0, out_shape=(NUM_LANE_LINES, IDX_N, LANE_LINES_WIDTH))
  _mdn(outs, 'road_edges', out_N=0, out_shape=(NUM_ROAD_EDGES, IDX_N, LANE_LINES_WIDTH))
  outs['lane_lines_prob'] = sigmoid(outs['lane_lines_prob'])
  outs['desire_pred'] = softmax(outs['desire_pred'].reshape((-1, DESIRE_PRED_LEN, DESIRE_PRED_WIDTH)))
  outs['meta'] = sigmoid(outs['meta'])
  outs['lead_prob'] = sigmoid(outs['lead_prob'])
  mhp = outs['lead'].shape[1] != 2 * LEAD_MHP_SELECTION * LEAD_TRAJ_LEN * LEAD_WIDTH
  if mhp: _mdn(outs, 'lead', LEAD_MHP_N, LEAD_MHP_SELECTION, (LEAD_TRAJ_LEN, LEAD_WIDTH))
  else:   _mdn(outs, 'lead', 0, 0, (LEAD_MHP_SELECTION, LEAD_TRAJ_LEN, LEAD_WIDTH))
  _mdn(outs, 'plan', out_N=0, out_shape=(IDX_N, PLAN_WIDTH))
  outs['desire_state'] = softmax(outs['desire_state'].reshape((-1, DESIRE_PRED_WIDTH)))
  return outs

# ---- transformations ----------------------------------------------------------------
def rot_from_euler(rpy):
  cx, sx = np.cos(rpy[0]), np.sin(rpy[0]); cy, sy = np.cos(rpy[1]), np.sin(rpy[1]); cz, sz = np.cos(rpy[2]), np.sin(rpy[2])
  Rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]]); Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
  Rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
  return Rz @ Ry @ Rx

view_frame_from_device_frame = np.array([[0., 0., 1.], [1., 0., 0.], [0., 1., 0.]]).T
MEDMODEL_INPUT_SIZE, MEDMODEL_CY = (512, 256), 47.6
medmodel_intrinsics = np.array([[910.0, 0, 256.0], [0, 910.0, MEDMODEL_CY], [0, 0, 1]])
sbigmodel_intrinsics = np.array([[455.0, 0, 256.0], [0, 455.0, 0.5 * (256 + MEDMODEL_CY)], [0, 0, 1]])

def _calib_from(model_intrinsics):
  view_from_calib = view_frame_from_device_frame @ rot_from_euler([0, 0, 0])
  return np.linalg.inv((model_intrinsics @ np.hstack((view_from_calib, [[0], [0], [0]])))[:, :3])

def get_warp_matrix(device_from_calib_euler, intrinsics, bigmodel_frame, road_plane=None):
  """camera pixel <- model pixel; optional road-plane projection correction."""
  return get_warp_matrix_from_rotation(rot_from_euler(device_from_calib_euler), intrinsics, bigmodel_frame,
                                       road_plane)


def get_warp_matrix_from_rotation(device_from_calib_rotation, intrinsics, bigmodel_frame, road_plane=None):
  """Return the camera-pixel <- model-pixel matrix, optionally exact on the road plane.

  For road_plane=(source_height_m, target_height_m), let V be view_frame_from_device_frame,
  R the live device_from_calib rotation, and n = V R [0, 0, -1] the road normal in optical
  coordinates (device z points down). The virtual target camera is translated from the source
  camera by t = (target_height_m - source_height_m) n. In target-camera coordinates the road is
  n.T X + d = 0, with d=target_height_m, so a target ray r intersects it at
  X = -d r/(n.T r). Expressing that point from the source camera gives the plane homography
  H = I - t n.T/d. Thus M = K_src H V R V.T K_model^-1. `Warp` applies the existing lens
  distortion after this pinhole mapping, so the same construction works for pinhole and fisheye.
  """
  model_intrinsics = sbigmodel_intrinsics if bigmodel_frame else medmodel_intrinsics
  calib_from_model = _calib_from(model_intrinsics)
  view_from_calib = view_frame_from_device_frame @ device_from_calib_rotation
  if road_plane is None:
    return intrinsics @ view_from_calib @ calib_from_model
  source_height_m, target_height_m = road_plane
  road_normal_calib = np.array([0., 0., -1.])
  road_normal_view = view_from_calib @ road_normal_calib
  target_from_source = (target_height_m - source_height_m) * road_normal_view
  plane_homography = np.eye(3) - np.outer(target_from_source, road_normal_view) / target_height_m
  return intrinsics @ plane_homography @ view_from_calib @ calib_from_model

# ---- drive_helpers.py (lateral/longitudinal action) -----------------------------------
DT_MDL = 0.05
LAT_SMOOTH_SECONDS, LONG_SMOOTH_SECONDS, MIN_LAT_CONTROL_SPEED = 0.0, 0.3, 0.3   # modeld.py

def should_stop(v_ego, a_target): return bool(v_ego < 0.3 and a_target < 0.1)

def smooth_value(val, prev_val, tau, dt=DT_MDL):
  alpha = 1 - np.exp(-dt / tau) if tau > 0 else 1
  return alpha * val + (1 - alpha) * prev_val

def action_from_outputs(outs, prev_accel, prev_curvature, v_ego):
  """modeld.get_action_from_model for a model with an `action` head (the 877M has one)."""
  accel = float(outs['action'][0, 1]); curvature = float(outs['action'][0, 0]) / max(1.0, v_ego) ** 2
  stop = should_stop(v_ego, accel)
  accel = smooth_value(accel, prev_accel, LONG_SMOOTH_SECONDS)
  curvature = smooth_value(curvature, prev_curvature, LAT_SMOOTH_SECONDS) if v_ego > MIN_LAT_CONTROL_SPEED else prev_curvature
  return accel, curvature, stop
