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

SECTIONS = {  # in page order: the G55 first, comma's public drives (the harness check) after
    "truck_closed_loop": "G55, closed loop",
    "truck_recovery": "G55, recovery tests",
    "truck_open_loop": "G55, open loop",
    "public_closed_loop": "comma's public drives, closed loop",
    "public_open_loop": "comma's public drives, open loop",
}
UNIT_WORD = {"segment": ("segment", "segments"), "window": ("window", "windows")}
BETTER_TEXT = {"lower": "lower is better", "higher": "higher is better",
               "near1": "closer to 1 is better; the delta is the change in distance from 1"}
MINUS = "−"
# The G55 numbers on every model's row. Lateral action rate is a diagnostic with no better direction:
# a lower rate can be a smaller request as well as a smoother one.
KEY_METRICS = [
    ("truck_closed_loop", "xtrack_2s", "G55 closed loop", "Median distance from the recorded path at 2 s, G55 steering model"),
    ("truck_open_loop", "lat_mae", "G55 lateral error", "Mean, request against the truck's lateral motion that followed"),
    ("truck_open_loop", "lat_rate_rms", "G55 lateral action rate", "Mean RMS rate of change of the lateral request"),
]
MORE_METRICS = [
    ("truck_recovery", "heading_xtrack_2s", "G55 at 2 s after a 2° heading error", "Median over windows"),
    ("truck_recovery", "offcentre_xtrack_2s", "G55 at 2 s after a 0.5 m offset", "Median over windows"),
    ("public_closed_loop", "xtrack_2s", "Distance from path at 2 s, comma's segments", "Closed loop, median over segments, comma's ideal car"),
    ("public_open_loop", "lat_mae", "Lateral error, comma's segments", "Mean, request against the car's lateral motion that followed"),
]
# Labels the site uses in place of a results file's own, for files written before a label changed.
LABEL = {"lat_best_lag": "Lead of the lateral request over the car's motion",
         "lat_mae_bl": "Lateral error at the model's own best lead"}
# Metrics with no better direction: a separated difference is marked lower or higher, not better or worse.
NEUTRAL = {"lat_rate_rms", "lon_rate_rms", "lat_best_lag"}
# Terms for the per-model verdict line.
VERDICT_TERM = {"xtrack_2s": "G55 closed-loop median", "lat_mae": "G55 open-loop lateral error",
                "lat_rate_rms": "G55 lateral action rate"}


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
            s += f', on master from {md["on_master"]}'
        return s
    if md.get("on_master"):
        s = f'Merged to openpilot master {md["on_master"]}'
        if src["kind"] == "openpilot-master" and src["date"] != md["on_master"]:
            s += f', again {src["date"]}'
        return s
    return "Hugging Face upload only, not shipped"


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
    """Replace {op args... fmt} with a number from the results. Ops:
      v S K M stat fmt         a model's value or median        d S K M B field fmt  a paired field
      pct S K M B              |delta| over B's value, in %      pctlo/pcthi S K B   range of pct over all models
      lo/hi S K M1,M2 stat fmt range of values across models     max S K M fmt       largest per-unit value (CSV)
      unitlo/unithi S K U fmt  range of unit U's value (CSV)      maxexcept S K U fmt largest value on any other unit
      count S                  models with section S scored
      num S K M1,M2            how many models M1,M2 names
    M1,M2 can be "all" (every model with the metric), "others" (all but the reference), "after" or
    "before" (first public after or before the reference).
    """
    idx = by_id(models)
    ref = cfg_ref[0]

    def ids(spec, s, k):
        if spec in ("all", "others", "after", "before"):
            rd = first_public(idx[ref])
            keep = {"all": lambda m: True, "others": lambda m: m["model"]["id"] != ref,
                    "after": lambda m: first_public(m) > rd, "before": lambda m: first_public(m) < rd}[spec]
            return [m["model"]["id"] for m in models if find_metric(m, s, k)[0] and keep(m)]
        return spec.split(",")

    def units(s, k):  # {model id: {unit: value}} from every model's CSV for section s
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

