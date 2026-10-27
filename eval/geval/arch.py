"""Parameters and multiply-accumulates of a driving-model ONNX, counted from the graph (derived).

params: distinct learned tensors (float initializers, each distinct content counted once, so a weight
stored twice counts once). MACs: Conv, MatMul and Gemm per forward pass at batch 1, counted each time
a weight is applied (as the 2026-10-27 architecture report counted them). Also reports whether the
graph has an RL residual head and the history-in-graph contract.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np


def count(onnx_path: Path) -> dict:
    import onnx
    from onnx import TensorProto, numpy_helper, shape_inference

    m = onnx.load(str(onnx_path))
    g = m.graph
    float_types = {TensorProto.FLOAT, TensorProto.FLOAT16, TensorProto.BFLOAT16}
    seen, params, stored = set(), 0, 0
    init_shape = {}
    for t in g.initializer:
        init_shape[t.name] = list(t.dims)
        if t.data_type not in float_types:
            continue
        n = int(np.prod(t.dims)) if t.dims else 1
        stored += n
        h = hashlib.sha1(numpy_helper.to_array(t).tobytes()).hexdigest() + str(list(t.dims))
        if h not in seen:
            seen.add(h)
            params += n
    names = [t.name for t in g.initializer]
    for t in g.initializer:                      # shapes only: drop the data before inference
        t.ClearField("raw_data")
        for f in ("float_data", "int32_data", "int64_data", "double_data"):
            t.ClearField(f)
    del g.initializer[:]
    for name in names:
        g.input.append(onnx.helper.make_tensor_value_info(name, TensorProto.FLOAT, init_shape[name]))
    inferred = shape_inference.infer_shapes(m, strict_mode=False)
    shapes = {}
    for vi in list(inferred.graph.value_info) + list(inferred.graph.input) + list(inferred.graph.output):
        dims = [d.dim_value if d.HasField("dim_value") else 1 for d in vi.type.tensor_type.shape.dim]
        shapes[vi.name] = dims
    shapes.update(init_shape)
    macs = 0
    for node in inferred.graph.node:
        if node.op_type == "Conv":
            w = shapes.get(node.input[1])
            out = shapes.get(node.output[0])
            if w and out:
                macs += int(np.prod(out)) * int(np.prod(w[1:]))
        elif node.op_type in ("MatMul", "Gemm"):
            a, b, out = shapes.get(node.input[0]), shapes.get(node.input[1]), shapes.get(node.output[0])
            if not (a and b and out):
                continue
            trans_a = any(at.name == "transA" and at.i for at in node.attribute)
            k = a[-2] if trans_a else a[-1]
            macs += int(np.prod(out)) * int(k)
    inputs = {i.name for i in g.input}
    residual = any("residual" in n for n in names)
    return {"params_m": round(params / 1e6, 2), "params_stored_m": round(stored / 1e6, 2),
            "gmac": round(macs / 1e9, 1), "residual_head": residual, "history_in_graph": "new_img" in inputs,
            "nav_inputs": any(n in inputs for n in ("nav_map", "navigation"))}


def checkpoint_of(onnx_path: Path):
    import onnx
    meta = {p.key: p.value for p in onnx.load(str(onnx_path), load_external_data=False).metadata_props}
    return meta.get("model_checkpoint")


if __name__ == "__main__":
    import json
    import sys
    print(json.dumps(count(Path(sys.argv[1]))))
