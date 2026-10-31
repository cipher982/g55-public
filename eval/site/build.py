# /// script
# requires-python = ">=3.11"
# ///
"""Build the openpilot G-Wagon evals site from results/*.json.

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
BETTER_TEXT = {"lower": "lower is better", "higher": "higher is better",
               "near1": "closer to 1 is better; the delta is the change in distance from 1"}
MINUS = "−"
# The G55 numbers on every model's row. Lateral action rate is a diagnostic with no better direction:
# a lower rate can be a smaller request as well as a smoother one.
KEY_METRICS = [
    ("truck_closed_loop", "xtrack_2s", "G55 closed loop", "Median distance from the recorded path at 2 s"),
    ("truck_open_loop", "lat_mae", "G55 lateral error", "Mean error between requested and measured lateral acceleration"),
    ("truck_open_loop", "lat_rate_rms", "G55 lateral action rate", "Mean RMS rate of change of the lateral request"),
]
MORE_METRICS = [
    ("truck_recovery", "heading_xtrack_2s", "G55 at 2 s after a 2° heading error", "Median over windows"),
    ("truck_recovery", "offcentre_xtrack_2s", "G55 at 2 s after a 0.5 m offset", "Median over windows"),
    ("public_closed_loop", "xtrack_2s", "Distance from path at 2 s on public segments", "Closed-loop median using comma's ideal car"),
    ("public_open_loop", "lat_mae", "Lateral error on public segments", "Mean error against measured lateral acceleration"),
]
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


def page(cfg: dict, title: str, desc: str, body: str, depth: int, current: str, path: str) -> str:
    up = "../" * depth
    css_v = _asset_hash("style.css")
    js_v = _asset_hash("site.js")
    umami = ""
    if cfg.get("umami_website_id"):
        dom = f' data-domains="{esc(cfg["umami_domains"])}"' if cfg.get("umami_domains") else ""
        umami = (f'<script defer src="{esc(cfg["umami_src"])}" '
                 f'data-website-id="{esc(cfg["umami_website_id"])}"{dom}></script>')
    idx_prefix = "" if path == "" else f"{up}index.html"
    nav = []
    for href, label, key in ((f"{idx_prefix}#results", "Results", "results"),
                             (f"{idx_prefix}#full", "Full tables", "full"),
                             (f"{idx_prefix}#log", "Log", "log"),
                             (f"{up}method.html", "Method", "method")):
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
<meta name="twitter:title" content="{esc(title)}">
<meta name="twitter:description" content="{esc(desc)}">
<meta name="twitter:image" content="{esc(og)}">
<meta name="color-scheme" content="light dark">
<script>try{{var t=localStorage.getItem("theme");if(t==="light"||t==="dark")document.documentElement.dataset.theme=t}}catch(e){{}}</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Overpass+Mono:wght@400;600;700&family=Overpass:ital,wght@0,400;0,600;0,700;0,800;1,400&display=swap">
<link rel="stylesheet" href="{up}style.css?v={css_v}">
<link rel="icon" href="{up}favicon.svg" type="image/svg+xml">
{umami}
</head>
<body>
<header class="top"><div class="wrap top-in">
<a class="word" href="{up}index.html"><span class="word-dot" aria-hidden="true"></span>{esc(cfg['site_name'])}</a>
<nav aria-label="Site">{''.join(nav)}<button class="theme" type="button" aria-label="Switch light or dark theme"><span aria-hidden="true"></span></button></nav>
</div></header>
<main class="wrap">
{body}
</main>
<footer class="wrap foot">
<p>Run by David Rose. Results come from one 2011 Mercedes-Benz G55 AMG (W463) and comma's public segments; each number carries its sample size and drive count.
<a href="{up}schema.txt">Results file format</a>.</p>
</footer>
<div id="tip" class="tip" role="status" hidden></div>
<script src="{up}site.js?v={js_v}" defer></script>
</body>
</html>
"""


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