def page(cfg: dict, title: str, desc: str, body: str, depth: int, current: str, path: str) -> str:
    up = "../" * depth
    umami = ""
    if cfg.get("umami_website_id"):
        dom = f' data-domains="{esc(cfg["umami_domains"])}"' if cfg.get("umami_domains") else ""
        umami = (f'<script defer src="{esc(cfg["umami_src"])}" '
                 f'data-website-id="{esc(cfg["umami_website_id"])}"{dom}></script>')
    nav = []
    for href, label, key in (("index.html#findings", "Findings", "findings"), ("index.html#log", "Log", "log"),
                             ("index.html#models", "Models", "models"), ("index.html#how", "How it works", "how"),
                             ("method.html", "Method", "method")):
        cur = ' aria-current="page"' if key == current else ""
        nav.append(f'<a href="{up}{href}"{cur}>{label}</a>')
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
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Overpass+Mono:wght@400;600&family=Overpass:ital,wght@0,400;0,600;0,800;1,400&display=swap">
<link rel="stylesheet" href="{up}style.css">
<link rel="icon" href="{up}favicon.svg" type="image/svg+xml">
{umami}
</head>
<body>
<header class="top"><div class="wrap top-in">
<a class="word" href="{up}index.html">{esc(cfg['site_name'])}</a>
<nav aria-label="Site">{''.join(nav)}<button class="theme" type="button" aria-label="Switch light or dark theme"><span aria-hidden="true"></span></button></nav>
</div></header>
<main class="wrap">
{body}
</main>
<footer class="wrap foot">
<p>Run by David Rose. Results come from one truck and a few dozen public segments; each number carries its sample size.
<a href="{esc(cfg['repo_url'])}/tree/main/eval">Code and schema</a>.</p>
</footer>
<div id="tip" class="tip" role="status" hidden></div>
<script src="{up}site.js" defer></script>
</body>
</html>
"""


# ---------------------------------------------------------------- per-model verdict and bars

def verdict_line(m: dict, models: list[dict], cfg: dict) -> str:
    """Where the model stands among the models scored on the G55: the lowest or highest of each key
    metric, and whether it ran in closed loop. config.json "verdicts" {model id: text} replaces it."""
    over = cfg.get("verdicts", {}).get(m["model"]["id"])
    if over:
        return fill(over, models, EVAL)
    out = []
    if m["model"]["id"] == cfg["reference_model"]:
        out.append("Reference model; the G55 drives with it.")
    bits = []
    for sec, key, label, _ in KEY_METRICS:
        stat = "value" if sec.endswith("open_loop") else "median"
        me = find_metric(m, sec, key)[0]
        vals = [x[stat] for x in (find_metric(o, sec, key)[0] for o in models) if x]
        if me and len(vals) > 2:
            word = "lowest" if me[stat] <= min(vals) else "highest" if me[stat] >= max(vals) else None
            if word:
                bits.append(f"{word} {VERDICT_TERM[key]} of the {len(vals)} scored")
    if bits:
        t = "; ".join(bits)
        out.append(t[0].upper() + t[1:] + ".")
    if (m["sections"].get("truck_closed_loop") or {}).get("status") != "ok":
        out.append("Open loop only on the G55.")
    if m["model"]["id"] == "96244b1f_12800":
        out.append("Navigation inputs fed zeros.")
    return " ".join(out)


def bar_scale(models, section, key) -> float:
    vals = [find_metric(m, section, key)[0] for m in models]
    vals = [x for x in vals if x]
    stat = "median" if not section.endswith("open_loop") else "value"
    v = [x.get(stat, x["value"]) for x in vals]
    # Up to 3 times the median model, so one outlier does not shrink every other bar; longer bars are cut.
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
        if rx and m is not ref:
            tick = f'<span class="b-ref" style="left:{min(rx[stat] / mx * 100, 100):.1f}%" title="lebowski"></span>'
        u, d, k = x["unit"], 2, 1
        if "xtrack" in key and u == "m":  # distances in cm, as in the ranking
            u, d, k = "cm", 1, 100
        tip = (f'{plain_name(m)}, {label.lower()}: {num(x[stat] * k, d)} {u} ({"median" if stat == "median" else "mean"} over {x.get("n", s["n"])} '
               f'{UNIT_WORD.get(s["unit_of_evidence"], ("unit", "units"))[1]})'
               + (f'; lebowski {num(rx[stat] * k, d)} {u}' if rx and m is not ref else ""))
        out.append(f'<div class="b-row">{head}<span class="b-track" tabindex="0" data-tip="{esc(tip)}" aria-label="{esc(tip)}">'
                   f'<span class="b-bar{" b-refbar" if m is ref else ""}{" b-cut" if w >= 100 else ""}" style="width:{w:.1f}%"></span>{tick}</span>'
                   f'<span class="b-v">{num(x[stat] * k, d)}<span class="b-u"> {esc(u)}</span></span></div>')
    return "".join(out)


def model_list(models: list[dict], cfg: dict) -> str:
    scales = {(s, k): bar_scale(models, s, k) for s, k, *_ in KEY_METRICS}
    rows = []
    for m in reversed(models):
        mid = m["model"]["id"]
        rows.append(
            f'<li class="mrow"><div class="m-id"><h3>{name_html(m, f"models/{mid}.html")}</h3>'
            f'<p class="m-st"><span class="m-date">{esc(first_public(m))}</span> {esc(status_text(m))}</p></div>'
            f'<p class="m-v">{esc(verdict_line(m, models, cfg))}</p>'
            f'<div class="m-bars">{bars(m, models, cfg, KEY_METRICS, scales, True)}</div></li>')
    head = ('<div class="mlist-head" aria-hidden="true"><span>Model</span><span>Summary</span>'
            '<span>G55; tick = lebowski</span></div>')
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

# the road camera at the world model's 256x128 (focal length and principal point, px) and comma's camera
# height (m): the geometry the earlier hero video drew its paths with
CAMERA = {"f": 455.0, "cx": 128.0, "cy": 23.8, "h": 1.22}


def hero_visual(models, cfg) -> str:
    """One public segment in closed loop: a player that draws the world model's frames at their native
    256x128 (static/hero-*.avif, one sprite per stream, from a re-run that kept the frames) to a canvas and
    the paths over them as vectors, beside a bird's-eye map of the same paths from hero.json. The page
    script (site.js) projects the paths with CAMERA and moves the map's dots with the player."""
    f = SITE / "hero.json"
    if not f.exists():
        return ""
    h = json.loads(f.read_text())
    idx = by_id(models)
    order = [mid for mid in h["paths"] if mid in idx]
    cls = {mid: highlight_class(idx[mid], cfg) for mid in order}
    # bird's-eye: forward up, left to the left; metres, sideways stretched (both axes ticked)
    ref = h["ref"]
    pts = [p for mid in order for p in h["paths"][mid]["xy"]] + ref
    fx = max(p[0] for p in pts)
    ylo, yhi = min(p[1] for p in pts) - 1, max(p[1] for p in pts) + 1
    W, Hh, L, R, T, B = 320, 430, 34, 112, 14, 30
    X = lambda y: L + (yhi - y) / (yhi - ylo) * (W - L - R)  # noqa: E731  left (positive y) on the left
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
    for ex, ey, mid in ends:  # direct labels at the path ends, nudged apart vertically
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


