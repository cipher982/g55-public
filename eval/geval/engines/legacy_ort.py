"""ONNX Runtime sessions for the historical openpilot models (engines/legacy.py), with one numerical fix.

comma's models from 0.9.5 to 0.11.1 are float16 end to end and end the vision trunk with an L2 normalisation
of the 512-wide feature (Mul -> ReduceSum -> Sqrt, or ReduceL2). In float16 that sum of squares overflows on
some frames; ONNX Runtime then returns a zero feature (the norm is inf), the history buffer fills with zeros
and the plan freezes for seconds. comma's devices do not show this: their references (comma's CI model replay)
never freeze. Here the sum of squares and the square root run in float32 and the norm is cast back to float16;
every other operation is the file's own. legacy_validate.py measures the result against those references.

The ONNX file's sha256 is of the file as published, before this change and the tinygrad-hint rewrite
(org.tinygrad::Contiguous -> Identity) that engines/ort_backend.py also makes.
"""
from __future__ import annotations

import base64
import hashlib
import pickle
from pathlib import Path

import numpy as np

FLOAT, FLOAT16 = 1, 10


def _strip_tinygrad(model):
    for node in model.graph.node:
        if node.domain == "org.tinygrad":
            if node.op_type != "Contiguous":
                raise RuntimeError(f"unhandled tinygrad op {node.op_type}")
            node.op_type, node.domain = "Identity", ""
            del node.attribute[:]
    opsets = [o for o in model.opset_import if o.domain != "org.tinygrad"]
    del model.opset_import[:]
    model.opset_import.extend(opsets)


def fp32_l2_norms(model):
    """Run every L2-norm's sum of squares and square root in float32. Returns how many norms were changed."""
    from onnx import helper
    g = model.graph
    prod = {o: n for n in g.node for o in n.output}
    before, after, changed = {}, {}, set()       # node name -> nodes to insert before / after it
    n_fixed = 0
    for n in g.node:
        if n.op_type == "Sqrt":
            rs = prod.get(n.input[0])
            mul = prod.get(rs.input[0]) if rs is not None and rs.op_type == "ReduceSum" else None
            if mul is None or mul.op_type != "Mul" or mul.input[0] != mul.input[1]:
                continue
            x = mul.input[0]
            before[id(mul)] = [helper.make_node("Cast", [x], [x + "/f32"], to=FLOAT)]
            mul.input[0] = mul.input[1] = x + "/f32"
            changed |= {mul.output[0], rs.output[0]}
            last = n
        elif n.op_type == "ReduceL2":
            x = n.input[0]
            before[id(n)] = [helper.make_node("Cast", [x], [x + "/f32"], to=FLOAT)]
            n.input[0] = x + "/f32"
            last = n
        else:
            continue
        out = last.output[0]
        last.output[0] = out + "/f32"
        changed.add(out + "/f32")
        after[id(last)] = [helper.make_node("Cast", [out + "/f32"], [out], to=FLOAT16)]
        n_fixed += 1
    if not n_fixed:
        return 0
    nodes = []
    for n in g.node:
        nodes += before.get(id(n), []) + [n] + after.get(id(n), [])
    del g.node[:]
    g.node.extend(nodes)
    keep = [v for v in g.value_info if v.name not in changed]   # their declared float16 type no longer holds
    del g.value_info[:]
    g.value_info.extend(keep)
    return n_fixed


class Backend:
    """One historical ONNX file: input_shapes {name: (shape, dtype)}, output_slices, output_size, sha256;
    called with a dict of inputs, returns the flat output vector (float32)."""

    TYPES = {"tensor(float16)": np.float16, "tensor(float)": np.float32, "tensor(uint8)": np.uint8}

    def __init__(self, path, provider="cpu"):
        import onnx
        import onnxruntime as ort
        path = Path(path)
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 24), b""):
                digest.update(block)
        self.sha256 = digest.hexdigest()
        model = onnx.load(str(path))
        meta = {p.key: p.value for p in model.metadata_props}
        self.output_slices = pickle.loads(base64.b64decode(meta["output_slices"]))
        self.checkpoint = meta.get("model_checkpoint")
        _strip_tinygrad(model)
        self.l2_norms_fixed = fp32_l2_norms(model)
        if provider == "cuda":
            ort.preload_dlls(directory="")
            providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        elif provider == "cpu":
            providers = ["CPUExecutionProvider"]
        else:
            raise ValueError(f"unknown ONNX Runtime provider {provider!r}")
        self.session = ort.InferenceSession(model.SerializeToString(), providers=providers)
        if provider == "cuda" and "CUDAExecutionProvider" not in self.session.get_providers():
            raise RuntimeError("ONNX Runtime did not enable CUDAExecutionProvider")
        self.feeds = [(v.name, np.dtype(self.TYPES[v.type])) for v in self.session.get_inputs()]
        self.input_shapes = {v.name: (tuple(v.shape), np.dtype(self.TYPES[v.type])) for v in self.session.get_inputs()}
        self.output_name = self.session.get_outputs()[0].name
        self.output_size = int(self.session.get_outputs()[0].shape[1])

    def __call__(self, inputs):
        feed = {n: np.ascontiguousarray(inputs[n], dtype=d) for n, d in self.feeds}
        return self.session.run([self.output_name], feed)[0]


def load(spec, provider="cpu"):
    """`name=a.onnx` or `name=vision.onnx+policy.onnx` -> (name, backends)."""
    name, files = spec.split("=", 1)
    return name, tuple(Backend(p, provider) for p in files.split("+"))