def summary_table(models, cfg) -> str:
    ref = cfg["reference_model"]
    more = [(s, k, lab) for s, k, lab, _ in MORE_METRICS]
    cols = [c for c in more if c[0].startswith("truck_")] + [(s, k, lab) for s, k, lab, _ in KEY_METRICS] + [c for c in more if not c[0].startswith("truck_")]
    th = "".join(f'<th class="n" scope="col">{esc(lab)}<span class="th-s">{esc(SECTIONS[s])}</span></th>' for s, k, lab in cols)
    body = []
    for m in by_release(models):
        cells = []
        for s, k, _ in cols:
            x, sec = find_metric(m, s, k)
            if not x:
                cells.append(f'<td class="na">{"not run" if not sec or sec["status"] == "not_run" else esc(sec["status"])}</td>')
                continue
            d = decimals_for(x["value"])
            p = x["paired"].get(ref)
            inner = f'{num(x["value"], d)}<span class="ci">{ci_text(x["ci"], d)}</span>'
            if p:
                v = verdict_of(x, p)
                inner += f'<span class="dl">{mark(v)}{num(p["delta"], d, True)} vs lebowski</span>'
            cells.append(f'<td class="n">{inner}</td>')
        body.append(f'<tr data-model="{esc(m["model"]["id"])}"><th scope="row">{name_html(m, "models/" + m["model"]["id"] + ".html")}</th>{"".join(cells)}</tr>')
    nd = max([(m["sections"].get(s) or {}).get("clusters", 0) for m in models for s in m["sections"] if s.startswith("truck_")] + [0])
    g55 = (f"The G55 sections come from {nd} drives or fewer and carry no interval." if nd < 10 else "")
    return (f'<div class="tw"><table class="sum"><caption>Mean with its 95% interval, then the paired difference from lebowski '
            f'on the units both have. {g55}</caption>'
            f'<thead><tr><th scope="col">Model</th>{th}</tr></thead><tbody data-order="release">{"".join(body)}</tbody></table></div>')


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
<figcaption><p><strong>One public segment in closed loop.</strong> Each model drove comma's ideal car for 7.2 s from the same recorded start while <code>worldmodel-4B</code> generated the camera view it saw. Recorded shows the drive as logged. Dashed magenta is the driver's path, solid is the model's own, the ring is where each is at 2 s. Distance from the driver's path at 2 s: {esc(names2)}.</p>
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

