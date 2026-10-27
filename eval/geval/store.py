"""Per-unit metric store: <work>/units/<section>/<model_id>.json = {"units": {unit: {key: value}}, "meta": {...}}.

Every scored model's per-unit values live here so a new model's paired deltas can be computed
against any baseline without re-running it. Truck unit ids stay in this private work directory.
"""
from __future__ import annotations

import json
import math
from pathlib import Path


def _clean(o):
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if hasattr(o, "item"):
        return _clean(o.item())
    return o


def path(work: Path, section: str, model: str) -> Path:
    return work / "units" / section / f"{model}.json"


def save(work: Path, section: str, model: str, units: dict, meta: dict | None = None) -> Path:
    p = path(work, section, model)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".part")
    tmp.write_text(json.dumps({"units": _clean(units), "meta": _clean(meta or {})}, indent=1))
    tmp.rename(p)
    return p


def load(work: Path, section: str, model: str):
    p = path(work, section, model)
    if not p.exists():
        return None
    return json.loads(p.read_text())


def values(entry, key: str) -> dict:
    """{unit: value} for one metric key; a key "test:metric" reads units stored as {"test": {...}}."""
    if entry is None:
        return {}
    out = {}
    for unit, m in entry["units"].items():
        v = m.get(key)
        if v is None and ":" in key:
            test, sub = key.split(":", 1)
            v = (m.get(test) or {}).get(sub)
        out[unit] = v
    return out
