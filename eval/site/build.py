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
from pathlib import Path

SITE = Path(__file__).resolve().parent
EVAL = SITE.parent

SECTIONS = {
    "public_open_loop": "comma's public drives, open loop",
    "public_closed_loop": "comma's public drives, closed loop",
    "truck_open_loop": "G55, open loop",
    "truck_closed_loop": "G55, closed loop",
    "truck_recovery": "G55, recovery tests",
}
UNIT_WORD = {"segment": ("segment", "segments"), "window": ("window", "windows")}
BETTER_TEXT = {"lower": "lower is better", "higher": "higher is better",
               "near1": "closer to 1 is better; the delta is the change in distance from 1"}
MINUS = "−"
# The three numbers every model has, in plain words (public open loop; lower is better on all three).
KEY_METRICS = [
    ("public_open_loop", "lat_rate_rms", "Lateral action rate", "RMS rate of change of the lateral request"),
    ("public_open_loop", "lat_best_lag", "Lateral lag", "Shift at which the request best matches the car's turning"),
    ("public_open_loop", "lat_mae", "Lateral error", "Request against the car's lateral motion that followed"),
]
MORE_METRICS = [
    ("public_closed_loop", "xtrack_4s", "Distance from path at 4 s, closed loop", "Median over segments, comma's ideal car"),
    ("truck_closed_loop", "xtrack_4s", "G55 distance from path at 4 s", "Median over windows, G55 steering model"),
    ("truck_recovery", "offcentre_xtrack_4s", "G55 at 4 s after a 0.5 m offset", "Median over windows"),
]
# Plain names for the per-model verdict line: (better, worse).
TERMS = {"lat_rate_rms": "lateral action rate", "lat_best_lag": "lateral lag", "lat_mae": "lateral error",
         "lon_mae": "longitudinal error"}


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
    """better | worse | tie | none (no interval). For near1 the delta is already a change in
    distance from 1, so negative is better, as for lower."""
    ci = paired.get("ci")
    if not ci:
        return "none"
    if not (ci[0] > 0 or ci[1] < 0):
        return "tie"
    good = paired["delta"] > 0 if metric["better"] == "higher" else paired["delta"] < 0
    return "better" if good else "worse"


