# /// script
# requires-python = ">=3.11"
# ///
"""Build the openpilot G-Wagon evals site from results/*.json: a living record of comma's driving models.

Since 2026-11-05 (David: "a living growing website that collects and tracks and explains over time") the
front page is comma's releases on a time axis and a feed of what happened; each release has a page that
fills in as its tests land, each test a page across releases, and "Inside the models" explains one
release's internals on comma's public segments (xray/). Nothing is ranked (ranking.check_site).

    uv run python site/build.py                       # real results, out to site/_site
    uv run python site/build.py --root site/fixtures

--root holds results/, per_unit/ and stills/ (default: eval/). The results files and the per-unit
CSVs are the only data read; every number on the site comes from them. config.json holds the
editorial parts: the findings (prose with placeholders that pull their numbers from the results,
so a missing number fails the build) and the models the hero chart names. pages/method.html is
the method prose.
"""
from __future__ import annotations

import argparse
import html
import hashlib
import json
import math
import re
import shutil
import statistics
import sys
from pathlib import Path

SITE = Path(__file__).resolve().parent
EVAL = SITE.parent
sys.path.insert(0, str(EVAL))
from geval import ranking, sections as GS  # noqa: E402  the order check; the simulators and limits
sys.path.insert(0, str(SITE))

SECTIONS = {  # in page order: G55 first, comma public drives after
    "truck_closed_loop": "G55 closed loop",
    "truck_recovery": "G55 recovery tests",
    "truck_open_loop": "G55 open loop",
    "public_closed_loop": "comma public drives (closed loop)",
    "public_open_loop": "comma public drives (open loop)",
}
UNIT_WORD = {"segment": ("segment", "segments"), "window": ("window", "windows")}
# Links into David's private repositories are never printed (a 404 for every reader); cipher982/g55-public,
# this code, is public since 2026-11-05 (ranking.is_private_link).
BETTER_TEXT = {"lower": "lower is better", "higher": "higher is better",
               "near1": "closer to 1 is better; the delta is the change in distance from 1"}
MINUS = "−"
# The G55 numbers on every model's row. Lateral action rate is a diagnostic with no better direction:
# a lower rate can be a smaller request as well as a smoother one.
# Labels the site uses in place of a results file's own, for files written before a label changed.
LABEL = {"lat_best_lag": "Lead of the lateral request over the car's motion",
         "lat_mae_bl": "Lateral error at the model's own best lead"}
# Metrics with no better direction: a separated difference is marked lower or higher, not better or worse.
NEUTRAL = {"lat_rate_rms", "lon_rate_rms", "lat_best_lag"}
# Terms for the per-model verdict line.
CAMERA_GROUPS = [
    ("v14-8mm", "V14 (8 mm + IMX662)", "Visor mount, 8 mm AR0234 narrow + IMX662 wide"),
    ("v13-8mm", "V13 (8 mm + IMX662)", "8 mm AR0234 narrow + IMX662 wide fill"),
    ("stock-aimedup", "Stock aimed up", "Dual AR0234 + fisheye, aimed up (2026-10-19)"),
    ("stock-newcase", "Stock new case", "Dual AR0234 + fisheye, revised housing"),
    ("stock-dual", "Stock dual", "Original dual AR0234 + fisheye"),
]


def esc(x) -> str:
    return html.escape("" if x is None else str(x), quote=True)


# ---------------------------------------------------------------- numbers

def decimals_for(v) -> int:
    if v is None or v == 0:
        return 3
    a = abs(v)
    return 1 if a >= 10 else 2 if a >= 1 else 3


def num(v, d: int, signed: bool = False) -> str:
    if v is None:
        return "n/a"
    s = f"{abs(v):.{d}f}"
    if v < 0 and float(s) != 0:
        return MINUS + s
    return ("+" + s) if signed and float(s) != 0 else s


def ci_text(ci, d: int, signed: bool = False) -> str:
    if not ci:
        return "no interval"
    return f"[{num(ci[0], d, signed)}, {num(ci[1], d, signed)}]"


def verdict_of(metric: dict, paired: dict) -> str:
    """better | worse | lower | higher | tie | none (no interval). lower/higher for a NEUTRAL metric.
    For near1 the delta is already a change in distance from 1, so negative is better, as for lower."""
    ci = paired.get("ci")
    if not ci:
        return "none"
    if not (ci[0] > 0 or ci[1] < 0):
        return "tie"
    if metric["key"] in NEUTRAL:
        return "lower" if paired["delta"] < 0 else "higher"
    good = paired["delta"] > 0 if metric["better"] == "higher" else paired["delta"] < 0
    return "better" if good else "worse"


MARK_TEXT = {
    "better": "better, 95% interval excludes zero",
    "worse": "worse, 95% interval excludes zero",
    "lower": "lower, 95% interval excludes zero; no better direction",
    "higher": "higher, 95% interval excludes zero; no better direction",
    "tie": "not separated, interval includes zero",
    "none": "no interval (too few drives)",
}


def mark(v: str) -> str:
    return f'<span class="mk mk-{v}" aria-hidden="true"></span><span class="sr">{MARK_TEXT[v]}</span>'


