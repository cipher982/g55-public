"""openpilot's big driving models on one or two cameras: warp, history, model, parse.

The host side of openpilot's modeld, for two model contracts. Both warp the narrow NV12 into
`img` and the wide view into `big_img` (nearest neighbour, 512x256 Y + 256x128 U/V, packed as
6 channels of 128x256), feed the desire as a rising-edge pulse, and parse the same output heads.

0.11.2's 877M model (release-chestnut: compile_modeld.py make_warp/make_run_policy, modeld.py
ModelState.run): the host keeps the history. Per 20 Hz frame
  1. shift each view into a 5-frame queue and feed frames t-4 and t (5 Hz context);
  2. feed the desire pulse through a 100-step queue max-pooled by 4, and the model's own
     previous hidden state through a 96-step queue sampled by 4;
  3. run the model, slice and parse its 2580 outputs, keep `hidden_state` for next frame.

The history-in-graph contract (openpilot master since 64f9b47b6e, 2026-09-15: Cinque v2 and
every model on huggingface.co/commaai/openpilot_driving_models): the host feeds this frame's two
views as `new_img` [2, 6, 128, 256], the pulse as `desire` [8], and each `state_*` input the
graph returned as `next_state_*` on the previous frame (zeros when cold). Inputs a model has
beyond these (the N16 nav model's `nav_map`, `navigation`) are fed zeros: no route.

With one camera both views use the narrow frame. An optional wide calibration and NV12 frame
give `big_img` its own intrinsics and lens model, including fisheye.

Backends: TensorRT (the Jetson) or tinygrad on the untouched ONNX (reference, any host); each
exposes `input_shapes` {name: (shape, dtype)} and `output_names`.
"""
import base64, ctypes, json, pickle, time
import numpy as np
import op011 as op

MODEL_W, MODEL_H = op.MEDMODEL_INPUT_SIZE           # 512 x 256

def ar0234_intrinsics(width=1920, height=1200, hfov_deg=85.0):
  """Pinhole estimate from the nominal field of view; replace with a calibration."""
  f = (width / 2) / np.tan(np.radians(hfov_deg) / 2)
  return np.array([[f, 0, width / 2], [0, f, height / 2], [0, 0, 1.0]])


# How a camera is mounted about its optical axis. Recorded frames are always the camera's
# own bytes; a consumer turns them upright with `upright` and uses `upright_intrinsics`.
IMAGE_ROTATIONS_DEG = (0, 180)


def _check_rotation(rotation_deg):
  if rotation_deg not in IMAGE_ROTATIONS_DEG:
    raise ValueError(f"image rotation must be one of {IMAGE_ROTATIONS_DEG} degrees, got {rotation_deg!r}")
  return int(rotation_deg)


def upright(image, rotation_deg):
  """The image as an upright camera would have captured it (H x W or H x W x C)."""
  if _check_rotation(rotation_deg) == 0:
    return image
  return np.ascontiguousarray(image[::-1, ::-1])


def upright_intrinsics(K, dist, width, height, rotation_deg, model="pinhole"):
  """K and distortion of the `upright` image, from the raw image's calibration.

  A 180 degree turn maps pixel (u, v) to (W-1-u, H-1-v) and normalized coordinates (x, y)
  to (-x, -y): the principal point moves to (W-1-cx, H-1-cy); radial terms and fisheye
  coefficients are unchanged, the pinhole tangential p1, p2 change sign, skew is unchanged."""
  K = np.array(K, float)
  if _check_rotation(rotation_deg) == 0:
    return K, dist
  K[0, 2], K[1, 2] = width - 1 - K[0, 2], height - 1 - K[1, 2]
  if dist is None or model == "fisheye":
    return K, dist
  d = [float(x) for x in np.asarray(dist, float).reshape(-1)]
  if len(d) >= 4:
    d[2], d[3] = -d[2], -d[3]
  return K, d


