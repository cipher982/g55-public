# G-Wagon eval

A fixed suite that scores each of comma's openpilot driving models the day it appears, against
lebowski (comma's 877M model of 2026-07-01) and against the model comma had on openpilot master
before it. One results file per model, `results/<model-id>.json`, in the format of
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
GEVAL_CONFIG=/path/to/private.toml uv run geval run hf:<uuid>/<checkpoint>   # or openpilot:<commit>, a model id, a .onnx
GEVAL_CONFIG=... uv run geval queue          # the job runner; takes the host's shared GPU lock
GEVAL_CONFIG=... uv run geval build          # rewrite results files from the per-unit store
```

Settings (paths, the GPU lock, the truck's windows and the engines that read the truck's drives)
live in a private TOML outside this repository; `geval/config.py` lists the keys. Per-unit values
for every scored model are kept in a store under the configured work directory, so a new model is
paired with any baseline without re-running it.

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
