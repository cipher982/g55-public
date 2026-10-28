# Results file: `results/<model-id>.json`

One file per scored model, written by the suite runner and read by the site generator. Nothing
else is read by the site. JSON, UTF-8, numbers in SI units unless a metric says otherwise.

```jsonc
{
  "schema": 1,
  "model": {
    "id": "8d736683_12864",            // file-stem used in URLs; stable
    "name": "Mountain Dew",            // comma's name if known, else null
    "sha256": "...",                   // of the ONNX/pkl actually scored
    "source": {"kind": "openpilot-master" | "huggingface" | "release",
               "ref": "commit or HF revision", "url": "https://...", "date": "2026-10-23"},
    "first_public": "2026-10-10",      // first date the weights were public (master or Hugging Face); the timeline's order
    "on_master": "2026-10-10",         // date merged to openpilot master, null if never
    "params_m": 383.0,                 // counted from the weights
    "gmac": 46.7,
    "family": "lebowski" | "big-rl" | "residual" | "...",
    "notes": "short, factual"
  },
  "scored": {"date": "2026-10-27T16:30:00Z", "suite": "suite version string", "code": "git sha of public/eval"},
  "baselines": ["lebowski_877m", "<previous model id>"],   // lebowski and the model first public before this one
  "sections": {
    "public_open_loop":   SECTION,     // comma's public segments
    "public_closed_loop": SECTION,     // worldmodel-4B, comma's ideal car, public segments
    "truck_open_loop":    SECTION,     // our drives as the truck runs them
    "truck_closed_loop":  SECTION,     // worldmodel-4B with the G55's steering response
    "truck_recovery":     SECTION      // off-centre, off-heading, braking lead
  },
  "stills": [ {"file": "stills/<id>/<n>.jpg", "condition": "night, 25 mph", "caption": "..."} ],
  "limits": ["one line per limit the reader must know"]
}
```

`SECTION`:

```jsonc
{
  "status": "ok" | "not_run" | "failed",
  "unit_of_evidence": "segment" | "window",
  "n": 39,                              // units with data
  "clusters": 23,                       // drives those units come from (the bootstrap's draws)
  "condition": "native intrinsics, both cameras",
  "baselines": ["lebowski_877m", "8d736683_12864"],  // this section's: lebowski, and the latest earlier
                                        // model on the timeline that has scores in this section
  "metrics": [
    {
      "key": "lat_mae",                 // stable key
      "label": "Lateral error",
      "unit": "m/s²",
      "better": "lower" | "higher" | "near1",
      "value": 0.143, "median": 0.131,               // mean and median over units
      "ci": [0.116, 0.171],                           // 95% bootstrap over drives; null below 10 drives
      "n": 39, "clusters": 23,                        // units and drives with a value for this metric
      "paired": {                                     // per section baseline, on shared units
        "lebowski_877m": {"delta": 0.035, "median": 0.03, "ci": [0.02, 0.05], "n": 39, "wins": 3,
                          "ties": 0, "clusters": 23, "cluster_wins": 2}
      }
    }
  ],
  "per_unit_file": "per_unit/<model-id>/<section>.csv",  // public sections only; null for truck
  "by_group": {                                          // optional: truck open loop, per camera setup
    "v13-8mm": {"n": 40, "clusters": 5,
                "metrics": [{"key": "lat_mae", "value": 0.2, "median": 0.18, "ci": null, "n": 40,
                             "clusters": 5, "paired": {}}]}
  }
}
```

Rules:

- `by_group` repeats a section's pooling and pairing within each group of units (for the truck:
  the camera setup a window was recorded with, never pooled silently); the section's own
  `metrics` pool every group.

- Units are grouped by drive: the route a public segment was cut from (`drive` in the CSV), the
  recorded drive a truck window comes from. An interval is a 95% percentile bootstrap that resamples
  drives (4000 draws, generator seeded 0, drives and units in sorted order) and takes the mean over
  all units drawn. With fewer than 10 drives there is no interval (`"ci": null`).
- A paired delta is model minus baseline per shared unit, averaged; for a `near1` metric it is the
  change in distance from 1 (`|model - 1| - |baseline - 1|`), so negative is better. `wins` counts
  units where the model is better by the metric's `better` direction, `ties` units where the two
  values are equal, `cluster_wins` drives whose mean delta favours the model.
- Censored values are left out of a metric: `lat_best_lag` at either edge of its 0 to 1.5 s search.
- Per-unit CSVs hold every value at full precision with censored values empty; `geval.reproduce`
  recomputes every published value and interval from them.
- Truck sections never carry per-unit rows, drive ids, times or places.
