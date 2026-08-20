# Code-First CAD

The CAD work uses Python and build123d for parametric geometry. The source model is code; STEP and STL are generated artifacts. Agents close the loop by rendering the mesh from fixed views and measuring its bounds and volume before changing the design.

The initial public draft contains the independent mesh review tool in
[`review.py`](review.py) and one vehicle-specific trim prototype:
[`parts/dash-faceplate/`](parts/dash-faceplate/README.md).
Vehicle-specific parts are included only when their source, fit status, and
release artifacts can be explained without private workspace context.

## Local setup

```sh
uv run --with numpy python review.py part.stl --out views
```

Rendering requires the OpenSCAD command-line tool. The review tool expects binary STL and emits a bounded report plus four PNG views.

## Parts

- [W463 dash faceplate](parts/dash-faceplate/README.md) — trim-only fit
  prototype; tablet retention deliberately excluded.
