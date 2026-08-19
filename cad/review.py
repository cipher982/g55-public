"""Render an STL to PNGs and print a bounded numeric report.

This is the feedback half of an agent CAD loop: a part script writes STL/STEP,
then this tool renders fixed viewpoints and reports geometry measurements so an
agent can inspect the result instead of writing geometry blind.

The renderer is the OpenSCAD CLI. Measurement parses binary STL directly with
numpy, so the tool stays independent of whichever CAD library produced the file.

    uv run --with numpy python review.py part.stl --out generated/views

Output is capped: four PNGs and a short table. It never prints per-triangle data.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import numpy as np

VIEWS = {
    "iso": (60, 0, 35),
    "front": (90, 0, 0),
    "right": (90, 0, 90),
    "top": (0, 0, 0),
}


def read_binary_stl(path: Path) -> np.ndarray:
    """Return an (n, 3, 3) array of triangle vertices from a binary STL."""
    raw = path.read_bytes()
    if raw[:5] == b"solid" and b"facet" in raw[:512]:
        raise SystemExit(f"{path} is ASCII STL; this reader expects binary STL.")
    count = int(np.frombuffer(raw, dtype="<u4", count=1, offset=80)[0])
    rec = np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")])
    tris = np.frombuffer(raw, dtype=rec, count=count, offset=84)
    return tris["v"].astype(np.float64)


def signed_volume(tris: np.ndarray) -> float:
    a, b, c = tris[:, 0], tris[:, 1], tris[:, 2]
    return float(np.abs(np.einsum("ij,ij->i", a, np.cross(b, c)).sum()) / 6.0)


def report(path: Path, tris: np.ndarray) -> str:
    pts = tris.reshape(-1, 3)
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    size = hi - lo
    rows = [
        f"file          {path.name}",
        f"triangles     {len(tris)}",
        f"bbox min      {lo[0]:9.3f} {lo[1]:9.3f} {lo[2]:9.3f}  mm",
        f"bbox max      {hi[0]:9.3f} {hi[1]:9.3f} {hi[2]:9.3f}  mm",
        f"size  X Y Z   {size[0]:9.3f} {size[1]:9.3f} {size[2]:9.3f}  mm",
        f"volume        {signed_volume(tris) / 1000.0:9.3f} cm^3",
    ]
    return "\n".join(rows)


def render(stl: Path, out_dir: Path, size: str = "900,700") -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    scad = out_dir / "_view.scad"
    scad.write_text(f'import("{stl.resolve()}");\n')
    written = []
    for name, (rx, ry, rz) in VIEWS.items():
        png = out_dir / f"{stl.stem}-{name}.png"
        cmd = [
            "openscad",
            "-o",
            str(png),
            f"--imgsize={size}",
            f"--camera=0,0,0,{rx},{ry},{rz},0",
            "--viewall",
            "--autocenter",
            "--colorscheme=Tomorrow",
            str(scad),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0 or not png.exists():
            print(proc.stderr[-2000:], file=sys.stderr)
            raise SystemExit(f"openscad failed rendering {name}")
        written.append(png)
    scad.unlink(missing_ok=True)
    return written


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stl", type=Path)
    ap.add_argument("--out", type=Path, default=None, help="PNG output directory")
    ap.add_argument("--no-render", action="store_true", help="measure only")
    args = ap.parse_args()

    tris = read_binary_stl(args.stl)
    print(report(args.stl, tris))
    if args.no_render:
        return
    out = args.out or args.stl.parent / "views"
    for png in render(args.stl, out):
        print(f"view          {png}")


if __name__ == "__main__":
    main()