class Warp:
  """Lookup-table form of comma's warp_perspective_tinygrad: round, clip, gather.

  With pinhole `dist` (OpenCV k1, k2, p1, p2[, k3]) or `model="fisheye"` and its four
  OpenCV fisheye coefficients, each model ray is mapped through the calibrated lens. Without
  distortion the pinhole lookup is exactly comma's."""
  def __init__(self, cam_w, cam_h, M, K=None, dist=None, model="pinhole"):
    self.cam_w, self.cam_h = cam_w, cam_h
    if model == "fisheye":
      D = np.zeros(4, np.float64) if dist is None else np.asarray(dist, np.float64).reshape(-1)
      self.y_idx = self._lut_fisheye(M, K, D, MODEL_W, MODEL_H, 1, cam_w, cam_h)
      self.uv_idx = self._lut_fisheye(M, K, D, MODEL_W // 2, MODEL_H // 2, 2, cam_w // 2, cam_h // 2)
    elif model == "pinhole":
      if dist is None:
        M_uv = M * np.array([[1.0, 1.0, 0.5], [1.0, 1.0, 0.5], [2.0, 2.0, 1.0]])
        self.y_idx = self._lut(M, MODEL_W, MODEL_H, cam_w, cam_h)
        self.uv_idx = self._lut(M_uv, MODEL_W // 2, MODEL_H // 2, cam_w // 2, cam_h // 2)
      else:
        self.y_idx = self._lut_dist(M, K, np.asarray(dist, np.float64), MODEL_W, MODEL_H, 1, cam_w, cam_h)
        self.uv_idx = self._lut_dist(M, K, np.asarray(dist, np.float64), MODEL_W // 2, MODEL_H // 2, 2, cam_w // 2, cam_h // 2)
    else:
      raise ValueError(f"unsupported camera model: {model}")
    self.real = view_share(M, K, dist, model, cam_w, cam_h)

  @staticmethod
  def _lut(M, w_dst, h_dst, w_src, h_src):
    x, y = np.meshgrid(np.arange(w_dst, dtype=np.float32), np.arange(h_dst, dtype=np.float32))
    M = M.astype(np.float32)
    sx = M[0, 0] * x + M[0, 1] * y + M[0, 2]; sy = M[1, 0] * x + M[1, 1] * y + M[1, 2]; sw = M[2, 0] * x + M[2, 1] * y + M[2, 2]
    xi = np.clip(np.round(sx / sw), 0, w_src - 1).astype(np.int64); yi = np.clip(np.round(sy / sw), 0, h_src - 1).astype(np.int64)
    return yi * w_src + xi

  @staticmethod
  def source_px(M, K, dist, model, x, y):
    """Camera pixel (u, v), unrounded and unclipped, of model pixels (x, y) at full resolution."""
    p = np.einsum('ij,jhw->ihw', M, np.stack([x, y, np.ones_like(x)]))
    u, v = p[0] / p[2], p[1] / p[2]                                   # pinhole camera pixel
    if model == "fisheye":
      D = np.zeros(4, np.float64) if dist is None else np.asarray(dist, np.float64).reshape(-1)
      yn = (v - K[1, 2]) / K[1, 1]
      xn = (u - K[0, 2] - K[0, 1] * yn) / K[0, 0]
      r = np.hypot(xn, yn); theta = np.arctan(r); t2 = theta * theta
      k1, k2, k3, k4 = D
      theta_d = theta * (1 + k1 * t2 + k2 * t2**2 + k3 * t2**3 + k4 * t2**4)
      factor = np.divide(theta_d, r, out=np.ones_like(r), where=r > 1e-12)
      xd, yd = xn * factor, yn * factor
      return K[0, 0] * xd + K[0, 1] * yd + K[0, 2], K[1, 1] * yd + K[1, 2]
    if dist is None:
      return u, v
    dist = np.asarray(dist, np.float64)
    xn, yn = (u - K[0, 2]) / K[0, 0], (v - K[1, 2]) / K[1, 1]
    k1, k2, p1, p2 = dist[:4]; k3 = dist[4] if len(dist) > 4 else 0.0
    r2 = xn * xn + yn * yn; radial = 1 + k1 * r2 + k2 * r2 * r2 + k3 * r2 ** 3
    xd = xn * radial + 2 * p1 * xn * yn + p2 * (r2 + 2 * xn * xn); yd = yn * radial + p1 * (r2 + 2 * yn * yn) + 2 * p2 * xn * yn
    return K[0, 0] * xd + K[0, 2], K[1, 1] * yd + K[1, 2]

  @staticmethod
  def _lut_dist(M, K, dist, w_dst, h_dst, scale, w_src, h_src, model="pinhole"):
    """`scale` 2 is the half-resolution U/V plane: same geometry at full-res coordinates, halved."""
    x, y = np.meshgrid(np.arange(w_dst, dtype=np.float64) * scale, np.arange(h_dst, dtype=np.float64) * scale)
    u, v = Warp.source_px(M, K, dist, model, x, y)
    xi = np.clip(np.round(u / scale), 0, w_src - 1).astype(np.int64); yi = np.clip(np.round(v / scale), 0, h_src - 1).astype(np.int64)
    return yi * w_src + xi

  @staticmethod
  def _lut_fisheye(M, K, D, w_dst, h_dst, scale, w_src, h_src):
    """Map pinhole model rays through OpenCV's four-coefficient fisheye projection."""
    return Warp._lut_dist(M, K, D, w_dst, h_dst, scale, w_src, h_src, model="fisheye")

  def __call__(self, nv12):
    """nv12: flat uint8, Y plane cam_w*cam_h then interleaved UV. Returns (6, 128, 256) uint8."""
    w, h = self.cam_w, self.cam_h
    yp = nv12[:w * h]; uv = nv12[w * h:w * h * 3 // 2].reshape(h // 2, w // 2, 2)
    Y = yp[self.y_idx]                                   # (256, 512)
    U = uv[..., 0].reshape(-1)[self.uv_idx]; V = uv[..., 1].reshape(-1)[self.uv_idx]   # (128, 256)
    return np.stack([Y[0::2, 0::2], Y[1::2, 0::2], Y[0::2, 1::2], Y[1::2, 1::2], U, V])



def view_share(M, K, dist, model, cam_w, cam_h, step=2):
  """The share of a model view's pixels that come from inside the camera image; the rest are the
  image's edge pixels repeated by the warp's clip. `step` samples every step-th model pixel."""
  x, y = np.meshgrid(np.arange(0, MODEL_W, step, dtype=np.float64), np.arange(0, MODEL_H, step, dtype=np.float64))
  K = None if K is None else np.asarray(K, np.float64)
  u, v = Warp.source_px(np.asarray(M, np.float64), K, dist, model, x, y)
  u, v = np.round(u), np.round(v)
  return float(np.mean((u >= 0) & (u <= cam_w - 1) & (v >= 0) & (v <= cam_h - 1)))


class ModelState:
  def __init__(self, backend, output_slices, cam_w, cam_h, intrinsics, device_from_calib_euler=(0, 0, 0),
               wide_camera=None, dist=None, device_from_wide_euler=(0, 0, 0), road_plane=None):
    """wide_camera is a separate camera.json dict. `device_from_wide_euler` is its fixed
    rotation relative to the narrow calibrated frame, composed with the live narrow pose;
    identity means aligned. Without wide_camera both views use the narrow camera.
    `road_plane=(source_height_m, target_height_m)` makes both views' lookup maps exact for
    that road plane by viewing the source image from a virtual camera at target height."""
    self.backend, self.slices = backend, output_slices
    # History-in-graph contract: the state inputs are those the graph also returns as next_*.
    # A backend that declares no input shapes (the training and test backends) is 0.11.2's.
    shapes = getattr(backend, 'input_shapes', {})
    self.graph_state = 'new_img' in shapes
    self.state_names = [n for n in shapes if f'next_{n}' in backend.output_names] if self.graph_state else []
    self.zero_inputs = {n: np.zeros(s, d) for n, (s, d) in shapes.items()
                        if self.graph_state and n not in self.state_names
                        and n not in ('new_img', 'desire', 'traffic_convention', 'action_t')}
    self.dist = dist
    self.wide_camera = wide_camera
    self.device_from_wide_euler = np.asarray(device_from_wide_euler, float)
    self.road_plane = road_plane
    self.set_calibration(cam_w, cam_h, intrinsics, device_from_calib_euler)
    self.reset()
    self.input_tap = None   # a list to receive a copy of every model input (calibration, debugging)

  def set_calibration(self, cam_w, cam_h, K, rpy):
    """Rebuild both lookups after narrow mount calibration changes."""
    if self.road_plane is None:
      self.warp = Warp(cam_w, cam_h, op.get_warp_matrix(np.array(rpy, float), K, False), K, self.dist)
    else:
      self.warp = Warp(cam_w, cam_h, op.get_warp_matrix(np.array(rpy, float), K, False, self.road_plane),
                       K, self.dist)
    if self.wide_camera is None:
      if self.road_plane is None:
        self.big_warp = Warp(cam_w, cam_h, op.get_warp_matrix(np.array(rpy, float), K, True), K, self.dist)
      else:
        self.big_warp = Warp(cam_w, cam_h, op.get_warp_matrix(np.array(rpy, float), K, True, self.road_plane),
                             K, self.dist)
    else:
      wide = self.wide_camera
      wide_K = np.asarray(wide['K'], float)
      wide_rotation = op.rot_from_euler(np.array(rpy, float)) @ op.rot_from_euler(self.device_from_wide_euler)
      if self.road_plane is None:
        wide_M = op.get_warp_matrix_from_rotation(wide_rotation, wide_K, True)
      else:
        wide_M = op.get_warp_matrix_from_rotation(wide_rotation, wide_K, True, self.road_plane)
      self.big_warp = Warp(wide['width'], wide['height'], wide_M, wide_K, wide.get('dist'),
                           wide.get('model', 'pinhole'))

  def real_share(self):
    """The share of each model view taken from inside its camera's image, under the current calibration."""
    return {'narrow': round(self.warp.real, 3), 'wide': round(self.big_warp.real, 3)}

  def reset(self):
    """Empty every history queue, as modeld does after warm-up: the model starts cold."""
    k = op.FRAME_SKIP
    self.img_q = np.zeros((k * 1 + 1, 6, 128, 256), np.uint8); self.big_img_q = np.zeros_like(self.img_q)
    self.desire_q = np.zeros((k * 25, 1, op.DESIRE_LEN), np.float32)
    self.feat_q = np.zeros((k * 24, 1, 512), np.float32)
    self.state = {n: np.zeros(*self.backend.input_shapes[n]) for n in self.state_names}
    self.prev_desire = np.zeros(op.DESIRE_LEN, np.float32); self.prev_feat = np.zeros((1, 512), np.float32)
    self.prev_accel = self.prev_curvature = 0.0

  @staticmethod
  def _shift(q, new): q[:-1] = q[1:]; q[-1] = new

  def step(self, nv12, desire=None, rhd=False, action_t=(0.275, 0.525), v_ego=None, wide_nv12=None):
    """One 20 Hz frame. Returns (parsed outputs, (accel, curvature, should_stop)).

    With v_ego unknown (None) only acceleration is computed; curvature and the stop decision
    need vehicle speed. With `wide_camera`, supply its matching NV12 frame as `wide_nv12`."""
    k = op.FRAME_SKIP
    if self.wide_camera is not None and wide_nv12 is None:
      raise ValueError("wide_nv12 is required when wide_camera is configured")
    img, big_img = self.warp(nv12), self.big_warp(nv12 if wide_nv12 is None else wide_nv12)
    desire = np.zeros(op.DESIRE_LEN, np.float32) if desire is None else np.asarray(desire, np.float32)
    desire = desire.copy(); desire[0] = 0
    pulse = np.where(desire - self.prev_desire > .99, desire, 0); self.prev_desire[:] = desire
    traffic = np.zeros((1, 2), np.float32); traffic[0, int(rhd)] = 1
    if self.graph_state:
      inputs = {'new_img': np.stack([img, big_img]), 'desire': pulse.astype(np.float32), 'traffic_convention': traffic,
                'action_t': np.array([action_t], np.float32), **self.state, **self.zero_inputs}
    else:
      self._shift(self.img_q, img); self._shift(self.big_img_q, big_img)
      self._shift(self.desire_q, pulse.reshape(1, -1)); self._shift(self.feat_q, self.prev_feat)
      inputs = {
        'img': self.img_q[::k].reshape(1, 12, 128, 256),
        'big_img': self.big_img_q[::k].reshape(1, 12, 128, 256),
        'desire_pulse': self.desire_q.reshape(25, k, 1, op.DESIRE_LEN).max(1).reshape(1, 25, op.DESIRE_LEN),
        'traffic_convention': traffic,
        'action_t': np.array([action_t], np.float32),
        'features_buffer': self.feat_q[::k].reshape(1, 24, 512),
      }
    if self.input_tap is not None: self.input_tap.append({k: np.array(v) for k, v in inputs.items()})
    if self.graph_state:
      outputs = self.backend(inputs, every=True)
      raw = np.asarray(outputs['outputs'], np.float32).reshape(-1)
    else:
      raw = self.backend(inputs).astype(np.float32).reshape(-1)
    if not np.isfinite(raw).all():   # never feed a broken state back in (modeld drops such frames too)
      self.reset()
      return None, None
    if self.graph_state:
      self.state = {n: outputs[f'next_{n}'] for n in self.state_names}
    else:
      self.prev_feat[:] = raw[self.slices['hidden_state']]
    outs = op.parse_outputs({name: raw[np.newaxis, s] for name, s in self.slices.items()})
    if v_ego is None:
      accel = op.smooth_value(float(outs['action'][0, 1]), self.prev_accel, op.LONG_SMOOTH_SECONDS)
      curvature, stop = None, None
      self.prev_accel = accel
    else:
      accel, curvature, stop = op.action_from_outputs(outs, self.prev_accel, self.prev_curvature, v_ego)
      self.prev_accel, self.prev_curvature = accel, curvature
    outs['raw'] = raw
    return outs, (accel, curvature, stop)


def output_slices(path):
  """From the model's ONNX metadata, or from the .slices.json written beside its engine."""
  if path.endswith('.json'):
    return {k: slice(a, b) for k, (a, b) in json.load(open(path)).items()}
  import onnx
  meta = {p.key: p.value for p in onnx.load(path, load_external_data=False).metadata_props}
  return pickle.loads(base64.b64decode(meta['output_slices']))


def write_slices_json(onnx_path, out_path):
  json.dump({k: [s.start, s.stop] for k, s in output_slices(onnx_path).items()}, open(out_path, 'w'))


class TrtBackend:
  """A TensorRT engine replayed as one CUDA graph: copy inputs in, run, copy the outputs out.

  Host buffers are pinned (cudaMallocHost) so the copies are DMA, and the whole step is one
  graph launch. Inputs are cast to the engine's binding dtypes (uint8 images for the 877M after clean_onnx)
  by writing into the pinned buffers. A call returns the first output; with every=True, all of
  them by name (the ground engine's p_free and cls, since 2026-10-18).
  """
  def __init__(self, engine_path, priority=0):
    import tensorrt as trt
    from cuda import cudart
    self.cu = cu = cudart
    self.eng = trt.Runtime(trt.Logger(trt.Logger.ERROR)).deserialize_cuda_engine(open(engine_path, 'rb').read())
    self.ctx = self.eng.create_execution_context()
    if priority == 'high': priority = self._ck(cu.cudaDeviceGetStreamPriorityRange())[1]   # numerically lowest
    self.stream = self._ck(cu.cudaStreamCreateWithPriority(cu.cudaStreamNonBlocking, priority))
    self.inputs, self.outputs = {}, {}
    for i in range(self.eng.num_io_tensors):
      n = self.eng.get_tensor_name(i); dt = np.dtype(trt.nptype(self.eng.get_tensor_dtype(n))); shp = tuple(self.eng.get_tensor_shape(n))
      nbytes = int(np.prod(shp)) * dt.itemsize
      hptr = self._ck(cu.cudaMallocHost(nbytes)); dev = self._ck(cu.cudaMalloc(nbytes))
      host = np.ctypeslib.as_array((ctypes.c_uint8 * nbytes).from_address(int(hptr))).view(dt).reshape(shp)
      self.ctx.set_tensor_address(n, dev)
      if self.eng.get_tensor_mode(n) == trt.TensorIOMode.INPUT: self.inputs[n] = (host, dev, nbytes)
      else: self.outputs[n] = (host, dev, nbytes)
    self.output = next(iter(self.outputs.values()))
    self.input_shapes = {n: (host.shape, host.dtype) for n, (host, _, _) in self.inputs.items()}
    self.output_names = tuple(self.outputs)
    H2D, D2H = cu.cudaMemcpyKind.cudaMemcpyHostToDevice, cu.cudaMemcpyKind.cudaMemcpyDeviceToHost
    def enqueue():
      for host, dev, nb in self.inputs.values(): self._ck(cu.cudaMemcpyAsync(dev, host.ctypes.data, nb, H2D, self.stream))
      self.ctx.execute_async_v3(self.stream)
      for host, dev, nb in self.outputs.values(): self._ck(cu.cudaMemcpyAsync(host.ctypes.data, dev, nb, D2H, self.stream))
    enqueue(); self._ck(cu.cudaStreamSynchronize(self.stream))            # TensorRT allocates on first run
    self._ck(cu.cudaStreamBeginCapture(self.stream, cu.cudaStreamCaptureMode.cudaStreamCaptureModeThreadLocal))
    enqueue(); self.graph = self._ck(cu.cudaGraphInstantiate(self._ck(cu.cudaStreamEndCapture(self.stream)), 0))

  def _ck(self, r):
    err, *rest = r if isinstance(r, tuple) else (r,)
    if int(err) != 0: raise RuntimeError(f"CUDA error {err}")
    return rest[0] if len(rest) == 1 else (rest or None)

  def __call__(self, inputs, every=False):
    for n, (host, _, _) in self.inputs.items(): host[...] = inputs[n].reshape(host.shape)
    self._ck(self.cu.cudaGraphLaunch(self.graph, self.stream))
    # Poll rather than block in the driver, so other Python threads (the frame decoder)
    # run while the GPU works: a blocking sync is not guaranteed to release the GIL.
    while self.cu.cudaStreamQuery(self.stream)[0] == self.cu.cudaError_t.cudaErrorNotReady: time.sleep(0.0005)
    self._ck(self.cu.cudaStreamQuery(self.stream))
    if every: return {n: o[0].copy() for n, o in self.outputs.items()}
    return self.output[0].copy()


class TinygradBackend:
  """comma's runtime on the untouched ONNX; the reference."""
  def __init__(self, onnx_path, tinygrad_dir):
    import sys; sys.path.insert(0, tinygrad_dir)
    from tinygrad import Tensor
    from tinygrad.nn.onnx import OnnxRunner
    from tinygrad.dtype import _to_np_dtype
    self.Tensor, self.runner = Tensor, OnnxRunner(onnx_path)
    self.dtypes = {k: v.dtype for k, v in self.runner.graph_inputs.items()}
    self.input_shapes = {k: (tuple(v.shape), np.dtype(_to_np_dtype(v.dtype))) for k, v in self.runner.graph_inputs.items()}
    self.output_names = tuple(self.runner.graph_outputs)

  def __call__(self, inputs, every=False):
    from tinygrad.dtype import _to_np_dtype
    feed = {k: self.Tensor(np.ascontiguousarray(v).astype(_to_np_dtype(self.dtypes[k]))) for k, v in inputs.items()}
    outputs = self.runner(feed)
    if every: return {n: t.numpy() for n, t in outputs.items()}
    return next(iter(outputs.values())).numpy()
