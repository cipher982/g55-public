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
    "params_m": 383.0,                 // counted from the weights
    "gmac": 46.7,
    "family": "lebowski" | "big-rl" | "residual" | "...",
    "notes": "short, factual"
  },
  "scored": {"date": "2026-10-27T16:30:00Z", "suite": "suite version string", "code": "git sha of public/eval"},
  "baselines": ["lebowski_877m", "<previous comma model id>"],   // what the paired deltas compare to
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
  "condition": "native intrinsics, both cameras",
  "metrics": [
    {
      "key": "lat_mae",                 // stable key
      "label": "Lateral error",
      "unit": "m/s²",
      "better": "lower" | "higher" | "near1",
      "value": 0.143, "ci": [0.116, 0.171],          // mean over units, 95% bootstrap over units
      "paired": {                                     // per baseline, on shared units
        "lebowski_877m": {"delta": 0.035, "ci": [0.02, 0.05], "n": 39, "wins": 3}
      }
    }
  ],
  "per_unit_file": "per_unit/<model-id>/<section>.csv"   // public sections only; null for truck
}
```

Rules: a metric with no interval carries `"ci": null`; a paired delta is model minus baseline;
`wins` counts units where the model is better by the metric's `better` direction. Truck sections
never carry per-unit rows, drive ids, times or places.