# ---------------------------------------------------------------- the front-page ranking

G55 = ("truck_closed_loop", "xtrack_2s")


def cm(v: float) -> str:
    return num(v * 100, 1)


def model_class(m, cfg) -> str:
    """The colour a model wears on the front page, by what it is: the reference (lebowski), a model first
    public before it, one shipped or on openpilot master, or a Hugging Face upload only."""
    ref = cfg["reference_model"]
    if m["model"]["id"] == ref:
        return "ref"
    if first_public(m) < first_public(by_id(MODELS)[ref]):
        return "old"
    return "ship" if shipped(m) else "hf"


def highlight_class(m, cfg) -> str:
    """The world-model player's colours: the models named in config.json "hero", the rest grey."""
    return {h[0]: h[1] for h in cfg.get("hero", {}).get("highlight", [])}.get(m["model"]["id"], "other")


def g55_ranked(root) -> dict | None:
    """The G55 closed-loop ranking once it has intervals (10 or more drives), else None."""
    t = ranking.truck_scores(root)
    if t and any(s["ci"] for s in t["models"].values()):
        return t["models"]
    return None


def ol_counts(models) -> str:
    """'N windows from D drives.' for the G55 open loop, the most any model was scored on."""
    secs = [s for s in ((m["sections"].get("truck_open_loop") or {}) for m in models) if s.get("status") == "ok"]
    return f'{max((s["n"] for s in secs), default=0)} windows from {max((s["clusters"] for s in secs), default=0)} drives.'


def g55_counts(models) -> tuple[int, int]:
    """Windows and drives of the G55 closed loop, the largest any model was scored on."""
    secs = [m["sections"].get(G55[0]) or {} for m in models]
    ok = [s for s in secs if s.get("status") == "ok"]
    return (max((s["n"] for s in ok), default=0), max((s["clusters"] for s in ok), default=0))


def dot_track(s, c, top, unit_word, label) -> str:
    """A model's median (dot) on its interval, on a 0..top cm track."""
    pos = lambda v: f"{v * 100 / top * 100:.2f}%"  # noqa: E731  metres -> % of the track
    grid = "".join(f'<i style="left:{t / top * 100:.2f}%"></i>' for t in range(0, top + 1, tick_step(top)))
    ci = s["ci"]
    tip = (f'{label}: median {cm(s["median"])} cm from the recorded path at 2 s, 95% interval '
           + (f'{cm(ci[0])} to {cm(ci[1])} cm' if ci else "none below 10 drives") + f', {s["n"]} {unit_word} from {s["clusters"]} drives')
    bar = (f'<span class="rk-ci ci-{c}" style="left:{pos(ci[0])};width:calc({pos(ci[1])} - {pos(ci[0])})"></span>' if ci else "")
    return (f'<div class="rk-track" tabindex="0" data-tip="{esc(tip)}" aria-label="{esc(tip)}"><span class="rk-grid">{grid}</span>'
            f'{bar}<span class="rk-dot dot-{c}" style="left:{pos(s["median"])}"></span>'
            + (f'<span class="rk-v" style="left:{pos(ci[1] if ci else s["median"])}">' if (ci[1] if ci else s["median"]) * 100 / top < 0.85
               else f'<span class="rk-v rk-vl" style="right:calc(100% - {pos(ci[0] if ci else s["median"])})">')
            + f'{cm(s["median"])} cm</span></div>')