def join_words(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


# ---------------------------------------------------------------- data

def first_public(m: dict) -> str:
    return m["model"].get("first_public") or m["model"]["source"]["date"]


def load(root: Path) -> list[dict]:
    files = sorted((root / "results").glob("*.json"))
    if not files:
        raise SystemExit(f"no results files in {root / 'results'}")
    models = []
    for f in files:
        d = json.loads(f.read_text())
        if d.get("schema") != 1:
            raise SystemExit(f"{f}: schema {d.get('schema')!r}, this generator reads schema 1")
        if d["model"]["id"] != f.stem:
            raise SystemExit(f"{f}: model.id {d['model']['id']!r} does not match the file name")
        d["_file"] = f
        models.append(d)
    models.sort(key=lambda d: (first_public(d), d["model"]["id"]))  # the timeline, oldest first
    return models


def short_hash(m: dict) -> str:
    """The checkpoint hash comma names a model by, when the id starts with one."""
    h = m["model"]["id"].split("_")[0][:8]
    return h if len(h) == 8 and all(c in "0123456789abcdef" for c in h) else ""


def plain_name(m: dict) -> str:
    """'Mountain Dew (8d736683)', or the hash alone for an unnamed model."""
    n, h = m["model"]["name"], short_hash(m)
    if n:
        return f"{n} ({h})" if h else n
    return h or m["model"]["id"]


def short_name(m: dict) -> str:
    return m["model"]["name"] or short_hash(m) or m["model"]["id"]


def name_html(m: dict, link: str | None = None) -> str:
    n, h = m["model"]["name"], short_hash(m)
    if n:
        inner = f'<span class="nm">{esc(n)}</span>' + (f' <code class="hx">{h}</code>' if h else "")
    else:
        inner = f'<code class="nm hx-only">{esc(h or m["model"]["id"])}</code>'
    return f'<a class="mlink" href="{esc(link)}">{inner}</a>' if link else inner


def status_text(m: dict) -> str:
    md, src = m["model"], m["model"]["source"]
    if src["kind"] == "release":
        s = f'Shipped in openpilot {src["ref"]}'
        if md.get("on_master"):
            s += f' (master {md["on_master"]})'
        return s
    if md.get("on_master"):
        s = f'Merged to openpilot master ({md["on_master"]})'
        if src["kind"] == "openpilot-master" and src["date"] != md["on_master"]:
            s += f', updated {src["date"]}'
        return s
    return "Hugging Face checkpoint (unshipped)"

def lc_status(m: dict) -> str:
    t = status_text(m)
    return t if t.startswith("Hugging") else t[0].lower() + t[1:]


def by_id(models) -> dict:
    return {m["model"]["id"]: m for m in models}


def by_release(models) -> list[dict]:
    """Newest first by first public date: the only order the site lists models in (ranking.check_lists)."""
    return sorted(models, key=lambda m: (first_public(m), m["model"]["id"]), reverse=True)


def find_metric(m: dict, section: str, key: str):
    s = m["sections"].get(section)
    if not s or s["status"] != "ok":
        return None, s
    for x in s["metrics"]:
        if x["key"] == key:
            return x, s
    return None, s


def section_bases(m: dict, section: str) -> list:
    s = m["sections"].get(section) or {}
    return s.get("baselines", m.get("baselines", []))


def previous_of(m: dict, ref: str, section: str = "public_open_loop"):
    rest = [b for b in section_bases(m, section) if b != ref]
    return rest[0] if rest else None


CFG: dict = {}
MODELS: list = []  # every model, set in main(); model_class() needs the reference's date
TIERS: dict = {}  # {test: ranking.order(...)}: what the check verifies a tier claim against; the site prints none


# ---------------------------------------------------------------- page shell

def _asset_hash(name: str) -> str:
    import hashlib
    p = SITE / "static" / name
    return hashlib.sha256(p.read_bytes()).hexdigest()[:8] if p.exists() else "1"


# ---------------------------------------------------------------- per-model verdict and bars

def value_text(x: dict, scale: float, unit: str, stat: str = "value") -> str:
    """'9.9 cm (95% interval 7.6 to 11.9)' for a metric's mean (stat "value") or median."""
    d = 1 if scale == 100 else decimals_for(x["value"])
    v = x.get(stat)
    ci = x.get("ci") if stat == "value" else None
    iv = f" (95% interval {num(ci[0] * scale, d)} to {num(ci[1] * scale, d)})" if ci else ""
    return f"{num(v * scale, d)} {unit}{iv}"


def summary_line(m: dict, cfg: dict, as_html: bool = True) -> str:
    """Values only, no comparison: the G55 closed loop at 2 s and the G55 open-loop lateral error."""
    mid = m["model"]["id"]
    out = []
    if mid == cfg["reference_model"]:
        out.append("lebowski, the 877M model of July 2026 that the truck drives with.")
    x2, _ = find_metric(m, "truck_closed_loop", "xtrack_2s")
    if x2:
        out.append(f'G55 closed loop: mean distance from the recorded path at 2 s {value_text(x2, 100, "cm")}, '
                   f'median {num(x2["median"] * 100, 1)} cm.')
    else:
        out.append("G55 open loop only.")
    ol, _ = find_metric(m, "truck_open_loop", "lat_mae")
    if ol:
        out.append(f'G55 open loop: lateral error {value_text(ol, 1, "m/s²")}.')
    if mid == "96244b1f_12800":
        out.append("Navigation inputs fed zeros.")
    t = " ".join(out)
    return esc(t) if as_html else t


def bar_scale(models, section, key) -> float:
    vals = [find_metric(m, section, key)[0] for m in models]
    vals = [x for x in vals if x]
    stat = "median" if not section.endswith("open_loop") else "value"
    v = [x.get(stat, x["value"]) for x in vals if x.get(stat, x.get("value")) is not None]
    return min(max(v), 3 * statistics.median(v)) * 1.15 if v else 1.0


def bars(m: dict, models: list[dict], cfg: dict, metrics, scales: dict, compact: bool) -> str:
    ref = by_id(models).get(cfg["reference_model"])
    out = []
    for section, key, label, sub in metrics:
        x, s = find_metric(m, section, key)
        stat = "median" if not section.endswith("open_loop") and x and x.get("median") is not None else "value"
        head = (f'<span class="b-l">{esc(label)}</span>' if compact else
                f'<span class="b-l">{esc(label)}<span class="b-s">{esc(sub)}</span></span>')
        if not x:
            st = "not run yet" if not s or s["status"] == "not_run" else s["status"]
            out.append(f'<div class="b-row b-off">{head}<span class="b-na">{esc(st)}</span></div>')
            continue
        mx = scales[(section, key)]
        w = min(x[stat] / mx * 100, 100)
        rx = find_metric(ref, section, key)[0] if ref else None
        tick = ""
        if rx and m is not ref and rx.get(stat) is not None:
            tick = f'<span class="b-ref" style="left:{min(rx[stat] / mx * 100, 100):.1f}%" title="lebowski"></span>'
        u, d, k = x["unit"], 2, 1
        if "xtrack" in key and u == "m":
            u, d, k = "cm", 1, 100
        tip = (f'{plain_name(m)}, {label.lower()}: {num(x[stat] * k, d)} {u} ({"median" if stat == "median" else "mean"} over {x.get("n", s["n"])} '
               f'{UNIT_WORD.get(s["unit_of_evidence"], ("unit", "units"))[1]})'
               + (f'; lebowski {num(rx[stat] * k, d)} {u}' if rx and m is not ref and rx.get(stat) is not None else ""))
        out.append(f'<div class="b-row">{head}<span class="b-track" tabindex="0" data-tip="{esc(tip)}" aria-label="{esc(tip)}">'
                   f'<span class="b-bar{" b-refbar" if m is ref else ""}{" b-cut" if w >= 100 else ""}" style="width:{w:.1f}%"></span>{tick}</span>'
                   f'<span class="b-v">{num(x[stat] * k, d)}<span class="b-u"> {esc(u)}</span></span></div>')
    return "".join(out)


# ---------------------------------------------------------------- researchers' table

def delta_cell(x, p, d) -> str:
    if not p:
        return '<td class="na">not paired</td>'
    v = verdict_of(x, p)
    if p.get("ties") and p["ties"] == p["n"]:
        return f'<td class="n">{mark("tie")}identical<span class="wins">on all {p["n"]}</span></td>'
    extra = ""
    if not p.get("ci") and p.get("clusters"):
        extra = f'<span class="wins">{p["cluster_wins"]}/{p["clusters"]} drives</span>'
    return (f'<td class="n">{mark(v)}{num(p["delta"], d, True)}'
            f'<span class="ci">{ci_text(p["ci"], d, True)}</span>'
            f'<span class="wins">{"lower" if x["key"] in NEUTRAL else "better"} on {p["wins"]}/{p["n"]}</span>{extra}</td>')



def legend() -> str:
    return ('<p class="legend"><span>' + mark("better") + ' Better, 95% interval excludes zero</span>'
            '<span>' + mark("worse") + ' Worse, interval excludes zero</span>'
            '<span>' + mark("lower") + mark("higher") + ' Lower or higher, no better direction (action rate, lead)</span>'
            '<span>' + mark("tie") + ' Not separated</span>'
            '<span>' + mark("none") + ' No interval: fewer than 10 drives</span></p>')


def unscored() -> str:
    f = SITE / "models.json"
    extra = json.loads(f.read_text())["unscored"] if f.exists() else []
    if not extra:
        return ""
    items = []
    for e in extra:
        name = esc(e["name"]) + (f' <code class="hx">{esc(e["checkpoint"])}</code>' if e.get("checkpoint") else "")
        why = esc(e["why"])
        if e.get("url"):
            why = f'<a href="{esc(e["url"])}">{why}</a>'
        items.append(f'<li>{name}, {esc(e.get("date") or "date unknown")}, {esc(e.get("size") or "size unknown")}: {why}.</li>')
    return f'<h3>comma models not scored here</h3><ul class="plain">{"".join(items)}</ul>'


# ---------------------------------------------------------------- the closed-loop example (method page)

CAMERA = {"f": 455.0, "cx": 128.0, "cy": 23.8, "h": 1.22}


def hero_visual(models, cfg) -> str:
    """One public segment in closed loop: a player that draws the world model's frames at their native
    256x128 (static/hero-*.avif, one sprite per stream, from a re-run that kept the frames) to a canvas and
    the paths over them as vectors, beside a bird's-eye map of the same paths from hero.json."""
    f = SITE / "hero.json"
    if not f.exists():
        return ""
    h = json.loads(f.read_text())
    idx = by_id(models)
    order = [mid for mid in h["paths"] if mid in idx]
    if not order:
        return ""
    cls = {mid: highlight_class(idx[mid], cfg) for mid in order}
    ref = h["ref"]
    pts = [p for mid in order for p in h["paths"][mid]["xy"]] + ref
    fx = max(p[0] for p in pts)
    ylo, yhi = min(p[1] for p in pts) - 1, max(p[1] for p in pts) + 1
    W, Hh, L, R, T, B = 320, 430, 34, 112, 14, 30
    X = lambda y: L + (yhi - y) / (yhi - ylo) * (W - L - R)  # noqa: E731
    Y = lambda x: Hh - B - x / fx * (Hh - T - B)  # noqa: E731

    def line(xy):
        return " ".join(f"{X(b):.1f},{Y(a):.1f}" for a, b in xy)
    g = []
    for t in range(0, int(fx) + 1, 20):
        g.append(f'<line class="grid" x1="{L}" x2="{W - R}" y1="{Y(t):.1f}" y2="{Y(t):.1f}"/>'
                 f'<text class="tick" x="{L - 6}" y="{Y(t) + 4:.1f}" text-anchor="end">{t}</text>')
    for t in range(math.ceil(ylo / 5) * 5, int(yhi) + 1, 5):
        g.append(f'<line class="grid" x1="{X(t):.1f}" x2="{X(t):.1f}" y1="{T}" y2="{Hh - B}"/>'
                 f'<text class="tick" x="{X(t):.1f}" y="{Hh - B + 16}" text-anchor="middle">{abs(t)}</text>')
    g.append(f'<text class="tick" x="{L - 6}" y="{T - 2}" text-anchor="end">m</text>')
    g.append(f'<text class="tick" x="{W - R + 4}" y="{Hh - B + 16}">m left</text>')
    k2 = int(round(2 / 0.2))
    paths, dots, labels = [], [], []
    paths.append(f'<polyline class="bp bp-rec" data-m="rec" points="{line(ref)}"/>')
    ends = []
    for mid in order:
        xy = h["paths"][mid]["xy"]
        paths.append(f'<polyline class="bp bp-{cls[mid]}" data-m="{mid}" points="{line(xy)}"/>')
        a, b = xy[k2]
        paths.append(f'<circle class="b2 b2-{cls[mid]}" cx="{X(b):.1f}" cy="{Y(a):.1f}" r="3.5"/>')
        dots.append(f'<circle class="bd dot-{cls[mid]}" data-m="{mid}" cx="{X(0):.1f}" cy="{Y(0):.1f}" r="6"/>')
        ends.append((X(xy[-1][1]), Y(xy[-1][0]), mid))
    ends.append((X(ref[-1][1]), Y(ref[-1][0]), "rec"))
    ends.sort(key=lambda e: e[1])
    last = -99
    for ex, ey, mid in ends:
        ey = max(ey, last + 15)
        last = ey
        txt = "driver" if mid == "rec" else short_name(idx[mid])
        labels.append(f'<text class="bl" x="{W - R + 6}" y="{ey + 4:.1f}">{esc(txt)}</text>'
                      f'<line class="bl-l bl-{"rec" if mid == "rec" else cls[mid]}" x1="{ex + 4:.1f}" y1="{ey:.1f}" x2="{W - R + 2}" y2="{ey:.1f}"/>')
    dots.append(f'<circle class="bd bd-rec" data-m="rec" cx="{X(0):.1f}" cy="{Y(0):.1f}" r="4.5"/>')
    svg = (f'<svg class="cl-map" viewBox="0 0 {W} {Hh}" role="img" aria-label="Bird\'s-eye view of the driver\'s path and each model\'s path over 7.2 s">'
           f'{"".join(g)}{"".join(paths)}{"".join(dots)}{"".join(labels)}</svg>')
    btns = '<button type="button" data-v="rec"><i class="sw sw-rec"></i>Recorded</button>' + "".join(
        f'<button type="button" data-v="{mid}"><i class="sw sw-{cls[mid]}"></i>{esc(short_name(idx[mid]))} <b data-m="{mid}">0.00 m</b></button>'
        for mid in order)
    d2 = {mid: h["paths"][mid]["dist"][k2] for mid in order}
    names2 = ", ".join(f"{short_name(idx[mid])} {num(d2[mid], 2)} m" for mid in order)
    data = json.dumps({"fps": h["fps"], "history": h["history"], "steps": h["steps"], "hold": h["hold"],
                       "frame": h["frame"], "cols": h["cols"], "camera": CAMERA, "start": order[0],
                       "ref_ahead": h["ref_ahead"], "ref_yaw": h["ref_yaw"], "cls": {"rec": "rec", **cls},
                       "names": {"rec": "Recorded drive", **{mid: short_name(idx[mid]) for mid in order}},
                       "sprites": {"rec": "hero-rec.avif", **{mid: f"hero-{mid}.avif" for mid in order}},
                       "paths": {mid: h["paths"][mid] for mid in order}})
    fw, fh = h["frame"]
    return f"""<figure class="cl">
<div class="cl-stage">
<div class="cl-player">
<div class="cl-vid">
<canvas role="img" aria-label="One camera view at a time of one public segment: the recorded drive, or the view worldmodel-4B generated for a model as it drove, with the driver's path and the model's path drawn on the road"></canvas>
<span class="cl-lab"></span>
<span class="cl-time" aria-hidden="true"></span>
</div>
<div class="cl-ctl">
<button type="button" class="cl-play" aria-label="Pause"><i></i></button>
<input type="range" class="cl-scrub" min="0" max="{(h["history"] + h["steps"] + h["hold"]) / h["fps"]:g}" step="0.01" value="0" aria-label="Time in the clip">
</div>
<div class="cl-views" role="group" aria-label="Camera view">{btns}</div>
</div>
{svg}
</div>
<figcaption><p><strong>One public segment in closed loop.</strong> Each model drove comma's ideal car for 7.2 s from the same recorded start while <code>worldmodel-4B</code> generated the camera view it saw. Recorded shows the drive as logged. Dashed yellow is the driver's path, solid is the model's own, the ring is where each is at 2 s. Distance from the driver's path at 2 s: {esc(names2)}.</p>
<p class="cl-how"><code>worldmodel-4B</code> generates one {fw}&times;{fh} frame per 0.2 s step, 5 frames per second. The frames are shown at that resolution, scaled to the screen by the browser, with no frames added between them. The page draws the paths, rings and distances from the logged positions, moving between the 0.2 s steps.</p>
<p class="cl-how">Segment <code>{esc(h["segment"])}</code> from comma's public CI data, at {num(h["v0"], 1)} m/s on a left curve, chosen because it is the segment where these three models differ most at 2 s. On every other segment all three are within 0.35 m of the driver's path at 2 s. Seed {h["seed"]} of the two the scores average, re-run to keep the frames; its paths are within 0.15 m of the scored run.</p></figcaption>
<script type="application/json" id="cl-data">{data}</script>
</figure>"""


# ---------------------------------------------------------------- colours and counts

G55 = ("truck_closed_loop", "xtrack_2s")


def highlight_class(m, cfg) -> str:
    return {h[0]: h[1] for h in cfg.get("hero", {}).get("highlight", [])}.get(m["model"]["id"], "other")


def cm(v: float) -> str:
    return num(v * 100, 1)


def model_class(m, cfg) -> str:
    """The colour a model wears on the front page, by what it is: the reference (lebowski), a model first
    public before it, one shipped or on openpilot master, or a Hugging Face upload only."""
    ref = cfg["reference_model"]
    if m["model"]["id"] == ref:
        return "ref"
    ref_m = by_id(MODELS).get(ref)
    if ref_m and first_public(m) < first_public(ref_m):
        return "old"
    return "ship" if shipped(m) else "hf"


def ol_counts(models) -> str:
    secs = [s for s in ((m["sections"].get("truck_open_loop") or {}) for m in models) if s.get("status") == "ok"]
    return f'{max((s.get("n", 0) for s in secs), default=0)} windows from {max((s.get("clusters", 0) for s in secs), default=0)} drives.'


def g55_counts(models) -> tuple[int, int]:
    secs = [m["sections"].get(G55[0]) or {} for m in models]
    ok = [s for s in secs if s.get("status") == "ok"]
    return (max((s.get("n", 0) for s in ok), default=0), max((s.get("clusters", 0) for s in ok), default=0))

# ---------------------------------------------------------------- per-camera setup matrix (#setups)


# ---------------------------------------------------------------- harness check & living parts

def scored_at(m) -> str:
    return m["scored"]["date"]


def shipped(m) -> bool:
    return bool(m["model"].get("on_master")) or m["model"]["source"]["kind"] in ("release", "openpilot-master")


def status_strip(models, cfg, root) -> str:
    latest = max(models, key=lambda m: (scored_at(m), m["model"]["id"]))
    ng = sum(1 for m in models if (m["sections"].get(G55[0]) or {}).get("status") == "ok")
    when = latest["scored"]["date"][:10]
    return (f'<p class="meta">{len(models)} models scored ({ng} in G55 closed loop) · Last updated {esc(when)} · '
            f'Hugging Face and openpilot branches checked every 20 minutes</p>')


def changelog(root) -> list:
    f = root / "CHANGELOG.md"
    if not f.exists():
        f = EVAL / "CHANGELOG.md"
    out = []
    for line in f.read_text().splitlines():
        mo = re.match(r"- (\d{4}-\d{2}-\d{2}): (.*?)(?: \[([^\]]+)\]\(([^)]+)\))?$", line.strip())
        if mo:
            out.append((mo.group(1), "change", mo.group(2), mo.group(4), mo.group(3)))
    return out


def log_entries(models, root) -> list:
    ev = changelog(root)
    runs = {}
    for m in models:
        md, src, mid = m["model"], m["model"]["source"], m["model"]["id"]
        page_url = f"models/{mid}.html"
        nm = plain_name(m)
        seen = set()

        def add(date, text, kind="model"):
            if (date, text) not in seen:
                seen.add((date, text))
                ev.append((date, kind, f"{nm} {text}", page_url, None))
        if md.get("on_master"):
            add(md["on_master"], "merged to openpilot master")
        if src["kind"] == "huggingface":
            add(src["date"], "uploaded to Hugging Face")
        if src["kind"] == "release":
            add(src["date"], f"shipped in openpilot {src['ref']}")
        if src["kind"] == "openpilot-master" and src["date"] != md.get("on_master"):
            add(src["date"], "merged to openpilot master again")
        if first_public(m) not in {d for d, _ in seen}:
            add(first_public(m), "first public")
        runs.setdefault((scored_at(m)[:10], m["scored"]["suite"]), []).append((first_public(m), nm, page_url))
    for (date, suite), ms in runs.items():
        ms.sort(reverse=True)
        ev.append((date, "scored", f"{len(ms)} model{'s' if len(ms) > 1 else ''} scored, suite {suite}",
                   [(nm, pg) for _, nm, pg in ms], None))
    order = {"change": 0, "scored": 1, "model": 2}
    ev.sort(key=lambda e: (e[0], -order[e[1]]), reverse=True)
    return ev


def log_feed(models, root) -> str:
    ev = log_entries(models, root)
    rows, last = [], None
    for date, kind, text, link, lab in ev:
        rep = ' class="rep"' if date == last else ""
        last = date
        body = esc(text)
        if isinstance(link, list):
            body += ": " + ", ".join(f'<a href="{esc(pg)}">{esc(nm)}</a>' for nm, pg in link)
        elif link and kind != "change":
            body = f'<a href="{esc(link)}">{body}</a>'
        elif link and ranking.is_private_link(link):    # a private repo: name the commit, link nothing
            body += f' <code class="lg-c">{esc(lab or link.rsplit("/", 1)[-1][:7])}</code>'
        elif link:
            body += f' <a class="lg-c" href="{esc(link)}">{esc(lab or "link")}</a>'
        rows.append(f'<li class="lg-{kind}"><time datetime="{date}"{rep}>{date}</time><span class="lg-k">{ {"change": "suite", "scored": "scored", "model": "model"}[kind]}</span><span>{body}</span></li>')
    head, rest = rows[:12], rows[12:]
    more = f'<details class="lg-more"><summary>{len(rest)} earlier entries</summary><ol class="log" data-history>{"".join(rest)}</ol></details>' if rest else ""
    return f'<ol class="log" data-history>{"".join(head)}</ol>{more}'


# ---------------------------------------------------------------- the results tables

# One table per test: (anchor, title, section, columns, what the test is). A column: (metric key, header,
# scale, unit). Each cell is the mean over units with its 95% interval and the median beneath.
TABLES = [
    ("g55-closed-loop", "G55 closed loop", "truck_closed_loop",
     [("xtrack_2s", "Distance from the recorded path at 2 s", 100, "cm"),
      ("xtrack_4s", "Distance from the recorded path at 4 s", 100, "cm"),
      ("lat_jerk", "Requested lateral jerk, RMS", 1, "m/s³")],
     "Each model drives the G55 simulation inside comma's worldmodel-4B, which renders the next camera frame "
     "from the model's own steering, for 7.2 s from the start of a recorded window. Distance is to the "
     "driver's recorded path."),
    ("g55-recovery", "G55 recovery tests", "truck_recovery",
     [("heading_xtrack_2s", "After a 2° heading error: distance at 2 s", 100, "cm"),
      ("offcentre_xtrack_2s", "After a 0.5 m lateral offset: distance at 2 s", 100, "cm"),
      ("brake_ahead_4s", "Braking lead: distance ahead of the recorded car at 4 s", 1, "m")],
     "The G55 closed loop with a disturbance at the start of the window, or on the windows where the car "
     "ahead braked."),
    ("g55-open-loop", "G55 open loop", "truck_open_loop",
     [("lat_mae", "Lateral error", 1, "m/s²"),
      ("lat_rate_rms", "Lateral action rate, RMS", 1, "m/s³")],
     "Each model runs on the G55's recorded camera frames. Its lateral request is compared with the "
     "truck's measured lateral acceleration; the action rate is how fast the request changes."),
    ("public-closed-loop", "comma's public segments, closed loop", "public_closed_loop",
     [("xtrack_2s", "Distance from the recorded path at 2 s", 100, "cm")],
     "comma's public two-camera segments in worldmodel-4B with comma's ideal car, a vehicle that follows "
     "each command exactly."),
    ("public-open-loop", "comma's public segments, open loop", "public_open_loop",
     [("lat_mae", "Lateral error", 1, "m/s²")],
     "comma's public two-camera segments replayed through each model, against the recorded car's "
     "lateral acceleration."),
]


def result_cell(x: dict | None, scale: float, unit: str, label: str, m: dict) -> str:
    if not x or x.get("value") is None:
        return f'<td class="na" data-l="{esc(label)}">not run</td>'
    d = 1 if scale == 100 else decimals_for(x["value"])
    ci = x.get("ci")
    iv = (f'<span class="ci">[{num(ci[0] * scale, d)}, {num(ci[1] * scale, d)}]</span>' if ci else
          f'<span class="ci">no interval ({x.get("clusters", 0)} drive{"s" if x.get("clusters", 0) != 1 else ""})</span>')
    med = f'<span class="med">median {num(x["median"] * scale, d)}</span>' if x.get("median") is not None else ""
    tip = f'{plain_name(m)}, {label.lower()}: mean {num(x["value"] * scale, d)} {unit}' + (
        f', 95% interval {num(ci[0] * scale, d)} to {num(ci[1] * scale, d)}' if ci else ", no interval") + (
        f', median {num(x["median"] * scale, d)} {unit}' if x.get("median") is not None else "") + (
        f', {x.get("n")} units from {x.get("clusters")} drives')
    return (f'<td class="n" data-l="{esc(label)} ({esc(unit)})" tabindex="0" data-tip="{esc(tip)}" aria-label="{esc(tip)}">'
            f'{num(x["value"] * scale, d)}<span class="u"> {esc(unit)}</span>{iv}{med}</td>')


def simulator_of(models, section: str) -> str | None:
    """The one simulator every model's `section` ran on; the build stops on a table that would mix two."""
    sims = {(m["sections"].get(section) or {}).get("simulator") for m in models
            if (m["sections"].get(section) or {}).get("status") == "ok"}
    if len(sims) > 1:
        raise SystemExit(f"{section}: results from more than one simulator {sorted(map(str, sims))}; "
                         f"rescore every model on one before publishing")
    return next(iter(sims), None)


def protocol_of(models) -> str:
    suites = sorted({m["scored"]["suite"] for m in models})
    return ", ".join(suites)


def revision_text(models) -> str:
    """The benchmark revision: protocol and G55 simulator, with what the simulator is."""
    sim = simulator_of(models, "truck_closed_loop")
    if sim and sim != simulator_of(models, "truck_recovery"):
        raise SystemExit("truck_closed_loop and truck_recovery ran on different simulators")
    desc = GS.SIMULATORS.get(sim, "") if sim else ""
    s = f"protocol {protocol_of(models)}"
    if sim:
        s += f", G55 simulator {sim}: {desc}"
    return s


def test_table(models, cfg, spec, up: str = "") -> str:
    anchor, title, section, cols, what = spec
    pool = [m for m in by_release(models) if (m["sections"].get(section) or {}).get("status") == "ok"]
    if not pool:
        return ""
    secs = [m["sections"][section] for m in pool]
    ns = {(x["n"], x.get("clusters")) for x in secs}
    uw = UNIT_WORD.get(secs[0]["unit_of_evidence"], ("unit", "units"))
    if len(ns) == 1:
        n, c = ns.pop()
        count = f"{n} {uw[1] if n != 1 else uw[0]} from {c} drives for every model."
    else:
        count = (f"{min(x['n'] for x in secs)} to {max(x['n'] for x in secs)} {uw[1]} per model, from "
                 f"{min(x.get('clusters', 0) for x in secs)} to {max(x.get('clusters', 0) for x in secs)} drives.")
    sim = simulator_of(pool, section)
    sim_t = f" Simulator {sim}." if sim else ""
    th = "".join(f'<th class="n" scope="col">{esc(lab)}<span class="th-s">{esc(unit)}</span></th>' for _, lab, _, unit in cols)
    rows = []
    for m in pool:
        mid = m["model"]["id"]
        cells = "".join(result_cell(find_metric(m, section, k)[0], sc, unit, lab, m) for k, lab, sc, unit in cols)
        rows.append(f'<tr data-model="{esc(mid)}"><th scope="row"><span class="sw sw-{model_class(m, cfg)}" aria-hidden="true"></span> '
                    f'{name_html(m, f"{up}models/{mid}.html")}<span class="th-s">{esc(first_public(m))}, {esc(lc_status(m))}</span></th>'
                    f'{cells}</tr>')
    cond = secs[0].get("condition", "")
    lim = GS.LIMITS.get(section, "")
    return (f'<section id="{anchor}" class="test">'
            f'<h2>{esc(title)}</h2>'
            f'<p class="sub">{esc(what)} {esc(count)}{esc(sim_t)}</p>'
            f'<div class="tw"><table class="res"><caption>Mean over {esc(uw[1])} with its 95% interval (resampling '
            f'whole drives), median beneath. Newest release first.</caption>'
            f'<thead><tr><th scope="col">Model<span class="th-s">first public · status</span></th>{th}</tr></thead>'
            f'<tbody data-order="release">{"".join(rows)}</tbody></table></div>'
            f'<details class="t-more"><summary>Conditions and limits</summary>'
            f'<p>{esc(cond)}</p>' + (f'<p>{esc(lim)}</p>' if lim else "") + '</details></section>')


def scope_counts(models) -> list[str]:
    out = []
    for section, label in (("truck_closed_loop", "G55 closed loop and recovery"), ("truck_open_loop", "G55 open loop"),
                           ("public_open_loop", "comma's public segments")):
        secs = [m["sections"][section] for m in models if (m["sections"].get(section) or {}).get("status") == "ok"]
        if secs:
            x = max(secs, key=lambda s: s["n"])
            uw = UNIT_WORD.get(x["unit_of_evidence"], ("unit", "units"))
            out.append(f"{label}: {x['n']} {uw[1]} from {x.get('clusters', 0)} drives")
    return out


# ---------------------------------------------------------------- model page

def by_group_block(s: dict) -> str:
    """Render a collapsible table of per-camera-setup results for a G55 section when `by_group` is present."""
    bg = s.get("by_group")
    if not bg:
        return ""
    present = [(gk, gl, gd) for gk, gl, gd in CAMERA_GROUPS if gk in bg]
    if not present:
        return ""
    th = "".join(
        f'<th class="n" scope="col" title="{esc(gd)}">{esc(gl)}'
        f'<span class="th-s">{bg[gk].get("n", 0)} win, {bg[gk].get("clusters", 0)} drv</span></th>'
        for gk, gl, gd in present
    )
    rows = []
    for x in s["metrics"]:
        k = x["key"]
        d = decimals_for(x["value"])
        cells = []
        for gk, _, _ in present:
            gm = next((m for m in bg[gk].get("metrics", []) if m["key"] == k), None)
            if not gm or gm.get("value") is None:
                cells.append('<td class="na">—</td>')
                continue
            med = f'<span class="med">med {num(gm["median"], d)}</span>' if gm.get("median") is not None else ""
            cells.append(f'<td class="n">{num(gm["value"], d)}{med}</td>')
        rows.append(f'<tr><th scope="row">{esc(LABEL.get(k, x["label"]))}<span class="th-s">{esc(x["unit"])}</span></th>{"".join(cells)}</tr>')
    return (
        f'<details class="bg-det"><summary>By G55 camera setup ({len(present)} setups)</summary>'
        f'<div class="tw"><table><thead><tr><th scope="col">Metric</th>{th}</tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div></details>'
    )


def section_block(m, key, s, models, root, out) -> str:
    title = SECTIONS.get(key, key)
    st = s["status"]
    if st != "ok":
        msg = {"not_run": "Not run for this model yet.", "failed": "The run failed; there are no scores."}.get(st, st)
        return f'<section class="sec sec-off"><h3>{esc(title)}</h3><p>{msg}</p></section>'
    uw = UNIT_WORD.get(s["unit_of_evidence"], (s["unit_of_evidence"],) * 2)
    bases = s.get("baselines", m.get("baselines", []))
    bname = {o["model"]["id"]: short_name(o) for o in models}
    th = "".join(f'<th class="n" scope="col">vs {esc(bname.get(b, b))}</th>' for b in bases)
    rows = []
    for x in s["metrics"]:
        d = decimals_for(x["value"])
        cells = "".join(delta_cell(x, x["paired"].get(b), d) for b in bases)
        nnote = f'<span class="wins">n = {x["n"]}</span>' if x.get("n") and x["n"] != s["n"] else ""
        med = f'<span class="med">median {num(x["median"], d)}</span>' if x.get("median") is not None else ""
        rows.append(f'<tr><th scope="row">{esc(LABEL.get(x["key"], x["label"]))}<span class="th-s">{esc(", ".join(p for p in (x["unit"], "no better direction" if x["key"] in NEUTRAL else BETTER_TEXT[x["better"]]) if p))}</span></th>'
                    f'<td class="n">{num(x["value"], d)}<span class="ci">{ci_text(x["ci"], d)}</span>{med}{nnote}</td>{cells}</tr>')
    dl = ""
    if s.get("per_unit_file"):
        dl = f' <a href="../{esc(copy_rel(root, out, s["per_unit_file"]))}">Per-{uw[0]} CSV</a>.'
    cl = f' from {s["clusters"]} drives' if s.get("clusters") else ""
    return (f'<section class="sec"><h3>{esc(title)}</h3>'
            f'<p class="sec-m"><strong>{s["n"]} {uw[1] if s["n"] != 1 else uw[0]}{cl}.</strong>{dl}</p>'
            f'<p class="cond">{esc(s["condition"])}</p>'
            f'<div class="tw"><table><thead><tr><th scope="col">Metric</th><th class="n" scope="col">Mean</th>{th}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>'
            f'{by_group_block(s)}</section>')


def copy_rel(root: Path, out: Path, rel: str) -> str:
    src = (root / rel).resolve()
    if not src.is_relative_to(root.resolve()):
        raise SystemExit(f"{rel}: outside {root}")
    if not src.is_file():
        raise SystemExit(f"{rel}: referenced by a results file but missing under {root}")
    dst = out / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return rel


# ---------------------------------------------------------------- method page

def method(models, cfg, root) -> str:
    prose = (SITE / "pages" / "method.html").read_text()
    prose = prose.replace("<!-- closed-loop example -->", f'<section id="closed-loop-example" class="cl-ex"><h2>A closed-loop example</h2>{hero_visual(models, cfg)}</section>')
    revs = "".join(f"<li><code>{esc(k)}</code>: {esc(v)}</li>" for k, v in GS.SIMULATORS.items())
    prose = prose.replace("<!-- simulators -->", f'<ul>{revs}</ul>')
    rows = []
    for m in by_release(models):
        md = m["model"]
        csvs = [f'<a href="{esc(s["per_unit_file"])}">{esc(SECTIONS.get(k, k))}</a>'
                for k, s in m["sections"].items() if s.get("per_unit_file")]
        rows.append(f'<tr data-model="{esc(md["id"])}"><th scope="row">{name_html(m, "models/" + md["id"] + ".html")}</th>'
                    f'<td><a href="results/{esc(md["id"])}.json">{esc(md["id"])}.json</a></td>'
                    f'<td>{", ".join(csvs) or "none"}</td></tr>')
    table = ('<section id="downloads" class="prose"><h2>Downloads</h2><p>One results file per model, in the format described in '
             '<a href="schema.txt">schema.txt</a>, and one CSV per public section with a row per segment. '
             'G55 sections publish scores only.</p>'
             '<div class="tw"><table><thead><tr><th scope="col">Model</th><th scope="col">Results</th><th scope="col">Per-segment CSVs</th></tr></thead>'
             f'<tbody data-order="release">{"".join(rows)}</tbody></table></div></section>')
    return prose + table


# ================================================================ the living site (2026-11-05)

import datetime as _dt  # noqa: E402

FAMILIES = [  # (results-file family, lane name, what it is), oldest architecture first
    ("supercombo", "One network", "supercombo"),
    ("vision-policy", "Vision + policy", "two networks"),
    ("lebowski", "lebowski", "877M encoder"),
    ("big-rl", "Big RL", "no correction head"),
    ("residual", "RL residual", "base + learned correction"),
]
FAMILY_NAME = {k: n for k, n, _ in FAMILIES}
# Where a model shipped is recorded in its notes; these are the forms the results files use.
RELEASE_RX = [re.compile(r"Shipped in openpilot (\d+\.\d+\.\d+)"), re.compile(r"In the openpilot (\d+\.\d+\.\d+) tag"),
              re.compile(r"The (\d+\.\d+\.\d+)(?: and \d+\.\d+\.\d+)? tags carry")]
STATE_TEXT = {"release": "shipped in an openpilot release", "master": "on openpilot master", "upload": "on Hugging Face only"}
# One headline number per test: the first column of its table.
TESTS = [(spec[0], spec[1], spec[2], spec[3][0]) for spec in TABLES]   # (slug, title, section, (key, label, scale, unit))
TEST_BY_SECTION = {t[2]: t for t in TESTS}
TEST_WORDS = {  # changelog lines that concern each test: any group whose words all appear
    "truck_closed_loop": (("g55", "closed"), ("simulator",), ("closed-loop",)),
    "truck_recovery": (("recovery",), ("simulator",)),
    "truck_open_loop": (("g55", "open loop"), ("truck", "open loop")),
    "public_closed_loop": (("public", "closed loop"), ("public", "closed-loop")),
    "public_open_loop": (("public", "open loop"), ("public", "open-loop")),
}
# a changelog line whose first sentence is about how the site presents results, not about a test
SITE_LINE = re.compile(r"\b(the site|front page|leaderboard|ranking|player|video|page)\b", re.I)
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]


