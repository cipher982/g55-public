"""geval: score one comma driving model with the fixed suite and write results/<model-id>.json.

  geval run <model-ref> [--sections a,b] [--no-wait]   resolve, queue every section's jobs, score, write
  geval score <model-id>                              re-score from finished job outputs, write results
  geval backfill                                      rebuild the store and results from the deep dive
  geval build [<model-id> ...]                        rewrite results files from the store
  geval queue [--slots N]                             run the GPU job queue (takes the shared GPU lock)
  geval status                                        queue and store state

<model-ref>: a Hugging Face file `hf:<revision>:<path>` or `hf:<uuid>/<ckpt>` (latest revision),
`openpilot:<commit>` (the big model ONNX in that commit), a known model id, or a local .onnx path.
Settings come from a private TOML (--config or $GEVAL_CONFIG); see README.md.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import SUITE_VERSION, config as C, queue as Q, results, sections as S, store

ROOT = Path(__file__).resolve().parents[1]


def out_dirs(cfg):
    p = cfg.get("paths", {})
    return Path(p.get("results", ROOT / "results")), Path(p.get("per_unit", ROOT / "per_unit"))


def model_arch(cfg, mid):
    rec = cfg.work / "models" / f"{mid}.json"
    if rec.exists():
        r = json.loads(rec.read_text())
        return {"params_m": r.get("params_m"), "gmac": r.get("gmac")}, r.get("sha256")
    if "backfill" in cfg and Path(cfg["backfill"]["arch"]).exists():
        from .backfill import arch_for, arch_table
        return arch_for(mid, arch_table(cfg["backfill"]["arch"])), None
    return {}, None


def cmd_build(cfg, ids):
    res, pu = out_dirs(cfg)
    reg = results.registry(cfg.work)
    for mid in ids or [m for m in reg if any(store.load(cfg.work, s, m) for s in S.SECTIONS)]:
        arch, sha = model_arch(cfg, mid)
        print(results.build(cfg.work, mid, arch, res, pu, sha256=sha))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="geval", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("ref")
    r.add_argument("--sections", default=",".join(S.SECTIONS))
    r.add_argument("--no-wait", action="store_true", help="queue the jobs and return; `geval score` later")
    sc = sub.add_parser("score")
    sc.add_argument("model_id")
    sc.add_argument("--sections", default=",".join(S.SECTIONS))
    sub.add_parser("backfill")
    b = sub.add_parser("build")
    b.add_argument("ids", nargs="*")
    q = sub.add_parser("queue")
    q.add_argument("--slots", type=int)
    q.add_argument("--stop-file", default="STOP")
    q.add_argument("--idle-exit-s", type=float, default=0)
    sub.add_parser("status")
    a = ap.parse_args(argv)
    cfg = C.load(a.config)
    if a.cmd == "backfill":
        from . import backfill
        res, pu = out_dirs(cfg)
        log, written = backfill.run(cfg, res, pu, f"{SUITE_VERSION} (backfill of the 2026-10-27 deep dive)")
        print(json.dumps(log, indent=1))
        print("\n".join(written))
    elif a.cmd == "build":
        cmd_build(cfg, a.ids)
    elif a.cmd == "queue":
        qc = cfg.get("queue", {})
        Q.run(cfg.work / "queue", cfg["paths"]["gpu_lock"], slots=a.slots or qc.get("slots", 2),
              share_s=qc.get("share_s", 2700), stop_file=a.stop_file, idle_exit_s=a.idle_exit_s)
    elif a.cmd == "status":
        st = Q.job_states(cfg.work / "queue")
        print(json.dumps({"jobs": st}, indent=1))
    elif a.cmd in ("run", "score"):
        from . import run as R
        secs = [s for s in a.sections.split(",") if s]
        if a.cmd == "run":
            mid = R.run(cfg, a.ref, secs, wait=not a.no_wait)
        else:
            mid = a.model_id
            R.score(cfg, mid, secs)
        if mid:
            cmd_build(cfg, [mid])
    return 0


if __name__ == "__main__":
    sys.exit(main())