def top_cm(r: dict) -> int:
    """cm, past the widest interval: a multiple of 2 up to 16 cm, of 5 beyond (see tick_step)."""
    hi = max([s["ci"][1] for s in r.values() if s["ci"]] + [s["median"] for s in r.values()] + [0.01]) * 100
    top = math.ceil(hi / 2 + 0.25) * 2
    return top if top <= 16 else math.ceil(hi / 5 + 0.25) * 5


def tick_step(top: int) -> int:
    return 2 if top <= 16 else 5


MEDAL = {1: "🥇", 2: "🥈", 3: "🥉"}


def g55_table(models, cfg, root) -> str:
    """The front page's leaderboard: every model scored on the G55 in closed loop, best first by the
    closed-loop median at 2 s (a bar from zero, lebowski's value as a line through every row), medals for
    the first three. Under 10 drives the order is by median alone; from 10 drives, by rank (shared ranks
    where the paired interval does not exclude zero). Then the recovery medians and the open-loop lateral
    error as shaded cells, stronger shading for better. A toggle re-sorts by first public date."""
    ref = cfg["reference_model"]
    t = (ranking.truck_scores(root) or {"models": {}})["models"]
    g = g55_ranked(root)
    if g:
        t = g
    top = top_cm(t) if t else 16
    shown = sorted((m for m in models if m["model"]["id"] in t),
                   key=lambda m: (t[m["model"]["id"]]["rank"] if g else 0, t[m["model"]["id"]]["median"]))
    place = {m["model"]["id"]: (t[m["model"]["id"]]["rank"] if g else i + 1) for i, m in enumerate(shown)}
    newest = max(first_public(m) for m in shown) if shown else ""
    cols = [("truck_recovery", "heading_xtrack_2s", "median", 100, 1),
            ("truck_recovery", "offcentre_xtrack_2s", "median", 100, 1),
            ("truck_open_loop", "lat_mae", "value", 1, 2)]
    span = {}
    for c in cols:
        vs = [x[c[2]] for x in (find_metric(m, c[0], c[1])[0] for m in shown) if x]
        span[c] = (min(vs), max(vs)) if vs else (0, 0)

    def cell(m, c, label):
        x = find_metric(m, c[0], c[1])[0]
        if not x:
            return f'<span class="gt-n gt-c" data-l="{label}">–</span>'
        lo, hi = span[c]
        h = (hi - x[c[2]]) / (hi - lo) if hi > lo else 0
        best = " gt-best" if x[c[2]] == lo else ""
        return f'<span class="gt-n gt-c{best}" data-l="{label}" style="--h:{h:.2f}">{num(x[c[2]] * c[3], c[4])}</span>'

    rx = t.get(ref)
    # Positions as a fraction of the scale (--p); the CSS keeps room after the scale for the value label.
    refline = (f'<i class="gt-refl" style="--p:{rx["median"] * 100 / top:.4f}" aria-hidden="true"></i>' if rx else "")
    rows = []
    for m in shown:
        mid = m["model"]["id"]
        s_ = t[mid]
        pl = place[mid]
        med = MEDAL.get(pl, "")
        tier = f" gt-m{pl}" if med else ""
        tags = ('<span class="gt-tag gt-new">newest</span>' if first_public(m) == newest else "") + \
               ('<span class="gt-tag">reference</span>' if mid == ref else "")
        w = s_["median"] * 100 / top
        tip = f'{plain_name(m)}: median {cm(s_["median"])} cm from the recorded path at 2 s, {s_["n"]} windows from {s_["clusters"]} drives'
        ci = (f'<span class="gt-ci" style="--p:{s_["ci"][0] * 100 / top:.4f};--q:{(s_["ci"][1] - s_["ci"][0]) * 100 / top:.4f}"></span>'
              if s_["ci"] else "")
        rows.append(
            f'<div class="gt-row{tier}{" gt-ref" if mid == ref else ""}" data-place="{pl}" data-date="{esc(first_public(m))}">'
            f'<span class="gt-rk" aria-label="place {pl}">{med or pl}</span>'
            f'<div class="gt-name"><span class="gt-nm">{name_html(m, "models/" + mid + ".html")}{tags}</span>'
            f'<span class="gt-d">{esc(first_public(m))}</span></div>'
            f'<div class="gt-bar" tabindex="0" data-tip="{esc(tip)}" aria-label="{esc(tip)}">{refline}'
            f'<span class="gt-fill gt-f-{model_class(m, cfg)}" style="--p:{w:.4f}"></span>{ci}'
            f'<span class="gt-v" style="--p:{w:.4f}">{cm(s_["median"])} cm</span></div>'
            f'{cell(m, cols[0], "2° heading error, cm")}{cell(m, cols[1], "0.5 m offset, cm")}{cell(m, cols[2], "open loop, m/s²")}</div>')
    ticks = "".join(f'<span class="rk-tk gt-tk" style="--p:{x / top:.4f}">{x}</span>' for x in range(0, top + 1, tick_step(top)))
    head = ('<div class="gt-row gt-head" aria-hidden="true"><span></span><span>Model</span>'
            f'<div class="rk-axis"><span class="rk-al">Closed loop: cm from the recorded path at 2 s</span>{ticks}</div>'
            '<span class="gt-n">2° heading<br>error, cm</span><span class="gt-n">0.5 m<br>offset, cm</span>'
            '<span class="gt-n">Open loop,<br>m/s²</span></div>')
    nw, nd = g55_counts(models)
    order = ("Ranked with 95% intervals; rows that share a rank are not separated." if g else
             f"Ordered by median. With {nd} drives the differences are not yet separable; ranks with intervals start at 10 drives.")
    return f"""<figure class="gt" aria-labelledby="gt-t">
<figcaption class="gt-cap"><p id="gt-t" class="fig-t">Leaderboard: the G55 in closed loop</p>
<div class="gt-sort" role="group" aria-label="Sort"><button type="button" aria-pressed="true" data-sort="place">Best first</button><button type="button" aria-pressed="false" data-sort="date">Newest first</button></div>
<p class="gt-ph">Bar: closed loop, cm from the recorded path at 2 s, 0 to {top} cm. Lower is better.</p></figcaption>
{head}<div class="gt-body">{"".join(rows)}</div>
<p class="gt-note">{order} Closed loop and recovery: {nw} windows from {nd} drives, each model driving a model of the G55's steering and powertrain in <code>worldmodel-4B</code>; the dashed line is lebowski. Open loop: lateral request against what the truck did. Lower is better in every column; stronger shading is better within a column. <a href="method.html">Method</a>, including why 2 s.</p>
</figure>"""


