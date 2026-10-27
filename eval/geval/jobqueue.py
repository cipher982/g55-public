"""A shared-GPU job queue: shell-script jobs run in name order under one host-wide GPU lock.

Jobs are shell scripts in <queue>/jobs/, started in name order (prefix P10_, P20_, ... for
priority). Up to `slots` run at once. The runner holds the GPU lock (an flock on a shared file
that every GPU user on the host takes) while it has work, and every `share_s` seconds stops
starting jobs, lets the running ones finish and releases the lock briefly so other GPU work can
get in. Jobs run pinned to `cpu_cores` (taskset) when given. Each job's state is a marker beside it (.running, .done, .failed); output goes to
<queue>/logs/<job>.log. New jobs can be added at any time; the runner exits when
<queue>/<stop_file> exists (the stop file is never deleted by the runner).

A job left .running by an earlier runner is adopted if its process is alive; if it is gone its
marker is renamed aside and the job runs again (jobs skip work whose output exists). Every job
writes its exit status to <job>.sh.rc on exit, which is how an adopted job's outcome is known; an
adopted job with no status file is marked failed.

Consolidated from the 2026-10-27 deep dive's gpu_queue_v3.py.
"""
from __future__ import annotations

import fcntl
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


def event(q: Path, msg: str) -> None:
    with open(q / "events.log", "a") as f:
        f.write(f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} {msg}\n")


def state(job: Path):
    for s in ("done", "failed", "running"):
        if job.with_suffix(f".{s}").exists():
            return s
    return None


def pids_of(job: Path):
    out = subprocess.run(["pgrep", "-f", f"bash {job}$"], capture_output=True, text=True).stdout.split()
    return [int(p) for p in out]


def alive(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists()


def add_job(q: Path, name: str, body: str) -> Path:
    """Write <q>/jobs/<name>.sh unless it exists; returns its path."""
    (q / "jobs").mkdir(parents=True, exist_ok=True)
    p = q / "jobs" / f"{name}.sh"
    if not p.exists():
        p.write_text("#!/bin/bash\ntrap 'echo $? > \"$0.rc\"' EXIT\nset -euo pipefail\n" + body.rstrip() + "\n")
    return p


def job_states(q: Path) -> dict:
    return {j.stem: state(j) for j in sorted((q / "jobs").glob("*.sh"))}


def run(q: Path, lock_path: str, slots: int = 2, share_s: float = 2700, stop_file: str = "STOP", idle_exit_s: float = 0,
        cpu_cores: str | None = None):
    jobs, logs = q / "jobs", q / "logs"
    jobs.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    running = {}
    for job in sorted(jobs.glob("*.sh")):
        if state(job) == "running":
            pids = pids_of(job)
            if pids:
                running[job] = (None, pids[0], time.time(), None)
                event(q, f"adopt {job.stem} pid={pids[0]}")
            else:
                job.with_suffix(".running").rename(job.with_suffix(f".stale.{int(time.time())}"))
                event(q, f"requeue {job.stem} (stale marker)")

    def reap():
        for job, (proc, pid, t0, log) in list(running.items()):
            if proc is not None:
                rc = proc.poll()
                if rc is None:
                    continue
                log.close()
                ok, note = rc == 0, f"rc={rc} seconds={time.time() - t0:.0f}\n"
            else:
                if alive(pid):
                    continue
                rcf = job.with_name(job.name + ".rc")
                rc = rcf.read_text().strip() if rcf.exists() else "?"
                ok = rc == "0"
                note = f"rc={rc} (adopted from an earlier runner; see the job's log)\n"
            job.with_suffix(".running").rename(job.with_suffix(".done" if ok else ".failed"))
            with open(job.with_suffix(".done" if ok else ".failed"), "a") as f:
                f.write(note)
            event(q, f"{'done' if ok else 'FAILED'} {job.stem} rc={rc} {time.time() - t0:.0f}s")
            del running[job]

    def pending():
        return [j for j in sorted(jobs.glob("*.sh")) if state(j) is None]

    event(q, f"runner start slots={slots} share_s={share_s:.0f} lock={lock_path}")
    lock = open(lock_path, "a")
    idle_since = time.time()
    while not (q / stop_file).exists():
        reap()
        if not pending() and not running:
            if idle_exit_s and time.time() - idle_since > idle_exit_s:
                break
            time.sleep(15)
            continue
        fcntl.flock(lock, fcntl.LOCK_EX)
        held = time.time()
        event(q, "lock taken")
        while not (q / stop_file).exists():
            reap()
            todo = pending()
            sharing = time.time() - held > share_s
            while todo and len(running) < slots and not sharing:
                job = todo.pop(0)
                job.with_suffix(".running").write_text(f"{datetime.now(timezone.utc).isoformat()}\n")
                log = open(logs / f"{job.stem}.log", "a")
                cmd = (["taskset", "-c", cpu_cores] if cpu_cores else []) + ["bash", str(job)]
                proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
                running[job] = (proc, proc.pid, time.time(), log)
                event(q, f"start {job.stem}")
            if not running and (sharing or not todo):
                break
            time.sleep(5)
        fcntl.flock(lock, fcntl.LOCK_UN)
        event(q, "lock released")
        idle_since = time.time()
        time.sleep(10)
    event(q, "runner stop")