def camera_setups_section(models: list[dict], cfg: dict, root: Path) -> str:
    """Interactive matrix showing model performance broken down by the 5 G55 camera hardware setups
    (from `by_group` in results/*.json)."""
    has_bg = any(
        (m["sections"].get("truck_closed_loop") or {}).get("by_group")
        or (m["sections"].get("truck_open_loop") or {}).get("by_group")
        for m in models
    )
    if not has_bg:
        return ""

    views = [
        ("cl2s", "Closed Loop (2 s, cm)", "truck_closed_loop", "xtrack_2s", "median", 100.0, 1, "cm", True),
        ("heading", "2° Heading Recovery (cm)", "truck_recovery", "heading_xtrack_2s", "median", 100.0, 1, "cm", True),
        ("offset", "0.5 m Offset Recovery (cm)", "truck_recovery", "offcentre_xtrack_2s", "median", 100.0, 1, "cm", True),
        ("openloop", "Open-Loop Lateral Error (m/s²)", "truck_open_loop", "lat_mae", "value", 1.0, 3, "m/s²", False),
    ]

    tabs = []
    panels = []
    for idx_v, (vkey, vlabel, sec_name, mkey, stat, scale, dec, unit_str, cl_only) in enumerate(views):
        active = idx_v == 0
        tabs.append(
            f'<button type="button" class="setup-tab" role="tab" aria-selected="{"true" if active else "false"}" '
            f'data-setup-tab="{vkey}">{esc(vlabel)}</button>'
        )

        # Find which models have this section
        pool = [m for m in models if (m["sections"].get(sec_name) or {}).get("status") == "ok"]
        pool = by_release(pool)

        # Compute column spans for heat shading
        col_spans = {}
        pooled_vals = []
        for m in pool:
            xm, _ = find_metric(m, sec_name, mkey)
            if xm and xm.get(stat, xm.get("value")) is not None:
                pooled_vals.append(float(xm.get(stat, xm["value"])))
        col_spans["pooled"] = (min(pooled_vals), max(pooled_vals)) if pooled_vals else (0.0, 1.0)

        group_counts = {}
        for gkey, _, _ in CAMERA_GROUPS:
            gvals = []
            for m in pool:
                bg = (m["sections"].get(sec_name) or {}).get("by_group", {}).get(gkey)
                if bg:
                    group_counts[gkey] = (bg.get("n", 0), bg.get("clusters", 0))
                    gm = next((x for x in bg.get("metrics", []) if x["key"] == mkey), None)
                    if gm and gm.get(stat, gm.get("value")) is not None:
                        gvals.append(float(gm.get(stat, gm["value"])))
            col_spans[gkey] = (min(gvals), max(gvals)) if gvals else (0.0, 1.0)

        # Build table header
        sec_sample = next(((m["sections"].get(sec_name) or {}) for m in pool), {})
        th_pooled = f'<th class="n" scope="col">Pooled (all setups)<span class="th-s">{sec_sample.get("n", 0)} windows, {sec_sample.get("clusters", 0)} drives</span></th>'
        th_groups = []
        for gkey, glabel, gdesc in CAMERA_GROUPS:
            gn, gc = group_counts.get(gkey, (0, 0))
            th_groups.append(
                f'<th class="n" scope="col" title="{esc(gdesc)}">{esc(glabel)}'
                f'<span class="th-s">{gn} windows, {gc} drive{"s" if gc != 1 else ""}</span></th>'
            )

        trows = []
        for m in pool:
            mid = m["model"]["id"]
            c_cls = model_class(m, cfg)
            xm, _ = find_metric(m, sec_name, mkey)
            if xm and xm.get(stat, xm.get("value")) is not None:
                pv = float(xm.get(stat, xm["value"]))
                p_cell = f'<td class="n setup-c">{num(pv * scale, dec)} <span class="gt-mu">{esc(unit_str)}</span></td>'
            else:
                p_cell = '<td class="na">—</td>'

            g_cells = []
            for gkey, _, _ in CAMERA_GROUPS:
                bg = (m["sections"].get(sec_name) or {}).get("by_group", {}).get(gkey)
                gm = next((x for x in (bg or {}).get("metrics", []) if x["key"] == mkey), None)
                if gm and gm.get(stat, gm.get("value")) is not None:
                    gv = float(gm.get(stat, gm["value"]))
                    g_cells.append(f'<td class="n setup-c">{num(gv * scale, dec)}</td>')
                else:
                    g_cells.append('<td class="na">—</td>')

            trows.append(
                f'<tr data-model="{esc(mid)}"><th scope="row"><span class="sw sw-{c_cls}" aria-hidden="true"></span> '
                f'{name_html(m, f"models/{mid}.html")}</th>{p_cell}{"".join(g_cells)}</tr>'
            )

        panels.append(
            f'<div class="setup-panel" data-setup-panel="{vkey}"{" hidden" if not active else ""}>'
            f'<div class="tw"><table class="setup-tbl">'
            f'<thead><tr><th scope="col">Model</th>{th_pooled}{"".join(th_groups)}</tr></thead>'
            f'<tbody data-order="release">{"".join(trows)}</tbody></table></div></div>'
        )

    return f"""<div id="setups" class="setups-sec">
<div class="sec-head">
<div>
<h3>By camera setup</h3>
<p class="sub">The G55's drives cover 5 camera mounts and lens combinations. Each column gives one setup's value beside the value over all drives: the median distance for the closed-loop tests, the mean lateral error for open loop. Rows are newest release first.</p>
</div>
<div class="setup-tabs" role="tablist" aria-label="Metric by camera setup">{"".join(tabs)}</div>
</div>
{"".join(panels)}
</div>"""


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


def test_table(models, cfg, spec) -> str:
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
                    f'{name_html(m, f"models/{mid}.html")}<span class="th-s">{esc(first_public(m))} · {esc(lc_status(m))}</span></th>'
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


# ---------------------------------------------------------------- overview