def public_chart(models, cfg, root) -> str:
    """comma's public segments in closed loop: median distance at 2 s, interval, rank. The harness check."""
    r = ranking.scores(root)["models"]
    if not r:
        return ""
    top = top_cm(r)
    ranked = sorted((m for m in models if m["model"]["id"] in r), key=first_public, reverse=True)
    ranked.sort(key=lambda m: r[m["model"]["id"]]["rank"])
    groups = []
    for rank in sorted({r[m["model"]["id"]]["rank"] for m in ranked}):
        ms = [m for m in ranked if r[m["model"]["id"]]["rank"] == rank]
        rows = "".join(f'<div class="rk-row"><div class="rk-name"><span class="rk-sw sw-{model_class(m, cfg)}" aria-hidden="true"></span>'
                       f'<span>{name_html(m, "models/" + m["model"]["id"] + ".html")}</span></div>'
                       f'{dot_track(r[m["model"]["id"]], model_class(m, cfg), top, "segments", plain_name(m))}</div>' for m in ms)
        groups.append(f'<div class="rk-group"><div class="rk-rank">{rank}<span>{"shared" if len(ms) > 1 else ""}</span></div><div class="rk-rows">{rows}</div></div>')
    any_s = next(iter(r.values()))
    ticks = "".join(f'<span class="rk-tk" style="left:{t / top * 100:.2f}%">{t}</span>' for t in range(0, top + 1, tick_step(top)))
    return f"""<figure class="rk rk-pub" aria-labelledby="rkp-t">
<figcaption class="rk-cap"><p id="rkp-t" class="fig-t">Closed loop on comma's public segments: median distance from the recorded path at 2 s</p>
<p>{any_s["n"]} segments from {any_s["clusters"]} drives, comma's ideal car in <code>worldmodel-4B</code>; 95% interval resampling drives. Rank as in the G55 chart.</p></figcaption>
<div class="rk-head" aria-hidden="true"><span></span><div class="rk-axis"><span class="rk-al">cm from the recorded path at 2 s, comma's segments</span>{ticks}</div></div>
{"".join(groups)}
</figure>"""


# ---------------------------------------------------------------- the living parts: status, over time, log

import datetime as _dt


def scored_at(m) -> str:
    return m["scored"]["date"]


def shipped(m) -> bool:
    """On openpilot master or in a release, as against a Hugging Face upload only."""
    return bool(m["model"].get("on_master")) or m["model"]["source"]["kind"] in ("release", "openpilot-master")


def status_strip(models, cfg, root) -> str:
    latest = max(models, key=lambda m: (scored_at(m), m["model"]["id"]))
    ng = sum(1 for m in models if (m["sections"].get(G55[0]) or {}).get("status") == "ok")
    when = latest["scored"]["date"][:10]
    return (f'<p class="meta">{len(models)} models, {ng} of them in G55 closed loop; last scored {esc(when)}; '
            f'{esc(cfg.get("watch", {}).get("text", ""))}</p>')


def _date(s: str) -> _dt.date:
    return _dt.date.fromisoformat(s[:10])


