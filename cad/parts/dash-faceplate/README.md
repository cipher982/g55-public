# W463 Dash Faceplate — Trim-Only Prototype

Parametric source and generated STEP/STL for a flat replacement speaker faceplate
on the upper dash recess of a 2011 W463 G-Class.

## What this prototype is

- A geometry and fit prototype for the trim panel.
- A 1.5 mm flat plate that is intended to form to the installed bezel's measured
  crown.
- A vent field generated from explicit parameters.
- A reproducible build from [`faceplate.py`](faceplate.py).

The outline and flat-print process were checked with a physical fit sequence on
one vehicle. The panel's old printed tab and tie retention path failed physical
testing and is intentionally absent here. This prototype carries no tablet load,
contains no tablet mount, and is not a road-ready retention system.

## Evidence grades

- **Measured:** recess width, speaker-ring bore, and ring centreline from one
  vehicle.
- **Derived:** ring Y offset, plate dimensions, clearance, vent field, and
  open-area estimate.
- **Confirmed by fit:** the outline, recess depth, and bezel-forming concept.
- **Open:** exact crown repeatability, final retention, long-term heat/cycle
  behavior, and any tablet support.

The dimensions are not guaranteed for every W463 model year or trim level. Check
this part against the target vehicle before manufacturing.

## Build

From this directory:

```sh
uv run --project ../.. python faceplate.py
```

The command writes `generated/dash-faceplate-v14-clean.step` and
`generated/dash-faceplate-v14-clean.stl`. Review the STL with the public mesh
review tool:

```sh
uv run --project ../.. python ../../review.py \
  generated/dash-faceplate-v14-clean.stl \
  --out generated/views
```

For an initial dimensional check, use a heat-stable filament and print flat.
Do not infer structural or crash performance from this trim prototype.