def overview(models, cfg, root) -> str:
    scope = "".join(f"<li>{esc(x)}.</li>" for x in scope_counts(models))
    tables = "".join(test_table(models, cfg, spec) for spec in TABLES)
    return f"""<section class="hero" id="about">
<div class="hero-t">
<h1>openpilot driving models on one truck</h1>
<p class="lede">Results for comma's released openpilot driving models, replayed on recordings from one truck, a 2011 Mercedes-Benz G55 AMG, and on comma's public segments. Each table gives every model's value with its 95% interval, newest release first, with the test's conditions and limits beside it.</p>
<div class="scope"><p><strong>Scope.</strong> One truck, these drives and one simulator version. The numbers describe these models on this truck's recordings; they do not say how a model drives another vehicle.</p>
<ul class="plain">{scope}</ul>
<p><strong>Benchmark revision:</strong> {esc(revision_text(models).rstrip(".") + ".")} Closed-loop and recovery results published before 2026-10-31 came from the simulator g55-2026-10-26 and are not comparable with these; the <a href="#log">log</a> lists each change.</p></div>
{status_strip(models, cfg, root)}
</div>
</section>

<div id="results" class="results">
{tables}
</div>

<section id="full" class="full">
<h2>Full tables</h2>
<details class="t-more"><summary>Every metric, with the paired difference from lebowski</summary>
<ul class="plain">
<li>Every value is a mean over units: a 60 s segment on comma's public drives, a fixed window on the G55's. Units are grouped by the drive they come from, and 95% intervals resample whole drives. A test with fewer than 10 drives reports the mean and win counts without an interval.</li>
<li>A paired difference compares two models on the same windows or segments; the count beside it is how often the model's value was the lower (or, for a measure where higher is better, the higher) one.</li>
<li>Results files (format: <a href="schema.txt">schema.txt</a>) and the per-segment CSVs of comma's public segments are on the <a href="method.html#downloads">Method page</a>; the public intervals can be recomputed from the CSVs. The G55's recordings stay private; their aggregate and per-setup values are published.</li>
</ul>
{legend()}
{summary_table(models, cfg)}
</details>
<details class="t-more"><summary>By G55 camera setup</summary>
{camera_setups_section(models, cfg, root)}
</details>
<details class="t-more"><summary>comma models not scored here</summary>
{unscored()}
</details>
</section>

<section id="method" class="prose">
<h2>Method</h2>
<details class="t-more"><summary>How the tests work</summary>
<div class="how">
<div><h3>Open loop</h3><p>The model runs on recorded camera frames. Its lateral and longitudinal requests are compared with the vehicle's measured motion.</p></div>
<div><h3>Closed loop</h3><p>Each model's steering and speed requests drive a simulated vehicle while <code>worldmodel-4B</code> renders the next camera frame at each 0.2 s step over a 7.2 s rollout. Distance is measured from the recorded path.</p></div>
<div><h3>The G55</h3><p>A 2011 Mercedes-Benz G55 AMG (W463) with recirculating-ball steering and its cameras 1.45 m up. The G55 closed loop runs a simulation of its steering and powertrain fitted to its own drives.</p></div>
</div>
<p>comma publishes its own test reports in <a href="https://commaai.github.io/model_reports/">model_reports</a>. Nearest equivalents:</p>
<div class="tw"><table class="vocab"><thead><tr><th scope="col">Here</th><th scope="col">Nearest test in comma's model_reports</th></tr></thead><tbody>
<tr><th scope="row">Lateral action rate (open loop)</th><td>jitter</td></tr>
<tr><th scope="row">Closed-loop path error (comma's ideal car)</th><td>MLSim lateral (<code>analyse_lat_mlsim</code>) in comma's world model</td></tr>
<tr><th scope="row">G55 recovery (0.5 m lateral offset or 2° heading error)</th><td>hugging (recovery under a lateral bias)</td></tr>
<tr><th scope="row">Braking lead car (2 windows)</th><td>unintended lead following</td></tr>
</tbody></table></div>
<p>The <a href="method.html">Method page</a> has the full method, a closed-loop rollout player and the downloads.</p>
</details>
</section>

<section id="log" class="logsec">
<h2>Log</h2>
<p class="sub">Model releases, scoring runs and changes to the benchmark.</p>
{log_feed(models, root)}
</section>"""


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


