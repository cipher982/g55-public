"""geval run: resolve a model, queue every section's jobs, wait, score into the unit store.

Jobs are shell scripts in <work>/queue/jobs (geval/queue.py runs them under the shared GPU lock).
Outputs land in <work>/out/<section>/; a job skips work whose output exists, so a rerun only does
what is missing. Scoring reads every output that holds the model, so several models run in one
job (one world-model batch) are each scored from it.
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import backfill, closedloop, openloop, jobqueue as Q, results, sections as S, store

ENGINES = Path(__file__).resolve().parent / "engines"
UUID_CKPT = re.compile(r"([0-9a-f]{8})-[0-9a-f-]{27}/(\d+)")


# ---------------------------------------------------------------- model resolution
def sha256_of(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 24), b""):
            h.update(b)
    return h.hexdigest()


def py(cfg, code: str, *args) -> str:
    r = subprocess.run([cfg["paths"]["python"], "-I", "-c", code, *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(r.stderr[-2000:])
    return r.stdout.strip()


def hf_fetch(cfg, path: str, revision: str | None):
    repo = json.loads((results.DATA / "models.json").read_text())["hf_repo"]
    out = json.loads(py(cfg, """
import json, sys
from huggingface_hub import HfApi, hf_hub_download
repo, path, rev = sys.argv[1], sys.argv[2], sys.argv[3] or None
api = HfApi()
commits = api.list_repo_commits(repo, revision=rev)
rev = rev or commits[0].commit_id
local = hf_hub_download(repo, path, revision=rev)
print(json.dumps({"local": local, "revision": rev, "date": commits[0].created_at.strftime("%Y-%m-%d")}))
""", repo, path, revision or ""))
    url = f"https://huggingface.co/{repo}/blob/{out['revision']}/{path}"
    return Path(out["local"]), {"kind": "huggingface", "ref": out["revision"], "url": url, "date": out["date"]}


def hf_find(cfg, uuid8: str, ckpt: str) -> str:
    repo = json.loads((results.DATA / "models.json").read_text())["hf_repo"]
    files = json.loads(py(cfg, "import json,sys;from huggingface_hub import HfApi;print(json.dumps(HfApi().list_repo_files(sys.argv[1])))", repo))
    hit = [f for f in files if f.startswith(uuid8) and f"/{ckpt}/" in f and f.endswith(".onnx") and "big_" in f]
    if not hit:
        raise SystemExit(f"no big-model ONNX for {uuid8}/{ckpt} on {repo}")
    return hit[0]


def openpilot_fetch(work: Path, commit: str):
    path = "selfdrive/modeld/models/big_driving_supercombo.onnx"
    meta = json.loads(urllib.request.urlopen(f"https://api.github.com/repos/commaai/openpilot/commits/{commit}").read())
    sha = meta["sha"]
    dst = work / "downloads" / f"openpilot_{sha[:10]}.onnx"
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://media.githubusercontent.com/media/commaai/openpilot/{sha}/{path}"
        try:
            with urllib.request.urlopen(url) as r, open(str(dst) + ".part", "wb") as f:
                while b := r.read(1 << 24):
                    f.write(b)
        except Exception as e:
            raise SystemExit(f"{commit}: no {path} in LFS ({e}); since 2026-09 master ships a compiled pkl only: "
                             f"score the Hugging Face upload with the same checkpoint (hf:<uuid>/<ckpt>)")
        Path(str(dst) + ".part").rename(dst)
    date = meta["commit"]["committer"]["date"][:10]
    src = {"kind": "openpilot-master", "ref": sha, "url": f"https://github.com/commaai/openpilot/commit/{sha}", "date": date}
    return dst, src


def model_id_for(cfg, onnx: Path, fallback: str) -> tuple[str, str | None]:
    ckpt = py(cfg, "import sys,onnx;m=onnx.load(sys.argv[1],load_external_data=False);"
                   "print({p.key:p.value for p in m.metadata_props}.get('model_checkpoint',''))", str(onnx)) or None
    m = UUID_CKPT.findall(ckpt or "")
    if m:
        uuid8, step = m[-1]
        return f"{uuid8}_{step}", ckpt
    tail = re.findall(r"([0-9a-f]{8})-[0-9a-f-]{27}$", ckpt or "")
    if tail:
        return f"{tail[-1]}", ckpt
    return fallback, ckpt


def resolve(cfg, ref: str) -> str:
    """-> model id; writes <work>/models/<id>.json and links <work>/models/<id>.onnx."""
    work = cfg.work
    reg = results.registry(work)
    (work / "models").mkdir(parents=True, exist_ok=True)
    src, onnx = None, None
    if ref in reg:
        mid = ref
        known = Path(cfg["paths"]["models_dir"]) / f"{mid}.onnx"
        if known.exists():
            onnx = known
        else:
            f = reg[mid]["file"]
            onnx, _ = (hf_fetch(cfg, f["path"], f["hf_revision"]) if "hf_revision" in f else openpilot_fetch(work, f["commit"]))
    elif ref.startswith("hf:"):
        body = ref[3:]
        if ":" in body:
            rev, path = body.split(":", 1)
        else:
            m = re.match(r"([0-9a-f]{8})[0-9a-f-]*/(\d+)$", body)
            if not m:
                raise SystemExit(f"bad hf ref {ref!r}: hf:<revision>:<path> or hf:<uuid>/<ckpt>")
            rev, path = None, hf_find(cfg, m.group(1), m.group(2))
        onnx, src = hf_fetch(cfg, path, rev)
    elif ref.startswith("openpilot:"):
        onnx, src = openpilot_fetch(work, ref.split(":", 1)[1])
    elif ref.endswith(".onnx") and Path(ref).exists():
        onnx = Path(ref).resolve()
        src = {"kind": "release", "ref": onnx.name, "url": None, "date": datetime.now(timezone.utc).strftime("%Y-%m-%d")}
    else:
        raise SystemExit(f"cannot resolve {ref!r}")
    if ref in reg:
        mid = ref
        ckpt = None
    else:
        mid, ckpt = model_id_for(cfg, onnx, Path(onnx).stem)
    link = work / "models" / f"{mid}.onnx"
    if not link.exists():
        link.symlink_to(Path(onnx).resolve())
    rec_path = work / "models" / f"{mid}.json"
    rec = json.loads(rec_path.read_text()) if rec_path.exists() else {"id": mid}
    if "sha256" not in rec:
        rec["sha256"] = sha256_of(link)
        rec.update(json.loads(py(cfg, "import sys,json;sys.path.insert(0,sys.argv[2]);import arch;"
                                      "print(json.dumps(arch.count(sys.argv[1])))", str(link), str(Path(__file__).parent))))
    if mid not in reg:
        family = "residual" if rec.get("residual_head") else ("big-rl" if rec.get("history_in_graph") else "other")
        rec["registry"] = {"id": mid, "name": None, "family": family, "first_public": src["date"],
                           "on_master": src["date"] if src["kind"] == "openpilot-master" else None, "source": src,
                           "sha256": rec["sha256"],
                           "notes": f"Resolved from {ref}" + (f"; checkpoint {ckpt}" if ckpt else "") + "."}
    rec_path.write_text(json.dumps(rec, indent=1))
    return mid


# ---------------------------------------------------------------- jobs
def wait_gpu(mib: int) -> str:
    return (f"for i in $(seq 1080); do free=$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits | head -1); "
            f"[ \"$free\" -ge {mib} ] && break; sleep 10; done; echo gpu free $free MiB")


def segment_names(cfg):
    root = Path(cfg["paths"]["segments_dir"])
    return [s for s in results.segments() if (root / s / "ecamera.hevc").exists()]


def after_line(q: Path, after: str | None) -> str:
    """Shell that waits for another job to finish (so two world-model jobs never share the GPU)."""
    if not after:
        return ""
    j = q / "jobs" / after
    return f"while [ ! -e {j}.done ] && [ ! -e {j}.failed ]; do sleep 30; done\n"


def queue_jobs(cfg, mids: list, sections: list, tag: str | None = None, truck_conds: list | None = None,
               after: str | None = None, prefix: str | None = None) -> list:
    """Queue the jobs for one or more models (several models share one world-model batch).

    Job names start P20 (public open loop), P22 (truck open loop), P30 (public closed loop), P32
    (truck closed loop and recovery), P34 (braking lead); the runner starts them in name order, so
    `prefix` (e.g. P70) queues this submission behind work already named lower. truck_conds=[]
    queues no P32 job (only the braking lead, when truck_recovery is asked)."""
    pf = (lambda d: prefix) if prefix else (lambda d: d)
    work, q, pyx = cfg.work, cfg.work / "queue", cfg["paths"]["python"]
    tag = tag or "_".join(mids)
    onnx = {m: work / "models" / f"{m}.onnx" for m in mids}
    names = []
    if "public_open_loop" in sections:
        out = work / "out" / "public_open_loop"
        out.mkdir(parents=True, exist_ok=True)
        for m in mids:
            for seg in segment_names(cfg):
                o = out / f"{seg}__{m}.npz"
                names.append(Q.add_job(q, f"{pf('P20')}_pol_{m}_{seg}", f"cd {ENGINES}\n[ -s {o} ] || {pyx} -I route_replay.py "
                                       f"{Path(cfg['paths']['segments_dir']) / seg} {o} {cfg['paths']['cereal']} {onnx[m]} "
                                       f"2>&1 | grep -v W:onnxruntime | tail -3").stem)
    if "truck_open_loop" in sections:
        tol = cfg["truck_open_loop"]
        out = work / "out" / "truck_open_loop"
        out.mkdir(parents=True, exist_ok=True)
        for m in mids:
            if tol.get("batch_command"):   # one process per model and part: the model loads once for many windows
                n = int(tol.get("batch_parts", 1))
                for k in range(n):
                    cmd = tol["batch_command"].format(python=pyx, model=m, onnx=onnx[m], out_dir=out, part=f"{k}/{n}")
                    names.append(Q.add_job(q, f"{pf('P22')}_tolb_{m}_{k}of{n}", f"cd {tol['cwd']}\n{cmd} 2>&1 | "
                                           f"grep -v -e W:onnxruntime -e 'Corrupt JPEG' | grep -v '^ ' | tail -3").stem)
                continue
            for w in tol["windows"]:
                o = out / f"{w['id']}__{m}.npz"
                cmd = tol["command"].format(python=pyx, frames_root=tol["frames_root"], model=m, onnx=onnx[m], out=o, **w)
                names.append(Q.add_job(q, f"{pf('P22')}_tol_{m}_{w['id']}", f"cd {tol['cwd']}\n[ -s {o} ] || {cmd} 2>&1 | "
                                       f"grep -v W:onnxruntime | tail -3").stem)
    cl = cfg.get("closed_loop", {})
    pol = " ".join(f"--policy {m}={onnx[m]}" for m in mids)
    common = (f"--seeds {','.join(map(str, cl.get('seeds', [0, 1])))} --wm-chunk {cl.get('wm_chunk', 3)} {pol}")
    filt = "grep --line-buffered -v -e 'Corrupt JPEG' -e W:onnxruntime -e _pytree.py -e triton_flex -e AUTOTUNE"
    if "public_closed_loop" in sections:
        o = work / "out" / "public_closed_loop" / f"{tag}.json"
        o.parent.mkdir(parents=True, exist_ok=True)
        segs = " ".join(f"--segment {s}" for s in segment_names(cfg))
        names.append(Q.add_job(q, f"{pf('P30')}_pcl_{tag}", after_line(q, after) + f"""export HF_HUB_OFFLINE=1 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
{wait_gpu(cl.get('min_free_mib', 15500))}
cd {ENGINES}
{pyx} -I wm_closed_loop.py --segments {cfg['paths']['segments_dir']} --cereal {cfg['paths']['cereal']} {segs} \\
  --per-segment {cl.get('public', {}).get('per_segment', 1)} --cars ideal --conds nominal {common} --out {o} 2>&1 | {filt}""").stem)
    if truck_conds is None:
        truck_conds = [c for s, c in (("truck_closed_loop", "nominal"), ("truck_recovery", "disturb"),
                                                 ("truck_recovery", "offcentre")) if s in sections]
    if truck_conds:
        t = cl["truck"]
        (work / "out" / "truck_closed_loop").mkdir(parents=True, exist_ok=True)
        jobs = [(f"{pf('P32')}_tcl_{tag}", work / "out" / "truck_closed_loop" / f"{tag}.json",
                 {k: t[k] for k in ("code", "wide_camera", "drives", "starts", "night")})]
        if t.get("plugin_args_file"):   # more windows: one job per entry of the file, each its own plugin args
            jobs += [(f"{pf('P32')}_tcl_{tag}_c{i}", work / "out" / "truck_closed_loop" / f"{tag}_c{i}.json",
                      {"code": t["code"], **extra}) for i, extra in enumerate(json.loads(Path(t["plugin_args_file"]).read_text()))]
        for name, o, args in jobs:
            names.append(Q.add_job(q, name, after_line(q, after) + f"""export HF_HUB_OFFLINE=1 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
{wait_gpu(cl.get('min_free_mib', 15500))}
cd {ENGINES}
{pyx} -I wm_closed_loop.py --plugin {t['plugin']} --plugin-args {shlex.quote(json.dumps(args))} --cars g55 \\
  --conds {','.join(dict.fromkeys(truck_conds))} {common} --out {o} 2>&1 | {filt}""").stem)
    if "truck_recovery" in sections and brake_cfg(cfg).get("starts"):
        t, b = cl["truck"], brake_cfg(cfg)
        o = work / "out" / "truck_brake" / f"{tag}.json"
        o.parent.mkdir(parents=True, exist_ok=True)
        args = json.dumps({"code": t["code"], "wide_camera": t["wide_camera"], "drives": b.get("drives") or t["drives"],
                           "night": b.get("night", t["night"]), "starts": b["starts"]})
        names.append(Q.add_job(q, f"{pf('P34')}_tbr_{tag}", after_line(q, after) + f"""export HF_HUB_OFFLINE=1 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
{wait_gpu(cl.get('min_free_mib', 15500))}
cd {ENGINES}
{pyx} -I wm_closed_loop.py --plugin {t['plugin']} --plugin-args {shlex.quote(args)} --cars g55 \\
  --conds nominal {common} --out {o} 2>&1 | {filt}""").stem)
    return names


def ensure_runner(cfg):
    q = cfg.work / "queue"
    pidf = q / "runner.pid"
    if pidf.exists() and Path(f"/proc/{pidf.read_text().strip()}").exists():
        return
    q.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "geval.cli", "--config", cfg["_path"], "queue", "--idle-exit-s", "900"]
    launcher = cfg.get("queue", {}).get("launcher")
    if launcher:
        # e.g. systemd-run --user, so the runner outlives a timer-started watcher; it writes runner.pid itself
        subprocess.run(list(launcher) + cmd, check=True, cwd=str(Path(__file__).resolve().parents[1]))
        return
    log = open(q / "runner.out", "a")
    p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                         cwd=str(Path(__file__).resolve().parents[1]))
    pidf.write_text(str(p.pid))


def wait(cfg, names):
    while True:
        st = Q.job_states(cfg.work / "queue")
        left = [n for n in names if st.get(n) not in ("done", "failed")]
        if not left:
            return {n: st.get(n) for n in names}
        ensure_runner(cfg)
        time.sleep(60)


# ---------------------------------------------------------------- scoring
def episodes_from(dirpath: Path, policy: str):
    wins = []
    for f in sorted(dirpath.glob("*.json")):
        for w in json.loads(f.read_text())["windows"]:
            eps = [e for e in w["episodes"] if e["policy"] == policy]
            if eps:
                wins.append({**w, "episodes": eps})
    return wins


def score(cfg, mid: str, sections: list):
    work = cfg.work
    seeds = cfg.get("closed_loop", {}).get("seeds", [0, 1])
    if "public_open_loop" in sections:
        units = {}
        for seg in segment_names(cfg):
            for d in (work / "out" / "public_open_loop", Path(cfg.get("backfill", {}).get("route_npz_dir", "/nonexistent"))):
                p = d / f"{seg}__{mid}.npz"
                if p.exists():
                    units[seg], _ = openloop.route_unit(p, mid)
                    break
        if units:
            segs = results.segments()
            moving = sum(1 for u in units.values() if u.get("lat_mae") is not None)
            cars = len({segs[u]["car"] for u, m in units.items() if u in segs and m.get("lat_mae") is not None})
            store.save(work, "public_open_loop", mid, units,
                       {"condition": f"{len(units)} segments, {moving} with movement from {cars} cars."})
    if "truck_open_loop" in sections:
        backfill.truck_open_loop_one(cfg, mid, [work / "out" / "truck_open_loop"])
    if "public_closed_loop" in sections:
        wins = episodes_from(work / "out" / "public_closed_loop", mid)
        units = closedloop.window_units(wins, mid, "ideal", "nominal", seeds)
        units = {wid.split(":")[0]: m for wid, m in units.items()}
        if units:
            segs = results.segments()
            store.save(work, "public_closed_loop", mid, units, {"condition": f"{len(units)} segments from "
                       f"{len({segs[u]['car'] for u in units if u in segs})} cars, one 7.2 s window each."})
    wins = episodes_from(work / "out" / "truck_closed_loop", mid)
    if "truck_closed_loop" in sections:
        # the old suite windows of the backfilled models live only in the backfill files; keep them beside new runs
        old, old_night = backfill.model_units(cfg, mid, "nominal")
        new = closedloop.window_units(wins, mid, "g55", "nominal", seeds)
        units = {**old, **new}
        if units:
            night = len((old_night - new.keys()) | {f"{w['drive']}:{w['start']}" for w in wins
                                                   if f"{w['drive']}:{w['start']}" in new and w.get("night")})
            meta = {"condition": f"{len(units)} windows ({len(units) - night} day, {night} night)."}
            groups = backfill.truck_groups(cfg)
            if groups:
                meta["groups"] = {u: groups[u] for u in units if u in groups}
            store.save(work, "truck_closed_loop", mid, units, meta)
    if "truck_recovery" in sections:
        for test, cond in (("heading", "disturb"), ("offcentre", "offcentre")):
            units = closedloop.window_units(wins, mid, "g55", cond, seeds)
            if units:
                backfill.merge_recovery(cfg, mid, test, units, f"{S.RECOVERY_TESTS[test]} {describe(wins, units)}")
        bw = episodes_from(work / "out" / "truck_brake", mid)
        if bw:
            units = brake_units(cfg, bw, mid, seeds)
            if units:
                backfill.merge_recovery(cfg, mid, "brake", units, f"{S.RECOVERY_TESTS['brake']} {describe(bw, units)}")


def describe(wins, units):
    night = sum(1 for w in wins if f"{w['drive']}:{w['start']}" in units and w.get("night"))
    return f"{len(units)} windows ({len(units) - night} day, {night} night)."


def brake_cfg(cfg) -> dict:
    """[closed_loop.truck_brake], merged with the JSON its `file` names (written by the private window picker)."""
    b = dict(cfg["closed_loop"].get("truck_brake", {}))
    if b.get("file") and Path(b["file"]).exists():
        b.update(json.loads(Path(b["file"]).read_text()))
    return b


def brake_units(cfg, wins, mid, seeds):
    """Braking lead: how far the model ran ahead of the recorded car, and the gap that leaves.
    gap(t) = recorded lead distance at t (from the config's per-window lead series) - ahead(t)."""
    leads = brake_cfg(cfg).get("lead_m", {})
    out = {}
    for w in wins:
        wid = f"{w['drive']}:{w['start']}"
        rows = [closedloop.episode_metrics(e, w["ref"], w["rec_speed"]) for e in w["episodes"]
                if e["car"] == "g55" and e["cond"] == "nominal" and e["seed"] in seeds]
        if not rows:
            continue
        m = {k: float(sum(r[k] for r in rows) / len(rows)) for k in ("along_2s", "along_4s", "rms_speed_err", "absx_4s")}
        lead = leads.get(wid)
        if lead:
            gaps = [min(lead[i] - r["along_series"][i] for i in range(min(len(lead), len(r["along_series"])))) for r in rows]
            m["gap_min"] = float(sum(gaps) / len(gaps))
        out[f"brake:{wid}"] = m
    return out


def run(cfg, ref: str, sections: list, wait_for: bool = True):
    mid = resolve(cfg, ref)
    print(json.dumps({"model": mid}), flush=True)
    names = queue_jobs(cfg, [mid], sections)
    ensure_runner(cfg)
    if not wait_for:
        print(json.dumps({"queued": names}), flush=True)
        return None
    states = wait(cfg, names)
    failed = [n for n, s in states.items() if s == "failed"]
    if failed:
        print(json.dumps({"failed": failed}), flush=True)
    score(cfg, mid, sections)
    return mid
