"""Rankings for the G-Wagon eval:

1. G55 composite ranking (`g55_composite`): combines the four G55 evaluation pillars on a 0–100
   scale (higher is better; 100 = best model on that pillar, 0 = worst):
     - Closed-loop path keeping (40%): nominal distance at 2 s in worldmodel-4B (25% median + 15% mean)
     - Closed-loop recovery (25%): 2 s distance after a 2° heading error (12.5%) and 0.5 m offset (12.5%)
     - Open-loop tracking (20%): mean lateral error on recorded G55 drives (across all scored models)
     - Closed-loop smoothness (15%): RMS requested lateral jerk in nominal closed loop

2. Single-section closed-loop rankings (`rank`, `scores`, `write_truck`): median and mean distance from
   the recorded path at 2 s with a drive-clustered 95% interval of the median, head-to-head window win
   rate across peers, paired differences on shared units, and strict ordinal ranks (1, 2, 3, ...).

    uv run python -m geval.ranking                                  # from eval/; the public table
    uv run python -m geval.ranking --section truck_closed_loop      # the published G55 closed-loop table
    uv run python -m geval.ranking --section composite              # the G55 composite ranking
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import stats
from .reproduce import read_csv

ROOT = Path(__file__).resolve().parents[1]
SECTION = "public_closed_loop"
TRUCK = "truck_closed_loop"
KEY = "xtrack_2s"

# Pillar weights for the G55 composite score (sum = 1.00).
COMPOSITE_WEIGHTS = {
    "path": 0.40,        # 25% nominal CL 2 s median + 15% nominal CL 2 s mean
    "recovery": 0.25,    # 12.5% 2° heading 2 s median + 12.5% 0.5 m offset 2 s median
    "open_loop": 0.20,   # 20% G55 open-loop lateral error (lat_mae mean across all scored models)
    "smoothness": 0.15,  # 15% G55 closed-loop lateral jerk RMS (lat_jerk mean)
}


def _win_rates(vals: dict) -> dict[str, float]:
    """Per model, fraction of head-to-head comparisons won across shared units (lower is better; tie = 0.5)."""
    models = sorted(vals)
    if len(models) < 2:
        return {m: 0.5 for m in models}
    wins = {m: 0.0 for m in models}
    totals = {m: 0 for m in models}
    for i, a in enumerate(models):
        va = vals[a]
        for b in models[i + 1:]:
            vb = vals[b]
            for u in set(va) & set(vb):
                xa, xb = va[u], vb[u]
                if stats.finite(xa) and stats.finite(xb):
                    totals[a] += 1
                    totals[b] += 1
                    if xa < xb:
                        wins[a] += 1.0
                    elif xb < xa:
                        wins[b] += 1.0
                    else:
                        wins[a] += 0.5
                        wins[b] += 0.5
    return {m: (wins[m] / totals[m] if totals[m] else 0.5) for m in models}


def rank(vals: dict, cluster_of) -> dict:
    """vals: {model: {unit: value}}. Medians, means, intervals, head-to-head win rates, ordinal ranks,
    and every ordered pair's difference."""
    out = {}
    win_rate = _win_rates(vals)
    for m, v in vals.items():
        mp = stats.median_pool(v, cluster_of)
        pp = stats.pool(v, cluster_of)
        out[m] = {
            "median": mp["median"],
            "mean": pp["value"],
            "ci": mp["ci"],
            "n": mp["n"],
            "clusters": mp["clusters"],
            "win_rate": win_rate.get(m, 0.5),
        }
    pairs = {(a, b): stats.median_diff(vals[a], vals[b], cluster_of) for a in vals for b in vals if a != b}
    paired_mean = {(a, b): stats.paired(vals[a], vals[b], "lower", cluster_of) for a in vals for b in vals if a != b}
    ordered = sorted(
        out,
        key=lambda m: (
            round(out[m]["median"] or 999.0, 4),
            round(out[m]["mean"] or 999.0, 4),
            -round(out[m]["win_rate"], 4),
            m,
        ),
    )
    for idx, m in enumerate(ordered, 1):
        out[m]["rank"] = idx
        out[m]["separated_below"] = sum(
            1 for o in vals
            if o != m and paired_mean[(o, m)] and paired_mean[(o, m)].get("ci")
            and paired_mean[(o, m)]["ci"][1] < 0
        )
    return {"models": out, "pairs": pairs, "paired_mean": paired_mean}


def scores(root: Path) -> dict:
    """{model id: {"median", "ci", "n", "clusters", "rank"}} for every model with a public closed-loop
    CSV, and {(a, b): median_diff(a, b)} for every ordered pair of them."""
    vals, drives = {}, {}
    for p in sorted((root / "results").glob("*.json")):
        sec = json.loads(p.read_text())["sections"].get(SECTION) or {}
        if sec.get("status") == "ok" and sec.get("per_unit_file"):
            v, d = read_csv(root / sec["per_unit_file"])
            vals[p.stem] = v[KEY]
            drives.update(d)
    return rank(vals, drives.get)