def model_page(m, models, cfg, root, out) -> str:
    md = m["model"]
    src = md["source"]
    sc = m["scored"]
    code = sc.get("code")
    code_html = f'<code>{esc(code[:10])}</code>' if code else "n/a"
    sims = sorted({x.get("simulator") for x in m["sections"].values() if x.get("simulator")})
    ident = [
        ("Model id", f'<code>{esc(md["id"])}</code>'),
        ("SHA-256", f'<code class="hash">{esc(md["sha256"])}</code>'),
        ("File scored", f'{esc(src["kind"])}, <code>{esc(src["ref"])}</code> (<a href="{esc(src["url"])}">link</a>), {esc(src["date"])}'),
        ("First public", esc(first_public(m))),
        ("Parameters", (f'{num(md["params_m"], 1)}M' if md["params_m"] is not None else "not counted")),
        ("GMAC per step", num(md["gmac"], 1)),
        ("Family", esc(md["family"])),
        ("Scored", f'{esc(sc["date"][:16].replace("T", " "))} UTC, protocol <code>{esc(sc["suite"])}</code>, harness {code_html}'),
        ("G55 simulator", ", ".join(f'<code>{esc(x)}</code>: {esc(GS.SIMULATORS.get(x, ""))}' for x in sims) or "none (open loop only)"),
    ]
    if md.get("notes"):
        ident.append(("Notes", esc(md["notes"])))
    dl = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in ident)
    order = [k for k in SECTIONS if k in m["sections"]] + [k for k in m["sections"] if k not in SECTIONS]
    secs = "".join(section_block(m, k, m["sections"][k], models, root, out) for k in order)
    mets = KEY_METRICS + MORE_METRICS
    scales = {(s, k): bar_scale(models, s, k) for s, k, *_ in mets}
    stills = ""
    if m.get("stills"):
        figs = "".join(
            f'<figure><img src="../{esc(copy_rel(root, out, s["file"]))}" alt="{esc(s["caption"])}" loading="lazy">'
            f'<figcaption><strong>{esc(s["condition"])}.</strong> {esc(s["caption"])}</figcaption></figure>'
            for s in m["stills"])
        stills = f'<section><h2>Stills</h2><div class="stills">{figs}</div></section>'
    limits = "".join(f"<li>{esc(x)}</li>" for x in m.get("limits", []))
    jrel = f'results/{md["id"]}.json'
    prev = previous_of(m, cfg["reference_model"])
    prevt = ""
    if prev:
        po = by_id(models).get(prev)
        prevt = f' The tables below give paired differences from lebowski and from the preceding release ({name_html(po, prev + ".html") if po else esc(prev)}).'
    return (f'<section class="mhead"><p class="crumb"><a href="../index.html#results">← All results</a></p>'
            f'<h1>{name_html(m)}</h1><p class="m-st"><span class="m-date">{esc(first_public(m))}</span> {esc(status_text(m))}</p>'
            f'<p class="m-verdict">{summary_line(m, cfg)}</p></section>'
            f'<section><h2>Key numbers</h2><p class="sub">Bars are this model\'s values; green ticks mark lebowski\'s.{prevt}</p>'
            f'<div class="kbars">{bars(m, models, cfg, mets, scales, False)}</div></section>'
            f'<section><h2>All scores</h2>{legend()}{secs}</section>{stills}'
            + (f'<section><h2>Limits</h2><ul class="limits">{limits}</ul></section>' if limits else "")
            + f'<section><h2>The model file</h2><dl class="ident">{dl}</dl>'
              f'<p><a href="../{esc(jrel)}">Results file</a> (<code>{esc(jrel)}</code>, schema 1).</p></section>')


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


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=EVAL, help="directory with results/, per_unit/, stills/")
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
    (out / "models").mkdir(parents=True)
    (out / "results").mkdir()
    for f in (SITE / "static").iterdir():
        if f.is_file():
            shutil.copy2(f, out / f.name)
    tag = "openpilot model eval"
    for m in models:
        shutil.copy2(m["_file"], out / "results" / m["_file"].name)
        mid = m["model"]["id"]
        (out / "models" / f"{mid}.html").write_text(
            page(cfg, f"{plain_name(m)} – {tag}", f"{plain_name(m)}: {status_text(m)}. {summary_line(m, cfg, as_html=False)}",
                 model_page(m, models, cfg, root, out), 1, "model", f"models/{mid}.html"))
    shutil.copy2(EVAL / "SCHEMA.md", out / "schema.txt")
    desc = ("Results for comma's openpilot driving models on recordings from one 2011 Mercedes-Benz G55 AMG and on "
            "comma's public segments, in open-loop replay and closed-loop simulation, each with its 95% interval.")
    (out / "index.html").write_text(page(cfg, f"openpilot driving models on one truck – {cfg['site_name']}",
                                         desc, overview(models, cfg, root), 0, "results", ""))
    (out / "method.html").write_text(page(cfg, f"Method – {cfg['site_name']}", "Scoring methodology for open-loop replay, "
                                          "worldmodel-4B closed-loop rollouts, drive-clustered intervals, and CSV downloads.", method(models, cfg, root), 0, "method", "method.html"))
    problems = ranking.check_site(out, TIERS, {m["model"]["id"]: first_public(m) for m in models})
    if problems:
        print("\n".join(f"FAIL {x}" for x in problems), file=sys.stderr)
        raise SystemExit(f"{len(problems)} problems: the site prints an order or a score the data do not support "
                         f"(geval.ranking.check_site); {out} is not publishable")
    print(f"{len(models)} models -> {out}; order check: 0 problems (lists newest release first, no places, no combined score)")


if __name__ == "__main__":
    main()