def d_of(s: str) -> _dt.date:
    return _dt.date.fromisoformat(s[:10])


def nice_date(s: str, year: bool = True) -> str:
    d = d_of(s)
    return f"{d.day} {MONTHS[d.month - 1][:3]}" + (f" {d.year}" if year else "")


def release_of(m: dict) -> str | None:
    src = m["model"]["source"]
    if src["kind"] == "release":
        return src["ref"].lstrip("v")
    for rx in RELEASE_RX:
        mo = rx.search(m["model"].get("notes") or "")
        if mo:
            return mo.group(1)
    return None


def state_of(m: dict) -> str:
    return "release" if release_of(m) else "master" if m["model"].get("on_master") else "upload"


def state_line(m: dict) -> str:
    r = release_of(m)
    if r:
        return f"shipped in openpilot {r}"
    if m["model"].get("on_master"):
        return f"on openpilot master since {nice_date(m['model']['on_master'])}"
    return "on Hugging Face, not on openpilot master"


def newest(models) -> dict:
    return max(models, key=lambda m: (first_public(m), m["model"]["id"]))


def section_ok(m, section) -> bool:
    return (m["sections"].get(section) or {}).get("status") == "ok"


def metric_text(x: dict | None, scale: float, unit: str, ci: bool = True) -> str:
    if not x or x.get("value") is None:
        return "not run"
    d = 1 if scale == 100 else decimals_for(x["value"])
    t = f"{num(x['value'] * scale, d)} {unit}"
    if ci and x.get("ci"):
        t += f" ({num(x['ci'][0] * scale, d)} to {num(x['ci'][1] * scale, d)})"
    return t


