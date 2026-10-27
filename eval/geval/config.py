"""The suite's settings: one TOML file kept outside this repository (paths, hosts and the truck's
windows are private). `--config PATH`, else $GEVAL_CONFIG.

Keys read here (see README.md for a full example):
  [paths]  work, python, segments_dir, cereal, models_dir, gpu_lock, results, per_unit
  [queue]  slots, share_s, cpu_cores
  [truck_open_loop]   condition, cwd, command, [[truck_open_loop.windows]] id, ...
  [closed_loop]       wm_args, [closed_loop.public] ..., [closed_loop.truck] ...
  [backfill]          legacy sources
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path


class Config(dict):
    @property
    def work(self) -> Path:
        return Path(self["paths"]["work"])

    def path(self, key: str) -> Path:
        return Path(self["paths"][key])


def load(path: str | None = None) -> Config:
    path = path or os.environ.get("GEVAL_CONFIG")
    if not path:
        raise SystemExit("no config: pass --config PATH or set GEVAL_CONFIG (the private suite settings)")
    with open(path, "rb") as f:
        cfg = Config(tomllib.load(f))
    cfg["_path"] = str(Path(path).resolve())
    return cfg
