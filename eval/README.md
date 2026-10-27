# G-Wagon eval

A fixed suite that scores each of comma's openpilot driving models the day it appears, against
lebowski (comma's 877M model of 2026-07-01) and against the previous model on the timeline (the
scored model with the latest `model.source.date` before its own, ties broken by id). One results file per model, `results/<model-id>.json`, in the format of
[SCHEMA.md](SCHEMA.md); the site is built from those files only.

| Section | What | Reproducible by others |
|---|---|---|
| `public_open_loop` | every model on comma's 45 public two-camera segments (23 cars), scored against what the car did next | yes: `geval/engines/route_replay.py`, the segment list in `geval/data/segments.tsv`, per-segment rows in `per_unit/` |
| `public_closed_loop` | comma's `worldmodel-4B` generates the scene, the model drives comma's ideal car, from public segments | yes: the engine and per-segment rows |
| `truck_open_loop` | the G55's recorded drives, replayed as the truck runs the model | scores only; the drives stay private |
| `truck_closed_loop` | the world-model loop with a simulation of the G55's steering and powertrain | scores only |
| `truck_recovery` | the same loop with a disturbance: 2° off heading, 0.5 m off centre, a braking lead | scores only |

What each number is: everything in a section marked "Measured" was computed by this code on the
inputs named. Parameter counts and GMAC are derived from the ONNX graph. Model names, dates and
lineage are borrowed from comma's GitHub pull requests and Hugging Face upload history (where two
releases are said to be the same weights, that was checked by tensor bytes).

## Use

```
uv sync
export GEVAL_CONFIG=/path/to/private.toml
uv run geval run hf:<uuid>/<checkpoint>     # or hf:<revision>:<path>, openpilot:<commit>, a model id, a .onnx
uv run geval run <ref> --no-wait            # queue only; `geval score <id>` when the jobs are done
uv run geval submit <ref> <ref> --sections public_closed_loop   # several models in one world-model batch
uv run geval queue                          # the job runner (`run` starts one if none is alive)
uv run geval build [<id> ...]               # rewrite results files from the per-unit store
uv run geval backfill                       # regenerate the first edition from the deep dive's outputs
uv run geval watch [--dry-run]              # one pass: new models on Hugging Face or openpilot branches -> queue, score
```

`run` resolves the model (downloads it, sha256, parameters and GMAC from the graph), writes one
shell job per unit of work into `<work>/queue/jobs`, waits while the runner executes them under
the host's shared GPU lock, scores every output that holds the model into the per-unit store, and
writes `results/<id>.json` plus `per_unit/<id>/<section>.csv` for the public sections. Jobs skip
work whose output exists, so a rerun finishes only what is missing.

Settings live in a private TOML outside this repository: `[paths]` (work dir, the interpreter with
the `run` extra's packages, comma's segments and cereal schema, the GPU lock), `[queue]`
(slots, CPU cores), `[truck_open_loop]` (the truck's windows and the private replay command),
`[closed_loop]` (seeds, and the plugin that serves the truck's drives and steering model to
`engines/wm_closed_loop.py`). Per-unit values of every scored model stay in the store, so a new
model is paired with any baseline without re-running the baseline.

`watch` is the release watcher: each pass reads the Hugging Face model repo and openpilot's
branches anonymously (a branch counts when its change touches the model directory; since 2026-09
openpilot ships compiled models only, so a checkpoint id named there is held until its ONNX is on
Hugging Face), queues the suite for anything new at the jobs' normal names, and scores models whose
jobs have finished. Its first pass only records what exists. It touches no GPU itself and posts
nothing; state and log are in `<work>/watch/`.

Engines: `engines/route_replay.py` (comma segments, both cameras, lockstep), `engines/wm_closed_loop.py`
(comma's worldmodel-4B; same initial noise for every policy at every step, so a model run alone
pairs with a baseline run earlier), `runner.py` and `op011.py` (openpilot's model I/O contract,
vendored from openpilot, MIT).

## Method in one paragraph

Open loop: per segment, over moving frames after a 5 s warm-up, the model's lateral action
against v times the yaw rate that followed (shifted by the model's own `action_t`), its
longitudinal action against the car's later acceleration, and the plan speed at 3 s against the
speed 3 s later. Closed loop: 36 generated frames at 5 Hz (7.2 s), each conditioned on 9 history
frames, 5 recorded anchor frames and the car's pose; the distance from the recorded path at 2, 4
and 7.2 s, with 4 s the primary horizon. Means over units with 95% percentile bootstrap
intervals over units; paired deltas are model minus baseline on shared units. Limits are listed in
every results file.

Backfill: the first edition comes from the 2026-10-27 deep dive's outputs (`geval backfill`
regenerates it from them).