def model_link(m, up: str) -> str:
    return f"{up}models/{m['model']['id']}.html"


# ---------------------------------------------------------------- page shell

def page(cfg: dict, title: str, desc: str, body: str, depth: int, current: str, path: str, extra_head: str = "") -> str:
    up = "../" * depth
    css_v, js_v = _asset_hash("style.css"), _asset_hash("site.js")
    umami = ""
    if cfg.get("umami_website_id"):
        dom = f' data-domains="{esc(cfg["umami_domains"])}"' if cfg.get("umami_domains") else ""
        umami = (f'<script defer src="{esc(cfg["umami_src"])}" '
                 f'data-website-id="{esc(cfg["umami_website_id"])}"{dom}></script>')
    nav = []
    for href, label, key in ((f"{up}index.html", "Releases", "releases"), (f"{up}tests/index.html", "Tests", "tests"),
                             (f"{up}inside/index.html", "Inside the models", "inside"), (f"{up}method.html", "Method", "method"),
                             (f"{up}log.html", "Log", "log")):
        cur = ' aria-current="page"' if key == current else ""
        nav.append(f'<a href="{href}"{cur}>{label}</a>')
    site = cfg["site_url"].rstrip("/")
    og = f"{site}/og.png"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(desc)}">
<link rel="canonical" href="{esc(site)}/{esc(path)}">
<link rel="alternate" type="application/rss+xml" title="{esc(cfg['site_name'])}" href="{up}feed.xml">
<meta property="og:type" content="website">
<meta property="og:site_name" content="{esc(cfg['site_name'])}">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(desc)}">
<meta property="og:url" content="{esc(site)}/{esc(path)}">
<meta property="og:image" content="{esc(og)}">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:image:alt" content="openpilot driving model evaluations">
<meta name="twitter:card" content="summary_large_image">
<meta name="color-scheme" content="light dark">
<script>try{{var t=localStorage.getItem("theme");if(t==="light"||t==="dark")document.documentElement.dataset.theme=t}}catch(e){{}}</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Barlow:ital,wght@0,400;0,500;0,600;1,400&family=Barlow+Semi+Condensed:wght@500;600;700&display=swap">
<link rel="stylesheet" href="{up}style.css?v={css_v}">
<link rel="icon" href="{up}favicon.svg" type="image/svg+xml">
{extra_head}{umami}
</head>
<body>
<a class="skip" href="#main">Skip to content</a>
<header class="top"><div class="wrap top-in">
<a class="word" href="{up}index.html">{esc(cfg['site_name'])}</a>
<nav aria-label="Site">{''.join(nav)}<a href="{esc(cfg['repo_url'])}">Code</a><button class="theme" type="button" aria-label="Switch light or dark theme"><span aria-hidden="true"></span></button></nav>
</div></header>
<main id="main" class="wrap">
{body}
</main>
<footer class="foot"><div class="wrap foot-in">
<p>Independent tests of comma.ai's openpilot driving models, not affiliated with comma.ai. Results come from one 2011 Mercedes-Benz G55 AMG (W463) and comma's public segments.</p>
<p><a href="{up}feed.xml">RSS</a> <a href="{up}method.html#downloads">Results files</a> <a href="{esc(cfg['repo_url'])}/tree/main/eval">Code</a> <a href="{up}schema.txt">File format</a></p>
</div></footer>
<div id="tip" class="tip" role="status" hidden></div>
<script src="{up}site.js?v={js_v}" defer></script>
</body>
</html>
"""


# ---------------------------------------------------------------- the release timeline

LABEL_PX = 7.0      # approximate width of one label character at the timeline's type size
TRACK_PX = 1000     # the width the label layout assumes (the track never draws narrower; it scrolls)


class Axis:
    """A time axis whose last 120 days get the right 62% of the width: releases now come weeks apart, and
    in 2023-25 months apart."""

    SPLIT = 38.0

    def __init__(self, dates: list[str], end: str, pad: int = 45, compress: bool = True):
        self.start = min(d_of(x) for x in dates) - _dt.timedelta(days=pad)
        self.end = d_of(end) + _dt.timedelta(days=12)
        self.brk = self.end - _dt.timedelta(days=120)
        if self.brk <= self.start or not compress:
            self.brk = self.start

    def x(self, s) -> float:
        d = d_of(s) if isinstance(s, str) else s
        if self.brk == self.start or d >= self.brk:
            a, b, lo, hi = self.brk, self.end, (0.0 if self.brk == self.start else self.SPLIT), 100.0
        else:
            a, b, lo, hi = self.start, self.brk, 0.0, self.SPLIT
        return lo + (d - a).days / max(1, (b - a).days) * (hi - lo)

    def ticks(self) -> list[tuple[float, str, str]]:
        """(x, label, kind): years before the break, months after it."""
        out = []
        for y in range(self.start.year + 1, self.brk.year + 1):
            d = _dt.date(y, 1, 1)
            if self.start < d < self.brk:
                out.append((self.x(d), str(y), "year"))
        d = _dt.date(self.brk.year, self.brk.month, 1)
        while d <= self.end:
            if d >= self.brk:
                out.append((self.x(d), MONTHS[d.month - 1][:3] + (f" {d.year}" if d.month == 1 else ""), "month"))
            d = _dt.date(d.year + (d.month == 12), d.month % 12 + 1, 1)
        return out


def place_labels(items: list[tuple[float, str]]) -> list[str | None]:
    """Below or above each mark, or nowhere when both are taken (the name is then in the mark's tooltip)."""
    ends = {"b": -1e9, "a": -1e9}
    out = []
    for x, name in items:
        px = x / 100 * TRACK_PX
        w = len(name) * LABEL_PX + 10
        left = px - w / 2
        for side in ("b", "a"):
            if left > ends[side]:
                ends[side] = px + w / 2
                out.append(side)
                break
        else:
            out.append(None)
    return out


def mark_html(m: dict, x: float, up: str, label: str | None, is_new: bool, here: bool = False, top: float = 32, lab_off: float = 13) -> str:
    st = state_of(m)
    name = short_name(m)
    tip = f"{plain_name(m)}: first public {nice_date(first_public(m))}, {state_line(m)}"
    cls = f"tm tm-{st}" + (" tm-new" if is_new else "") + (" tm-here" if here else "")
    side = {"b": "top", "a": "bottom"}.get(label or "", "top")
    lab = f'<span class="tl tl-{label}" style="{side}:{lab_off:.0f}px">{esc(name)}</span>' if label else ""
    return (f'<a class="{cls}" style="left:{x:.2f}%;top:{top:.0f}px" href="{esc(model_link(m, up))}" data-model="{esc(m["model"]["id"])}" '
            f'data-tip="{esc(tip)}" aria-label="{esc(tip)}"><i aria-hidden="true"></i>{lab}</a>')


DODGE_PX, STEP_PX = 17, 14   # marks closer than this on the assumed track stack vertically, this far apart


def dodge(xs: list[float]) -> list[int]:
    """A level per mark (0 on the line, then -1 above, +1 below, -2, +2 ...) so no two marks overlap."""
    order = [0, -1, 1, -2, 2, -3, 3]
    placed: dict[int, list[float]] = {}
    out = []
    for x in xs:
        px = x / 100 * TRACK_PX
        for lv in order:
            if all(abs(px - q) >= DODGE_PX for q in placed.get(lv, [])):
                placed.setdefault(lv, []).append(px)
                out.append(lv)
                break
    return out


def timeline(models, up: str = "", only_family: str | None = None, here: str | None = None) -> str:
    """comma's releases on one time axis, one lane per architecture family. A lane is a chronological list
    (data-order="timeline", oldest first): position is the first public date, nothing else."""
    nw = newest(models)["model"]["id"]
    end = max([first_public(m) for m in models] + [scored_at(m)[:10] for m in models])
    pool = [m for m in models if only_family is None or m["model"]["family"] == only_family]
    if only_family is not None:   # a family's own lane spans that family, not the whole site
        end = max(first_public(m) for m in pool)
    ax = Axis([first_public(m) for m in pool], end, pad=45 if only_family is None else 10, compress=only_family is None)
    lanes = []
    for fam, name, what in FAMILIES:
        ms = [m for m in models if m["model"]["family"] == fam and m in pool]
        if not ms:
            continue
        ms.sort(key=lambda m: (first_public(m), m["model"]["id"]))
        xs = [ax.x(first_public(m)) for m in ms]
        lv = dodge(xs)
        n_up, n_dn = -min(lv), max(lv)
        mid = 32 + n_up * STEP_PX
        # names only for marks on the line; a stacked mark's name is in its tooltip and on its page
        flat = [i for i, v in enumerate(lv) if v == 0]
        got = dict(zip(flat, place_labels([(xs[i], short_name(ms[i])) for i in flat])))
        marks = "".join(mark_html(m, x, up, got.get(i), m["model"]["id"] == nw, m["model"]["id"] == here,
                                  top=mid + lv[i] * STEP_PX, lab_off=13 + (n_dn if got.get(i) == "b" else n_up) * STEP_PX)
                        for i, (m, x) in enumerate(zip(ms, xs)))
        span = f'<span class="ln-span" style="left:{xs[0]:.2f}%;width:{max(0.4, xs[-1] - xs[0]):.2f}%;top:{mid - 1}px"></span>'
        lanes.append(f'<div class="ln"><div class="ln-h"><b>{esc(name)}</b><span>{esc(what)}</span></div>'
                     f'<div class="ln-t" data-order="timeline" style="height:{64 + (n_up + n_dn) * STEP_PX}px">{span}{marks}</div></div>')
    if only_family is None:
        f = SITE / "models.json"
        extra = [e for e in (json.loads(f.read_text())["unscored"] if f.exists() else []) if e.get("date")]
        if extra:
            xs = [ax.x(e["date"]) for e in extra]
            marks = "".join(
                f'<span class="tm tm-none" style="left:{x:.2f}%" tabindex="0" data-tip="{esc(e["name"] + ", " + nice_date(e["date"]) + ": not scored here (" + e["why"] + ")")}">'
                f'<i aria-hidden="true"></i><span class="sr">{esc(e["name"])}, not scored here</span></span>' for e, x in zip(extra, xs))
            lanes.append(f'<div class="ln ln-none"><div class="ln-h"><b>Not scored</b><span>no usable file</span></div><div class="ln-t">{marks}</div></div>')
    ticks = "".join(f'<span class="tk tk-{k}" style="left:{x:.2f}%">{esc(t)}</span>' for x, t, k in ax.ticks())
    grid = "".join(f'<i class="tg tg-{k}" style="left:{x:.2f}%"></i>' for x, _, k in ax.ticks())
    brk = (f'<span class="tbrk" style="left:{Axis.SPLIT:.2f}%" data-tip="The last 120 days are drawn wider: releases now come weeks apart." '
           f'tabindex="0" aria-label="Scale changes here: the last 120 days are drawn wider"></span>') if ax.brk > ax.start else ""
    return (f'<div class="tlw"><div class="tlx"><div class="tlg" aria-hidden="true">{grid}{brk}</div>{"".join(lanes)}'
            f'<div class="ln ln-ax"><div class="ln-h"></div><div class="ln-t tks">{ticks}</div></div></div></div>')


def timeline_key() -> str:
    return ('<p class="tkey"><span><i class="tm-k tm-release"></i>shipped in an openpilot release</span>'
            '<span><i class="tm-k tm-master"></i>on openpilot master</span>'
            '<span><i class="tm-k tm-upload"></i>on Hugging Face only</span>'
            '<span><i class="tm-k tm-new"></i>newest</span>'
            '<span><i class="tm-k tm-none"></i>not scored here</span></p>')


# ---------------------------------------------------------------- results in brief

def brief(m: dict, up: str) -> str:
    """Every test's headline number for one release, in test order; not-run tests say so."""
    items = []
    for slug, title, section, (key, label, scale, unit) in TESTS:
        x, s = find_metric(m, section, key)
        if not s:
            continue
        v = metric_text(x, scale, unit, ci=True) if x else ("not run" if s["status"] == "not_run" else s["status"])
        items.append(f'<div><dt><a href="{up}tests/{slug}.html">{esc(title)}</a></dt><dd{" class=\"off\"" if not x else ""}>{esc(v)}</dd></div>')
    return f'<dl class="brief">{"".join(items)}</dl>'


# ---------------------------------------------------------------- the feed

def explainers() -> list[dict]:
    f = SITE / "explainers.json"
    return json.loads(f.read_text())["explainers"] if f.exists() else []


def feed_items(models, root) -> list[dict]:
    """Everything that happened, newest first: releases, scoring runs, changes to the tests, explainers."""
    idx = by_id(models)
    first = {m["model"]["id"]: first_public(m) for m in models}
    out = []
    for date, kind, text, link, lab in log_entries(models, root):
        if kind == "change":
            head, _, rest = text.partition(". ")
            href = link if link and not ranking.is_private_link(link) else None
            out.append({"date": date, "kind": "test", "title": head.rstrip(".") + ".", "body": rest, "href": href,
                        "href_label": (lab or (link or "").rsplit("/", 1)[-1][:7]) if href else None, "model": None})
        elif kind == "scored":
            out.append({"date": date, "kind": "run", "title": text, "links": link, "model": None})
        else:
            mid = link.split("/")[-1][:-5]
            out.append({"date": date, "kind": "release", "title": text, "page": link, "model": mid,
                        "results": first.get(mid) == date and mid in idx})
    for e in explainers():
        out.append({"date": e["date"], "kind": "explainer", "title": e["title"], "page": e["href"], "body": e["summary"], "model": None})
    order = {"explainer": 0, "release": 1, "run": 2, "test": 3}
    out.sort(key=lambda e: (e["date"], -order[e["kind"]]), reverse=True)
    return out


KIND_TEXT = {"release": "Release", "run": "Tested", "test": "Test change", "explainer": "Explainer"}


def feed_html(models, root, up: str, limit: int | None = None) -> str:
    idx = by_id(models)
    items = feed_items(models, root)
    shown = items if limit is None else items[:limit]
    rows, month = [], None
    for e in shown:
        mo = e["date"][:7]
        if mo != month:
            if month is not None:
                rows.append("</ol>")
            d = d_of(e["date"])
            rows.append(f'<h3 class="fm">{MONTHS[d.month - 1]} {d.year}</h3><ol class="feed" data-history>')
            month = mo
        k = e["kind"]
        if k in ("release", "explainer"):
            title = f'<a href="{up}{esc(e["page"])}">{esc(e["title"])}</a>'
        else:
            title = esc(e["title"])
        body = ""
        if k == "run" and e.get("links"):
            body = "<p>" + ", ".join(f'<a href="{up}{esc(pg)}">{esc(nm)}</a>' for nm, pg in e["links"]) + "</p>"
        elif e.get("body"):
            body = f"<p>{esc(e['body'])}</p>"
        if k == "test" and e.get("href"):
            body += f'<p class="fc"><a href="{esc(e["href"])}">commit {esc(e["href_label"])}</a></p>'
        if k == "release" and e.get("results"):
            body += brief(idx[e["model"]], up)
        rows.append(f'<li class="fe fe-{k}" data-date="{e["date"]}"><time datetime="{e["date"]}">{d_of(e["date"]).day}</time>'
                    f'<div><p class="fk">{KIND_TEXT[k]}</p><p class="ft">{title}</p>{body}</div></li>')
    rows.append("</ol>")
    more = "" if limit is None or len(items) <= limit else f'<p class="more"><a href="{up}log.html">All {len(items)} entries since {nice_date(items[-1]["date"])}</a></p>'
    return f'<p class="since" hidden></p>{"".join(rows)}{more}'


def rss(models, root, cfg) -> str:
    site = cfg["site_url"].rstrip("/")
    items = []
    for e in feed_items(models, root)[:60]:
        if e["kind"] == "run":
            continue
        link = f'{site}/{e["page"]}' if e.get("page") else (e.get("href") or f"{site}/log.html")
        d = d_of(e["date"])
        pub = _dt.datetime(d.year, d.month, d.day, 12, 0, tzinfo=_dt.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
        desc = e.get("body") or ""
        if e["kind"] == "release" and e.get("results"):
            m = by_id(models)[e["model"]]
            parts = []
            for slug, title, section, (key, label, scale, unit) in TESTS:
                x, s = find_metric(m, section, key)
                if s:
                    parts.append(f"{title}: {metric_text(x, scale, unit) if x else 'not run'}")
            desc = "; ".join(parts)
        guid = f'{site}/log.html#{e["date"]}-{hashlib.sha256((e["kind"] + "|" + e["title"]).encode()).hexdigest()[:10]}'
        items.append(f"<item><title>{esc(KIND_TEXT[e['kind']] + ': ' + e['title'])}</title><link>{esc(link)}</link>"
                     f"<guid isPermaLink=\"false\">{esc(guid)}</guid><pubDate>{pub}</pubDate><description>{esc(desc)}</description></item>")
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0"><channel><title>{esc(cfg["site_name"])}</title>'
            f'<link>{esc(site)}/</link><description>New openpilot driving models and their test results on one 2011 Mercedes-Benz G55.</description>'
            f'<language>en</language>{"".join(items)}</channel></rss>\n')


# ---------------------------------------------------------------- the front page

def index_page(models, cfg, root) -> str:
    nw = newest(models)
    done = sum(1 for s in nw["sections"].values() if s.get("status") == "ok")
    tot = len(nw["sections"])
    last = max(scored_at(m) for m in models)
    side_ex = "".join(f'<li><a href="{esc(e["href"])}">{esc(e["title"])}</a><span>{esc(nice_date(e["date"]))}, {esc(e["covers"])}</span></li>' for e in explainers())
    tests = "".join(
        f'<li><a href="tests/{slug}.html">{esc(title)}</a><span>{sum(1 for m in models if section_ok(m, section))} of {len(models)} releases</span></li>'
        for slug, title, section, _ in TESTS)
    return f"""<section class="lead">
<h1>comma's driving models, {len(models)} releases</h1>
<p>Each one tested on a 2011 Mercedes-Benz G55 and on comma's public segments. A new release on Hugging Face or openpilot master is found within 20 minutes and tested the same day. Last results {esc(nice_date(last))}.</p>
<p class="nw">Newest: <a href="{esc(model_link(nw, ''))}">{esc(plain_name(nw))}</a>, first public {esc(nice_date(first_public(nw)))}, {esc(state_line(nw))}. {done} of {tot} tests done.</p>
</section>
<section class="tlsec" aria-label="Release timeline">
{timeline(models)}
{timeline_key()}
<p class="swipe">Swipe the timeline sideways for earlier releases.</p>
</section>
<div class="cols">
<section class="feedsec" aria-labelledby="what-happened">
<h2 id="what-happened">What happened</h2>
<p class="sub">New releases with their results, test runs, changes to the tests and explainers. Newest first.</p>
{feed_html(models, root, "", limit=16)}
</section>
<aside class="side">
<section><h2>Tests</h2><ul class="sl">{tests}</ul></section>
<section><h2>Explainers</h2><ul class="sl">{side_ex}</ul></section>
<section><h2>Follow along</h2><ul class="sl"><li><a href="feed.xml">RSS feed</a><span>releases, results, test changes</span></li>
<li><a href="method.html#downloads">Results files</a><span>one JSON file per release</span></li>
<li><a href="{esc(cfg['repo_url'])}/tree/main/eval">The harness and this site</a><span>on GitHub</span></li></ul></section>
</aside>
</div>"""


def log_page(models, root) -> str:
    return (f'<section class="lead"><h1>Log</h1><p>Every release, test run and change to the tests since {nice_date(feed_items(models, root)[-1]["date"])}, newest first. '
            f'Also as an <a href="feed.xml">RSS feed</a>.</p></section><section class="feedsec">{feed_html(models, root, "")}</section>')


# ---------------------------------------------------------------- interval strips (release and test pages)

def nice_ticks(lo: float, hi: float, n: int = 4) -> list[float]:
    span = hi - lo
    if span <= 0:
        return [lo]
    step = 10 ** math.floor(math.log10(span / n))
    for k in (1, 2, 2.5, 5, 10):
        if span / (step * k) <= n:
            step *= k
            break
    t = math.ceil(lo / step) * step
    out = []
    while t <= hi + 1e-12:
        out.append(round(t, 10))
        t += step
    return out


def scale_for(models, section, key, scale) -> tuple[float, float]:
    vals = []
    for m in models:
        x, _ = find_metric(m, section, key)
        if x and x.get("value") is not None:
            vals += [x["value"] * scale] + ([c * scale for c in x["ci"]] if x.get("ci") else [])
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.08 or abs(hi) * 0.1 or 1
    return lo - pad, hi + pad


def strip(x: dict | None, lo: float, hi: float, scale: float, others: list[float] = (), cls: str = "") -> str:
    p = lambda v: f"{(v - lo) / (hi - lo) * 100:.2f}%"  # noqa: E731
    o = "".join(f'<i class="so" style="left:{p(v)}"></i>' for v in others)
    if not x or x.get("value") is None:
        return f'<span class="st st-off">{o}</span>'
    v = x["value"] * scale
    iv = ""
    if x.get("ci"):
        a, b = x["ci"][0] * scale, x["ci"][1] * scale
        iv = f'<i class="sci" style="left:{p(a)};width:calc({p(b)} - {p(a)})"></i>'
    return f'<span class="st {cls}">{o}{iv}<i class="smn" style="left:{p(v)}"></i></span>'


def axis_row(lo: float, hi: float, unit: str, label_cls: str = "") -> str:
    p = lambda v: (v - lo) / (hi - lo) * 100  # noqa: E731
    tk = nice_ticks(lo, hi)
    step = (tk[1] - tk[0]) if len(tk) > 1 else 1
    d = max(0, -math.floor(math.log10(step) + 1e-9)) + (1 if round(step / 10 ** math.floor(math.log10(step)), 3) == 2.5 else 0)
    ts = "".join(f'<span style="left:{p(t):.2f}%">{num(t, d)}</span>' for t in tk)
    return f'<span class="sax {label_cls}">{ts}<b>{esc(unit)}</b></span>'


# ---------------------------------------------------------------- test pages

def test_changes(section: str, root) -> list:
    groups = TEST_WORDS.get(section, ())
    return [(d, t, link, lab) for d, _, t, link, lab in changelog(root)
            if any(all(w in t.lower() for w in g) for g in groups) and not SITE_LINE.search(t.split(". ")[0])]


def setups_table(models, section, key, scale, unit, up) -> str:
    present = [(g, lab, desc) for g, lab, desc in CAMERA_GROUPS
               if any(((m["sections"].get(section) or {}).get("by_group") or {}).get(g) for m in models)]
    if not present:
        return ""
    pool = [m for m in by_release(models) if section_ok(m, section)]
    th = "".join(f'<th class="n" scope="col" title="{esc(d)}">{esc(lab)}</th>' for g, lab, d in present)
    rows = []
    for m in pool:
        s = m["sections"][section]
        cells = []
        for g, _, _ in present:
            bg = (s.get("by_group") or {}).get(g) or {}
            gm = next((y for y in bg.get("metrics", []) if y["key"] == key), None)
            cells.append(f'<td class="n">{metric_text(gm, scale, unit, ci=False)}<span class="ci">{bg.get("n", 0)} windows</span></td>' if gm else '<td class="na">none</td>')
        x, _ = find_metric(m, section, key)
        rows.append(f'<tr data-model="{esc(m["model"]["id"])}"><th scope="row">{name_html(m, model_link(m, up))}</th>'
                    f'<td class="n">{metric_text(x, scale, unit, ci=False)}</td>{"".join(cells)}</tr>')
    return (f'<div class="tw"><table><caption>Mean per camera setup; the G55\'s drives cover {len(present)} mounts and lens combinations. Newest release first.</caption>'
            f'<thead><tr><th scope="col">Release</th><th class="n" scope="col">All setups</th>{th}</tr></thead><tbody data-order="release">{"".join(rows)}</tbody></table></div>')


def forest(models, section, key, scale, unit, up, better: str) -> str:
    """Every release, newest first, the interval as the main mark; releases this test has not run on keep
    their row, empty."""
    lo, hi = scale_for(models, section, key, scale)
    rows, month = [], None
    ordered = by_release(models)
    tail = []   # the oldest releases this test has never run on: one line, not a row each
    while ordered and not find_metric(ordered[-1], section, key)[0]:
        tail.insert(0, ordered.pop())
    for m in ordered:
        mo = first_public(m)[:7]
        sep = " fr-m" if month is not None and mo != month else ""
        month = mo
        x, s = find_metric(m, section, key)
        val = metric_text(x, scale, unit) if x else ("not run" if not s or s["status"] == "not_run" else s["status"])
        tip = f"{plain_name(m)}: {val}" + (f", median {num(x['median'] * scale, 1 if scale == 100 else decimals_for(x['median']))} {unit}" if x and x.get("median") is not None else "")
        rows.append(f'<li class="fr{sep}{" fr-off" if not x else ""}{" fr-new" if m is newest(models) else ""}" data-model="{esc(m["model"]["id"])}">'
                    f'<span class="fn"><a href="{esc(model_link(m, up))}">{esc(short_name(m))}</a><span>{esc(nice_date(first_public(m)))}, {esc(FAMILY_NAME.get(m["model"]["family"], m["model"]["family"]))}</span></span>'
                    f'<span class="ftr" tabindex="0" data-tip="{esc(tip)}" aria-label="{esc(tip)}">{strip(x, lo, hi, scale)}</span>'
                    f'<span class="fv">{esc(val)}</span></li>')
    if tail:
        names = ", ".join(short_name(m) for m in tail)
        rows.append(f'<li class="fr fr-off fr-tail"><span class="fn"><span>Not run on the {len(tail)} releases before {esc(short_name(ordered[-1]))}, '
                    f'{esc(nice_date(first_public(tail[0])))} to {esc(nice_date(first_public(tail[-1])))}: {esc(names)}.</span></span></li>')
    head = f'<div class="fh"><span></span>{axis_row(lo, hi, unit)}<span></span></div>'
    return (f'<figure class="forest"><figcaption>{esc(BETTER_TEXT.get(better, "").capitalize())}. Bar: 95% interval from resampling whole drives; '
            f'tick: mean. Rows are releases, newest first; a gap between two releases smaller than their intervals does not separate them.</figcaption>'
            f'{head}<ol class="fl" data-order="release">{"".join(rows)}</ol></figure>')


def test_page(models, cfg, root, spec) -> str:
    slug, title, section, cols, what = spec
    up = "../"
    pool = [m for m in models if section_ok(m, section)]
    if not pool:
        return f'<section class="lead"><h1>{esc(title)}</h1><p>{esc(what)}</p><p>Not run on any release yet.</p></section>'
    secs = [m["sections"][section] for m in pool]
    uw = UNIT_WORD.get(secs[0]["unit_of_evidence"], ("unit", "units"))
    n, c = max((x["n"], x.get("clusters", 0)) for x in secs)
    sim = simulator_of(pool, section)
    tabs = "".join(f'<a href="{s2}.html"{" aria-current=\"page\"" if s2 == slug else ""}>{esc(t2)}</a>' for s2, t2, _, _ in TESTS)
    charts = []
    for key, lab, scale, unit in cols:
        x0 = next((x for x in (find_metric(m, section, key)[0] for m in pool) if x), None)
        if not x0:
            continue
        charts.append(f'<section class="chart"><h2>{esc(lab)}</h2>{forest(models, section, key, scale, unit, up, x0["better"])}</section>')
    ch = "".join(f'<li><time datetime="{d}">{esc(nice_date(d))}</time><span>{esc(t)}' +
                 (f' <a href="{esc(link)}">commit {esc(lab or link.rsplit("/", 1)[-1][:7])}</a>' if link and not ranking.is_private_link(link) else "") + '</span></li>'
                 for d, t, link, lab in test_changes(section, root))
    setups = setups_table(models, section, cols[0][0], cols[0][2], cols[0][3], up) if section.startswith("truck_") else ""
    lim = GS.LIMITS.get(section, "")
    return f"""<nav class="tabs" aria-label="Tests">{tabs}</nav>
<section class="lead"><h1>{esc(title)}</h1>
<p>{esc(what)}</p>
<p class="meta">{n} {esc(uw[1])} from {c} drives. Run on {len(pool)} of {len(models)} releases.{(" Simulator " + esc(sim) + ": " + esc(GS.SIMULATORS.get(sim, "").rstrip(".")) + ".") if sim else ""} When the test changes, every release is run again.</p></section>
{"".join(charts)}
<section class="more-sec">
<details><summary>The table: mean, interval and median</summary>{test_table(models, cfg, spec, up)}</details>
{f'<details><summary>By camera setup</summary>{setups}</details>' if setups else ""}
<details><summary>Conditions and limits</summary><p>{esc(secs[0].get("condition", ""))}</p>{f"<p>{esc(lim)}</p>" if lim else ""}</details>
</section>
{f'<section><h2>How this test has changed</h2><ol class="chg" data-history>{ch}</ol></section>' if ch else ""}"""


def count_word(n: int) -> str:
    return ["no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"][n] if n <= 10 else str(n)


def tests_index(models) -> str:
    items = []
    for slug, title, section, cols, what in TABLES:
        k = sum(1 for m in models if section_ok(m, section))
        items.append(f'<li><h2><a href="{slug}.html">{esc(title)}</a></h2><p>{esc(what)}</p><p class="meta">Run on {k} of {len(models)} releases. Measures: {esc(join_words([c[1].lower() for c in cols]))}.</p></li>')
    g55 = sum(1 for t in TABLES if t[2].startswith("truck_"))
    return (f'<section class="lead"><h1>Tests</h1><p>{count_word(len(TABLES)).capitalize()} tests, the same for every release: {count_word(g55)} on the G55\'s own drives and {count_word(len(TABLES) - g55)} on comma\'s public segments. '
            f'Each page lays out every release with its 95% interval.</p></section><ul class="tlist">{"".join(items)}</ul>')


# ---------------------------------------------------------------- release pages

def provenance(m) -> str:
    sc = m["scored"]
    code = sc.get("code")
    sims = sorted({x.get("simulator") for x in m["sections"].values() if x.get("simulator")})
    code_t = (f'harness <a href="{esc(CFG["repo_url"])}/commit/{esc(code)}"><code>{esc(code[:10])}</code></a>' if code else "harness version not recorded")
    sim_t = ("; G55 simulator " + ", ".join(f"<code>{esc(x)}</code>" for x in sims)) if sims else ""
    return f'Tested {esc(sc["date"][:16].replace("T", " "))} UTC: protocol <code>{esc(sc["suite"])}</code>, {code_t}{sim_t}.'


def release_tests(m, models) -> str:
    fam = m["model"]["family"]
    rows = []
    for slug, title, section, (key, label, scale, unit) in TESTS:
        x, s = find_metric(m, section, key)
        if s is None:
            continue
        lo, hi = scale_for(models, section, key, scale)
        others = [y["value"] * scale for o in models if o is not m and o["model"]["family"] == fam
                  for y in [find_metric(o, section, key)[0]] if y and y.get("value") is not None]
        if x:
            val, stt = metric_text(x, scale, unit), f'done {nice_date(m["scored"]["date"])}'
        else:
            val = "not run"
            stt = {"not_run": "not run", "failed": "run failed"}.get(s["status"], s["status"])
        rows.append(f'<li class="rt{" rt-off" if not x else ""}"><span class="rn"><a href="../tests/{slug}.html">{esc(title)}</a><span>{esc(label)}</span></span>'
                    f'<span class="rv">{esc(val)}</span>'
                    f'<span class="rs">{strip(x, lo, hi, scale, others)}{axis_row(lo, hi, unit, "sax-s")}</span>'
                    f'<span class="rst">{esc(stt)}</span></li>')
    return (f'<ol class="rtl">{"".join(rows)}</ol><p class="sub">Bar: this release\'s 95% interval, tick its mean. Grey dots: the means of the other releases '
            f'in the {esc(FAMILY_NAME.get(fam, fam))} family. Each axis spans every release the test has run on.</p>')


def history_of(m, models, root) -> str:
    mid = m["model"]["id"]
    ev = [(d, t) for d, k, t, link, _ in log_entries(models, root) if k == "model" and isinstance(link, str) and link.endswith(f"/{mid}.html")]
    ev.append((m["scored"]["date"][:10], f'Tests run ({sum(1 for s in m["sections"].values() if s.get("status") == "ok")} of {len(m["sections"])}), results published'))
    ev.sort(reverse=True)
    return "".join(f'<li><time datetime="{d}">{esc(nice_date(d))}</time><span>{esc(t)}</span></li>' for d, t in ev)


def release_page(m, models, cfg, root, out, xrays: set) -> str:
    md, src = m["model"], m["model"]["source"]
    fam = md["family"]
    order = [k for k in SECTIONS if k in m["sections"]] + [k for k in m["sections"] if k not in SECTIONS]
    secs = "".join(section_block(m, k, m["sections"][k], models, root, out) for k in order)
    ident = [
        ("Model id", f'<code>{esc(md["id"])}</code>'),
        ("SHA-256", f'<code class="hash">{esc(md["sha256"])}</code>'),
        ("File tested", f'{esc(src["kind"])}, <code>{esc(src["ref"][:12])}</code> (<a href="{esc(src["url"])}">source</a>), {esc(src["date"])}'),
        ("Parameters", (f'{num(md["params_m"], 1)}M' if md["params_m"] is not None else "not counted")),
        ("GMAC per step", num(md["gmac"], 1)),
    ]
    if md.get("notes"):
        ident.append(("Notes", esc(md["notes"])))
    dl = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in ident)
    limits = "".join(f"<li>{esc(x)}</li>" for x in m.get("limits", []))
    jrel = f'results/{md["id"]}.json'
    inside = (f'<p><a href="../inside/{esc(md["id"])}.html">Which parts of the camera view move its steering</a>, on 4 of comma\'s public segments.</p>'
              if md["id"] in xrays else '<p>Not run for this release yet.</p>')
    stills = ""
    if m.get("stills"):
        figs = "".join(f'<figure><img src="../{esc(copy_rel(root, out, s["file"]))}" alt="{esc(s["caption"])}" loading="lazy">'
                       f'<figcaption><strong>{esc(s["condition"])}.</strong> {esc(s["caption"])}</figcaption></figure>' for s in m["stills"])
        stills = f'<section><h2>Stills</h2><div class="stills">{figs}</div></section>'
    st = state_of(m)
    kin = [o for o in models if o["model"]["family"] == fam]
    where = (timeline(models, "../", only_family=fam, here=md["id"]) if len(kin) > 1 else
             f'<p>The only release in the {esc(FAMILY_NAME.get(fam, fam))} family so far. <a href="../index.html">All releases</a>.</p>')
    return f"""<p class="crumb"><a href="../index.html">Releases</a> / {esc(FAMILY_NAME.get(fam, fam))}</p>
<section class="lead rel" data-model="{esc(md["id"])}"><h1>{name_html(m)}</h1>
<p class="rstate"><i class="tm-k tm-{st}" aria-hidden="true"></i>{esc(state_line(m)[:1].upper() + state_line(m)[1:])}. First public {esc(nice_date(first_public(m)))}.{(" " + num(md["params_m"], 1) + "M parameters.") if md["params_m"] else ""}</p></section>
<section><h2>Where it sits</h2>{where}</section>
<section><h2>Tests</h2>{release_tests(m, models)}<p class="prov">{provenance(m)}</p></section>
<section><h2>Inside the model</h2>{inside}</section>
<section><details><summary>Every metric, with paired differences</summary>{legend()}{secs}</details></section>
{stills}{f'<section><h2>Limits</h2><ul class="limits">{limits}</ul></section>' if limits else ""}
<section><h2>The model file</h2><dl class="ident">{dl}</dl><p><a href="../{esc(jrel)}">Results file</a> (<code>{esc(jrel)}</code>)</p></section>
<section><h2>History of this page</h2><ol class="chg" data-history>{history_of(m, models, root)}</ol></section>"""


