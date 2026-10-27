"""geval watch: find new comma driving models, queue the suite for each, score them when done.

Sources (read only, anonymous):
  Hugging Face  the model repo in data/models.json (`hf_repo`): every `<uuid>/<ckpt>/big_*.onnx`
                not yet scored or queued becomes `hf:<uuid>/<ckpt>`. One API call.
  GitHub        commaai/openpilot branches (master included). A branch whose head moved, or a new
                branch, is compared with its old head (or with master) and kept when the change
                touches the model directory. Its big-model ONNX, when the commit still ships one
                in LFS, is queued as `openpilot:<sha>`; otherwise the commit messages and branch
                name are searched for a checkpoint id (`<uuid8>/<ckpt>`, `<uuid8>-<ckpt>`), which
                the Hugging Face pass picks up once comma uploads it. Calls per pass are capped
                (`github_calls`, anonymous limit 60 per hour); the first pass only records heads.

Each pass, in order: discover, queue (`geval submit` jobs at their normal names, so the shared
runner starts them in its usual order under the host's GPU lock; nothing here touches the GPU),
then score every queued model whose jobs have all finished and rewrite its results file. State
and a log live in <work>/watch/. Nothing is posted anywhere.

Optionally a pass then commits newly scored models' results to a repository checkout and pushes
its main (publish_repo; files matching a publish_block pattern are never copied).

Settings, [watch] in the private TOML: sections (default: public and truck open loop, truck
closed loop, truck recovery), github_calls (default 25), publish_repo, publish_block, author.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import jobqueue as Q, results, store

GH = "https://api.github.com/repos/commaai/openpilot"
HF_FILE = re.compile(r"^([0-9a-f]{8})-[0-9a-f-]{27}/(\d+)/big_[^/]*\.onnx$")
CKPT_TEXT = re.compile(r"\b([0-9a-f]{8})(?:-[0-9a-f-]{27})?[/_-](\d{4,6})\b")
MODEL_DIRS = ("selfdrive/modeld/models/", "openpilot/selfdrive/modeld/models/")
DEFAULT_SECTIONS = ["public_open_loop", "truck_open_loop", "truck_closed_loop", "truck_recovery"]
UA = {"User-Agent": "geval-watch (read-only)", "Accept": "application/vnd.github+json"}


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Watch:
    def __init__(self, cfg, dry_run=False):
        self.cfg, self.dry = cfg, dry_run
        self.dir = cfg.work / "watch"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.dir / "state.json"
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else \
            {"branches": {}, "models": {}, "pending_ckpts": {}, "seen_commits": {}}
        w = cfg.get("watch", {})
        self.sections = w.get("sections", DEFAULT_SECTIONS)
        self.budget = int(w.get("github_calls", 25))

    # ------------------------------------------------------------ io
    def log(self, msg: str):
        line = f"{now()} {msg}"
        print(line, flush=True)
        if not self.dry:
            with open(self.dir / "events.log", "a") as f:
                f.write(line + "\n")

    def save(self):
        if not self.dry:
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, indent=1))
            tmp.replace(self.state_path)

    def gh(self, path: str):
        if self.budget <= 0:
            raise RuntimeError("github call budget spent for this pass")
        self.budget -= 1
        req = urllib.request.Request(GH + path, headers=UA)
        with urllib.request.urlopen(req, timeout=60) as r:
            left = int(r.headers.get("X-RateLimit-Remaining", "60"))
            body = json.loads(r.read())
        if left < 5:
            self.budget = 0
        return body

    # ------------------------------------------------------------ discovery
    def known(self) -> set:
        return set(results.registry(self.cfg.work)) | set(self.state["models"])

    def hf_candidates(self) -> list:
        repo = json.loads((results.DATA / "models.json").read_text())["hf_repo"]
        req = urllib.request.Request(f"https://huggingface.co/api/models/{repo}", headers={"User-Agent": UA["User-Agent"]})
        with urllib.request.urlopen(req, timeout=60) as r:
            meta = json.loads(r.read())
        out = []
        for s in meta.get("siblings", []):
            m = HF_FILE.match(s["rfilename"])
            if m:
                out.append((f"{m.group(1)}_{m.group(2)}", f"hf:{m.group(1)}/{m.group(2)}"))
        self.state["hf_sha"] = meta.get("sha")
        return sorted(set(out))

    def branch_heads(self) -> dict:
        heads, page = {}, 1
        while True:
            rows = self.gh(f"/branches?per_page=100&page={page}")
            heads.update({r["name"]: r["commit"]["sha"] for r in rows})
            if len(rows) < 100:
                return heads
            page += 1

    def github_candidates(self) -> list:
        """[(model id or None, ref or None, note)] for branch changes that touch the model directory."""
        heads = self.branch_heads()
        old = self.state["branches"]
        first = not old
        out = []
        for name, sha in sorted(heads.items(), key=lambda kv: kv[0] != "master"):
            if first or old.get(name) == sha:
                continue
            base = old.get(name) or "master"
            try:
                cmp = self.gh(f"/compare/{base}...{sha}")
            except RuntimeError:
                self.log(f"github budget spent; {name} left for the next pass")
                heads[name] = old.get(name)      # not checked yet: keep the old head
                continue
            except urllib.error.HTTPError as e:
                self.log(f"github compare {name} {base[:10]}...{sha[:10]}: HTTP {e.code}")
                continue
            files = [f["filename"] for f in cmp.get("files", [])]
            model_files = [f for f in files if f.startswith(MODEL_DIRS)]
            if not model_files:
                continue
            text = name + "\n" + "\n".join(c["commit"]["message"] for c in cmp.get("commits", []))
            ckpts = sorted({f"{a}_{b}" for a, b in CKPT_TEXT.findall(text)})
            onnx = [f for f in model_files if f.endswith("big_driving_supercombo.onnx")]
            note = f"branch {name} {sha[:10]}: {', '.join(model_files[:4])}" + (f"; checkpoints named {ckpts}" if ckpts else "")
            self.log(f"model change on {note}")
            if onnx:
                out.append((None, f"openpilot:{sha}", note))
            for c in ckpts:
                self.state["pending_ckpts"].setdefault(c, {"first_seen": now(), "where": note})
            self.state["seen_commits"][sha] = note
        self.state["branches"] = {k: v for k, v in heads.items() if v}
        if first:
            self.log(f"first pass: recorded {len(heads)} branch heads, nothing compared")
        return out

    # ------------------------------------------------------------ queue and score
    def submit(self, mid_hint, ref, why):
        from . import run as R
        if self.dry:
            self.log(f"would queue {ref} ({why})")
            return
        try:
            mid = R.resolve(self.cfg, ref)
        except SystemExit as e:
            self.log(f"cannot resolve {ref}: {str(e)[:300]}")
            if mid_hint:
                self.state["models"][mid_hint] = {"ref": ref, "status": "unresolvable", "note": str(e)[:300], "at": now()}
            return
        if mid in self.state["models"] or any(store.load(self.cfg.work, s, mid) for s in self.sections):
            self.log(f"{ref} is {mid}, already queued or scored")
            return
        jobs = R.queue_jobs(self.cfg, [mid], self.sections)
        self.state["models"][mid] = {"ref": ref, "why": why, "status": "queued", "jobs": jobs,
                                     "sections": self.sections, "queued_at": now()}
        self.state["pending_ckpts"].pop(mid, None)
        self.save()
        self.log(f"queued {mid} from {ref}: {len(jobs)} jobs")
        R.ensure_runner(self.cfg)

    def score_finished(self):
        from . import run as R
        from .cli import cmd_build
        st = Q.job_states(self.cfg.work / "queue")
        for mid, m in self.state["models"].items():
            if m.get("status") != "queued":
                continue
            left = [j for j in m["jobs"] if st.get(j) not in ("done", "failed")]
            if left:
                continue
            failed = [j for j in m["jobs"] if st.get(j) == "failed"]
            if self.dry:
                self.log(f"would score {mid}")
                continue
            R.score(self.cfg, mid, m["sections"])
            cmd_build(self.cfg, [mid])
            have = [s for s in m["sections"] if store.load(self.cfg.work, s, mid)]
            m.update(status="scored", scored_at=now(), failed_jobs=failed, scored_sections=have)
            self.log(f"scored {mid}: {have}" + (f"; failed jobs {failed}" if failed else ""))

    def publish(self):
        """Commit each scored, unpublished model's results file and public per-unit CSVs to the
        repository checkout `[watch] publish_repo` and push its main. A file matching any of the
        private patterns `[watch] publish_block` is never copied. Commit author: `[watch] author`."""
        import re as _re
        import shutil
        import subprocess
        w = self.cfg.get("watch", {})
        repo = w.get("publish_repo")
        todo = [mid for mid, m in self.state["models"].items() if m.get("status") == "scored" and not m.get("published")]
        if not repo or not todo or self.dry:
            return
        from .cli import out_dirs
        res, pu = out_dirs(self.cfg)
        block = [_re.compile(x) for x in w.get("publish_block", [])]
        repo = Path(repo)

        def git(*args, **kw):
            return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, **kw)

        git("fetch", "-q", "origin", "main")
        git("checkout", "-q", "-B", "main", "origin/main")
        paths = []
        for mid in todo:
            files = [res / f"{mid}.json"] + sorted((pu / mid).glob("*.csv"))
            hits = [str(f.name) for f in files if f.exists() and any(b.search(f.read_text()) for b in block)]
            if hits:
                self.state["models"][mid]["published"] = False
                self.log(f"not publishing {mid}: private pattern in {hits}")
                continue
            for f in files:
                if not f.exists():
                    continue
                dst = repo / "eval" / ("results" if f.suffix == ".json" else f"per_unit/{mid}") / f.name
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(f, dst)
                paths.append(str(dst.relative_to(repo)))
            self.state["models"][mid]["published"] = True
        if not paths:
            return
        git("add", "--", *paths)
        if not git("diff", "--cached", "--name-only").stdout.strip():
            self.log("publish: nothing changed")
            return
        a = w.get("author", {})
        env = {**__import__("os").environ, "GIT_AUTHOR_NAME": a.get("name", "geval watch"),
               "GIT_AUTHOR_EMAIL": a.get("email", "geval-watch@localhost"),
               "GIT_COMMITTER_NAME": a.get("name", "geval watch"), "GIT_COMMITTER_EMAIL": a.get("email", "geval-watch@localhost")}
        names = ", ".join(m for m in todo if self.state["models"][m].get("published"))
        git("commit", "-q", "-m", f"results: {names} (release watcher)", env=env)
        try:
            git("push", "-q", "origin", "HEAD:main")
            self.log(f"published {names}: {len(paths)} files, {git('rev-parse', '--short=12', 'HEAD').stdout.strip()}")
        except subprocess.CalledProcessError as e:
            for m in todo:
                self.state["models"][m]["published"] = False      # retried next pass on a fresh main
            self.log(f"publish push failed: {e.stderr.strip()[:300]}")

    def run_pass(self):
        self.log("pass start")
        known = self.known()
        try:
            cands = self.hf_candidates()
            if "hf_seen" not in self.state:     # first pass: what is there now is the baseline, not news
                self.state["hf_seen"] = [mid for mid, _ in cands]
                self.log(f"first pass: recorded {len(cands)} Hugging Face models, nothing queued "
                         f"({len([m for m, _ in cands if m not in known])} not in the first edition)")
            for mid, ref in cands:
                if mid not in known and mid not in self.state["hf_seen"]:
                    self.submit(mid, ref, "new on Hugging Face")
                    known.add(mid)
                if mid not in self.state["hf_seen"]:
                    self.state["hf_seen"].append(mid)
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            self.log(f"hugging face check failed: {e}")
        try:
            for mid, ref, why in self.github_candidates():
                self.submit(mid, ref, why)
        except (urllib.error.URLError, TimeoutError, ValueError, RuntimeError) as e:
            self.log(f"github check failed: {e}")
        waiting = sorted(c for c in self.state["pending_ckpts"] if c not in self.known())
        if waiting:
            self.log(f"named on openpilot branches, not yet on Hugging Face: {waiting}")
        self.score_finished()
        try:
            self.publish()
        except Exception as e:                  # a publish problem never stops discovery or scoring
            self.log(f"publish failed: {type(e).__name__}: {str(e)[:300]}")
        self.state["last_pass"] = now()
        self.save()
        self.log("pass end")


def main(cfg, once=True, every_s=1200, dry_run=False):
    w = Watch(cfg, dry_run)
    while True:
        w.run_pass()
        if once:
            return
        time.sleep(every_s)
        w = Watch(cfg, dry_run)