def write_truck(work: Path, root: Path) -> Path | None:
    """From the private unit store: rankings/truck_closed_loop.json, aggregates only."""
    from . import results, store
    unit_dir = work / "units" / TRUCK
    if not unit_dir.is_dir():
        return None
    key = next(m[4] for m in results.S.SECTIONS[TRUCK]["metrics"] if m[0] == KEY)
    vals = {p.stem: {u: v for u, v in store.values(store.load(work, TRUCK, p.stem), key).items()}
            for p in sorted(unit_dir.glob("*.json"))}
    vals = {m: v for m, v in vals.items() if any(stats.finite(x) for x in v.values())}
    r = rank(vals, results.cluster_fn(TRUCK, None))
    doc = {"section": TRUCK, "key": KEY, "unit": "m",
           "models": {m: {k: stats.rounded(v) if k in ("median", "mean", "ci", "win_rate") else v
                          for k, v in s.items()}
                      for m, s in r["models"].items()}}
    p = root / "rankings" / f"{TRUCK}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, indent=1) + "\n")
    return p


def truck_scores(root: Path) -> dict | None:
    """The published G55 closed-loop ranking, or None before `geval build` has written one."""
    p = root / "rankings" / f"{TRUCK}.json"
    return json.loads(p.read_text()) if p.exists() else None


def _metric_val(m: dict, section: str, key: str, stat: str = "value") -> float | None:
    s = m.get("sections", {}).get(section)
    if not s or s.get("status") != "ok":
        return None
    for x in s.get("metrics", []):
        if x.get("key") == key:
            v = x.get(stat)
            return float(v) if v is not None else (float(x["value"]) if x.get("value") is not None else None)
    return None


def _norm_0_100(val: float | None, vals: list[float]) -> float | None:
    if val is None or not vals:
        return None
    lo, hi = min(vals), max(vals)
    if hi <= lo:
        return 100.0
    return 100.0 * (hi - val) / (hi - lo)