# ---------------------------------------------------------------- inside the models

CAR_NAMES = {"FORD_ESCAPE_MK4_5": "Ford Escape (mk4.5)", "FORD MUSTANG MACH-E 1ST GEN": "Ford Mustang Mach-E (1st gen)",
             "RIVIAN_R1_GEN1": "Rivian R1 (gen 1)", "RAM 1500 5TH GEN": "Ram 1500 (5th gen)"}


def car_name(c: str) -> str:
    return CAR_NAMES.get(c, c.replace("_", " "))


def model_phrase(m) -> str:
    n = short_name(m)
    return f"the {n}" if n.endswith("model") else n


def xray_sets(root: Path) -> dict:
    """{model id: [clip dirs]} from xray/<model>/<clip>/clip.json."""
    out = {}
    for d in sorted((root / "xray").glob("*")) if (root / "xray").is_dir() else []:
        clips = [c for c in sorted(d.iterdir()) if (c / "clip.json").is_file()]
        pf = d / "pooled.json"
        if pf.is_file():
            order = json.loads(pf.read_text())["clips"]
            clips.sort(key=lambda c: order.index(c.name) if c.name in order else len(order))
        if clips:
            out[d.name] = clips
    return out


def ratio_cell(t: dict) -> str:
    r, ci = t["ratio"], t.get("ratio_ci95")
    passed = t["diff_ci95"][0] > 0
    iv = f' ({num(ci[0], 2)} to {num(ci[1], 2)})' if ci else ""
    return f'<td class="n{" pass" if passed else " fail"}">{num(r, 2)}×{iv}<span class="ci">{"top beats random" if passed else "interval reaches zero"}</span></td>'


