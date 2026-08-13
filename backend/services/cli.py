"""In-UI command line.

Lets a developer run the harness's own CLI subcommands from the UI and watch the
output stream — the same commands they could run in a terminal. Execution is
restricted to ``python harness.py <subcommand>`` with an allowlisted first token
(no arbitrary shell), and the server only binds to localhost.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import threading
import time
from datetime import datetime
from typing import Optional

from .. import engine

harness = engine.harness

# Harness subcommands a dev may launch from the UI.
ALLOWED = {"setup", "doctor", "run", "judge", "report", "all"}

# The canonical command list shown in the UI (with copy-paste forms).
REFERENCE = [
    {"cmd": "setup", "desc": "Build the per-arm MCP configs from ~/.claude.json."},
    {"cmd": "doctor", "desc": "Verify the Bito MCP is live and list indexed repos."},
    {"cmd": "run --arm A --prompts prompts.json", "desc": "Run one arm (A/B/C) over the prompt set."},
    {"cmd": "judge --prompts prompts.json", "desc": "Blind-judge the three answers per prompt."},
    {"cmd": "report", "desc": "Write reports/summary.md and summary.csv."},
    {"cmd": "all --prompts prompts.json", "desc": "Full pipeline: setup → doctor → A/B/C → judge → report."},
]


class CliJob:
    def __init__(self, job_id: str, args: list[str]):
        self.id = job_id
        self.args = args
        self.lines: list[str] = []
        self.status = "running"          # running | done | error
        self.returncode: Optional[int] = None
        self.proc: Optional[subprocess.Popen] = None
        self.lock = threading.Lock()

    def append(self, line: str) -> None:
        with self.lock:
            self.lines.append(line)


JOBS: dict[str, CliJob] = {}


def _python() -> str:
    return sys.executable or "python3"


def exec_command(arg_string: str) -> dict:
    """Validate + launch a harness subcommand. Returns {id, argv}."""
    try:
        parts = shlex.split(arg_string.strip(), posix=(os.name != "nt"))
    except ValueError as e:
        raise ValueError(f"Couldn't parse the command: {e}")
    # Tolerate a leading "python harness.py" / "harness.py" if the dev pastes it.
    while parts and parts[0] in (_python(), "python", "python3", "harness.py", "./harness.py", "py"):
        parts.pop(0)
    if parts and parts[0] == "harness.py":
        parts.pop(0)
    if not parts:
        raise ValueError("Enter a command, e.g. `doctor` or `report`.")
    if parts[0] not in ALLOWED:
        raise ValueError(f"Only these subcommands are allowed from the UI: {', '.join(sorted(ALLOWED))}.")

    argv = [_python(), str(harness.ROOT / "harness.py"), *parts]
    job_id = "cli" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    job = CliJob(job_id, argv)
    JOBS[job_id] = job
    job.append(f"$ python harness.py {' '.join(parts)}")
    threading.Thread(target=_run, args=(job,), daemon=True).start()
    return {"id": job_id, "argv": argv}


def _run(job: CliJob) -> None:
    env = {**os.environ, "CI": "1", "PYTHONUNBUFFERED": "1"}  # CI=1 skips confirmations
    try:
        proc = subprocess.Popen(
            job.args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(harness.ROOT),
            env=env,
        )
        job.proc = proc
        assert proc.stdout is not None
        for line in proc.stdout:
            job.append(line.rstrip("\n"))
        job.returncode = proc.wait()
        job.status = "done" if job.returncode == 0 else "error"
        job.append(f"\n[exit code {job.returncode}]")
    except Exception as e:
        job.status = "error"
        job.append(f"[failed to launch: {type(e).__name__}: {e}]")


def stop(job_id: str) -> None:
    job = JOBS.get(job_id)
    if not job or not job.proc or job.proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            # terminate() only kills the launcher; taskkill /T kills the whole tree.
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(job.proc.pid)],
                capture_output=True, timeout=10,
            )
        else:
            job.proc.terminate()
    except Exception:
        pass


def get(job_id: str) -> Optional[CliJob]:
    return JOBS.get(job_id)
