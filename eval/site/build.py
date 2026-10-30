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
import csv
import datetime as _dt
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
from geval import ranking  # noqa: E402  the rankings: public from the per-unit CSVs, G55 from rankings/
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
VERDICT_TERM = {"xtrack_2s": "G55 closed-loop median", "lat_mae": "G55 open-loop lateral error",
                "lat_rate_rms": "G55 lateral action rate"}

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


# ---------------------------------------------------------------- findings: prose with numbers from the results

def fmt(v: float, f: str) -> str:
    if f == "cm":
        return f"{round(v * 100):d}"
    if f == "cm1":
        return num(v * 100, 1)
    if f == "pct":
        return f"{round(abs(v)):d}"
    if f.startswith("d"):  # d2 = two decimals
        return num(v, int(f[1:]))
    if f == "int":
        return f"{int(v)}"
    raise SystemExit(f"unknown number format {f!r}")


def fill(text: str, models: list[dict], root: Path) -> str:
    """Replace {op args... fmt} with a number from the results."""
    idx = by_id(models)
    ref = cfg_ref[0]

    def ids(spec, s, k):
        if spec in ("all", "others", "after", "before"):
            rd = first_public(idx[ref])
            keep = {"all": lambda m: True, "others": lambda m: m["model"]["id"] != ref,
                    "after": lambda m: first_public(m) > rd, "before": lambda m: first_public(m) < rd}[spec]
            return [m["model"]["id"] for m in models if find_metric(m, s, k)[0] and keep(m)]
        return spec.split(",")

    def units(s, k):
        out = {}
        for m in models:
            sec = m["sections"].get(s) or {}
            if sec.get("status") == "ok" and sec.get("per_unit_file"):
                with open(root / sec["per_unit_file"]) as f:
                    out[m["model"]["id"]] = {r["segment"]: float(r[k]) for r in csv.DictReader(f) if r[k] != ""}
        return out

    def metric(s, k, mid):
        x, _ = find_metric(idx[mid], s, k)
        if not x:
            raise SystemExit(f"finding needs {mid} {s} {k}, which has no score")
        return x

    def pct(s, k, mid, b):
        p = metric(s, k, mid)["paired"].get(b)
        if not p:
            raise SystemExit(f"finding needs {mid} {s} {k} paired with {b}")
        return 100 * p["delta"] / metric(s, k, b)["value"]

    def op(mo):
        a = mo.group(1).split()
        o = a[0]
        if o == "v":
            return fmt(metric(a[1], a[2], a[3])[a[4]], a[5])
        if o == "d":
            p = metric(a[1], a[2], a[3])["paired"].get(a[4])
            if not p:
                raise SystemExit(f"finding needs {a[3]} {a[1]} {a[2]} paired with {a[4]}")
            return fmt(p[a[5]], a[6])
        if o == "pct":
            return fmt(pct(a[1], a[2], a[3], a[4]), "pct")
        if o in ("pctlo", "pcthi"):
            vals = [abs(pct(a[1], a[2], m["model"]["id"], a[3])) for m in models
                    if m["model"]["id"] != a[3] and find_metric(m, a[1], a[2])[0]]
            return fmt(min(vals) if o == "pctlo" else max(vals), "pct")
        if o in ("lo", "hi"):
            vals = [metric(a[1], a[2], mid)[a[4]] for mid in ids(a[3], a[1], a[2])]
            return fmt(min(vals) if o == "lo" else max(vals), a[5])
        if o == "max":
            s = idx[a[3]]["sections"][a[1]]
            with open(root / s["per_unit_file"]) as f:
                vals = [float(r[a[2]]) for r in csv.DictReader(f) if r[a[2]] != ""]
            return fmt(max(vals), a[4])
        if o in ("unitlo", "unithi"):
            vals = [u[a[3]] for u in units(a[1], a[2]).values() if a[3] in u]
            if not vals:
                raise SystemExit(f"finding needs unit {a[3]} in {a[1]} {a[2]}")
            return fmt(min(vals) if o == "unitlo" else max(vals), a[4])
        if o == "maxexcept":
            return fmt(max(v for u in units(a[1], a[2]).values() for k, v in u.items() if k != a[3]), a[4])
        if o in ("best", "worst"):
            got = [(metric(a[1], a[2], mid)[a[4]], mid) for mid in ids(a[3], a[1], a[2])]
            return plain_name(idx[(min if o == "best" else max)(got)[1]])
        if o == "num":
            return str(len(ids(a[3], a[1], a[2])))
        if o == "count":
            return str(sum(1 for m in models if (m["sections"].get(a[1]) or {}).get("status") == "ok"))
        raise SystemExit(f"unknown finding op {o!r}")

    return re.sub(r"\{([a-z]+ [^{}]+)\}", op, text)