def inside_page(m, models, clips: list[Path], root: Path, out: Path) -> str:
    mid = m["model"]["id"]
    dst = out / "inside" / mid
    meta = []
    for c in clips:
        shutil.copytree(c, dst / c.name, dirs_exist_ok=True)
        meta.append(json.loads((c / "clip.json").read_text()))
    pooled_f = root / "xray" / mid / "pooled.json"
    pooled = json.loads(pooled_f.read_text()) if pooled_f.is_file() else None
    ks = ["k1", "k2", "k4", "k8"]
    rows = []
    for c in meta:
        rows.append(f'<tr><th scope="row">{esc(c["segment"])}<span class="th-s">{esc(car_name(c["car"]))}</span></th>'
                    + "".join(ratio_cell(c["check"][k]["top_vs_random"]) for k in ks)
                    + f'<td>{"passes" if c["check"]["passed"] else "fails"}</td></tr>')
    if pooled:
        rows.append(f'<tr class="pooled"><th scope="row">All 4 clips<span class="th-s">{pooled["check"]["k1"]["top_vs_random"]["frames"]} frames</span></th>'
                    + "".join(ratio_cell(pooled["check"][k]["top_vs_random"]) for k in ks)
                    + f'<td>{"passes" if pooled["passed"] else "fails"}</td></tr>')
    act = ""
    if pooled:
        act = "".join(f'<td class="n">{num(pooled["check"][k]["act_vs_random"]["ratio"], 2)}×<span class="ci">{num(pooled["check"][k]["act_vs_random"]["ratio_ci95"][0], 2)} to {num(pooled["check"][k]["act_vs_random"]["ratio_ci95"][1], 2)}</span></td>' for k in ks)
    rb = meta[0]["row_bands"]
    bands = []
    for r in range(4):
        nb, wb = rb["narrow"]["rows"][r], rb["wide"]["rows"][r]

        def band(b):
            far, near = b["ground_m_far_to_near"]
            if far is None and near is None:
                return "above the horizon"
            if far is None:
                return f"horizon and road beyond {num(near, 0)} m"
            return f"road {num(near, 1)} to {num(far, 1)} m ahead"
        bands.append(f'<tr><th scope="row">{r + 1}</th><td>{esc(band(nb))}</td><td>{esc(band(wb))}</td></tr>')
    sp = (pooled or meta[0])["spearman_per_frame"]
    share = (pooled or {}).get("rows", {}).get("top1_cell_row_share")
    rows_line = (f"<p>The bottom row, the road closest to the car, holds the square that moves the steering most in {share[3]} of {sum(share)} frames.</p>"
                 if share else "")
    data = json.dumps({"clips": [{"dir": f"{mid}/{c['segment']}", "segment": c["segment"], "car": c["car"]} for c in meta]})
    picker = "".join(f'<button type="button" data-clip="{i}"{" aria-pressed=\"true\"" if i == 0 else " aria-pressed=\"false\""}>{esc(car_name(c["car"]))}<span>{esc(c["segment"])}</span></button>' for i, c in enumerate(meta))
    return f"""<p class="crumb"><a href="index.html">Inside the models</a> / <a href="../models/{esc(mid)}.html">{esc(plain_name(m))}</a></p>
<section class="lead"><h1>What {esc(model_phrase(m))} looks at when it steers</h1>
<p>The model's vision network turns its two camera views into a grid of 4 rows and 8 columns of squares. Everything it does next, the path openpilot draws and the steering it asks for, is read from those 32 squares and the grids before them. Here each square is hidden in turn, replaced by its average over the clip, and the change in the steering request is measured.</p>
<p class="meta">Release {esc(plain_name(m))}, on {len(meta)} of comma's public segments, 20 s each, 5 frames a second. Measured.</p></section>

<section class="xv" aria-label="Clip viewer">
<div class="xv-pick" role="group" aria-label="Clip">{picker}</div>
<div class="xv-views">
<figure><div class="xv-img"><img alt="Narrow camera view at the current frame" data-view="n"><div class="xv-grid" data-grid="n"></div></div><figcaption>Narrow camera, as the model sees it (512 × 256)</figcaption></figure>
<figure><div class="xv-img"><img alt="Wide camera view at the current frame" data-view="w"><div class="xv-grid" data-grid="w"></div></div><figcaption>Wide camera. The same 32 squares read both views at once.</figcaption></figure>
</div>
<div class="xv-ctl"><button type="button" class="xv-play" aria-label="Play"><i></i></button>
<input type="range" class="xv-scrub" min="0" max="99" step="1" value="0" aria-label="Frame">
<output class="xv-read" aria-live="off"></output></div>
<div class="xv-trace"><svg viewBox="0 0 1000 120" preserveAspectRatio="none" role="img" aria-label="Lateral acceleration over the clip: the model's request and what the car did"></svg>
<p class="xv-key"><span><i class="k-model"></i>model's lateral request</span><span><i class="k-meas"></i>what the car did (speed × yaw rate)</span><span class="xv-scale">m/s², left positive</span></p></div>
<div class="xv-rows"><p>Each row of squares over the clip: the sum of its 8 squares' effect on steering.</p><canvas height="80" aria-label="Rows of squares over time"></canvas></div>
<p class="xv-legend"><span><i class="k-left"></i>hiding it, the model steers further left</span><span><i class="k-right"></i>further right</span><span>Stronger colour: a bigger change (square root of its size over this clip's largest), so small effects stay visible.</span></p>
<script type="application/json" id="xv-data">{data}</script>
</section>

<section class="prose"><h2>What the check says</h2>
<p>A map like this can look convincing and still be wrong. So it is tested on the picture itself: blur the image under the k squares the map ranks highest, and separately under k random squares, rerun the whole model, and compare how far the steering request moves. The map passes when the top squares move it more, with the 95% interval above zero, at every k.</p>
<div class="tw"><table><caption>Steering change from the top k squares divided by the change from k random squares, with its 95% interval (1 s blocks resampled).</caption>
<thead><tr><th scope="col">Clip</th><th class="n" scope="col">k = 1</th><th class="n" scope="col">k = 2</th><th class="n" scope="col">k = 4</th><th class="n" scope="col">k = 8</th><th scope="col">Every k</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table></div>
<p>Pooled over the four clips, the map passes. Clip by clip it passes on {sum(1 for c in meta if c["check"]["passed"])} of {len(meta)}.</p>
{f'<p>Ranking the squares by how active the final grid is, instead of by hiding them one at a time, picks squares that move the steering more:</p><div class="tw"><table><thead><tr><th scope="col">All 4 clips</th><th class="n" scope="col">k = 1</th><th class="n" scope="col">k = 2</th><th class="n" scope="col">k = 4</th><th class="n" scope="col">k = 8</th></tr></thead><tbody><tr><th scope="row">Most active squares</th>{act}</tr></tbody></table></div><p>Hiding one square at a time misses what squares do together.</p>' if act else ""}
</section>

<section class="prose"><h2>Activity is not the same as importance</h2>
<p>A common way to look inside a network is to colour each region by how active it is. Measured against the effect of hiding it, frame by frame over all {sp["view_41_norm_vs_abs_eff_a_lat"]["frames"]} frames: the median rank correlation is {num(sp["view_41_norm_vs_abs_eff_a_lat"]["median"], 2)} in the final grid of squares. Inside the vision network it is {num(sp["stage0_norm_pooled_vs_abs_eff_a_lat"]["median"], 2)}, {num(sp["stage1_norm_pooled_vs_abs_eff_a_lat"]["median"], 2)}, {num(sp["stage2_norm_pooled_vs_abs_eff_a_lat"]["median"], 2)} and {num(sp["stage3_norm_pooled_vs_abs_eff_a_lat"]["median"], 2)} after stages 1 to 4: there, how active a region is says little about whether it steers.</p>
{rows_line}
</section>

<section class="prose"><h2>From the picture to the steering</h2>
<ol class="pipe">
<li><b>Cameras</b><span>narrow and wide, now and 0.2 s before</span></li>
<li><b>Stage 1</b><span>32 × 64, 3 blocks</span></li>
<li><b>Stage 2</b><span>16 × 32, 3 blocks</span></li>
<li><b>Stage 3</b><span>8 × 16, 27 blocks</span></li>
<li><b>Stage 4</b><span>4 × 8, 3 blocks</span></li>
<li class="hl"><b>32 squares</b><span>512 numbers each</span></li>
<li><b>Memory</b><span>9 grids, 0.2 s apart</span></li>
<li><b>Policy</b><span>shared by both outputs</span></li>
<li><b>Two outputs</b><span>the drawn path and lanes; the steering and speed request</span></li>
</ol>
<p>Read from the model file. The steering request does not read the drawn path: both come from the shared policy and split at the end.</p>
</section>

<section class="prose"><h2>What each row of squares sees</h2>
<div class="tw"><table><caption>Distances along a flat road with the camera 1.22 m up (openpilot's default); higher cars see further for the same row.</caption>
<thead><tr><th scope="col">Row</th><th scope="col">Narrow camera</th><th scope="col">Wide camera</th></tr></thead><tbody>{"".join(bands)}</tbody></table></div>
</section>

<section class="prose"><h2>Limits</h2><ul>
<li>One release, {len(meta)} clips of 20 s on comma's public segments. Nothing here comes from the G55.</li>
<li>Hiding a square replaces it with its average over the clip; another replacement gives other numbers.</li>
<li>These runs use a PyTorch copy of the model file; it has not been compared with the original on these clips.</li>
<li>The extraction code is not in this repository yet.</li>
</ul></section>"""