TRENDS_G55 = [  # (title, unit, how to read one model): "g55rank" or (section, metric[, "cm" = median in cm])
    ("Closed loop, cm at 2 s", "cm", "g55rank"),
    ("Open-loop error, m/s²", "m/s²", ("truck_open_loop", "lat_mae")),
    ("2° heading error, cm at 2 s", "cm", ("truck_recovery", "heading_xtrack_2s", "cm")),
    ("0.5 m offset, cm at 2 s", "cm", ("truck_recovery", "offcentre_xtrack_2s", "cm")),
]
TRENDS_PUBLIC = [  # the public closed loop is the harness check's chart above these
    ("Lateral action rate, open loop, comma's segments (m/s³)", "m/s³", ("public_open_loop", "lat_rate_rms")),
    ("Lead of the lateral request over the car's motion, open loop, comma's segments (s)", "s", ("public_open_loop", "lat_best_lag")),
]


def is_wins(kind) -> bool:
    return isinstance(kind, tuple) and kind[0] == "wins"


def trend_values(kind, models, ref, r):
    """{model id: (value, lo, hi, n, unit word)}; the reference line value or None."""
    out, line = {}, None
    for m in models:
        mid = m["model"]["id"]
        if kind in ("rank", "g55rank"):
            s = r.get(mid)
            if s:
                v = (s["median"] * 100, s["ci"][0] * 100 if s["ci"] else None, s["ci"][1] * 100 if s["ci"] else None,
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
            if x and kind[2:] == ("cm",):
                v = (x["median"] * 100, None, None, f'{x["n"]} windows from {x["clusters"]} drives')
                if mid == ref:
                    line = v
                else:
                    out[mid] = v
            elif x:
                v = (x["value"], x["ci"][0] if x["ci"] else None, x["ci"][1] if x["ci"] else None,
                     f'{x["n"]} {sec.get("unit_of_evidence", "segment")}s from {x["clusters"]} drives')
                if mid == ref:
                    line = v
                else:
                    out[mid] = v
    return out, line


def trend_panel(title, unit, kind, models, cfg, r, W=360, H=190) -> str:
    ref = cfg["reference_model"]
    idx = by_id(models)
    vals, line = trend_values(kind, models, ref, r)
    # From the oldest model with a value here (or the reference): older models never run in this
    # section (comma's historical models have open-loop scores only) neither stretch the axis nor count as pending.
    have = [first_public(m) for m in models if m["model"]["id"] in vals or (line and m["model"]["id"] == ref)]
    since = min(have) if have else ""
    others = [m for m in models if m["model"]["id"] != ref and first_public(m) >= since]
    dates = [_date(first_public(m)) for m in others] or [_date(first_public(idx[ref]))]
    d0, d1 = min(dates) - _dt.timedelta(days=6), max(dates) + _dt.timedelta(days=6)
    L, Rr, T, B = 44, 14, 14, 34
    span = (d1 - d0).days
    X = lambda d: L + (d - d0).days / span * (W - L - Rr)  # noqa: E731
    ys = [v[0] for v in vals.values()] + [v[i] for v in vals.values() for i in (1, 2) if v[i] is not None]
    if line:
        ys.append(line[0])
    if is_wins(kind):
        n = max([int(v[3].split()[0]) for v in vals.values()] + [int(line[3].split()[0]) if line else 18])
        y0, y1 = 0.0, float(n)
    else:
        lo, hi = (min(ys), max(ys)) if ys else (0.0, 1.0)
        pad = (hi - lo) * 0.15 or 0.1
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
        if span > 200:   # years of models: half-yearly ticks
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
        lab = f"{line[0]:g} of {int(line[0] * 2)}" if is_wins(kind) else "lebowski"
        better = "" if is_wins(kind) else f'<rect class="better" x="{L}" y="{Y(line[0]):.1f}" width="{W - Rr - L}" height="{H - B - Y(line[0]):.1f}"/>'
        refl = (better + f'<line class="refl" x1="{L}" x2="{W - Rr}" y1="{Y(line[0]):.1f}" y2="{Y(line[0]):.1f}"/>'
                f'<text class="refl-t" x="{L + 4}" y="{Y(line[0]) - 5:.1f}">{lab}</text>')
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
        fill = "hollow" if c == "hf" else f"fl-{c}"
        marks.append(f'<circle class="tp st-{c} {fill}" cx="{cx:.1f}" cy="{cy:.1f}" r="5" tabindex="0" data-tip="{esc(tip)}" aria-label="{esc(tip)}"/>')
    pend = [m for m in others if m["model"]["id"] not in vals]
    for m in pend:
        cx = X(_date(first_public(m)))
        tip = f"{plain_name(m)}, first public {first_public(m)}: not scored here yet"
        marks.append(f'<path class="pend" d="M{cx - 3:.1f},{H - B - 3:.1f} l6,6 m0,-6 l-6,6" tabindex="0" data-tip="{esc(tip)}" aria-label="{esc(tip)}"/>')
    latest = ordered[-1] if ordered else None
    if latest:
        v, lo, hi, nw = vals[latest["model"]["id"]]
        nv = f"{v:.0f} of {nw.split()[0]} windows" if is_wins(kind) else f"{num(v, 1 if unit == 'cm' else 2)} {unit}"
        cap = f'Newest: {esc(short_name(latest))}, {esc(nv)}'
        if line and not is_wins(kind):
            cap += f'; lebowski {num(line[0], 1 if unit == "cm" else 2)}'
        cap += f'. {esc(nw)}.'
        if pend:
            cap += f" {len(pend)} not scored yet (×)."
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


def trend_legend(cfg, bars: bool = True, pend: bool = True) -> str:
    dot = lambda c, hollow=False: (f'<svg width="12" height="12" aria-hidden="true"><circle cx="6" cy="6" r="4.5" '  # noqa: E731
                                   f'class="st-{c} {"hollow" if hollow else "fl-" + c}" stroke-width="2"/></svg>')
    return ('<p class="trend-key">'
            f'<span>{dot("ship")}shipped or on openpilot master</span>'
            f'<span>{dot("hf", True)}Hugging Face upload only</span>'
            f'<span>{dot("ref")}lebowski, the reference (July 2026)</span>'
            f'<span>{dot("old")}earlier models</span>'
            '<span><i class="key-better" aria-hidden="true"></i>better than lebowski</span>'
            + ('<span><svg width="12" height="12" aria-hidden="true"><path class="pend" d="M3,3 l6,6 m0,-6 l-6,6"/></svg>not scored yet</span>' if pend else '')
            + ('<span>bars: 95% interval</span>' if bars else '') + '</p>')


def over_time(models, cfg, root) -> str:
    """The G55 panels: closed-loop and recovery medians, then the open-loop lateral error."""
    r = (ranking.truck_scores(root) or {"models": {}})["models"]
    panels = "".join(trend_panel(t, u, k, models, cfg, r, W=290, H=165) for t, u, k in TRENDS_G55)
    key = trend_legend(cfg, "tw-ci" in panels, 'class="pend"' in panels)
    return f'{key}<div class="trends">{panels}</div>'


def harness_trends(models, cfg, root) -> str:
    r = ranking.scores(root)["models"]
    return '<div class="trends">' + "".join(trend_panel(t, u, k, models, cfg, r) for t, u, k in TRENDS_PUBLIC) + "</div>"


def harness(models, cfg, root, heading="h2") -> str:
    """comma's public drives: a check of the harness, rerunnable by anyone."""
    return f"""<{heading}>Harness check on comma's public drives</{heading}>
<p class="sub">Purpose: to check that the harness reproduces sensible behaviour on comma's own data, and to let others rerun it. The same suite runs on comma's public segments, open loop and closed loop with comma's ideal car; per-segment results and code are published, and <code>python -m geval.reproduce</code> recomputes every interval from them.</p>
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
    """(date, kind, text, link, link label), newest first: suite changes from CHANGELOG.md, then each
    model's releases from its results file, then one entry per day and suite of scoring runs, whose
    link is a list of (model name, page)."""
    ev = changelog(root)
    runs = {}
    for m in models:
        md, src, mid = m["model"], m["model"]["source"], m["model"]["id"]
        page = f"models/{mid}.html"
        nm = plain_name(m)
        seen = set()

        def add(date, text, kind="model"):
            if (date, text) not in seen:
                seen.add((date, text))
                ev.append((date, kind, f"{nm} {text}", page, None))
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
        runs.setdefault((scored_at(m)[:10], m["scored"]["suite"]), []).append((first_public(m), nm, page))
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

def overview(models, cfg, root) -> str:
    def finding(f):
        return (f'<li><span class="f-fig">{esc(fill(f["figure"], models, root))}</span>'
                f'<div class="f-b"><p>{esc(fill(f["line"], models, root))}</p>'
                f'<details><summary>Detail</summary><p>{esc(fill(f["text"], models, root))}</p></details></div></li>')
    findings = "".join(finding(f) for f in cfg.get("findings", []))
    return f"""<section class="hero">
<div class="hero-t">
<h1>openpilot driving model evaluations</h1>
<p class="lede">Every comma.ai driving model, scored on drives from a 2011 Mercedes G55, a car openpilot does not support.</p>
{status_strip(models, cfg, root)}
</div>
{g55_table(models, cfg, root)}
<div id="time" class="time"><p class="fig-t">Over time, by the date each model was first public</p><p class="meta">Closed loop and recovery: {g55_counts(models)[0]} windows from {g55_counts(models)[1]} drives. Open loop: {ol_counts(models)} Lower is better in all four.</p>{over_time(models, cfg, root)}</div>
</section>

<section id="findings">
<h2>Findings</h2>
<ul class="findings">{findings}</ul>
</section>

<section id="log" class="logsec">
<h2>Log</h2>
<p class="sub">Model releases and scoring runs from the results files; suite changes from <a href="{esc(cfg['repo_url'])}/blob/main/eval/CHANGELOG.md">CHANGELOG.md</a>. Newest first.</p>
{log_feed(models, root)}
</section>

<section id="models">
<h2>The models</h2>
<p class="sub">Newest first, by the date the weights were first public. Each links to its full scores.</p>
{model_list(models, cfg)}
</section>

<section id="how" class="prose">
<h2>How it works</h2>
<div class="how">
<div><h3>Open loop</h3><p>The model runs on recorded camera frames. Its lateral and longitudinal requests are compared with the car's measured motion that followed.</p></div>
<div><h3>Closed loop</h3><p>The model's requests move a simulated car, and comma's public world model, <code>worldmodel-4B</code>, generates the next camera frame from the new position, 7.2 s per episode. Scored as distance from the recorded path.</p></div>
<div><h3>The G55</h3><p>A 2011 Mercedes G55 (W463) with recirculating-ball steering and the camera 1.45 m above the road. openpilot does not support it, so its drives are not in comma's training data. Its closed loop uses a model of the G55's steering and powertrain in place of comma's ideal car.</p></div>
</div>
<p>comma publishes its own test reports in <a href="https://commaai.github.io/model_reports/">model_reports</a>; the newest there is Cinque v3, from 2026-09-18. Nearest equivalents:</p>
<div class="tw"><table class="vocab"><thead><tr><th scope="col">Here</th><th scope="col">Nearest test in comma's model_reports</th></tr></thead><tbody>
<tr><th scope="row">Lateral action rate, open loop</th><td>jitter</td></tr>
<tr><th scope="row">Distance from path, closed loop, comma's ideal car</th><td>MLSim lateral (<code>analyse_lat_mlsim</code>), their world model as the simulator</td></tr>
<tr><th scope="row">G55 recovery: 0.5 m lateral offset or 2° heading error</th><td>no direct match; closest is hugging, recovery under a lateral bias</td></tr>
<tr><th scope="row">Braking lead car (not built yet)</th><td>unintended lead following</td></tr>
</tbody></table></div>
<p><a href="method.html">The full method</a>, with a closed-loop example from <code>worldmodel-4B</code>.</p>
</section>

<section id="harness" class="harness">
{harness(models, cfg, root)}
</section>

<section id="research" class="research">
<h2>For researchers</h2>
<ul class="plain">
<li>Every number is a mean over units: a 60 s segment of a public drive, or a window of a G55 drive. Units are grouped by the drive they come from, and the 95% intervals resample whole drives. Below 10 drives there is no interval, so the G55 sections report means and win counts only.</li>
<li>A paired difference compares two models on the units both were scored on; the count beside it is the units where the model did better.</li>
<li>Per-segment CSVs, one results file per model (<a href="{esc(cfg['repo_url'])}/blob/main/eval/SCHEMA.md">schema</a>) and the code are public; <code>python -m geval.reproduce</code> recomputes every public interval from the CSVs. <a href="method.html#downloads">Downloads</a></li>
<li>The G55 drives stay private; their scores are published.</li>
</ul>
{legend()}
{summary_table(models, cfg)}
{unscored()}
</section>"""


# ---------------------------------------------------------------- model page

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
            f'<tbody>{"".join(rows)}</tbody></table></div></section>')


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
        prevt = f' Compared with lebowski and with {name_html(po, prev + ".html") if po else esc(prev)}, the model first public before it.'
    return (f'<section class="mhead"><p class="crumb"><a href="../index.html#models">All models</a></p>'
            f'<h1>{name_html(m)}</h1><p class="m-st"><span class="m-date">{esc(first_public(m))}</span> {esc(status_text(m))}</p>'
            f'<p class="m-verdict">{esc(verdict_line(m, models, cfg))}</p></section>'
            f'<section><h2>Key numbers</h2><p class="sub">The tick is lebowski. Lateral action rate has no better direction; on the others lower is better.{prevt}</p>'
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
            page(cfg, f"{plain_name(m)} – {tag}", f"{plain_name(m)}: {status_text(m)}. {verdict_line(m, models, cfg)}",
                 model_page(m, models, cfg, root, out), 1, "model", f"models/{mid}.html"))
    desc = ("Every comma.ai openpilot driving model, including Hugging Face uploads, scored on comma's public drives "
            "and on drives from a 2011 Mercedes G55, in open and closed loop.")
    (out / "index.html").write_text(page(cfg, f"openpilot driving model evaluations – {cfg['site_name']}",
                                         desc, overview(models, cfg, root), 0, "overview", ""))
    (out / "method.html").write_text(page(cfg, f"Method – {cfg['site_name']}", "How each openpilot driving model is scored: "
                                          "open and closed loop, intervals, limits and downloads.", method(models, cfg, root), 0, "method", "method.html"))
    print(f"{len(models)} models -> {out}")


if __name__ == "__main__":
    main()