CFG: dict = {}
MODELS: list = []  # every model, set in main(); model_class() needs the reference's date
cfg_ref = ["lebowski_877m"]  # set from config.json in main(); fill() reads it for "others"


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
    for href, label, key in ((f"{idx_prefix}#leaderboard", "Leaderboard", "leaderboard"),
                             (f"{idx_prefix}#setups", "Camera setups", "setups"),
                             (f"{idx_prefix}#time", "Over time", "time"),
                             (f"{idx_prefix}#findings", "Findings", "findings"),
                             (f"{idx_prefix}#models", "Models", "models"),
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
<a href="{esc(cfg['repo_url'])}/tree/main/eval">Code and schema</a>.</p>
</footer>
<div id="tip" class="tip" role="status" hidden></div>
<script src="{up}site.js?v={js_v}" defer></script>
</body>
</html>
"""


# ---------------------------------------------------------------- per-model verdict and bars

def verdict_line(m: dict, models: list[dict], cfg: dict, root: Path | None = None) -> str:
    """Factual summary for every model: G55 composite rank & score, closed-loop 2 s median, recovery,
    and open-loop lateral error."""
    over = cfg.get("verdicts", {}).get(m["model"]["id"])
    if over:
        return fill(over, models, root or EVAL)
    mid = m["model"]["id"]
    ref = cfg["reference_model"]
    comp = ranking.g55_composite(models, ranking.truck_scores(root or EVAL))
    cs = comp.get(mid, {})
    n_cl = sum(1 for v in comp.values() if v.get("rank") is not None)
    n_ol = sum(1 for v in comp.values() if v.get("ol_rank") is not None)

    out = []
    if mid == ref:
        out.append("Reference 877M model (July 2026); the G55 drives with it.")
    if cs.get("rank") is not None and cs.get("score") is not None:
        out.append(f'Ranked #{cs["rank"]} of {n_cl} in G55 closed loop (G55 score {num(cs["score"], 1)}/100).')
    elif cs.get("ol_rank") is not None:
        out.append(f'Ranked #{cs["ol_rank"]} of {n_ol} in G55 open loop.')

    x2, _ = find_metric(m, "truck_closed_loop", "xtrack_2s")
    if x2 and x2.get("median") is not None:
        p = x2.get("paired", {}).get(ref)
        w_str = f' ({p["wins"]}/{p["n"]} windows closer than lebowski)' if p and mid != ref else ""
        out.append(f'Closed-loop 2 s median {num(x2["median"] * 100, 1)} cm{w_str}.')
    else:
        out.append("Open loop only on the G55.")

    ol, _ = find_metric(m, "truck_open_loop", "lat_mae")
    if ol and ol.get("value") is not None:
        p = ol.get("paired", {}).get(ref)
        w_str = f' ({p["wins"]}/{p["n"]} vs lebowski)' if p and mid != ref else ""
        out.append(f'Open-loop lateral error {num(ol["value"], 3)} m/s²{w_str}.')

    if mid == "96244b1f_12800":
        out.append("Navigation inputs fed zeros.")
    return " ".join(out)


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


def model_list(models: list[dict], cfg: dict, root: Path) -> str:
    scales = {(s, k): bar_scale(models, s, k) for s, k, *_ in KEY_METRICS}
    comp = ranking.g55_composite(models, ranking.truck_scores(root))
    rows = []
    for m in reversed(models):
        mid = m["model"]["id"]
        md = m["model"]
        cs = comp.get(mid, {})
        rk_badge = ""
        if cs.get("rank") is not None:
            rk_badge = f'<span class="m-rk-pill">#{cs["rank"]} G55 ({num(cs["score"], 1)})</span>'
        elif cs.get("ol_rank") is not None:
            rk_badge = f'<span class="m-rk-pill m-rk-ol">OL #{cs["ol_rank"]}</span>'
        arch_bits = []
        if md.get("params_m") is not None:
            arch_bits.append(f'{num(md["params_m"], 0)}M params')
        if md.get("gmac") is not None:
            arch_bits.append(f'{num(md["gmac"], 1)} GMAC')
        if md.get("family"):
            arch_bits.append(md["family"])
        arch_html = f'<p class="m-arch">{esc(" · ".join(arch_bits))}</p>' if arch_bits else ""
        rows.append(
            f'<li class="mrow"><div class="m-id"><h3>{name_html(m, f"models/{mid}.html")} {rk_badge}</h3>'
            f'<p class="m-st"><span class="m-date">{esc(first_public(m))}</span> {esc(status_text(m))}</p>{arch_html}</div>'
            f'<p class="m-v">{esc(verdict_line(m, models, cfg, root))}</p>'
            f'<div class="m-bars">{bars(m, models, cfg, KEY_METRICS, scales, True)}</div></li>')
    head = ('<div class="mlist-head" aria-hidden="true"><span>Model &amp; architecture</span><span>Summary</span>'
            '<span>G55 key metrics; green tick = lebowski</span></div>')
    return f'{head}<ol class="mlist" reversed>{"".join(rows)}</ol>'


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
    for m in reversed(models):
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
        body.append(f'<tr><th scope="row">{name_html(m, "models/" + m["model"]["id"] + ".html")}</th>{"".join(cells)}</tr>')
    nd = max([(m["sections"].get(s) or {}).get("clusters", 0) for m in models for s in m["sections"] if s.startswith("truck_")] + [0])
    g55 = (f"The G55 sections come from {nd} drives or fewer and carry no interval." if nd < 10 else "")
    return (f'<div class="tw"><table class="sum"><caption>Mean with its 95% interval, then the paired difference from lebowski '
            f'on the units both have. {g55}</caption>'
            f'<thead><tr><th scope="col">Model</th>{th}</tr></thead><tbody>{"".join(body)}</tbody></table></div>')


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


# ---------------------------------------------------------------- the front-page ranking & telemetry

G55 = ("truck_closed_loop", "xtrack_2s")


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


def highlight_class(m, cfg) -> str:
    return {h[0]: h[1] for h in cfg.get("hero", {}).get("highlight", [])}.get(m["model"]["id"], "other")


def g55_ranked(root) -> dict | None:
    """The G55 closed-loop ranking once it has intervals (10 or more drives), else None."""
    t = ranking.truck_scores(root)
    if t and any(s["ci"] for s in t["models"].values()):
        return t["models"]
    return None


def ol_counts(models) -> str:
    secs = [s for s in ((m["sections"].get("truck_open_loop") or {}) for m in models) if s.get("status") == "ok"]
    return f'{max((s.get("n", 0) for s in secs), default=0)} windows from {max((s.get("clusters", 0) for s in secs), default=0)} drives.'


def g55_counts(models) -> tuple[int, int]:
    secs = [m["sections"].get(G55[0]) or {} for m in models]
    ok = [s for s in secs if s.get("status") == "ok"]
    return (max((s.get("n", 0) for s in ok), default=0), max((s.get("clusters", 0) for s in ok), default=0))

def top_cm(r: dict) -> int:
    hi = max([s["ci"][1] for s in r.values() if s.get("ci")] + [s["median"] for s in r.values() if s.get("median") is not None] + [0.01]) * 100
    top = math.ceil(hi / 2 + 0.25) * 2
    return top if top <= 16 else math.ceil(hi / 5 + 0.25) * 5


def tick_step(top: int) -> int:
    return 2 if top <= 16 else 5


def dot_track(s, c, top, unit_word, label) -> str:
    """A model's median (dot) on its interval, on a 0..top cm track."""
    pos = lambda v: f"{v * 100 / top * 100:.2f}%"  # noqa: E731
    grid = "".join(f'<i style="left:{t / top * 100:.2f}%"></i>' for t in range(0, top + 1, tick_step(top)))
    ci = s.get("ci")
    wr = f', head-to-head win rate {s["win_rate"] * 100:.1f}%' if s.get("win_rate") is not None else ""
    tip = (f'{label}: median {cm(s["median"])} cm from the recorded path at 2 s, 95% interval '
           + (f'{cm(ci[0])} to {cm(ci[1])} cm' if ci else "none below 10 drives")
           + f', {s["n"]} {unit_word} from {s["clusters"]} drives{wr}')
    bar = (f'<span class="rk-ci ci-{c}" style="left:{pos(ci[0])};width:calc({pos(ci[1])} - {pos(ci[0])})"></span>' if ci else "")
    return (f'<div class="rk-track" tabindex="0" data-tip="{esc(tip)}" aria-label="{esc(tip)}"><span class="rk-grid">{grid}</span>'
            f'{bar}<span class="rk-dot dot-{c}" style="left:{pos(s["median"])}"></span>'
            + (f'<span class="rk-v" style="left:{pos(ci[1] if ci else s["median"])}">' if (ci[1] if ci else s["median"]) * 100 / top < 0.82
               else f'<span class="rk-v rk-vl" style="right:calc(100% - {pos(ci[0] if ci else s["median"])})">')
            + f'{cm(s["median"])} cm</span></div>')


MEDAL = {1: "🥇", 2: "🥈", 3: "🥉"}


def kpi_strip(models: list[dict], cfg: dict, root: Path) -> str:
    """Four high-signal summary cards above the leaderboard: #1 G55 model, shipped release, latest scored, and corpus scope."""
    idx = by_id(models)
    trk = ranking.truck_scores(root)
    comp = ranking.g55_composite(models, trk)
    ranked_ids = sorted((mid for mid, d in comp.items() if d.get("rank") is not None), key=lambda mid: comp[mid]["rank"])
    latest = max(models, key=lambda m: (scored_at(m), first_public(m), m["model"]["id"]))
    shipped_models = [m for m in models if m["model"]["id"] in comp and comp[m["model"]["id"]].get("rank") is not None and shipped(m) and m["model"]["id"] != cfg["reference_model"]]
    best_shipped = min(shipped_models, key=lambda m: comp[m["model"]["id"]]["rank"]) if shipped_models else None

    cards = []
    if ranked_ids:
        top_id = ranked_ids[0]
        top_m = idx[top_id]
        ts = comp[top_id]
        wr = f' · {ts["win_rate"]*100:.0f}% window wins' if ts.get("win_rate") is not None else ""
        cards.append(
            f'<div class="kpi-card kpi-gold"><span class="kpi-k">🥇 #1 on G55 (composite)</span>'
            f'<strong class="kpi-v">{name_html(top_m, f"models/{top_id}.html")}</strong>'
            f'<span class="kpi-s">G55 score <b>{num(ts["score"], 1)}</b>/100 · {cm(ts["cl_median"])} cm at 2 s{esc(wr)}</span></div>'
        )
    if best_shipped:
        sid = best_shipped["model"]["id"]
        ss = comp[sid]
        cards.append(
            f'<div class="kpi-card"><span class="kpi-k">Best shipped / master model</span>'
            f'<strong class="kpi-v">{name_html(best_shipped, f"models/{sid}.html")}</strong>'
            f'<span class="kpi-s">Ranked <b>#{ss["rank"]}</b> of {len(ranked_ids)} · G55 score <b>{num(ss["score"], 1)}</b> · {cm(ss["cl_median"])} cm at 2 s</span></div>'
        )
    lid = latest["model"]["id"]
    ls = comp.get(lid, {})
    l_rk = f'Ranked #{ls["rank"]} ({num(ls["score"], 1)}/100) · {cm(ls["cl_median"])} cm at 2 s' if ls.get("rank") else f'First public {first_public(latest)}'
    cards.append(
        f'<div class="kpi-card"><span class="kpi-k">Newest scored ({esc(scored_at(latest)[:10])})</span>'
        f'<strong class="kpi-v">{name_html(latest, f"models/{lid}.html")}</strong>'
        f'<span class="kpi-s">{esc(l_rk)}</span></div>'
    )
    nw_cl, nd_cl = g55_counts(models)
    secs_ol = [s for s in ((m["sections"].get("truck_open_loop") or {}) for m in models) if s.get("status") == "ok"]
    nw_ol = max((s.get("n", 0) for s in secs_ol), default=0)
    nd_ol = max((s.get("clusters", 0) for s in secs_ol), default=0)
    cards.append(
        f'<div class="kpi-card"><span class="kpi-k">G55 benchmark corpus</span>'
        f'<strong class="kpi-v">{len(models)} models ({len(ranked_ids)} closed loop)</strong>'
        f'<span class="kpi-s">{nw_cl} closed-loop windows ({nd_cl} drives) · {nw_ol} open-loop ({nd_ol} drives) · 5 camera setups</span></div>'
    )
    return f'<div class="kpi-strip">{"".join(cards)}</div>'


def g55_table(models, cfg, root) -> str:
    """The front page's interactive multi-metric leaderboard:
    - Ranks every G55 closed-loop model 1..N (strict ordinal rank, never 8 models tied at #1) by the
      4-pillar G55 Composite Score (0–100) by default.
    - Interactive tabs and clickable column headers re-rank by: Composite Score, Closed Loop (2 s),
      2° Heading Recovery, 0.5 m Offset Recovery, Closed-Loop Smoothness (Jerk), Open Loop (all 18 models),
      or Newest First.
    - Includes a toggle to expand the 9 historical open-loop-only releases (2023–2026)."""
    ref = cfg["reference_model"]
    trk_doc = ranking.truck_scores(root) or {"models": {}}
    t = trk_doc["models"]
    g = g55_ranked(root)
    if g:
        t = g

    # Fallback if rankings/truck_closed_loop.json is absent (e.g. fixtures)
    if not t:
        for m in models:
            mid = m["model"]["id"]
            x2, sec = find_metric(m, "truck_closed_loop", "xtrack_2s")
            if not x2:
                x2, sec = find_metric(m, "truck_closed_loop", "xtrack_4s")
            if x2 and sec:
                val = x2.get("median") if x2.get("median") is not None else x2.get("value")
                if val is not None:
                    t[mid] = {"median": val, "mean": x2.get("value", val), "ci": x2.get("ci"),
                              "n": x2.get("n", sec.get("n", 0)), "clusters": x2.get("clusters", sec.get("clusters", 0)),
                              "rank": 1}

    comp = ranking.g55_composite(models, {"models": t})
    top = top_cm(t) if t else 20

    # Separate models into closed-loop scored vs open-loop-only historical models
    cl_models = [m for m in models if m["model"]["id"] in t]
    ol_only_models = [m for m in models if m["model"]["id"] not in t and (m["sections"].get("truck_open_loop") or {}).get("status") == "ok"]

    cl_models.sort(key=lambda m: (comp[m["model"]["id"]].get("rank") or 999, t[m["model"]["id"]]["median"]))
    ol_only_models.sort(key=lambda m: (first_public(m), m["model"]["id"]), reverse=True)

    all_shown = cl_models + ol_only_models
    newest = max((first_public(m) for m in all_shown), default="")

    # Metric spans for mini-bars across columns
    def metric_span(sec, key, stat, pool_models):
        vs = [x.get(stat, x.get("value")) for x in (find_metric(m, sec, key)[0] for m in pool_models) if x and x.get(stat, x.get("value")) is not None]
        return (min(vs), max(vs)) if vs else (0.0, 1.0)

    sp_hdg = metric_span("truck_recovery", "heading_xtrack_2s", "median", cl_models)
    sp_off = metric_span("truck_recovery", "offcentre_xtrack_2s", "median", cl_models)
    sp_jrk = metric_span("truck_closed_loop", "lat_jerk", "value", cl_models)
    sp_ol = metric_span("truck_open_loop", "lat_mae", "value", all_shown)

    rx = t.get(ref)
    refline = (f'<i class="gt-refl" style="left:{min(rx["median"] * 100 / top * 100, 100):.2f}%" title="lebowski: {cm(rx["median"])} cm" aria-hidden="true"></i>' if rx else "")
    def render_metric_cell(m, sec, key, stat, scale, dec, unit_str, span_pair, col_key, label_attr, tip_extra=""):
        x, s = find_metric(m, sec, key)
        if not x or x.get(stat, x.get("value")) is None:
            return f'<span class="gt-mcell gt-na" data-col="{col_key}" data-l="{label_attr}">–</span>', None
        raw_v = float(x.get(stat, x["value"]))
        lo, hi = span_pair
        h = (hi - raw_v) / (hi - lo) if hi > lo else 0.5
        best = " gt-best" if abs(raw_v - lo) < 1e-9 and len(cl_models) > 1 else ""
        tip = f'{plain_name(m)}, {label_attr}: {num(raw_v * scale, dec)} {unit_str}{tip_extra}'
        return (
            f'<span class="gt-mcell{best}" data-col="{col_key}" data-l="{label_attr}" '
            f'style="--h:{h:.2f}" tabindex="0" data-tip="{esc(tip)}" aria-label="{esc(tip)}">'
            f'<strong>{num(raw_v * scale, dec)}</strong><small class="gt-mu"> {esc(unit_str)}</small>'
            f'</span>'
        ), raw_v

    rows = []
    for m in all_shown:
        mid = m["model"]["id"]
        md = m["model"]
        cs = comp.get(mid, {})
        has_cl = mid in t
        pl = cs.get("rank") if has_cl else cs.get("ol_rank", 99)
        med = MEDAL.get(pl, "") if has_cl else ""
        tier = f" gt-m{pl}" if (has_cl and med) else ""
        c_cls = model_class(m, cfg)

        tags = []
        if first_public(m) == newest:
            tags.append('<span class="gt-tag gt-new">newest</span>')
        if mid == ref:
            tags.append('<span class="gt-tag gt-ref-tag">ref</span>')
        elif md["source"]["kind"] == "release":
            tags.append(f'<span class="gt-tag gt-ship-tag">{esc(md["source"]["ref"])}</span>')
        elif md.get("on_master"):
            tags.append('<span class="gt-tag gt-ship-tag">master</span>')

        meta_parts = [esc(first_public(m))]
        if md.get("params_m") is not None:
            meta_parts.append(f'{num(md["params_m"], 0)}M')
        if md.get("family"):
            meta_parts.append(esc(md["family"]))

        # Column 3: Compact inline G55 Score + 4-segment bar
        if has_cl and cs.get("score") is not None:
            p = cs["pillars"]
            w_path = (p["path"] or 0) * 0.40
            w_rec = (p["recovery"] or 0) * 0.25
            w_ol = (p["open_loop"] or 0) * 0.20
            w_sm = (p["smoothness"] or 0) * 0.15
            sc_tip = (f'{plain_name(m)} — G55 Score {num(cs["score"], 1)}/100: '
                      f'Path keeping {num(p["path"], 0)}/100 (40%), '
                      f'Recovery {num(p["recovery"], 0)}/100 (25%), '
                      f'Open-loop {num(p["open_loop"], 0)}/100 (20%), '
                      f'Smoothness {num(p["smoothness"], 0)}/100 (15%)')
            score_cell = (
                f'<div class="gt-score" data-col="composite" data-l="G55 Score" tabindex="0" data-tip="{esc(sc_tip)}" aria-label="{esc(sc_tip)}">'
                f'<strong class="gt-sc-v">{num(cs["score"], 1)}</strong>'
                f'<div class="gt-sbar" aria-hidden="true">'
                f'<i class="sb-path" style="width:{w_path:.1f}%"></i>'
                f'<i class="sb-rec" style="width:{w_rec:.1f}%"></i>'
                f'<i class="sb-ol" style="width:{w_ol:.1f}%"></i>'
                f'<i class="sb-sm" style="width:{w_sm:.1f}%"></i>'
                f'</div>'
                f'</div>'
            )
        else:
            ol_rk = cs.get("ol_rank")
            score_cell = (
                f'<div class="gt-score gt-na" data-col="composite" data-l="G55 Score">'
                f'<span class="gt-ol-badge">OL #{ol_rk if ol_rk else "–"}</span>'
                f'</div>'
            )

        # Column 4: Single-line Closed-Loop 2s track (bar on left, clean value + CI on right)
        if has_cl:
            s_ = t[mid]
            bar_pct = min(s_["median"] * 100 / top * 100, 100)
            ci_html = ""
            ci_str = ""
            if s_.get("ci"):
                c0 = min(s_["ci"][0] * 100 / top * 100, 100)
                c1 = min(s_["ci"][1] * 100 / top * 100, 100)
                ci_html = f'<span class="gt-ci" style="left:{c0:.2f}%;width:{max(0.0, c1 - c0):.2f}%"></span>'
                ci_str = f'[{cm(s_["ci"][0])}, {cm(s_["ci"][1])}]'
            wr_tip = f', {s_["win_rate"]*100:.1f}% head-to-head window win rate' if s_.get("win_rate") is not None else ""
            cl_tip = (f'{plain_name(m)}: median {cm(s_["median"])} cm (mean {cm(s_.get("mean", s_["median"]))} cm) '
                      f'from the recorded path at 2 s, 95% interval {ci_str or "none"}, '
                      f'{s_["n"]} windows from {s_["clusters"]} drives{wr_tip}')
            cl_cell = (
                f'<div class="gt-cl" data-col="cl2s" data-l="CL 2s" tabindex="0" data-tip="{esc(cl_tip)}" aria-label="{esc(cl_tip)}">'
                f'<div class="gt-bar">{refline}'
                f'<span class="gt-fill gt-f-{c_cls}" style="width:{bar_pct:.2f}%"></span>'
                f'{ci_html}'
                f'<span class="gt-dot dot-{c_cls}" style="left:{bar_pct:.2f}%"></span>'
                f'</div>'
                f'<div class="gt-cl-read">'
                f'<strong>{cm(s_["median"])}</strong><small class="gt-mu"> cm</small>'
                f'<span class="gt-ci-txt">{esc(ci_str)}</span>'
                f'</div>'
                f'</div>'
            )
            cl_val = s_["median"]
        else:
            cl_cell = '<div class="gt-cl gt-na" data-col="cl2s" data-l="CL 2s"><span class="gt-na-txt">open loop only</span></div>'
            cl_val = None

        c_hdg, v_hdg = render_metric_cell(m, "truck_recovery", "heading_xtrack_2s", "median", 100, 1, "cm", sp_hdg, "heading", "2° heading")
        c_off, v_off = render_metric_cell(m, "truck_recovery", "offcentre_xtrack_2s", "median", 100, 1, "cm", sp_off, "offset", "0.5 m offset")
        c_jrk, v_jrk = render_metric_cell(m, "truck_closed_loop", "lat_jerk", "value", 1, 2, "m/s³", sp_jrk, "jerk", "Smoothness")
        c_ol, v_ol = render_metric_cell(m, "truck_open_loop", "lat_mae", "value", 1, 3, "m/s²", sp_ol, "openloop", "Open loop")

        ol_only_attr = "" if has_cl else ' data-ol-only="true" hidden'
        rows.append(
            f'<div class="gt-row{tier}{" gt-ref" if mid == ref else ""}{" gt-ol-row" if not has_cl else ""}"'
            f' data-place="{pl}"'
            f' data-has-cl="{"1" if has_cl else "0"}"'
            f' data-composite="{cs["score_raw"] if cs.get("score_raw") is not None else -1:.4f}"'
            f' data-cl2s="{cl_val if cl_val is not None else 999:.6f}"'
            f' data-heading="{v_hdg if v_hdg is not None else 999:.6f}"'
            f' data-offset="{v_off if v_off is not None else 999:.6f}"'
            f' data-jerk="{v_jrk if v_jrk is not None else 999:.6f}"'
            f' data-openloop="{v_ol if v_ol is not None else 999:.6f}"'
            f' data-date="{esc(first_public(m))}"{ol_only_attr}>'
            f'<span class="gt-rk" aria-label="place {pl}">{med or pl}</span>'
            f'<div class="gt-name">'
            f'<div class="gt-nm"><span class="sw sw-{c_cls}" aria-hidden="true"></span>{name_html(m, "models/" + mid + ".html")}{"".join(tags)}</div>'
            f'<span class="gt-d">{" · ".join(meta_parts)}</span>'
            f'</div>'
            f'{score_cell}{cl_cell}{c_hdg}{c_off}{c_jrk}{c_ol}'
            f'</div>'
        )

    ticks = "".join(f'<span class="rk-tk gt-tk" style="left:{x / top * 100:.2f}%">{x}</span>' for x in range(0, top + 1, tick_step(top)))
    head = (
        '<div class="gt-row gt-head" role="row">'
        '<span>#</span>'
        '<span>Model</span>'
        '<button type="button" class="gt-th is-active" data-sort="composite" title="Sort by G55 Composite Score (higher is better)"><span class="th-top">G55 Score <i aria-hidden="true">↓</i></span><span class="th-sub">0–100 index</span></button>'
        f'<div class="gt-th-cl"><button type="button" class="gt-th" data-sort="cl2s" title="Sort by closed-loop 2 s median distance (lower is better)"><span class="th-top">Closed loop at 2 s <i aria-hidden="true">↑</i></span><span class="th-sub">cm from path · dashed = lebowski</span></button><div class="rk-axis" aria-hidden="true">{ticks}</div></div>'
        '<button type="button" class="gt-th gt-th-r" data-sort="heading" title="Sort by 2° heading recovery at 2 s (lower is better)"><span class="th-top">2° Heading <i aria-hidden="true">↑</i></span><span class="th-sub">2 s median</span></button>'
        '<button type="button" class="gt-th gt-th-r" data-sort="offset" title="Sort by 0.5 m offset recovery at 2 s (lower is better)"><span class="th-top">0.5 m Offset <i aria-hidden="true">↑</i></span><span class="th-sub">2 s median</span></button>'
        '<button type="button" class="gt-th gt-th-r" data-sort="jerk" title="Sort by closed-loop lateral jerk RMS (lower is smoother)"><span class="th-top">Smoothness <i aria-hidden="true">↑</i></span><span class="th-sub">CL jerk</span></button>'
        '<button type="button" class="gt-th gt-th-r" data-sort="openloop" title="Sort all 18 models by open-loop lateral error (lower is better)"><span class="th-top">Open Loop <i aria-hidden="true">↑</i></span><span class="th-sub">lat error</span></button>'
        '</div>'
    )

    nw, nd = g55_counts(models)
    more_btn = ""
    if ol_only_models:
        more_btn = (
            f'<div class="gt-more-wrap">'
            f'<button type="button" class="gt-more-btn" aria-expanded="false" data-count="{len(ol_only_models)}">'
            f'Show {len(ol_only_models)} earlier openpilot releases (2023–2026, scored in G55 open loop on 119 windows)'
            f'</button></div>'
        )

    return f"""<figure id="leaderboard" class="gt" aria-labelledby="gt-t">
<figcaption class="gt-cap">
<div class="gt-cap-bar">
<div>
<h2 id="gt-t" class="fig-t">Leaderboard: the G55 benchmark</h2>
<p class="gt-legend" aria-hidden="true">
<span><i class="sw sw-ship"></i>Shipped / master</span>
<span><i class="sw sw-hf"></i>Hugging Face only</span>
<span><i class="sw sw-ref"></i>lebowski (ref)</span>
<span><i class="sw sw-old"></i>Earlier (2023–2026)</span>
<span class="gt-leg-sep">·</span>
<span>Score bar: <i class="sb-dot sb-path"></i>Path 40% <i class="sb-dot sb-rec"></i>Recovery 25% <i class="sb-dot sb-ol"></i>Open-Loop 20% <i class="sb-dot sb-sm"></i>Smoothness 15%</span>
</p>
</div>
<div class="gt-sort" role="group" aria-label="Rank models by">
<button type="button" aria-pressed="true" data-sort="composite">G55 Score</button>
<button type="button" aria-pressed="false" data-sort="cl2s">Closed Loop 2s</button>
<button type="button" aria-pressed="false" data-sort="heading">2° Heading</button>
<button type="button" aria-pressed="false" data-sort="offset">0.5 m Offset</button>
<button type="button" aria-pressed="false" data-sort="jerk">Smoothness</button>
<button type="button" aria-pressed="false" data-sort="openloop">Open Loop ({len(all_shown)})</button>
<button type="button" aria-pressed="false" data-sort="date">Newest</button>
</div>
</div>
</figcaption>
<div class="gt-scroll">{head}<div class="gt-body">{"".join(rows)}</div></div>
{more_btn}
<p class="gt-note"><strong>How the ranking works:</strong> Each closed-loop model runs {nw} G55 windows from {nd} drives across 3 closed-loop tests in <code>worldmodel-4B</code> with the G55's measured steering and powertrain response (nominal start, 2° heading error, 0.5 m lateral offset) plus {ol_counts(models)[:-1]} in open loop. The <strong>G55 Score</strong> (0–100, higher is better) weights closed-loop path keeping at 2 s (40%: 25% median + 15% mean), disturbance recovery at 2 s (25%: 12.5% heading + 12.5% offset), open-loop lateral error across all {len(all_shown)} models (20%), and closed-loop lateral jerk RMS (15%). In the metric columns, lower is better; <code>[lo, hi]</code> is the 95% drive-clustered bootstrap interval, and {mark("better")} marks a paired difference from lebowski whose 95% interval excludes zero. <a href="method.html">Full method</a>.</p>
</figure>"""


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

    comp = ranking.g55_composite(models, ranking.truck_scores(root))
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
        if cl_only:
            pool.sort(key=lambda m: (comp.get(m["model"]["id"], {}).get("rank") or 999, first_public(m)))
        else:
            pool.sort(key=lambda m: (comp.get(m["model"]["id"], {}).get("ol_rank") or 999, first_public(m)))

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
                lo, hi = col_spans["pooled"]
                h = (hi - pv) / (hi - lo) if hi > lo else 0.5
                best = " gt-best" if abs(pv - lo) < 1e-9 else ""
                p_cell = f'<td class="n setup-c{best}" style="--h:{h:.2f}">{num(pv * scale, dec)} <span class="gt-mu">{esc(unit_str)}</span></td>'
            else:
                p_cell = '<td class="na">—</td>'

            g_cells = []
            for gkey, _, _ in CAMERA_GROUPS:
                bg = (m["sections"].get(sec_name) or {}).get("by_group", {}).get(gkey)
                gm = next((x for x in (bg or {}).get("metrics", []) if x["key"] == mkey), None)
                if gm and gm.get(stat, gm.get("value")) is not None:
                    gv = float(gm.get(stat, gm["value"]))
                    lo, hi = col_spans[gkey]
                    h = (hi - gv) / (hi - lo) if hi > lo else 0.5
                    best = " gt-best" if abs(gv - lo) < 1e-9 else ""
                    g_cells.append(f'<td class="n setup-c{best}" style="--h:{h:.2f}">{num(gv * scale, dec)}</td>')
                else:
                    g_cells.append('<td class="na">—</td>')

            trows.append(
                f'<tr><th scope="row"><span class="sw sw-{c_cls}" aria-hidden="true"></span> '
                f'{name_html(m, f"models/{mid}.html")}</th>{p_cell}{"".join(g_cells)}</tr>'
            )

        panels.append(
            f'<div class="setup-panel" data-setup-panel="{vkey}"{" hidden" if not active else ""}>'
            f'<div class="tw"><table class="setup-tbl">'
            f'<thead><tr><th scope="col">Model</th>{th_pooled}{"".join(th_groups)}</tr></thead>'
            f'<tbody>{"".join(trows)}</tbody></table></div></div>'
        )

    return f"""<section id="setups" class="setups-sec">
<div class="sec-head">
<div>
<h2>Scores by camera setup</h2>
<p class="sub">The G55 test drives cover 5 camera mounts and lens combinations. Each column shows a single setup beside the pooled score across all drives. Lower is better; bold marks the best model in each column.</p>
</div>
<div class="setup-tabs" role="tablist" aria-label="Metric by camera setup">{"".join(tabs)}</div>
</div>
{"".join(panels)}
</section>"""


# ---------------------------------------------------------------- harness check & living parts

def public_chart(models, cfg, root) -> str:
    """comma's public segments in closed loop: median distance at 2 s, interval, ordinal rank."""
    r = ranking.scores(root)["models"]
    if not r:
        return ""
    top = top_cm(r)
    ranked = sorted((m for m in models if m["model"]["id"] in r), key=lambda m: (r[m["model"]["id"]]["rank"], r[m["model"]["id"]]["median"]))
    rows = []
    for m in ranked:
        mid = m["model"]["id"]
        s = r[mid]
        rk = s["rank"]
        med = MEDAL.get(rk, str(rk))
        wr = f'<span class="rk-wr">{s["win_rate"]*100:.0f}% win rate</span>' if s.get("win_rate") is not None else ""
        rows.append(
            f'<div class="rk-row">'
            f'<span class="rk-ord">{med}</span>'
            f'<div class="rk-name"><span class="rk-sw sw-{model_class(m, cfg)}" aria-hidden="true"></span>'
            f'<div>{name_html(m, "models/" + mid + ".html")}{wr}</div></div>'
            f'{dot_track(s, model_class(m, cfg), top, "segments", plain_name(m))}</div>'
        )
    any_s = next(iter(r.values()))
    ticks = "".join(f'<span class="rk-tk" style="left:{t / top * 100:.2f}%">{t}</span>' for t in range(0, top + 1, tick_step(top)))
    return f"""<figure class="rk rk-pub" aria-labelledby="rkp-t">
<figcaption class="rk-cap"><p id="rkp-t" class="fig-t">Closed-loop path error at 2 s on comma's public segments</p>
<p>{any_s["n"]} public segments across {any_s["clusters"]} drives using comma's ideal car in <code>worldmodel-4B</code>. Bars show the 2 s median and 95% drive-clustered bootstrap interval.</p></figcaption>
<div class="rk-head" aria-hidden="true"><span>#</span><span>Model</span><div class="rk-axis"><span class="rk-al">cm from recorded path at 2 s (public segments)</span>{ticks}</div></div>
<div class="rk-rows">{"".join(rows)}</div>
</figure>"""


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


def _date(s: str) -> _dt.date:
    return _dt.date.fromisoformat(s[:10])


TRENDS_G55 = [
    ("Closed-loop path error at 2 s (cm)", "cm", "g55rank"),
    ("Open-loop lateral error (m/s², 2023–2026)", "m/s²", ("truck_open_loop", "lat_mae")),
    ("2° heading recovery at 2 s (cm)", "cm", ("truck_recovery", "heading_xtrack_2s", "cm")),
    ("0.5 m offset recovery at 2 s (cm)", "cm", ("truck_recovery", "offcentre_xtrack_2s", "cm")),
]
TRENDS_PUBLIC = [
    ("Open-loop lateral action rate on public segments (m/s³)", "m/s³", ("public_open_loop", "lat_rate_rms")),
    ("Open-loop lateral request lead on public segments (s)", "s", ("public_open_loop", "lat_best_lag")),
]


def is_wins(kind) -> bool:
    return isinstance(kind, tuple) and kind[0] == "wins"


def trend_values(kind, models, ref, r):
    out, line = {}, None
    for m in models:
        mid = m["model"]["id"]
        if kind in ("rank", "g55rank"):
            s = r.get(mid)
            if s:
                v = (s["median"] * 100, s["ci"][0] * 100 if s.get("ci") else None, s["ci"][1] * 100 if s.get("ci") else None,
                     f'{s["n"]} {"segments" if kind == "rank" else "windows"} from {s["clusters"]} drives')
                if mid == ref:
                    line = v
                else:
                    out[mid] = v
        elif is_wins(kind):
            x, _ = find_metric(m, kind[1], kind[2])
            p = x and x["paired"].get(ref)
            if mid == ref and x:
                line = (x["n"] / 2, None, None, f'{x["n"]} windows')
            elif p:
                out[mid] = (p["wins"], None, None, f'{p["n"]} windows from {p["clusters"]} drives')
        else:
            x, sec = find_metric(m, kind[0], kind[1])
            if x and kind[2:] == ("cm",) and x.get("median") is not None:
                v = (x["median"] * 100, None, None, f'{x.get("n", sec.get("n", 0))} windows from {x.get("clusters", sec.get("clusters", 0))} drives')
                if mid == ref:
                    line = v
                else:
                    out[mid] = v
            elif x:
                v = (x["value"], x["ci"][0] if x.get("ci") else None, x["ci"][1] if x.get("ci") else None,
                     f'{x.get("n", sec.get("n", 0))} {sec.get("unit_of_evidence", "segment")}s from {x.get("clusters", sec.get("clusters", 0))} drives')
                if mid == ref:
                    line = v
                else:
                    out[mid] = v
    return out, line


def trend_panel(title, unit, kind, models, cfg, r, W=460, H=215) -> str:
    ref = cfg["reference_model"]
    idx = by_id(models)
    vals, line = trend_values(kind, models, ref, r)
    have = [first_public(m) for m in models if m["model"]["id"] in vals or (line and m["model"]["id"] == ref)]
    since = min(have) if have else ""
    others = [m for m in models if m["model"]["id"] != ref and first_public(m) >= since]
    dates = [_date(first_public(m)) for m in others] or [_date(first_public(idx[ref]))]
    d0, d1 = min(dates) - _dt.timedelta(days=6), max(dates) + _dt.timedelta(days=10)
    L, Rr, T, B = 46, 20, 24, 34
    span = max(1, (d1 - d0).days)
    X = lambda d: L + (d - d0).days / span * (W - L - Rr)  # noqa: E731
    ys = [v[0] for v in vals.values()] + [v[i] for v in vals.values() for i in (1, 2) if v[i] is not None]
    if line:
        ys.append(line[0])
    if is_wins(kind):
        n = max([int(v[3].split()[0]) for v in vals.values()] + [int(line[3].split()[0]) if line else 18])
        y0, y1 = 0.0, float(n)
    else:
        lo, hi = (min(ys), max(ys)) if ys else (0.0, 1.0)
        pad = (hi - lo) * 0.16 or 0.1
        y0, y1 = max(0.0, lo - pad), hi + pad
    Y = lambda v: H - B - (v - y0) / (y1 - y0) * (H - T - B)  # noqa: E731
    step = _nice((y1 - y0) / 4)
    g = []
    t = math.ceil(y0 / step) * step
    while t <= y1 + 1e-9:
        g.append(f'<line class="grid" x1="{L}" x2="{W - Rr}" y1="{Y(t):.1f}" y2="{Y(t):.1f}"/>'
                 f'<text class="tick" x="{L - 6}" y="{Y(t) + 4:.1f}" text-anchor="end">{_fmt_tick(t, step)}</text>')
        t += step
    d = _dt.date(d0.year, d0.month, 1)
    while d <= d1:
        if span > 200:
            ticks = [(d, str(d.year) if d.month == 1 else "Jul")] if d.month in (1, 7) else []
        else:
            ticks = [(d.replace(day=day), f'{d.strftime("%b")} {day}') for day in (1, 15)]
        for dd, lab in ticks:
            if d0 <= dd <= d1:
                g.append(f'<line class="xt" x1="{X(dd):.1f}" x2="{X(dd):.1f}" y1="{H - B}" y2="{H - B + 4}"/>'
                         f'<text class="tick" x="{X(dd):.1f}" y="{H - B + 17}" text-anchor="middle">{lab}</text>')
        d = (d.replace(day=28) + _dt.timedelta(days=4)).replace(day=1)
    refl = ""
    if line:
        lab = f"{line[0]:g} of {int(line[0] * 2)}" if is_wins(kind) else f"lebowski ({num(line[0], 1 if unit == 'cm' else 2)})"
        better = "" if is_wins(kind) else f'<rect class="better" x="{L}" y="{Y(line[0]):.1f}" width="{W - Rr - L}" height="{max(0.0, H - B - Y(line[0])):.1f}"/>'
        refl = (better + f'<line class="refl" x1="{L}" x2="{W - Rr}" y1="{Y(line[0]):.1f}" y2="{Y(line[0]):.1f}"/>'
                f'<text class="refl-t" x="{L + 5}" y="{max(T + 10, Y(line[0]) - 5):.1f}">{lab}</text>')
    ordered = sorted((m for m in others if m["model"]["id"] in vals), key=lambda m: (first_public(m), m["model"]["id"]))
    path = " ".join(f'{X(_date(first_public(m))):.1f},{Y(vals[m["model"]["id"]][0]):.1f}' for m in ordered)
    marks = []
    for m in ordered:
        mid = m["model"]["id"]
        v, lo, hi, nw = vals[mid]
        cx, cy = X(_date(first_public(m))), Y(v)
        c = model_class(m, cfg)
        if lo is not None:
            marks.append(f'<line class="tw-ci st-{c}" x1="{cx:.1f}" x2="{cx:.1f}" y1="{Y(lo):.1f}" y2="{Y(hi):.1f}"/>')
        vt = f"{v:.0f} of {nw}" if is_wins(kind) else f"{num(v, 1 if unit == 'cm' else 2)} {unit}" + (f" [{num(lo, 1 if unit == 'cm' else 2)}, {num(hi, 1 if unit == 'cm' else 2)}]" if lo is not None else "") + f", {nw}"
        tip = f'{plain_name(m)}, first public {first_public(m)}, {"on openpilot master or shipped" if shipped(m) else "Hugging Face upload only"}: {vt}'
        fill_cls = "hollow" if c == "hf" else f"fl-{c}"
        marks.append(f'<circle class="tp st-{c} {fill_cls}" cx="{cx:.1f}" cy="{cy:.1f}" r="5" tabindex="0" data-tip="{esc(tip)}" aria-label="{esc(tip)}"/>')

    # Label notable points (best, newest) without overlapping
    # Label notable points (best, newest) without overlapping or clipping
    if ordered:
        best_m = min(ordered, key=lambda m: vals[m["model"]["id"]][0])
        newest_m = ordered[-1]
        ref_y = Y(line[0]) if line and not is_wins(kind) else -999.0

        def clamp_y(y_val):
            return max(14.0, min(float(H - B - 6), y_val))

        if best_m is newest_m:
            mid = best_m["model"]["id"]
            cx, cy = X(_date(first_public(best_m))), Y(vals[mid][0])
            anchor = "end" if cx > W - 95 else "start"
            dx = -7 if anchor == "end" else 7
            dy = -9 if cy > T + 18 else 14
            if abs((cy + dy) - ref_y) < 10:
                dy = 14 if dy < 0 else -9
            marks.append(f'<text class="tp-lbl" x="{cx + dx:.1f}" y="{clamp_y(cy + dy):.1f}" text-anchor="{anchor}">{esc(short_name(best_m))}</text>')
        else:
            b_id, n_id = best_m["model"]["id"], newest_m["model"]["id"]
            bx, by = X(_date(first_public(best_m))), Y(vals[b_id][0])
            nx, ny = X(_date(first_public(newest_m))), Y(vals[n_id][0])
            close_x = abs(nx - bx) < 120
            if close_x:
                b_dy = -9 if by <= ny else 14
                n_dy = 14 if by <= ny else -9
            else:
                b_dy = -9 if by > T + 18 else 14
                n_dy = -9 if ny > T + 18 else 14
            for lm, cx, cy, dy in ((best_m, bx, by, b_dy), (newest_m, nx, ny, n_dy)):
                if abs((cy + dy) - ref_y) < 9:
                    dy = -9 if cy < ref_y else 14
                anchor = "end" if cx > W - 95 else "start"
                dx = -7 if anchor == "end" else 7
                marks.append(f'<text class="tp-lbl" x="{cx + dx:.1f}" y="{clamp_y(cy + dy):.1f}" text-anchor="{anchor}">{esc(short_name(lm))}</text>')
    pend = [m for m in others if m["model"]["id"] not in vals]
    for m in pend:
        cx = X(_date(first_public(m)))
        tip = f"{plain_name(m)}, first public {first_public(m)}: not scored here yet"
        marks.append(f'<path class="pend" d="M{cx - 3:.1f},{H - B - 3:.1f} l6,6 m0,-6 l-6,6" tabindex="0" data-tip="{esc(tip)}" aria-label="{esc(tip)}"/>')
    latest = ordered[-1] if ordered else None
    if latest:
        v, lo, hi, nw = vals[latest["model"]["id"]]
        best_m = min(ordered, key=lambda m: vals[m["model"]["id"]][0])
        bv = vals[best_m["model"]["id"]][0]
        d_fmt = 1 if unit == "cm" else (3 if unit == "m/s²" else 2)
        cap = f'Best: <strong>{esc(short_name(best_m))}</strong> ({num(bv, d_fmt)} {esc(unit)}) · Newest: <strong>{esc(short_name(latest))}</strong> ({num(v, d_fmt)} {esc(unit)})'
        if line and not is_wins(kind):
            cap += f' · lebowski {num(line[0], d_fmt)} {esc(unit)}'
    else:
        cap = "No model scored here yet."
    return (f'<figure class="trend"><p class="trend-t">{esc(title)}</p>'
            f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{esc(title)}, by the date each model was first public">'
            f'{"".join(g)}{refl}<polyline class="tl" points="{path}"/>{"".join(marks)}</svg>'
            f'<figcaption>{cap}</figcaption></figure>')


def _nice(x: float) -> float:
    e = 10 ** math.floor(math.log10(x)) if x > 0 else 1
    for f in (1, 2, 2.5, 5, 10):
        if x <= f * e:
            return f * e
    return 10 * e


def _fmt_tick(t: float, step: float) -> str:
    d = 0 if step >= 1 else 1 if step >= 0.1 else 2
    return f"{t:.{d}f}"


def trend_legend(cfg, bars_shown: bool = True, pend: bool = True) -> str:
    dot = lambda c, hollow=False: (f'<svg width="12" height="12" aria-hidden="true"><circle cx="6" cy="6" r="4.5" '  # noqa: E731
                                   f'class="st-{c} {"hollow" if hollow else "fl-" + c}" stroke-width="2"/></svg>')
    return ('<p class="trend-key">'
            f'<span>{dot("ship")}shipped or on openpilot master</span>'
            f'<span>{dot("hf", True)}Hugging Face checkpoint</span>'
            f'<span>{dot("ref")}lebowski reference (July 2026)</span>'
            f'<span>{dot("old")}earlier releases (2023–2026)</span>'
            '<span><i class="key-better" aria-hidden="true"></i>better than lebowski</span>'
            + ('<span><svg width="12" height="12" aria-hidden="true"><path class="pend" d="M3,3 l6,6 m0,-6 l-6,6"/></svg>not scored yet</span>' if pend else '')
            + ('<span>vertical bars: 95% interval</span>' if bars_shown else '') + '</p>')


def over_time(models, cfg, root) -> str:
    """The G55 panels: closed-loop and recovery medians, then the open-loop lateral error."""
    r = (ranking.truck_scores(root) or {"models": {}})["models"]
    panels = "".join(trend_panel(t, u, k, models, cfg, r, W=460, H=210) for t, u, k in TRENDS_G55)
    key = trend_legend(cfg, "tw-ci" in panels, 'class="pend"' in panels)
    return f'{key}<div class="trends">{panels}</div>'


def harness_trends(models, cfg, root) -> str:
    r = ranking.scores(root)["models"]
    return '<div class="trends">' + "".join(trend_panel(t, u, k, models, cfg, r, W=460, H=210) for t, u, k in TRENDS_PUBLIC) + "</div>"


def harness(models, cfg, root, heading="h2") -> str:
    """comma's public drives: a check of the harness, rerunnable by anyone."""
    return f"""<{heading}>Harness check on comma's public drives</{heading}>
<p class="sub">Runs the same evaluation pipeline on comma's 45 public two-camera segments (28 in closed loop with comma's kinematic car model). Per-segment CSVs and scoring code are published so anyone can verify the numbers with <code>python -m geval.reproduce</code>.</p>
{public_chart(models, cfg, root)}
{harness_trends(models, cfg, root)}"""


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
    more = f'<details class="lg-more"><summary>{len(rest)} earlier entries</summary><ol class="log">{"".join(rest)}</ol></details>' if rest else ""
    return f'<ol class="log">{"".join(head)}</ol>{more}'


# ---------------------------------------------------------------- overview

def findings_section(models: list[dict], cfg: dict, root: Path) -> str:
    """Auto-computed analytical findings from the published results files (zero manual maintenance)."""
    ref_id = cfg["reference_model"]
    idx = by_id(models)
    ref_m = idx.get(ref_id)
    comp_models = ranking.g55_composite(models, ranking.truck_scores(root))
    ranked_ids = sorted(
        (mid for mid, d in comp_models.items() if d.get("rank") is not None),
        key=lambda mid: comp_models[mid]["rank"],
    )
    def _find_metric(m_doc: dict, sec_name: str, key: str) -> dict | None:
        return find_metric(m_doc, sec_name, key)[0]


    cl_models = [idx[mid] for mid in ranked_ids if mid in idx]
    ol_models = [
        m for m in models
        if (m["sections"].get("truck_open_loop") or {}).get("status") == "ok"
        and _find_metric(m, "truck_open_loop", "lat_mae")
    ]

    # Fallback for minimal synthetic fixtures (< 3 closed-loop models)
    if len(cl_models) < 3 or not ref_m:
        def _legacy(f):
            try:
                return (f'<li class="f-card"><span class="f-fig">{esc(fill(f["figure"], models, root))}</span>'
                        f'<div class="f-b"><p>{esc(fill(f["line"], models, root))}</p>'
                        f'<p class="f-sub">{esc(fill(f["text"], models, root))}</p></div></li>')
            except (SystemExit, ValueError, KeyError):
                if root.resolve() == EVAL.resolve():
                    raise
                return ""
        items = "".join(_legacy(f) for f in cfg.get("findings", []))
        return f'<section id="findings"><h2>Findings</h2><ul class="findings-grid">{items}</ul></section>'

    after_cl = [m for m in cl_models if m["model"]["id"] != ref_id and first_public(m) > first_public(ref_m)]
    nw_cl, nd_cl = g55_counts(models)

    def m_a(m: dict) -> str:
        return f'<a class="mlink" href="models/{esc(m["model"]["id"])}.html"><strong>{esc(short_name(m))}</strong></a>'

    # 1. Composite Ranking & Podium
    m1, m2, m3 = cl_models[0], cl_models[1], cl_models[2]
    s1 = comp_models[m1["model"]["id"]]["score"]
    s2 = comp_models[m2["model"]["id"]]["score"]
    s3 = comp_models[m3["model"]["id"]]["score"]
    s_ref = comp_models.get(ref_id, {}).get("score", 0.0)
    newest_cl = max(cl_models, key=first_public)
    s_new = comp_models[newest_cl["model"]["id"]]
    shipped_cl = [m for m in cl_models if m["model"]["id"] != ref_id and (m["model"]["source"]["kind"] in ("release", "openpilot-master") or m["model"].get("on_master"))]
    top_ship = shipped_cl[0] if shipped_cl else m1
    s_ship = comp_models[top_ship["model"]["id"]]

    card1 = (
        '<li class="f-card">'
        '<div class="f-top"><span class="f-kicker">Composite Ranking · 4 Pillars</span>'
        f'<span class="f-fig">#{s_new["rank"] if newest_cl is m1 else 1} {esc(short_name(m1))} · {s1:.1f}<small>/100</small></span></div>'
        f'<p class="f-lead">{m_a(m1)} (<strong>{s1:.1f}</strong>) leads {m_a(m2)} (<strong>{s2:.1f}</strong>) and {m_a(m3)} (<strong>{s3:.1f}</strong>) '
        f'on the 4-pillar G55 score (40% path keeping, 25% recovery, 20% open loop, 15% smoothness). '
        f'The July 2026 reference model {m_a(ref_m)} scores <strong>{s_ref:.1f}</strong>.</p>'
        '<ul class="f-meta">'
        f'<li><span>Newest model:</span> {m_a(newest_cl)} ({esc(first_public(newest_cl))}) ranks <strong>#{s_new["rank"]} of {len(cl_models)}</strong> ({s_new["score"]:.1f}/100).</li>'
        f'<li><span>Top shipped / master model:</span> {m_a(top_ship)} ranks <strong>#{s_ship["rank"]} overall</strong> ({s_ship["score"]:.1f}/100).</li>'
        '</ul></li>'
    )

    # 2. Closed-Loop Nominal Path Keeping (2 s & 4 s)
    cl2_vals = [(m, _find_metric(m, "truck_closed_loop", "xtrack_2s")) for m in after_cl]
    cl2_vals = [(m, x) for m, x in cl2_vals if x and x.get("median") is not None]
    cl2_vals.sort(key=lambda t: t[1]["median"])
    best_cl2_m, best_cl2_x = cl2_vals[0]
    worst_cl2_m, worst_cl2_x = cl2_vals[-1]
    ref_cl2_x = _find_metric(ref_m, "truck_closed_loop", "xtrack_2s") or {}
    best_cl2_p = (best_cl2_x.get("paired") or {}).get(ref_id) or {}
    win_pct = round(best_cl2_p["wins"] / best_cl2_p["n"] * 100) if best_cl2_p.get("wins") is not None and best_cl2_p.get("n") else None
    delta_cm = (best_cl2_p.get("delta") or 0.0) * 100

    cl4_vals = [(m, _find_metric(m, "truck_closed_loop", "xtrack_4s")) for m in after_cl]
    cl4_vals = [(m, x) for m, x in cl4_vals if x and x.get("median") is not None]
    cl4_vals.sort(key=lambda t: t[1]["median"])
    best_cl4_m, best_cl4_x = cl4_vals[0]
    worst_cl4_x = cl4_vals[-1][1]
    ref_cl4_x = _find_metric(ref_m, "truck_closed_loop", "xtrack_4s") or {}

    card2 = (
        '<li class="f-card">'
        f'<div class="f-top"><span class="f-kicker">Closed Loop · Nominal Path ({nw_cl} windows, {nd_cl} drives)</span>'
        f'<span class="f-fig">{best_cl2_x["median"] * 100:.1f}–{worst_cl2_x["median"] * 100:.1f} cm <small>vs {ref_cl2_x.get("median", 0) * 100:.1f} cm ref</small></span></div>'
        f'<p class="f-lead">All {len(after_cl)} models released after {m_a(ref_m)} ({ref_cl2_x.get("median", 0) * 100:.1f} cm) track the recorded G55 path more closely at 2 s. '
        f'{m_a(best_cl2_m)} has the lowest 2 s median at <strong>{best_cl2_x["median"] * 100:.1f} cm</strong> '
        f'(<strong>{delta_cm:+.1f} cm</strong> paired mean vs lebowski'
        + (f', winning <strong>{win_pct}%</strong> of windows' if win_pct is not None else '') + ').</p>'
        '<ul class="f-meta">'
        f'<li><span>4 s horizon:</span> Medians span <strong>{best_cl4_x["median"] * 100:.1f}–{worst_cl4_x["median"] * 100:.1f} cm</strong> ({m_a(best_cl4_m)} {best_cl4_x["median"] * 100:.1f} cm vs lebowski {ref_cl4_x.get("median", 0) * 100:.1f} cm).</li>'
        f'<li><span>Largest post-lebowski drift at 2 s:</span> {m_a(worst_cl2_m)} at <strong>{worst_cl2_x["median"] * 100:.1f} cm</strong> median ({worst_cl2_x.get("value", 0) * 100:.1f} cm mean).</li>'
        '</ul></li>'
    )

    # 3. Disturbance Recovery vs Nominal Path Trade-Off
    rec_sorted = sorted(
        cl_models,
        key=lambda m: comp_models[m["model"]["id"]]["pillars"].get("recovery", 0.0),
        reverse=True,
    )
    r1, r2, r_slow = rec_sorted[0], rec_sorted[1], rec_sorted[-1]
    r1_hdg = (_find_metric(r1, "truck_recovery", "heading_xtrack_2s") or {}).get("median", 0) * 100
    r1_off = (_find_metric(r1, "truck_recovery", "offcentre_xtrack_2s") or {}).get("median", 0) * 100
    r2_hdg = (_find_metric(r2, "truck_recovery", "heading_xtrack_2s") or {}).get("median", 0) * 100
    r2_off = (_find_metric(r2, "truck_recovery", "offcentre_xtrack_2s") or {}).get("median", 0) * 100
    rs_hdg = (_find_metric(r_slow, "truck_recovery", "heading_xtrack_2s") or {}).get("median", 0) * 100
    rs_off = (_find_metric(r_slow, "truck_recovery", "offcentre_xtrack_2s") or {}).get("median", 0) * 100

    card3 = (
        '<li class="f-card">'
        '<div class="f-top"><span class="f-kicker">Closed Loop · Disturbance Recovery</span>'
        f'<span class="f-fig">{r1_hdg:.1f} cm <small>2° heading</small> · {r1_off:.1f} cm <small>0.5 m offset</small></span></div>'
        f'<p class="f-lead">{m_a(r1)} (<strong>{r1_hdg:.1f} cm</strong> / <strong>{r1_off:.1f} cm</strong>) and '
        f'{m_a(r2)} (<strong>{r2_hdg:.1f} cm</strong> / <strong>{r2_off:.1f} cm</strong>) correct a 2° heading error or 0.5 m lateral offset fastest at 2 s. '
        'Models tuned for smooth nominal tracking return more gradually.</p>'
        '<ul class="f-meta">'
        f'<li><span>Slowest recovery:</span> {m_a(r_slow)} averages <strong>{rs_hdg:.1f} cm</strong> (2° heading) and <strong>{rs_off:.1f} cm</strong> (0.5 m offset) at 2 s.</li>'
        f'<li><span>Best post-lebowski recovery:</span> {m_a(r2 if r1 is ref_m else r1)} scores <strong>{comp_models[(r2 if r1 is ref_m else r1)["model"]["id"]]["pillars"]["recovery"]:.0f}/100</strong> on the recovery pillar.</li>'
        '</ul></li>'
    )

    # 4. Control Smoothness & Steering Gain
    jrk_vals = [(m, _find_metric(m, "truck_closed_loop", "lat_jerk")) for m in after_cl]
    jrk_vals = [(m, x) for m, x in jrk_vals if x and x.get("value") is not None]
    jrk_vals.sort(key=lambda t: t[1]["value"])
    best_jrk_m, best_jrk_x = jrk_vals[0]
    sec_jrk_m, sec_jrk_x = jrk_vals[1]
    worst_jrk_x = jrk_vals[-1][1]
    ref_jrk_v = (_find_metric(ref_m, "truck_closed_loop", "lat_jerk") or {}).get("value", 0.57)
    min_drop = round((1.0 - worst_jrk_x["value"] / ref_jrk_v) * 100)
    max_drop = round((1.0 - best_jrk_x["value"] / ref_jrk_v) * 100)

    gains_after = [(_find_metric(m, "truck_open_loop", "lat_gain") or {}).get("value") for m in after_cl]
    gains_after = [g for g in gains_after if g is not None]
    ref_gain = (_find_metric(ref_m, "truck_open_loop", "lat_gain") or {}).get("value", 0.80)

    card4 = (
        '<li class="f-card">'
        '<div class="f-top"><span class="f-kicker">Control Smoothness · Jerk &amp; Gain</span>'
        f'<span class="f-fig">−{min_drop}% to −{max_drop}% <small>CL jerk vs lebowski</small></span></div>'
        f'<p class="f-lead">Post-July 2026 models cut closed-loop lateral jerk from {m_a(ref_m)}\'s <strong>{ref_jrk_v:.2f} m/s³</strong> '
        f'down to <strong>{best_jrk_x["value"]:.2f}–{worst_jrk_x["value"]:.2f} m/s³</strong>. '
        f'{m_a(best_jrk_m)} ({best_jrk_x["value"]:.2f} m/s³) and {m_a(sec_jrk_m)} ({sec_jrk_x["value"]:.2f} m/s³) have the lowest closed-loop jerk.</p>'
        '<ul class="f-meta">'
        f'<li><span>Open-loop lateral gain:</span> {m_a(ref_m)} requests <strong>{ref_gain:.2f}×</strong> the G55\'s lateral acceleration vs <strong>{min(gains_after):.2f}–{max(gains_after):.2f}×</strong> on newer models.</li>'
        '</ul></li>'
    )

    # 5. Open-Loop vs Closed-Loop Divergence
    ol_sorted = sorted(ol_models, key=lambda m: _find_metric(m, "truck_open_loop", "lat_mae")["value"])
    best_ol_m = ol_sorted[0]
    sec_ol_m = ol_sorted[1]
    worst_ol_m = ol_sorted[-1]
    best_ol_v = _find_metric(best_ol_m, "truck_open_loop", "lat_mae")["value"]
    sec_ol_v = _find_metric(sec_ol_m, "truck_open_loop", "lat_mae")["value"]
    worst_ol_v = _find_metric(worst_ol_m, "truck_open_loop", "lat_mae")["value"]
    ref_ol_v = (_find_metric(ref_m, "truck_open_loop", "lat_mae") or {}).get("value", 0.0)
    best_ol_comp_rk = comp_models.get(best_ol_m["model"]["id"], {}).get("rank")

    card5 = (
        '<li class="f-card">'
        f'<div class="f-top"><span class="f-kicker">Open vs Closed Loop · {len(ol_models)} Models (2023–2026)</span>'
        f'<span class="f-fig">{best_ol_v:.3f}–{worst_ol_v:.3f} m/s² <small>open-loop error</small></span></div>'
        f'<p class="f-lead">{m_a(best_ol_m)} has the lowest open-loop lateral error across all {len(ol_models)} models (<strong>{best_ol_v:.3f} m/s²</strong> over {ol_counts(models).rstrip(".")}), '
        f'ahead of {m_a(sec_ol_m)} (<strong>{sec_ol_v:.3f} m/s²</strong>) and {m_a(ref_m)} (<strong>{ref_ol_v:.3f} m/s²</strong>).</p>'
        '<ul class="f-meta">'
        f'<li><span>Open vs closed loop:</span> {m_a(best_ol_m)} ranks #1 in open loop ({best_ol_v:.3f} m/s²) but #{best_ol_comp_rk} on the composite score due to slower 2° heading recovery ({(_find_metric(best_ol_m, "truck_recovery", "heading_xtrack_2s") or {}).get("median", 0) * 100:.1f} cm).</li>'
        f'<li><span>2023–2025 releases:</span> Earlier single-signature models span up to <strong>{worst_ol_v:.3f} m/s²</strong> ({m_a(worst_ol_m)}).</li>'
        '</ul></li>'
    )

    # 6. Camera Mount & Lens Sensitivity (by_group across 5 G55 setups)
    def _gm_val(m_doc: dict, sec_name: str, g_key: str, mkey: str, stat: str) -> float | None:
        bg = ((m_doc["sections"].get(sec_name) or {}).get("by_group") or {}).get(g_key)
        if not bg:
            return None
        gm = next((x for x in bg.get("metrics", []) if x["key"] == mkey), None)
        if not gm:
            return None
        v = gm.get(stat, gm.get("value"))
        return float(v) if v is not None else None

    setup_means_cl = []
    setup_means_ol = []
    for g_key, g_short, g_desc in CAMERA_GROUPS:
        c_vals = [_gm_val(m, "truck_closed_loop", g_key, "xtrack_2s", "median") for m in cl_models]
        c_vals = [v * 100 for v in c_vals if v is not None]
        if c_vals:
            setup_means_cl.append((sum(c_vals) / len(c_vals), g_short, g_desc))
        o_vals = [_gm_val(m, "truck_open_loop", g_key, "lat_mae", "value") for m in cl_models]
        o_vals = [v for v in o_vals if v is not None]
        if o_vals:
            setup_means_ol.append((sum(o_vals) / len(o_vals), g_short, g_desc))

    setup_means_cl.sort()
    setup_means_ol.sort()
    best_cl_s, worst_cl_s = setup_means_cl[0], setup_means_cl[-1]
    best_ol_s, worst_ol_s = setup_means_ol[0], setup_means_ol[-1]

    card6 = (
        '<li class="f-card">'
        '<div class="f-top"><span class="f-kicker">Hardware · 5 Camera Setups</span>'
        f'<span class="f-fig">{best_cl_s[0]:.1f} cm <small>{esc(best_cl_s[1])}</small> vs {worst_cl_s[0]:.1f} cm <small>{esc(worst_cl_s[1])}</small></span></div>'
        f'<p class="f-lead">Across the {len(cl_models)} closed-loop models, 2 s path error is lowest on '
        f'<strong>{esc(best_cl_s[1])}</strong> (<strong>{best_cl_s[0]:.1f} cm</strong>) and highest on '
        f'<strong>{esc(worst_cl_s[1])}</strong> (<strong>{worst_cl_s[0]:.1f} cm</strong>).</p>'
        '<ul class="f-meta">'
        f'<li><span>Open-loop replay by setup:</span> Lowest lateral error on <strong>{esc(best_ol_s[1])}</strong> (<strong>{best_ol_s[0]:.3f} m/s²</strong>) vs <strong>{esc(worst_ol_s[1])}</strong> (<strong>{worst_ol_s[0]:.3f} m/s²</strong>).</li>'
        '<li><span>Full breakdown:</span> Compare Closed Loop, Recovery, and Open Loop in <a href="#setups">Scores by camera setup</a> above.</li>'
        '</ul></li>'
    )

    return (
        '<section id="findings" class="findings-sec">'
        '<div class="sec-head"><div>'
        '<h2>Key findings</h2>'
        '<p class="sub">Computed directly from the current results across all scored models.</p>'
        '</div></div>'
        f'<ul class="findings-grid">{card1}{card2}{card3}{card4}{card5}{card6}</ul>'
        '</section>'
    )


def overview(models, cfg, root) -> str:
    return f"""<section class="hero">
<div class="hero-t">
<h1>openpilot driving model evaluations</h1>
<p class="lede">Benchmarking comma.ai's openpilot driving models on a 2011 Mercedes G55 AMG in open-loop replay and closed-loop simulation.</p>
{status_strip(models, cfg, root)}
</div>
{g55_table(models, cfg, root)}
</section>

{camera_setups_section(models, cfg, root)}

<section id="time" class="time">
<h2>Trends by release date</h2>
<p class="sub">Plotted by each model's first public release date ({g55_counts(models)[0]} closed-loop windows across {g55_counts(models)[1]} drives; {ol_counts(models).rstrip(".")} in open loop). Lower is better.</p>
{over_time(models, cfg, root)}
</section>

{findings_section(models, cfg, root)}

<section id="models">
<h2>All models</h2>
<p class="sub">Ordered from newest to oldest by public release date. Select any model for its full metrics and per-setup tables.</p>
{model_list(models, cfg, root)}
</section>

<section id="log" class="logsec">
<h2>Log</h2>
<p class="sub">Recent model releases, scoring runs, and benchmark changes from <a href="{esc(cfg['repo_url'])}/blob/main/eval/CHANGELOG.md">CHANGELOG.md</a>.</p>
{log_feed(models, root)}
</section>

<section id="how" class="prose">
<h2>How it works</h2>
<div class="how">
<div><h3>Open loop</h3><p>The model runs on recorded camera frames. Its lateral and longitudinal requests are compared against the vehicle's measured motion.</p></div>
<div><h3>Closed loop</h3><p>Each model's steering and speed requests drive a simulated vehicle while <code>worldmodel-4B</code> renders the next camera frame at each 0.2 s step over a 7.2 s rollout. Models are scored by distance from the recorded path.</p></div>
<div><h3>The G55</h3><p>The test vehicle is a 2011 Mercedes G55 AMG (W463) with recirculating-ball steering and a 1.45 m camera height. openpilot does not support the W463, so none of these drives appear in comma's training set. Closed-loop rollouts use a plant model fitted to the G55's steering and powertrain response.</p></div>
</div>
<p>comma publishes its own test reports in <a href="https://commaai.github.io/model_reports/">model_reports</a> (latest: Cinque v3, 2026-09-18). Nearest equivalents:</p>
<div class="tw"><table class="vocab"><thead><tr><th scope="col">Here</th><th scope="col">Nearest test in comma's model_reports</th></tr></thead><tbody>
<tr><th scope="row">Lateral action rate (open loop)</th><td>jitter</td></tr>
<tr><th scope="row">Closed-loop path error (comma's ideal car)</th><td>MLSim lateral (<code>analyse_lat_mlsim</code>) in comma's world model</td></tr>
<tr><th scope="row">G55 recovery (0.5 m lateral offset or 2° heading error)</th><td>hugging (recovery under a lateral bias)</td></tr>
<tr><th scope="row">Braking lead car (2 windows)</th><td>unintended lead following</td></tr>
</tbody></table></div>
<p>See the <a href="method.html">Method page</a> for full scoring details and an interactive <code>worldmodel-4B</code> rollout player.</p>
</section>

<section id="harness" class="harness">
{harness(models, cfg, root)}
</section>

<section id="research" class="research">
<h2>For researchers</h2>
<ul class="plain">
<li>Every score is a mean or median over evaluation units (60 s segments on public drives; fixed windows on G55 drives). Units are grouped by drive, and 95% confidence intervals resample whole drives. Tests with fewer than 10 drives report means and win counts without an interval.</li>
<li>A paired difference compares two models on the exact same windows or segments; the win count shows how often the model beat the baseline.</li>
<li>Per-segment CSVs, model results files (<a href="{esc(cfg['repo_url'])}/blob/main/eval/SCHEMA.md">schema</a>), and scoring code are public. Run <code>python -m geval.reproduce</code> to recompute every public interval from the CSVs. <a href="method.html#downloads">Downloads</a></li>
<li>Raw G55 drive recordings stay private; their aggregate and per-setup scores are published.</li>
</ul>
{legend()}
{summary_table(models, cfg)}
{unscored()}
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
    code_html = f'<a href="{esc(cfg["repo_url"])}/commit/{esc(code)}"><code>{esc(code[:10])}</code></a>' if code else "n/a"
    ident = [
        ("Model id", f'<code>{esc(md["id"])}</code>'),
        ("SHA-256", f'<code class="hash">{esc(md["sha256"])}</code>'),
        ("File scored", f'{esc(src["kind"])}, <code>{esc(src["ref"])}</code> (<a href="{esc(src["url"])}">link</a>), {esc(src["date"])}'),
        ("First public", esc(first_public(m))),
        ("Parameters", (f'{num(md["params_m"], 1)}M' if md["params_m"] is not None else "not counted")),
        ("GMAC per step", num(md["gmac"], 1)),
        ("Family", esc(md["family"])),
        ("Scored", f'{esc(sc["date"][:16].replace("T", " "))} UTC, suite <code>{esc(sc["suite"])}</code>, code {code_html}'),
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
        prevt = f' Paired deltas compare against lebowski and against the preceding release ({name_html(po, prev + ".html") if po else esc(prev)}).'
    return (f'<section class="mhead"><p class="crumb"><a href="../index.html#leaderboard">← Leaderboard &amp; all models</a></p>'
            f'<h1>{name_html(m)}</h1><p class="m-st"><span class="m-date">{esc(first_public(m))}</span> {esc(status_text(m))}</p>'
            f'<p class="m-verdict">{esc(verdict_line(m, models, cfg, root))}</p></section>'
            f'<section><h2>Key numbers</h2><p class="sub">Green ticks mark lebowski. Lower is better on path and lateral error metrics.{prevt}</p>'
            f'<div class="kbars">{bars(m, models, cfg, mets, scales, False)}</div></section>'
            f'<section><h2>All scores</h2>{legend()}{secs}</section>{stills}'
            + (f'<section><h2>Limits</h2><ul class="limits">{limits}</ul></section>' if limits else "")
            + f'<section><h2>The model file</h2><dl class="ident">{dl}</dl>'
              f'<p><a href="../{esc(jrel)}">Results file</a> (<code>{esc(jrel)}</code>, schema 1).</p></section>')


# ---------------------------------------------------------------- method page

def method(models, cfg, root) -> str:
    prose = (SITE / "pages" / "method.html").read_text()
    prose = prose.replace("<!-- closed-loop example -->", f'<section id="closed-loop-example" class="cl-ex"><h2>A closed-loop example</h2>{hero_visual(models, cfg)}</section>')
    prose = prose.replace("<!-- harness check -->", f'<section id="harness" class="harness">{harness(models, cfg, root)}</section>')
    rows = []
    for m in reversed(models):
        md = m["model"]
        csvs = [f'<a href="{esc(s["per_unit_file"])}">{esc(SECTIONS.get(k, k))}</a>'
                for k, s in m["sections"].items() if s.get("per_unit_file")]
        rows.append(f'<tr><th scope="row">{name_html(m, "models/" + md["id"] + ".html")}</th>'
                    f'<td><a href="results/{esc(md["id"])}.json">{esc(md["id"])}.json</a></td>'
                    f'<td>{", ".join(csvs) or "none"}</td></tr>')
    table = ('<section id="downloads" class="prose"><h2>Downloads</h2><p>One results file per model, in the format described in '
             f'<a href="{esc(cfg["repo_url"])}/blob/main/eval/SCHEMA.md">SCHEMA.md</a>, and one CSV per public section with a row per segment. '
             'G55 sections publish scores only.</p>'
             '<div class="tw"><table><thead><tr><th scope="col">Model</th><th scope="col">Results</th><th scope="col">Per-segment CSVs</th></tr></thead>'
             f'<tbody>{"".join(rows)}</tbody></table></div></section>')
    return prose + table


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=EVAL, help="directory with results/, per_unit/, stills/")
    ap.add_argument("--out", type=Path, default=SITE / "_site")
    ap.add_argument("--config", type=Path, default=SITE / "config.json")
    a = ap.parse_args()
    cfg = json.loads(a.config.read_text())
    cfg_ref[0] = cfg["reference_model"]
    CFG.update(cfg)
    root, out = a.root.resolve(), a.out.resolve()
    models = load(root)
    MODELS[:] = models
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
            page(cfg, f"{plain_name(m)} – {tag}", f"{plain_name(m)}: {status_text(m)}. {verdict_line(m, models, cfg, root)}",
                 model_page(m, models, cfg, root, out), 1, "model", f"models/{mid}.html"))
    desc = ("Benchmarking comma.ai's openpilot driving models on a 2011 Mercedes G55 AMG "
            "and comma's public segments in open-loop replay and closed-loop simulation.")
    (out / "index.html").write_text(page(cfg, f"openpilot driving model evaluations – {cfg['site_name']}",
                                         desc, overview(models, cfg, root), 0, "leaderboard", ""))
    (out / "method.html").write_text(page(cfg, f"Method – {cfg['site_name']}", "Scoring methodology for open-loop replay, "
                                          "worldmodel-4B closed-loop rollouts, drive-clustered intervals, and CSV downloads.", method(models, cfg, root), 0, "method", "method.html"))
    print(f"{len(models)} models -> {out}")


if __name__ == "__main__":
    main()