def inside_index(models, xr: dict) -> str:
    idx = by_id(models)
    items = "".join(f'<li><a href="{esc(mid)}.html">What {esc(model_phrase(idx[mid]))} looks at when it steers</a><span>{esc(plain_name(idx[mid]))}, {len(clips)} public segments</span></li>'
                    for mid, clips in xr.items() if mid in idx)
    return (f'<section class="lead"><h1>Inside the models</h1><p>How a release turns the camera picture into a steering request, measured on comma\'s public segments. '
            f'{count_word(len(xr)).capitalize()} release{"s" if len(xr) != 1 else ""} so far; each new one is added as it is run.</p></section><ul class="sl big">{items}</ul>')


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=EVAL, help="directory with results/, per_unit/, xray/")
    ap.add_argument("--out", type=Path, default=SITE / "_site")
    ap.add_argument("--config", type=Path, default=SITE / "config.json")
    a = ap.parse_args()
    cfg = json.loads(a.config.read_text())
    CFG.update(cfg)
    root, out = a.root.resolve(), a.out.resolve()
    models = load(root)
    MODELS[:] = models
    TIERS.clear()
    TIERS.update(ranking.site_tests(root))
    if out.exists():
        shutil.rmtree(out)
    for d in ("models", "results", "tests", "inside"):
        (out / d).mkdir(parents=True)
    for f in (SITE / "static").iterdir():
        if f.is_file():
            shutil.copy2(f, out / f.name)
    tag = cfg["site_name"]
    xr = {k: v for k, v in xray_sets(root).items() if k in by_id(models)}
    for m in models:
        shutil.copy2(m["_file"], out / "results" / m["_file"].name)
        mid = m["model"]["id"]
        (out / "models" / f"{mid}.html").write_text(
            page(cfg, f"{plain_name(m)} – {tag}", f"{plain_name(m)}: {state_line(m)}. {summary_line(m, cfg, as_html=False)}",
                 release_page(m, models, cfg, root, out, set(xr)), 1, "releases", f"models/{mid}.html"))
    for spec in TABLES:
        (out / "tests" / f"{spec[0]}.html").write_text(
            page(cfg, f"{spec[1]} – {tag}", spec[4], test_page(models, cfg, root, spec), 1, "tests", f"tests/{spec[0]}.html"))
    (out / "tests" / "index.html").write_text(page(cfg, f"Tests – {tag}", f"The {count_word(len(TABLES))} tests every openpilot release gets here.",
                                                   tests_index(models), 1, "tests", "tests/index.html"))
    for mid, clips in xr.items():
        m = by_id(models)[mid]
        (out / "inside" / f"{mid}.html").write_text(
            page(cfg, f"What {model_phrase(m)} looks at when it steers – {tag}", "Which squares of the model's camera view move its steering, on comma's public segments.",
                 inside_page(m, models, clips, root, out), 1, "inside", f"inside/{mid}.html"))
    (out / "inside" / "index.html").write_text(page(cfg, f"Inside the models – {tag}", "How openpilot driving models turn the camera picture into steering.",
                                                    inside_index(models, xr), 1, "inside", "inside/index.html"))
    shutil.copy2(EVAL / "SCHEMA.md", out / "schema.txt")
    desc = ("comma's openpilot driving models as they are released, each tested on a 2011 Mercedes-Benz G55 and on comma's public "
            "segments, with every value's 95% interval.")
    (out / "index.html").write_text(page(cfg, f"comma's driving models, tested on one truck – {tag}", desc, index_page(models, cfg, root), 0, "releases", ""))
    (out / "log.html").write_text(page(cfg, f"Log – {tag}", "Every release, test run and change to the tests.", log_page(models, root), 0, "log", "log.html"))
    (out / "method.html").write_text(page(cfg, f"Method – {tag}", "Scoring methodology for open-loop replay, "
                                          "worldmodel-4B closed-loop rollouts, drive-clustered intervals, and CSV downloads.", method(models, cfg, root), 0, "method", "method.html"))
    (out / "feed.xml").write_text(rss(models, root, cfg))
    problems = ranking.check_site(out, TIERS, {m["model"]["id"]: first_public(m) for m in models})
    if problems:
        print("\n".join(f"FAIL {x}" for x in problems), file=sys.stderr)
        raise SystemExit(f"{len(problems)} problems: the site prints an order or a score the data do not support "
                         f"(geval.ranking.check_site); {out} is not publishable")
    print(f"{len(models)} models -> {out}; order check: 0 problems (lists newest release first, no places, no combined score)")


if __name__ == "__main__":
    main()