MARK_TEXT = {
    "better": "better, 95% interval excludes zero",
    "worse": "worse, 95% interval excludes zero",
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
    """
    idx = by_id(models)

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
            vals = [metric(a[1], a[2], mid)[a[4]] for mid in a[3].split(",")]
            return fmt(min(vals) if o == "lo" else max(vals), a[5])
        if o == "max":
            s = idx[a[3]]["sections"][a[1]]
            with open(root / s["per_unit_file"]) as f:
                vals = [float(r[a[2]]) for r in csv.DictReader(f) if r[a[2]] != ""]
            return fmt(max(vals), a[4])
        raise SystemExit(f"unknown finding op {o!r}")

    return re.sub(r"\{([a-z]+ [^{}]+)\}", op, text)


# ---------------------------------------------------------------- page shell

def page(cfg: dict, title: str, desc: str, body: str, depth: int, current: str, path: str) -> str:
    up = "../" * depth
    umami = ""
    if cfg.get("umami_website_id"):
        dom = f' data-domains="{esc(cfg["umami_domains"])}"' if cfg.get("umami_domains") else ""
        umami = (f'<script defer src="{esc(cfg["umami_src"])}" '
                 f'data-website-id="{esc(cfg["umami_website_id"])}"{dom}></script>')
    nav = []
    for href, label, key in (("index.html#models", "Models", "models"), ("index.html#how", "How it works", "how"),
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


# ---------------------------------------------------------------- the hero chart

def hero_chart(models: list[dict], cfg: dict) -> str:
    """Lateral lag (x) against lateral action rate (y), one dot per model, public open loop.
    Lower-left is better on both. The reference and the models config.json names are labelled."""
    ref = cfg["reference_model"]
    hl = cfg.get("hero", {}).get("highlight", [])  # [[model id, css class, label, "left" or "right"], ...]
    pts = []
    for m in models:
        x, sx = find_metric(m, "public_open_loop", "lat_best_lag")
        y, _ = find_metric(m, "public_open_loop", "lat_rate_rms")
        if x and y:
            pts.append((m, x, y, sx))
    if len(pts) < 2:
        return ""
    W, H, L, R, T, B = 560, 400, 58, 20, 18, 62
    xs, ys = [p[1]["value"] for p in pts], [p[2]["value"] for p in pts]
    x0, x1 = math.floor((min(xs) - 0.03) * 10) / 10, math.ceil((max(xs) + 0.12) * 10) / 10
    y0, y1 = math.floor((min(ys) - 0.03) * 10) / 10, math.ceil((max(ys) + 0.03) * 10) / 10
    px = lambda v: L + (v - x0) / (x1 - x0) * (W - L - R)  # noqa: E731
    py = lambda v: H - B - (v - y0) / (y1 - y0) * (H - T - B)  # noqa: E731
    g = []
    t = x0
    while t <= x1 + 1e-9:
        X = px(t)
        g.append(f'<line class="grid" x1="{X:.1f}" x2="{X:.1f}" y1="{T}" y2="{H - B}"/>'
                 f'<text class="tick" x="{X:.1f}" y="{H - B + 20}" text-anchor="middle">{t:.1f}</text>')
        t += 0.1
    t = y0
    while t <= y1 + 1e-9:
        Y = py(t)
        g.append(f'<line class="grid" x1="{L}" x2="{W - R}" y1="{Y:.1f}" y2="{Y:.1f}"/>'
                 f'<text class="tick" x="{L - 8}" y="{Y + 4:.1f}" text-anchor="end">{t:.1f}</text>')
        t += 0.1
    g.append(f'<text class="axis" x="{(L + W - R) / 2:.0f}" y="{H - 14}" text-anchor="middle">'
             'Lateral lag (s)</text>')
    g.append(f'<text class="axis" transform="translate(16 {(T + H - B) / 2:.0f}) rotate(-90)" text-anchor="middle">'
             'Lateral action rate, RMS (m/s³)</text>')
    g.append(f'<text class="better" x="{L + 10}" y="{H - B - 12}">Lower is better on both axes</text>')
    style = {h[0]: (h[1], h[2]) for h in hl}
    pos = {}
    dots, labels = [], []
    for m, x, y, s in sorted(pts, key=lambda p: p[0]["model"]["id"] in style):
        mid = m["model"]["id"]
        X, Y = px(x["value"]), py(y["value"])
        pos[mid] = (X, Y)
        cls = style.get(mid, ("other", None))[0]
        tip = (f'{plain_name(m)}: lateral lag {num(x["value"], 2)} s over {x["n"]} segments, '
               f'lateral action rate {num(y["value"], 2)} m/s³ over {y["n"]} segments')
        r = 7 if cls != "other" else 5.5
        dots.append(f'<circle class="dot d-{cls}" cx="{X:.1f}" cy="{Y:.1f}" r="{r}" tabindex="0" '
                    f'data-tip="{esc(tip)}" aria-label="{esc(tip)}"/>')
    for mid, cls, label, side in hl:
        if mid not in pos:
            continue
        X, Y = pos[mid]
        anchor, dx = ("end", -13) if side == "left" else ("start", 13)
        labels.append(f'<text class="lbl" x="{X + dx:.1f}" y="{Y + 5:.1f}" text-anchor="{anchor}">{esc(label)}</text>')
    arrow = ""
    a = cfg.get("hero", {}).get("arrow")
    if a and a[0] in pos and a[1] in pos:
        (ax, ay), (bx, by) = pos[a[0]], pos[a[1]]
        dx, dy = bx - ax, by - ay
        d = (dx * dx + dy * dy) ** 0.5 or 1
        sx, sy, ex, ey = ax + dx / d * 11, ay + dy / d * 11, bx - dx / d * 12, by - dy / d * 12
        arrow = (f'<path class="arrow" d="M{sx:.1f},{sy:.1f} L{ex:.1f},{ey:.1f}" marker-end="url(#ah)"/>')
    legend = "".join(f'<span><i class="key k-{h[1]}"></i>{esc(h[2])}</span>' for h in hl)
    legend += '<span><i class="key k-other"></i>Other comma models</span>'
    rows = "".join(f'<tr><th scope="row">{esc(plain_name(m))}</th><td class="n">{num(x["value"], 2)}</td>'
                   f'<td class="n">{num(y["value"], 2)}</td></tr>' for m, x, y, s in reversed(pts))
    n = max(s["n"] for *_, s in pts)
    return f"""<figure class="hero-fig">
<p class="fig-t">{esc(cfg.get("hero", {}).get("title", ""))}</p>
<div class="legend-row">{legend}</div>
<svg class="scatter" viewBox="0 0 {W} {H}" role="img" aria-label="Scatter of lateral lag against lateral action rate for each scored model">
<defs><marker id="ah" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" class="ah"/></marker></defs>
{''.join(g)}{arrow}{''.join(dots)}{''.join(labels)}
</svg>
<figcaption>One dot per model; means over comma's public segments, open loop ({n} with movement; lag excludes values at the 0 and 1.5 s search limits). Lateral lag: the time shift at which the model's lateral request best correlates with the car's measured lateral motion. Lateral action rate: RMS rate of change of that request.
<details><summary>Numbers behind the chart</summary><div class="tw"><table><thead><tr><th scope="col">Model</th><th class="n" scope="col">Lateral lag (s)</th><th class="n" scope="col">Lateral action rate (m/s³)</th></tr></thead><tbody>{rows}</tbody></table></div></details>
</figcaption>
</figure>"""


# ---------------------------------------------------------------- per-model verdict and bars

def verdict_line(m: dict, models: list[dict], cfg: dict) -> str:
    """What was measured and the number, from the public open-loop results: lateral action rate and
    longitudinal error against lebowski where the interval excludes zero, lateral error either way,
    then which metrics separate from the previous model, by direction. config.json "verdicts"
    {model id: text} replaces it."""
    over = cfg.get("verdicts", {}).get(m["model"]["id"])
    if over:
        return fill(over, models, EVAL)
    ref = cfg["reference_model"]
    idx = by_id(models)
    sec = "public_open_loop"
    if m["model"]["id"] == ref:
        out = "Reference model; the G55 drives with it."
        lags = [find_metric(o, sec, "lat_best_lag")[0] for o in models]
        rates = [find_metric(o, sec, "lat_rate_rms")[0] for o in models]
        me_l, me_r = find_metric(m, sec, "lat_best_lag")[0], find_metric(m, sec, "lat_rate_rms")[0]
        bits = []
        if me_r and all(x is None or x["value"] <= me_r["value"] for x in rates):
            bits.append("highest lateral action rate")
        if me_l and all(x is None or x["value"] >= me_l["value"] for x in lags):
            bits.append("lowest lateral lag")
        if bits:
            t = join_words(bits)
            out += " " + t[0].upper() + t[1:] + " of the models scored."
        return out

    def pct_vs_ref(key):
        x, _ = find_metric(m, sec, key)
        p = x and x["paired"].get(ref)
        if not p:
            return None, None, None
        base = find_metric(idx[ref], sec, key)[0]["value"]
        return verdict_of(x, p), 100 * p["delta"] / base, p["n"]

    parts, n = [], None
    for key, always in (("lat_rate_rms", False), ("lat_mae", True), ("lon_mae", False)):
        v, pc, nn = pct_vs_ref(key)
        if v is None or (not always and v not in ("better", "worse")):
            continue
        n = n or nn
        if v == "tie":
            parts.append(f"{TERMS[key]} not separated")
        else:
            parts.append(f"{TERMS[key]} {round(abs(pc))}% {'lower' if pc < 0 else 'higher'}")
    out = []
    if parts:
        t = join_words(parts)
        out.append(t[0].upper() + t[1:] + f" than lebowski ({n} segments).")
    prev = previous_of(m, ref)
    if prev and prev in idx:
        lower, higher = [], []
        for k, term in TERMS.items():
            x, _ = find_metric(m, sec, k)
            p = x and x["paired"].get(prev)
            if p and verdict_of(x, p) in ("better", "worse"):
                (lower if p["delta"] < 0 else higher).append(term)
        pn = short_name(idx[prev])
        bits = ([f"lower {join_words(lower)}"] if lower else []) + ([f"higher {join_words(higher)}"] if higher else [])
        out.append(f"Against {pn}: " + "; ".join(bits) + "." if bits else f"No metric separates from {pn}.")
    if m["model"]["id"] == "96244b1f_12800":
        out.append("Navigation inputs fed zeros.")
    return " ".join(out)


def bar_scale(models, section, key) -> float:
    vals = [find_metric(m, section, key)[0] for m in models]
    vals = [x for x in vals if x]
    stat = "median" if section != "public_open_loop" else "value"
    return max(x.get(stat, x["value"]) for x in vals) * 1.15 if vals else 1.0


def bars(m: dict, models: list[dict], cfg: dict, metrics, scales: dict, compact: bool) -> str:
    ref = by_id(models).get(cfg["reference_model"])
    out = []
    for section, key, label, sub in metrics:
        x, s = find_metric(m, section, key)
        stat = "median" if section != "public_open_loop" and x and x.get("median") is not None else "value"
        head = (f'<span class="b-l">{esc(label)}</span>' if compact else
                f'<span class="b-l">{esc(label)}<span class="b-s">{esc(sub)}</span></span>')
        if not x:
            st = "not run yet" if not s or s["status"] == "not_run" else s["status"]
            out.append(f'<div class="b-row b-off">{head}<span class="b-na">{esc(st)}</span></div>')
            continue
        mx = scales[(section, key)]
        w = x[stat] / mx * 100
        rx = find_metric(ref, section, key)[0] if ref else None
        tick = ""
        if rx and m is not ref:
            tick = f'<span class="b-ref" style="left:{rx[stat] / mx * 100:.1f}%" title="lebowski"></span>'
        u = x["unit"]
        d = 2
        tip = (f'{plain_name(m)}, {label.lower()}: {num(x[stat], d)} {u} ({"median" if stat == "median" else "mean"} over {x.get("n", s["n"])} '
               f'{UNIT_WORD.get(s["unit_of_evidence"], ("unit", "units"))[1]})'
               + (f'; lebowski {num(rx[stat], d)} {u}' if rx and m is not ref else ""))
        out.append(f'<div class="b-row">{head}<span class="b-track" tabindex="0" data-tip="{esc(tip)}" aria-label="{esc(tip)}">'
                   f'<span class="b-bar{" b-refbar" if m is ref else ""}" style="width:{w:.1f}%"></span>{tick}</span>'
                   f'<span class="b-v">{num(x[stat], d)}<span class="b-u"> {esc(u)}</span></span></div>')
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
            '<span>comma\'s public segments, open loop; lower is better; tick = lebowski</span></div>')
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
            f'<span class="wins">better on {p["wins"]}/{p["n"]}</span>{extra}</td>')


def summary_table(models, cfg) -> str:
    ref = cfg["reference_model"]
    cols = [(s, k, lab) for s, k, lab, _ in KEY_METRICS] + [(s, k, lab) for s, k, lab, _ in MORE_METRICS]
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
    return (f'<div class="tw"><table class="sum"><caption>Mean with its 95% interval, then the paired difference from lebowski '
            f'on the units both have. The G55 sections come from four drives or fewer and carry no interval.</caption>'
            f'<thead><tr><th scope="col">Model</th>{th}</tr></thead><tbody>{"".join(body)}</tbody></table></div>')


def legend() -> str:
    return ('<p class="legend"><span>' + mark("better") + ' Better, 95% interval excludes zero</span>'
            '<span>' + mark("worse") + ' Worse, interval excludes zero</span>'
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


# ---------------------------------------------------------------- overview

def overview(models, cfg, root) -> str:
    findings = "".join(
        f'<li><span class="f-fig">{esc(fill(f["figure"], models, root))}</span>'
        f'<p>{esc(fill(f["text"], models, root))}</p></li>' for f in cfg.get("findings", []))
    newest = models[-1]

    def landed(m):  # the latest date this model's weights went onto openpilot master
        md, src = m["model"], m["model"]["source"]
        return src["date"] if src["kind"] == "openpilot-master" else md.get("on_master")
    on_master = [m for m in models if landed(m)]
    master_line = ""
    if on_master:
        mm = max(on_master, key=lambda m: (landed(m), m["model"]["id"]))
        if mm is not newest:
            master_line = (f' Newest on openpilot master: {name_html(mm, "models/" + mm["model"]["id"] + ".html")}, '
                           f'{esc(landed(mm))}.')
    return f"""<section class="hero">
<div class="hero-t">
<h1>openpilot driving model evaluations</h1>
<p class="lede">Every comma.ai openpilot driving model, including Hugging Face uploads, scored with one fixed suite on comma's public drives and on drives from a 2011 Mercedes G55, in open loop and in closed loop.</p>
<p class="newest">Newest scored: {name_html(newest, "models/" + newest["model"]["id"] + ".html")}, first public {esc(first_public(newest))} ({esc(lc_status(newest))}).{master_line} <a href="#models">All models</a></p>
</div>
{hero_chart(models, cfg)}
</section>

<section id="found" class="found">
<h2>Results</h2>
<p class="asof">Measured by this suite, as of {esc(cfg.get("findings_date", ""))}. The reference is lebowski (877M parameters, July 2026), the model the G55 drives with.</p>
<ul class="findings">{findings}</ul>
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
<p><a href="method.html">The full method</a></p>
</section>

<section id="research" class="research">
<h2>For researchers</h2>
<ul class="plain">
<li>Every number is a mean over units: a 60 s segment of a public drive, or a window of a G55 drive. Units are grouped by the drive they come from, and the 95% intervals resample whole drives. Below 10 drives there is no interval, so the G55 sections report means and win counts only.</li>
<li>A paired difference compares two models on the units both were scored on; the count beside it is the units where the model did better.</li>
<li>Per-segment CSVs, one results file per model (<a href="{esc(cfg['repo_url'])}/blob/main/eval/SCHEMA.md">schema</a>) and the code are public; <code>python -m geval.reproduce</code> recomputes every public interval from the CSVs. <a href="method.html#downloads">Downloads</a></li>
<li>The G55 drives stay private; their scores are published, not the frames.</li>
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
        rows.append(f'<tr><th scope="row">{esc(x["label"])}<span class="th-s">{esc(", ".join(p for p in (x["unit"], BETTER_TEXT[x["better"]]) if p))}</span></th>'
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
            f'<section><h2>Key numbers</h2><p class="sub">Lower is better on all of these; the tick is lebowski.{prevt}</p>'
            f'<div class="kbars">{bars(m, models, cfg, mets, scales, False)}</div></section>'
            f'<section><h2>All scores</h2>{legend()}{secs}</section>{stills}'
            + (f'<section><h2>Limits</h2><ul class="limits">{limits}</ul></section>' if limits else "")
            + f'<section><h2>The model file</h2><dl class="ident">{dl}</dl>'
              f'<p><a href="../{esc(jrel)}">Results file</a> (<code>{esc(jrel)}</code>, schema 1).</p></section>')


# ---------------------------------------------------------------- method page

def method(models, cfg) -> str:
    prose = (SITE / "pages" / "method.html").read_text()
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
    root, out = a.root.resolve(), a.out.resolve()
    models = load(root)
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
                                          "open and closed loop, intervals, limits and downloads.", method(models, cfg), 0, "method", "method.html"))
    print(f"{len(models)} models -> {out}")


if __name__ == "__main__":
    main()
