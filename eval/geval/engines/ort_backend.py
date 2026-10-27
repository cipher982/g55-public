"""ONNX Runtime backend for runner.ModelState (CUDA or CPU), from the G55 project's replay_frames.py.

comma's ONNX carries one tinygrad layout hint (org.tinygrad::Contiguous) that ONNX Runtime cannot
load; it is rewritten to Identity in memory, nothing else changes.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np


class OnnxRuntimeBackend:
    def __init__(self, onnx_path: Path, provider: str = "cuda"):
        import onnx
        import onnxruntime as ort

        if provider == "cuda":
            if "CUDAExecutionProvider" not in ort.get_available_providers():
                raise RuntimeError("onnxruntime-gpu has no CUDAExecutionProvider")
            ort.preload_dlls(directory="")
            providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        elif provider == "cpu":
            providers = ["CPUExecutionProvider"]
        else:
            raise ValueError(f"unknown ONNX Runtime provider {provider!r}")
        model = onnx.load(str(onnx_path))
        for node in model.graph.node:
            if node.domain == "org.tinygrad":
                if node.op_type != "Contiguous":
                    raise RuntimeError(f"unhandled tinygrad op {node.op_type} in {onnx_path}")
                node.op_type, node.domain = "Identity", ""
                del node.attribute[:]
        opsets = [o for o in model.opset_import if o.domain != "org.tinygrad"]
        del model.opset_import[:]
        model.opset_import.extend(opsets)
        digest = hashlib.sha256()
        with open(onnx_path, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 24), b""):
                digest.update(block)
        self.sha256 = digest.hexdigest()
        self.output_names = tuple(o.name for o in model.graph.output)
        ort_types = {"tensor(float16)": np.float16, "tensor(float)": np.float32, "tensor(uint8)": np.uint8}
        session = ort.InferenceSession(model.SerializeToString(), providers=providers)
        self.feeds = [(v.name, np.dtype(ort_types[v.type])) for v in session.get_inputs()]
        self.input_shapes = {v.name: (tuple(v.shape), np.dtype(ort_types[v.type])) for v in session.get_inputs()}
        self.outputs = [v.name for v in session.get_outputs()]
        self.session = session
        self.providers = tuple(session.get_providers())
        if provider == "cuda" and "CUDAExecutionProvider" not in self.providers:
            raise RuntimeError(f"ONNX Runtime did not enable CUDAExecutionProvider: {self.providers}")

    def __call__(self, inputs: dict, every: bool = False):
        values = dict(inputs)
        results = self.session.run(self.outputs, {n: np.ascontiguousarray(values[n], dtype=d) for n, d in self.feeds})
        values.update(zip(self.outputs, results))
        if every:
            return {name: values[name] for name in self.output_names}
        return values[self.output_names[0]]
