# /// script
# requires-python = ">=3.11"
# ///
"""Build the G-Wagon eval site from results/*.json.

    uv run eval/site/build.py                       # real results, out to eval/site/_site
    uv run eval/site/build.py --root eval/site/fixtures

--root is the directory holding results/, per_unit/ and stills/ (default: eval/).
The results files are the only input (SCHEMA.md); the method prose is pages/method.html.
"""
from __future__ import annotations

import argparse
import html
import json
import math
import shutil
from pathlib import Path

SITE = Path(__file__).resolve().parent
EVAL = SITE.parent

SECTIONS = {
    "public_open_loop": "Public segments, open loop",
    "public_closed_loop": "Public segments, closed loop",
    "truck_open_loop": "Truck, open loop",
    "truck_closed_loop": "Truck, closed loop",
    "truck_recovery": "Truck, recovery tests",
}
UNIT_WORD = {"segment": ("segment", "segments"), "window": ("window", "windows")}
BETTER_TEXT = {"lower": "lower is better", "higher": "higher is better", "near1": "closer to 1 is better"}
MINUS = "−"
WORDS = {2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine"}


def esc(x) -> str:
    return html.escape("" if x is None else str(x), quote=True)


# ---------------------------------------------------------------- numbers

def decimals_for(v: float | None) -> int:
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
    return ("+" + s) if signed else s


def ci_text(ci, d: int, signed: bool = False) -> str:
    if not ci:
        return "no interval"
    return f"[{num(ci[0], d, signed)}, {num(ci[1], d, signed)}]"


def verdict_of(metric: dict, paired: dict) -> str:
    """better | worse | tie | diff (separated, direction unclear) | none (no interval)."""
    ci = paired.get("ci")
    if not ci:
        return "none"
    if not (ci[0] > 0 or ci[1] < 0):
        return "tie"
    delta, better = paired["delta"], metric["better"]
    if better == "lower":
        return "better" if delta < 0 else "worse"
    if better == "higher":
        return "better" if delta > 0 else "worse"
    v = metric["value"]
    if v is None:
        return "diff"
    base = v - delta
    if abs(v - 1) < abs(base - 1):
        return "better"
    if abs(v - 1) > abs(base - 1):
        return "worse"
    return "diff"


MARK_TEXT = {
    "better": "better, interval excludes zero",
    "worse": "worse, interval excludes zero",
    "diff": "different, interval excludes zero",
    "tie": "interval includes zero",
    "none": "no interval",
}


def mark(v: str) -> str:
    return f'<span class="mk mk-{v}" aria-hidden="true"></span><span class="sr">{MARK_TEXT[v]}</span>'


def lc(label: str) -> str:
    """Sentence-internal form of a metric label: 'Lateral error' -> 'lateral error'."""
    if len(label) > 1 and label[0].isupper() and label[1].islower():
        return label[0].lower() + label[1:]
    return label


def join_words(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


# ---------------------------------------------------------------- data

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
    models.sort(key=lambda d: (d["model"]["source"]["date"], d["scored"]["date"], d["model"]["id"]))
    return models


def name_of(m: dict) -> str:
    return m["model"]["name"] or m["model"]["id"]


def short_hash(m: dict) -> str:
    """The checkpoint hash comma names a model by, when the id starts with one."""
    h = m["model"]["id"].split("_")[0][:8]
    return h if len(h) == 8 and all(c in "0123456789abcdef" for c in h) else ""


def hash_code(m: dict) -> str:
    h = short_hash(m)
    return f" <code>{h}</code>" if h else ""


def find_metric(m: dict, section: str, key: str):
    s = m["sections"].get(section)
    if not s or s["status"] != "ok":
        return None, s
    for x in s["metrics"]:
        if x["key"] == key:
            return x, s
    return None, s


def headline_metrics(models: list[dict], cfg: dict) -> list[tuple[str, str, dict]]:
    """(section, key, an example metric) for each configured headline metric present somewhere."""
    out = []
    for section, key in cfg["headline"]:
        for m in models:
            x, _ = find_metric(m, section, key)
            if x:
                out.append((section, key, x))
                break
    if out:
        return out
    seen = set()
    for m in models:  # nothing configured matched: the first metric of each section
        for section, s in m["sections"].items():
            if s["status"] == "ok" and s["metrics"] and section not in seen:
                seen.add(section)
                out.append((section, s["metrics"][0]["key"], s["metrics"][0]))
    return out


def previous_baseline(m: dict, ref: str):
    rest = [b for b in m["baselines"] if b != ref]
    return rest[0] if rest else None


# ---------------------------------------------------------------- page shell

def page(cfg: dict, title: str, body: str, depth: int, current: str) -> str:
    up = "../" * depth
    umami = ""
    if cfg.get("umami_website_id"):
        umami = (f'<script defer src="{esc(cfg["umami_src"])}" '
                 f'data-website-id="{esc(cfg["umami_website_id"])}"></script>')
    nav = []
    for href, label, key in (("index.html", "Overview", "overview"), ("method.html", "Method", "method")):
        cur = ' aria-current="page"' if key == current else ""
        nav.append(f'<a href="{up}{href}"{cur}>{label}</a>')
    full = f"{title} | {cfg['site_name']}" if title != cfg["site_name"] else title
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(full)}</title>
<meta name="description" content="Independent scores for each comma.ai openpilot driving model, on comma's public segments and on one 2011 Mercedes G55.">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible+Mono:wght@400;500&family=Atkinson+Hyperlegible+Next:wght@400;500;700;800&display=swap">
<link rel="stylesheet" href="{up}style.css">
{umami}
</head>
<body>
<header class="top"><div class="wrap top-in">
<a class="word" href="{up}index.html">{esc(cfg['site_name'])}</a>
<nav>{''.join(nav)}</nav>
</div></header>
<main class="wrap">
{body}
</main>
<footer class="wrap foot">
<p>Run by David Rose. Not affiliated with comma.ai or Mercedes-Benz. Results come from one truck and a few dozen public segments; each number carries its sample size.
<a href="{esc(cfg['repo_url'])}/tree/main/eval">Code and schema</a>.</p>
</footer>
<div id="tip" class="tip" role="status" hidden></div>
<script src="{up}site.js" defer></script>
</body>
</html>
"""


# ---------------------------------------------------------------- overview

def nice_ticks(lo: float, hi: float, n: int = 4) -> tuple[list[float], int]:
    """Round tick values across [lo, hi] and the decimals their labels need."""
    span = hi - lo or 1.0
    raw = span / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(s * mag for s in (1, 2, 2.5, 5, 10) if s * mag >= raw)
    d = max(0, -math.floor(math.log10(step) + 1e-9))
    if abs(round(step * 10 ** d) - step * 10 ** d) > 1e-9:
        d += 1
    t = math.ceil(lo / step) * step
    out = []
    while t <= hi + 1e-12:
        out.append(round(t, 10))
        t += step
    return out, d


def forest(models, ref, section, key, example):
    rows = []
    for m in models:
        if m["model"]["id"] == ref:
            continue
        x, s = find_metric(m, section, key)
        p = x and x["paired"].get(ref)
        rows.append((m, x, s, p))
    if not any(p for *_, p in rows):
        return ""
    vals = [0.0]
    for _, _, _, p in rows:
        if p:
            vals += [p["delta"]] + (p["ci"] or [])
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.08 or 0.01
    lo, hi = lo - pad, hi + pad
    pos = lambda v: (v - lo) / (hi - lo) * 100
    d = decimals_for(example["value"])
    out = [f'<figure class="forest"><figcaption><span class="f-t">{esc(example["label"])}</span>'
           f'<span class="f-s">{esc(SECTIONS.get(section, section))}, {esc(example["unit"])}, '
           f'{BETTER_TEXT[example["better"]]}</span></figcaption><div class="f-rows">']
    for m, x, s, p in rows:
        label = f'<a href="models/{esc(m["model"]["id"])}.html">{esc(name_of(m))}</a>'
        if not p:
            why = "not run" if not s or s["status"] != "ok" else "no paired score"
            out.append(f'<div class="f-row"><div class="f-l">{label}</div><div class="f-track f-empty">{why}</div></div>')
            continue
        v = verdict_of(x, p)
        uw = UNIT_WORD.get(s["unit_of_evidence"], (s["unit_of_evidence"],) * 2)
        tip = (f'{name_of(m)} minus lebowski: {num(p["delta"], d, True)} {x["unit"]} '
               f'{ci_text(p["ci"], d, True)}; better on {p["wins"]} of {p["n"]} {uw[1]}; {MARK_TEXT[v]}')
        whisk = ""
        if p["ci"]:
            a, b = pos(p["ci"][0]), pos(p["ci"][1])
            whisk = f'<span class="f-ci" style="left:{a:.2f}%;width:{b - a:.2f}%"></span>'
        out.append(
            f'<div class="f-row"><div class="f-l">{label}</div>'
            f'<div class="f-track" tabindex="0" data-tip="{esc(tip)}" aria-label="{esc(tip)}">'
            f'<span class="f-zero" style="left:{pos(0):.2f}%"></span>{whisk}'
            f'<span class="f-dot v-{v}" style="left:{pos(p["delta"]):.2f}%"></span></div></div>')
    tv, td = nice_ticks(lo, hi)
    ticks = "".join(f'<span style="left:{pos(t):.2f}%">{num(t, td, True) if t else "0"}</span>' for t in tv)
    out.append(f'<div class="f-row f-axis"><div class="f-l"></div><div class="f-ticks">{ticks}</div></div></div></figure>')
    return "".join(out)


def delta_cell(x, p, d) -> str:
    if not p:
        return '<td class="na">not paired</td>'
    v = verdict_of(x, p)
    return (f'<td class="n">{mark(v)}{num(p["delta"], d, True)}'
            f'<span class="ci">{ci_text(p["ci"], d, True)}</span>'
            f'<span class="wins">{p["wins"]}/{p["n"]}</span></td>')


def delta_table(models, ref, heads, by_model, caption):
    """by_model(m) -> baseline id to compare against, or None to skip the row."""
    th = "".join(f'<th class="n" scope="col">{esc(x["label"])}<span class="th-s">{esc(SECTIONS.get(s, s))}, {esc(x["unit"])}</span></th>'
                 for s, k, x in heads)
    body = []
    for m in models:
        b = by_model(m)
        if not b:
            continue
        bname = next((name_of(o) for o in models if o["model"]["id"] == b), b)
        cells = []
        for s, k, ex in heads:
            x, sec = find_metric(m, s, k)
            if not x:
                st = sec["status"].replace("_", " ") if sec else "not run"
                cells.append(f'<td class="na">{esc(st)}</td>')
                continue
            cells.append(delta_cell(x, x["paired"].get(b), decimals_for(x["value"])))
        body.append(f'<tr><th scope="row"><a href="models/{esc(m["model"]["id"])}.html">{esc(name_of(m))}</a>'
                    f'<span class="th-s">vs {esc(bname)}</span></th>{"".join(cells)}</tr>')
    if not body:
        return ""
    return (f'<p class="cap">{caption}</p><div class="tw"><table aria-label="{esc(caption)}"><thead><tr><th scope="col">Model</th>{th}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


def legend() -> str:
    return ('<p class="legend"><span>' + mark("better") + ' Better than the baseline, 95% interval excludes zero</span>'
            '<span>' + mark("worse") + ' Worse, interval excludes zero</span>'
            '<span>' + mark("tie") + ' Not separated: the interval includes zero</span>'
            '<span>Delta is model minus baseline; the fraction is units where the model did better.</span></p>')


def verdict_sentences(newest, models, ref, heads) -> str:
    """One sentence against lebowski, one against the previous model, from the headline metrics only."""
    name = name_of(newest)

    def groups(bid):
        g = {"better": [], "worse": [], "diff": [], "tie": []}
        for s, k, _ in heads:
            x, _ = find_metric(newest, s, k)
            p = x and x["paired"].get(bid)
            if p and verdict_of(x, p) in g:
                where = "truck" if s.startswith("truck") else "public segments"
                g[verdict_of(x, p)].append(f'{lc(x["label"])} ({where})')
        return g

    def sentence(subject, g, bname):
        sep = [(w, g[w]) for w in ("worse", "better", "diff") if g[w]]
        n_tie = len(g["tie"])
        if not sep and not n_tie:
            return None
        if not sep:
            return f"{subject} is not separated from {bname} on any headline metric."
        word = {"worse": "worse than", "better": "better than", "diff": "different from"}
        parts = [f"{word[w]} {bname} on {join_words(items)}" if i == 0 else f"{word[w].split()[0]} on {join_words(items)}"
                 for i, (w, items) in enumerate(sep)]
        out = f"{subject} is " + join_words(parts)
        if n_tie:
            rest = "the other headline metric" if n_tie == 1 else f"the other {WORDS.get(n_tie, n_tie)} headline metrics"
            out += f", and not separated from it on {rest}"
        return out + "."

    lines = []
    if newest["model"]["id"] != ref:
        lines.append(sentence(name, groups(ref), "lebowski"))
        prev = previous_baseline(newest, ref)
        if prev:
            pname = next((name_of(o) for o in models if o["model"]["id"] == prev), prev)
            lines.append(sentence("It", groups(prev), f"{pname}, the model before it,"))
    return " ".join(x for x in lines if x) or "No paired scores against lebowski yet."


def evidence_line(newest) -> str:
    parts = []
    for key, s in newest["sections"].items():
        if s["status"] == "ok":
            uw = UNIT_WORD.get(s["unit_of_evidence"], (s["unit_of_evidence"],) * 2)
            parts.append(f'{s["n"]} {uw[1] if s["n"] != 1 else uw[0]} ({lc(SECTIONS.get(key, key))})')
    not_run = [lc(SECTIONS.get(k, k)) for k, s in newest["sections"].items() if s["status"] != "ok"]
    out = "Scored on " + join_words(parts) + "." if parts else "No section has scores yet."
    if not_run:
        out += " Not scored: " + join_words(not_run) + "."
    return out


def timeline(models) -> str:
    mx = max((m["model"]["params_m"] or 0) for m in models) or 1
    rows = []
    for m in models:
        md = m["model"]
        w = (md["params_m"] or 0) / mx * 100
        rows.append(
            f'<tr><td class="n">{esc(md["source"]["date"])}</td>'
            f'<th scope="row"><a href="models/{esc(md["id"])}.html">{esc(name_of(m))}</a>{hash_code(m)}</th>'
            f'<td class="n size"><span class="bar" style="width:{w:.1f}%"></span><span>{num(md["params_m"], 0)}M</span></td>'
            f'<td class="n">{num(md["gmac"], 1)}</td><td>{esc(md["family"])}</td></tr>')
    return ('<p class="cap">Every scored model, oldest first. Parameters are counted from the weights; '
            'GMAC is multiply-accumulates per model step.</p><div class="tw"><table class="tl" aria-label="comma\'s models">'
            '<thead><tr><th class="n" scope="col">Published</th><th scope="col">Model</th><th class="n" scope="col">Parameters</th>'
            f'<th class="n" scope="col">GMAC</th><th scope="col">Family</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>')


def overview(models, cfg) -> str:
    ref = cfg["reference_model"]
    heads = headline_metrics(models, cfg)
    newest = models[-1]
    nm = newest["model"]
    hero = (f'<section class="hero"><p class="hero-k">Newest model: <a href="models/{esc(nm["id"])}.html">{esc(name_of(newest))}</a>{hash_code(newest)}, published {esc(nm["source"]["date"])}, scored {esc(newest["scored"]["date"][:10])}</p>'
            f'<p class="hero-v">{esc(verdict_sentences(newest, models, ref, heads))}</p>'
            f'<p class="hero-n">{esc(evidence_line(newest))}</p></section>')
    intro = ('<p class="intro">Each comma.ai openpilot driving model, scored with one fixed suite on comma\'s public segments and on '
             'recorded drives from one 2011 Mercedes G55. Every score is compared with lebowski, the 877M model from July 2026 that '
             'many openpilot users still run, and with the comma model released before it. '
             '<a href="method.html">How the scores are made</a>.</p>')
    charts = "".join(forest(models, ref, s, k, x) for s, k, x in heads)
    vs_ref = delta_table(models, ref, heads, lambda m: ref if m["model"]["id"] != ref and m["baselines"] and ref in m["baselines"] else None,
                         "Paired delta against lebowski, with its 95% interval and units won")
    vs_prev = delta_table(models, ref, heads, lambda m: previous_baseline(m, ref),
                          "Paired delta against the comma model released before it")
    return (hero + intro
            + '<section><h2>Against lebowski</h2>' + legend()
            + (f'<div class="forests">{charts}</div>' if charts else '<p>No model has paired scores against lebowski yet.</p>')
            + vs_ref + '</section>'
            + ('<section><h2>Against the previous model</h2>' + vs_prev + '</section>' if vs_prev else '')
            + '<section><h2>comma\'s models</h2>' + timeline(models) + '</section>')


# ---------------------------------------------------------------- model page

def section_block(m, key, s, models, root, out) -> str:
    title = SECTIONS.get(key, key)
    st = s["status"]
    if st != "ok":
        msg = {"not_run": "Not run for this model.", "failed": "The run failed; there are no scores."}.get(st, st)
        cond = f' <span class="muted">Condition: {esc(s.get("condition"))}.</span>' if s.get("condition") else ""
        return f'<section class="sec sec-off"><h3>{esc(title)}</h3><p><strong>{msg}</strong>{cond}</p></section>'
    uw = UNIT_WORD.get(s["unit_of_evidence"], (s["unit_of_evidence"],) * 2)
    bases = [b for b in m["baselines"]]
    bname = {o["model"]["id"]: name_of(o) for o in models}
    th = "".join(f'<th class="n" scope="col">vs {esc(bname.get(b, b))}</th>' for b in bases)
    rows = []
    for x in s["metrics"]:
        d = decimals_for(x["value"])
        cells = "".join(delta_cell(x, x["paired"].get(b), d) for b in bases)
        rows.append(f'<tr><th scope="row">{esc(x["label"])}<span class="th-s">{esc(x["unit"])}, {BETTER_TEXT[x["better"]]}</span></th>'
                    f'<td class="n">{num(x["value"], d)}<span class="ci">{ci_text(x["ci"], d)}</span></td>{cells}</tr>')
    dl = ""
    if s.get("per_unit_file"):
        dl = f' <a href="../{esc(copy_rel(root, out, s["per_unit_file"]))}">Per-{uw[0]} CSV</a>.'
    return (f'<section class="sec"><h3>{esc(title)}</h3>'
            f'<p class="sec-m">{s["n"]} {uw[1] if s["n"] != 1 else uw[0]}. Condition: {esc(s["condition"])}.{dl}</p>'
            f'<div class="tw"><table><thead><tr><th scope="col">Metric</th><th class="n" scope="col">Value</th>{th}</tr></thead>'
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
        ("Source", f'{esc(src["kind"])}, <code>{esc(src["ref"])}</code> (<a href="{esc(src["url"])}">link</a>)'),
        ("Published", esc(src["date"])),
        ("Parameters", f'{num(md["params_m"], 1)}M'),
        ("GMAC", num(md["gmac"], 1)),
        ("Family", esc(md["family"])),
        ("Scored", f'{esc(sc["date"][:16].replace("T", " "))} UTC, suite <code>{esc(sc["suite"])}</code>, code {code_html}'),
    ]
    if md.get("notes"):
        ident.append(("Notes", esc(md["notes"])))
    dl = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in ident)
    order = [k for k in SECTIONS if k in m["sections"]] + [k for k in m["sections"] if k not in SECTIONS]
    secs = "".join(section_block(m, k, m["sections"][k], models, root, out) for k in order)
    stills = ""
    if m.get("stills"):
        figs = "".join(
            f'<figure><img src="../{esc(copy_rel(root, out, s["file"]))}" alt="{esc(s["caption"])}" loading="lazy">'
            f'<figcaption><strong>{esc(s["condition"])}.</strong> {esc(s["caption"])}</figcaption></figure>'
            for s in m["stills"])
        stills = f'<section><h2>Stills</h2><div class="stills">{figs}</div></section>'
    limits = "".join(f"<li>{esc(x)}</li>" for x in m.get("limits", []))
    jrel = f'results/{md["id"]}.json'
    return (f'<section class="mhead"><h1>{esc(name_of(m))}{hash_code(m)}</h1><dl class="ident">{dl}</dl></section>'
            f'<section><h2>Scores</h2>{legend()}{secs}</section>{stills}'
            + (f'<section><h2>Limits</h2><ul class="limits">{limits}</ul></section>' if limits else "")
            + f'<section><h2>Download</h2><p><a href="../{esc(jrel)}">Results file</a> (<code>{esc(jrel)}</code>, '
              f'schema 1). Per-segment CSVs are linked from each public section.</p></section>')


# ---------------------------------------------------------------- method page

def method(models, cfg, out) -> str:
    prose = (SITE / "pages" / "method.html").read_text()
    rows = []
    for m in models:
        md = m["model"]
        csvs = [f'<a href="{esc(s["per_unit_file"])}">{esc(SECTIONS.get(k, k))}</a>'
                for k, s in m["sections"].items() if s.get("per_unit_file")]
        rows.append(f'<tr><th scope="row"><a href="models/{esc(md["id"])}.html">{esc(name_of(m))}</a>{hash_code(m)}</th>'
                    f'<td><a href="results/{esc(md["id"])}.json">{esc(md["id"])}.json</a></td>'
                    f'<td>{", ".join(csvs) or "none"}</td></tr>')
    table = ('<section id="downloads"><h2>Downloads</h2><p>One results file per model, in the format described in '
             f'<a href="{esc(cfg["repo_url"])}/blob/main/eval/SCHEMA.md">SCHEMA.md</a>, and one CSV per public section with a row per segment. '
             'Truck sections publish scores only.</p>'
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
    for f in ("style.css", "site.js"):
        shutil.copy2(SITE / "static" / f, out / f)
    for m in models:
        shutil.copy2(m["_file"], out / "results" / m["_file"].name)
        (out / "models" / f'{m["model"]["id"]}.html').write_text(
            page(cfg, name_of(m), model_page(m, models, cfg, root, out), 1, "model"))
    (out / "index.html").write_text(page(cfg, cfg["site_name"], overview(models, cfg), 0, "overview"))
    (out / "method.html").write_text(page(cfg, "Method", method(models, cfg, out), 0, "method"))
    print(f"{len(models)} models -> {out}")


if __name__ == "__main__":
    main()