def g55_composite(models: list[dict], truck_rk: dict | None = None) -> dict[str, dict]:
    """Compute the G55 composite score (0–100) and pillar sub-scores for every model in `models`.
    Models with G55 closed-loop scores receive a composite score and strict ordinal rank 1..N;
    open-loop-only models receive their open-loop pillar score and open-loop ordinal rank."""
    trk = (truck_rk or {}).get("models", truck_rk or {})

    def cl_med(m):
        mid = m["model"]["id"]
        if mid in trk and trk[mid].get("median") is not None:
            return float(trk[mid]["median"])
        return _metric_val(m, "truck_closed_loop", "xtrack_2s", "median")

    def cl_mean(m):
        mid = m["model"]["id"]
        if mid in trk and trk[mid].get("mean") is not None:
            return float(trk[mid]["mean"])
        return _metric_val(m, "truck_closed_loop", "xtrack_2s", "value")

    cl_meds = [v for v in (cl_med(m) for m in models) if v is not None]
    cl_means = [v for v in (cl_mean(m) for m in models) if v is not None]
    hdg_meds = [v for v in (_metric_val(m, "truck_recovery", "heading_xtrack_2s", "median") for m in models) if v is not None]
    off_meds = [v for v in (_metric_val(m, "truck_recovery", "offcentre_xtrack_2s", "median") for m in models) if v is not None]
    jrk_vals = [v for v in (_metric_val(m, "truck_closed_loop", "lat_jerk", "value") for m in models) if v is not None]
    ol_vals = [v for v in (_metric_val(m, "truck_open_loop", "lat_mae", "value") for m in models) if v is not None]

    out: dict[str, dict] = {}
    for m in models:
        mid = m["model"]["id"]
        s_cl_med = _norm_0_100(cl_med(m), cl_meds)
        s_cl_mean = _norm_0_100(cl_mean(m), cl_means)
        s_path = (0.625 * s_cl_med + 0.375 * s_cl_mean) if (s_cl_med is not None and s_cl_mean is not None) else s_cl_med

        s_hdg = _norm_0_100(_metric_val(m, "truck_recovery", "heading_xtrack_2s", "median"), hdg_meds)
        s_off = _norm_0_100(_metric_val(m, "truck_recovery", "offcentre_xtrack_2s", "median"), off_meds)
        if s_hdg is not None and s_off is not None:
            s_rec = 0.5 * (s_hdg + s_off)
        else:
            s_rec = s_hdg if s_hdg is not None else s_off

        s_ol = _norm_0_100(_metric_val(m, "truck_open_loop", "lat_mae", "value"), ol_vals)
        s_jrk = _norm_0_100(_metric_val(m, "truck_closed_loop", "lat_jerk", "value"), jrk_vals)

        pillars = {
            "path": s_path,
            "recovery": s_rec,
            "open_loop": s_ol,
            "smoothness": s_jrk,
        }
        if s_path is not None:
            avail = [(k, v, COMPOSITE_WEIGHTS[k]) for k, v in pillars.items() if v is not None]
            w_sum = sum(w for _, _, w in avail)
            comp = sum(v * w for _, v, w in avail) / w_sum if w_sum > 0 else s_path
        else:
            comp = None

        out[mid] = {
            "score": round(comp, 1) if comp is not None else None,
            "score_raw": comp,
            "pillars": {k: (round(v, 1) if v is not None else None) for k, v in pillars.items()},
            "cl_median": cl_med(m),
            "cl_mean": cl_mean(m),
            "hdg_median": _metric_val(m, "truck_recovery", "heading_xtrack_2s", "median"),
            "off_median": _metric_val(m, "truck_recovery", "offcentre_xtrack_2s", "median"),
            "ol_mae": _metric_val(m, "truck_open_loop", "lat_mae", "value"),
            "cl_jerk": _metric_val(m, "truck_closed_loop", "lat_jerk", "value"),
            "win_rate": trk.get(mid, {}).get("win_rate"),
        }

    cl_ordered = sorted(
        (mid for mid, d in out.items() if d["score_raw"] is not None),
        key=lambda mid: (-round(out[mid]["score_raw"], 4), round(out[mid]["cl_median"] or 999.0, 4), mid),
    )
    for idx, mid in enumerate(cl_ordered, 1):
        out[mid]["rank"] = idx

    cl2_ordered = sorted(
        (mid for mid, d in out.items() if d["cl_median"] is not None),
        key=lambda mid: (round(out[mid]["cl_median"], 4), round(out[mid]["cl_mean"] or 999.0, 4), mid),
    )
    for idx, mid in enumerate(cl2_ordered, 1):
        out[mid]["cl_rank"] = idx

    ol_ordered = sorted(
        (mid for mid, d in out.items() if d["ol_mae"] is not None),
        key=lambda mid: (round(out[mid]["ol_mae"], 4), mid),
    )
    for idx, mid in enumerate(ol_ordered, 1):
        out[mid]["ol_rank"] = idx

    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--section", default=SECTION, choices=[SECTION, TRUCK, "composite"])
    a = ap.parse_args(argv)
    if a.section == "composite":
        docs = [json.loads(p.read_text()) for p in sorted((a.root / "results").glob("*.json"))]
        t = truck_scores(a.root)
        comp = g55_composite(docs, t)
        for m, s in sorted((kv for kv in comp.items() if kv[1].get("rank")), key=lambda kv: kv[1]["rank"]):
            p = s["pillars"]
            print(f"{s['rank']:>2}  {m:<16} G55 score {s['score']:5.1f}  "
                  f"[path {p['path']:4.0f} | rec {p['recovery']:4.0f} | ol {p['open_loop']:4.0f} | smooth {p['smoothness']:4.0f}]  "
                  f"CL 2s {s['cl_median']*100:4.1f} cm")
        return 0
    if a.section == TRUCK:
        t = truck_scores(a.root)
        for m, s in sorted((t or {"models": {}})["models"].items(), key=lambda kv: (kv[1]["rank"], kv[1]["median"])):
            ci = "no interval" if not s["ci"] else f"[{s['ci'][0]:.4f}, {s['ci'][1]:.4f}]"
            wr = f"  win_rate {s['win_rate']*100:4.1f}%" if s.get("win_rate") is not None else ""
            print(f"{s['rank']:>2}  {m:<16} median {s['median']:.4f} m {ci}{wr}  {s['n']} windows, {s['clusters']} drives")
        return 0
    r = scores(a.root)
    for m, s in sorted(r["models"].items(), key=lambda kv: (kv[1]["rank"], kv[1]["median"])):
        ci = "no interval" if not s["ci"] else f"[{s['ci'][0]:.4f}, {s['ci'][1]:.4f}]"
        wr = f"  win_rate {s['win_rate']*100:4.1f}%" if s.get("win_rate") is not None else ""
        print(f"{s['rank']:>2}  {m:<16} median {s['median']:.4f} m {ci}{wr}  {s['n']} segments, {s['clusters']} drives")
    for (a, b), d in sorted(r["pairs"].items()):
        if a < b and d:
            ci = "no interval" if not d["ci"] else f"[{d['ci'][0]:+.4f}, {d['ci'][1]:+.4f}]"
            print(f"    {a} minus {b}: {d['delta']:+.4f} m {ci}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
