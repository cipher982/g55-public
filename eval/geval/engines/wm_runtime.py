"""comma's worldmodel-4B on an Ampere GPU (RTX 3090): load, dequantize, encode, step, decode.

The checkpoint ships FP8 attention and NVFP4 MLP weights and an FP8 KV cache, formats that
need compute capability 10.0 (its meta says so). Here every torchao Float8Tensor/NVFP4Tensor is
dequantized to dense BF16 at load (7.9 GB on the GPU) and generation runs with a BF16 KV cache.

The package's own code (torchtitan's worldmodel model, KV cache and rectified-flow sampler) is
used as shipped, through torch.package; `predict` mirrors comma's openpilot.distill rl/server.py
Runtime.predict and rl/env.py Env.step.
"""
from __future__ import annotations

import io
import time

import numpy as np
import torch
from huggingface_hub import hf_hub_download
from torch.package import PackageImporter

WORLD_MODEL = ("commaai/worldmodel-4B", "model.fp8_nvfp4.torchpackage")
VAE = "commaai/vit-ae-2x-f8c32"
FUTURE_FRAMES = 5
HISTORY_FRAMES = 9
CONTEXT_FRAMES = HISTORY_FRAMES + 1          # history + the frame being generated
MAX_ROLLOUT = 36                             # the frame-index embedding has 50 entries: 9 + 36 + 5


class WorldModelRuntime:
    def __init__(self, device: str = "cuda", compile_blocks: bool = False):
        t0 = time.perf_counter()
        self.device = torch.device(device)
        importer = PackageImporter(hf_hub_download(*WORLD_MODEL))
        self.meta = importer.load_pickle("meta", "meta.pkl")
        self.model = importer.load_pickle("model", "model.pkl")
        state = torch.load(io.BytesIO(importer.load_binary("assets", "state_dict.pt")), map_location="cpu",
                           weights_only=False)
        dense = {}
        for name, value in state.items():
            if type(value) is torch.Tensor:
                dense[name] = value.to(self.device)
            else:                                    # torchao Float8Tensor / NVFP4Tensor
                dense[name] = value.to(self.device).dequantize().to(torch.bfloat16)
        del state
        self.model.load_state_dict(dense, strict=True, assign=True)
        del dense
        self.model.eval()
        if compile_blocks:
            for block in self.model.blocks:
                block.compile()
        self.encoder = torch.export.load(hf_hub_download(VAE, "encoder.pt2")).module().to(self.device, torch.bfloat16)
        self.decoder = torch.export.load(hf_hub_download(VAE, "decoder.pt2")).module().to(self.device, torch.bfloat16)
        self.load_s = time.perf_counter() - t0

    @torch.inference_mode()
    def encode(self, frames: np.ndarray, batch: int = 32) -> torch.Tensor:
        """(N, 128, 256, 6) uint8 RGB -> (N, 32, 16, 32) BF16 latents."""
        out = []
        for s in range(0, len(frames), batch):
            pixels = torch.from_numpy(np.ascontiguousarray(frames[s:s + batch])).permute(0, 3, 1, 2)
            out.append(self.encoder(pixels.to(self.device, torch.bfloat16).div(127.5).sub(1)))
        return torch.cat(out).to(torch.bfloat16)

    @torch.inference_mode()
    def decode(self, latents: torch.Tensor) -> np.ndarray:
        """(N, 32, 16, 32) -> (N, 128, 256, 6) uint8 RGB (the decoder returns channels-last)."""
        frames = self.decoder(latents.to(self.device, torch.bfloat16)).float().add(1).mul(127.5).clamp(0, 255).byte()
        return frames.cpu().numpy()

    @torch.inference_mode()
    def predict(self, future: torch.Tensor, history: torch.Tensor, pose: np.ndarray, future_fidx0: int,
                steps: int, cfg: float = 2.0, generator_seed: int | None = None) -> tuple[torch.Tensor, np.ndarray]:
        """One generated 5 Hz frame.

        future: (5, 32, 16, 32) anchor latents; history: (9, ...) the last nine frames;
        pose: (dx, dy, dyaw) of the new frame relative to the last history frame, in the
        model's own sign convention; future_fidx0: the first anchor's frame index (9 + frames
        left before the anchors). Returns the latent and the plan (33 x 15 means)."""
        latents = torch.cat((future, history, torch.zeros_like(history[:1])))[None].to(self.device, torch.bfloat16)
        if generator_seed is not None:
            torch.manual_seed(generator_seed)
        latents[0, -1] = torch.randn(latents.shape[2:], device=self.device, dtype=torch.float32).to(torch.bfloat16)
        position = latents.new_zeros((1, latents.shape[1], 3))
        euler = torch.zeros_like(position)
        pose_mask = torch.ones((1, latents.shape[1]), dtype=torch.int64, device=self.device)
        position[0, -1, 0], position[0, -1, 1] = float(pose[0]), float(pose[1])
        euler[0, -1, 2] = float(pose[2])
        pose_mask[0, -1] = 0
        fidxs = torch.cat((torch.arange(future_fidx0, future_fidx0 + FUTURE_FRAMES),
                           torch.arange(CONTEXT_FRAMES)))[None].to(self.device)
        output = self.model.generate(
            latents=latents, augments_pos_ref_augment=position, ref_augment_from_augments_euler=euler,
            pose_mask=pose_mask, fidxs=fidxs, steps=steps, num_prefill_frames=latents.shape[1] - 1,
            dtype=torch.bfloat16, inference_schedule="linear", cfg=cfg, kv_cache_dtype="bfloat16",
        )
        plan = output["plan"][0]
        plan = plan[: plan.numel() // 2].reshape(-1, 15).float().cpu().numpy()
        return output["latents"][0, 0], plan

    def rollout(self, future: torch.Tensor, history: torch.Tensor, poses: np.ndarray, steps: int,
                cfg: float = 2.0, seed: int = 0) -> tuple[torch.Tensor, np.ndarray]:
        """Generate len(poses) frames autoregressively, as comma's Env.step does: the anchors stay,
        their frame indices count down, each generated latent joins the history."""
        if len(poses) > MAX_ROLLOUT:
            raise ValueError(f"at most {MAX_ROLLOUT} steps (the frame-index embedding has 50 entries)")
        history = history.clone()
        fidx0 = HISTORY_FRAMES + len(poses)
        latents, plans = [], []
        for k, pose in enumerate(poses):
            latent, plan = self.predict(future, history, pose, fidx0 - k, steps, cfg, generator_seed=seed * 1000 + k)
            history = torch.roll(history, -1, dims=0)
            history[-1] = latent
            latents.append(latent)
            plans.append(plan)
        return torch.stack(latents), np.stack(plans)
