"""Benchmark runner: the A/B Testing engine.

Runs a batch = (tool × arms × prompts × repeats) as a background thread, writing
each run into the harness ``runs`` table (namespaced by batch so batches/tools
don't collide) and streaming progress events the UI subscribes to over SSE.
After the runs, an optional blind judge scores A/B/C per prompt — reusing the
harness's judge prompt + parser primitives.
"""

from __future__ import annotations

import json
import os
import random
import signal
import subprocess
import threading
import time
from datetime import datetime
from typing import Any, Callable, Optional

from .. import engine
from ..adapters.registry import get_adapter
from . import prompts as prompts_svc

harness = engine.harness

# Benchmark mode → per-run agentic-turn budget (unless caller overrides).
MODE_TURNS = {"quick": 40, "standard": harness.DEFAULT_MAX_TURNS, "thorough": 320}


# ---------------------------------------------------------------------------
# In-memory job registry
# ---------------------------------------------------------------------------
class Job:
    def __init__(self, batch_id: str, config: dict):
        self.batch_id = batch_id
        self.config = config
        self.status = "queued"      # queued|running|stopped|done|error
        self.events: list[dict] = []
        self.progress = 0
        self.total = 0
        self.error: Optional[str] = None
        self.stop_flag = threading.Event()
        self.active_proc: Optional[subprocess.Popen] = None
        self.thread: Optional[threading.Thread] = None
        self.lock = threading.Lock()

    def emit(self, etype: str, **data) -> None:
        ev = {"type": etype, "ts": time.time(), **data}
        with self.lock:
            self.events.append(ev)


JOBS: dict[str, Job] = {}


def stop_all() -> None:
    """Signal every in-flight job to stop and kill its active subprocess. Called on
    server shutdown (Ctrl+C) so we don't orphan claude/MCP child processes."""
    for job in list(JOBS.values()):
        if job.status in ("queued", "running"):
            try:
                job.stop_flag.set()
                _kill_active(job)
            except Exception:
                pass


def _prune_jobs(keep_terminal: int = 30) -> None:
    """Bound JOBS memory: keep every live job plus the most recent `keep_terminal`
    finished ones (enough for SSE reconnect/replay), and drop older finished jobs.
    Without this, JOBS grew unbounded — each finished Job pins its full event list."""
    terminal = [bid for bid, j in JOBS.items()  # dict is insertion-ordered: oldest first
                if j.status not in ("queued", "running")]
    for bid in terminal[: max(0, len(terminal) - keep_terminal)]:
        JOBS.pop(bid, None)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _insert_run(conn, *, arm, prompt_id, base_pid, prompt, res, tool, repo, category, batch_id):
    conn.execute(
        """INSERT INTO runs (arm, prompt_id, prompt, response, duration_ms, duration_api_ms,
            num_turns, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
            total_cost_usd, model, tool_calls_json, session_id, exit_code, error,
            bito_violation, started_at, tool, repo, category, batch_id, base_prompt_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(arm, prompt_id) DO UPDATE SET
             prompt=excluded.prompt, response=excluded.response,
             duration_ms=excluded.duration_ms, duration_api_ms=excluded.duration_api_ms,
             num_turns=excluded.num_turns, input_tokens=excluded.input_tokens,
             output_tokens=excluded.output_tokens, cache_read_tokens=excluded.cache_read_tokens,
             cache_creation_tokens=excluded.cache_creation_tokens,
             total_cost_usd=excluded.total_cost_usd, model=excluded.model,
             tool_calls_json=excluded.tool_calls_json, session_id=excluded.session_id,
             exit_code=excluded.exit_code, error=excluded.error,
             bito_violation=excluded.bito_violation, started_at=excluded.started_at,
             tool=excluded.tool, repo=excluded.repo, category=excluded.category,
             batch_id=excluded.batch_id, base_prompt_id=excluded.base_prompt_id""",
        (
            arm, prompt_id, prompt, res.response, res.duration_ms, res.duration_api_ms,
            res.num_turns, res.input_tokens, res.output_tokens, res.cache_read_tokens,
            res.cache_creation_tokens, res.total_cost_usd, res.model,
            json.dumps(res.tool_calls or []), res.session_id, res.exit_code, res.error,
            _bito_violation(arm, res.tool_calls), _now(), tool, repo, category, batch_id, base_pid,
        ),
    )
    conn.commit()


def _bito_violation(arm: str, tool_calls) -> int:
    reason = harness.skill_policy_violation(arm, tool_calls)
    return 1 if reason else 0


def _save_batch(conn, job: Job) -> None:
    c = job.config
    conn.execute(
        """INSERT INTO batches (batch_id, label, tool, repo, prompt_set, mode, arms, n_runs,
              model, judge_model, status, progress, total, error, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(batch_id) DO UPDATE SET
             status=excluded.status, progress=excluded.progress, total=excluded.total,
             error=excluded.error, updated_at=excluded.updated_at""",
        (
            job.batch_id, c.get("label"), c["tool"], c.get("repo"), c.get("prompt_set"),
            c.get("mode"), json.dumps(c["arms"]), c.get("n_runs", 1), c.get("model"),
            c.get("judge_model"), job.status, job.progress, job.total, job.error,
            c.get("_created_at", _now()), _now(),
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def _resolve_arm_tools(config: dict, arms: list[str]) -> dict[str, str]:
    """Build {arm: tool_id} for every requested arm.

    Per-arm overrides in config["arm_tools"] take priority over the global
    config["tool"].  Returns a mapping that covers every arm in `arms`.
    """
    default_tool = config.get("tool", "claude")
    overrides: dict = config.get("arm_tools") or {}
    return {arm: overrides.get(arm, default_tool) for arm in arms}


def start_batch(config: dict) -> dict:
    """Validate, create the batch, and launch the background run thread."""
    tool = config.get("tool", "claude")
    # Validate the global default tool.
    adapter = get_adapter(tool)
    if not adapter:
        raise ValueError(f"Unknown tool '{tool}'.")
    installed, _v, detail = adapter.detect()
    if not installed:
        raise ValueError(f"{adapter.name} is not installed: {detail}")
    if not adapter.supports_headless:
        raise ValueError(
            f"{adapter.name} can't be driven automatically ({adapter.headless_note}). "
            "Pick a tool that supports benchmark runs (e.g. Claude Code, Cursor)."
        )

    # Validate any per-arm tool overrides too, so errors surface before the run starts.
    arm_tools_raw: dict = config.get("arm_tools") or {}
    for arm_key, arm_tool_id in arm_tools_raw.items():
        if arm_tool_id == tool:
            continue  # already validated above
        arm_adapter = get_adapter(arm_tool_id)
        if not arm_adapter:
            raise ValueError(f"Unknown tool '{arm_tool_id}' for arm {arm_key}.")
        arm_inst, _, arm_det = arm_adapter.detect()
        if not arm_inst:
            raise ValueError(
                f"Arm {arm_key} tool '{arm_adapter.name}' is not installed/authenticated: {arm_det}"
            )
        if not arm_adapter.supports_headless:
            raise ValueError(
                f"Arm {arm_key} tool '{arm_adapter.name}' does not support headless runs "
                f"({arm_adapter.headless_note})."
            )

    arms = [a.upper() for a in config.get("arms", ["A", "B", "C"])]
    if any(a not in ("A", "B", "C") for a in arms):
        raise ValueError("Arms must be a subset of A, B, C.")

    prompts = prompts_svc.get_prompts_for_run(config.get("prompt_set"))
    if not prompts:
        raise ValueError("No prompts to run. Add prompts or pick a prompt set first.")

    # Per-arm MCP configs are built automatically (no manual "build" step). If the
    # Bito-enabled config is missing and arms B/C are requested, build it now.
    needs_bito = "B" in arms or "C" in arms
    from . import bito_oauth
    if needs_bito:
        # Guarantee arms B/C actually reach Bito: refresh the OAuth token and re-stamp
        # it onto the arm-bito config from the app's own token store. This makes the
        # run independent of ~/.claude.json, which the claude CLI rewrites and strips —
        # so Bito can't silently drop mid-benchmark and degrade B/C to baseline.
        # Build the config first if it's missing, then (re)stamp auth.
        if not engine.harness.read_mcp_servers(engine.CONFIGS / "mcp-arm-bito.json"):
            from . import detection
            try:
                detection.build_arm_configs()
            except ValueError as e:
                raise ValueError(
                    "Couldn't prepare the Bito benchmark configuration automatically: "
                    f"{e} Connect Bito on the Setup tab first."
                )
        # Make the bearer usable, covering BOTH auth modes: an explicit static token
        # (config["bito_token"]) is stamped as-is; otherwise an OAuth token is refreshed
        # and re-stamped; otherwise the existing config bearer is left in place. This
        # makes the run independent of ~/.claude.json, which the claude CLI rewrites.
        verdict = bito_oauth.ensure_bito_ready(config.get("bito_token"))
        # Fail LOUD: if arms B/C are requested but the config the headless run will read
        # carries no usable Bito bearer, refuse to start. Producing a "B/C lost" result
        # that is really just Bito auth being down is worse than no result at all.
        if not verdict.get("ok"):
            detail = verdict.get("detail", "Bito is not authenticated.")
            hint = (
                f" (token refresh also failed: {verdict['refresh_error']})"
                if verdict.get("refresh_error") else ""
            )
            raise ValueError(
                f"Bito is not ready for arms B/C: {detail}{hint} "
                "Connect/sign in to Bito on the Setup tab, then re-run. "
                "Refusing to start so the results don't misreport B/C as losing when "
                "they simply couldn't reach AI Architect."
            )

    # Workspace mode: "fresh-clone" (default) = empty cwd, arms clone source; "local-repo"
    # = run against a copy of the user's local repo (incl. uncommitted changes), so arms
    # can make local code changes and be compared with vs without AI Architect.
    workspace_mode = config.get("workspace_mode", "fresh-clone")
    local_repo_path = config.get("local_repo_path")
    if workspace_mode == "local-repo":
        from pathlib import Path as _Path
        if not local_repo_path or not _Path(local_repo_path).expanduser().is_dir():
            raise ValueError(
                "workspace_mode is 'local-repo' but local_repo_path is missing or not a "
                f"directory: {local_repo_path!r}. Point it at the repo checkout to test."
            )

    batch_id = "b" + datetime.now().strftime("%Y%m%d-%H%M%S")
    n_runs = max(1, int(config.get("n_runs", 1)))
    mode = config.get("mode", "standard")
    arm_tools_resolved = _resolve_arm_tools(config, arms)
    config = {
        **config,
        "tool": tool,
        "arm_tools": arm_tools_resolved,   # {arm: tool_id} for every arm
        "arms": arms,
        "n_runs": n_runs,
        "mode": mode,
        "workspace_mode": workspace_mode,
        "local_repo_path": local_repo_path,
        "model": config.get("model") or harness.DEFAULT_MODEL,
        "max_turns": config.get("max_turns") or MODE_TURNS.get(mode, harness.DEFAULT_MAX_TURNS),
        "_created_at": _now(),
        "_prompts": prompts,
    }

    job = Job(batch_id, config)
    job.total = len(arms) * len(prompts) * n_runs
    JOBS[batch_id] = job

    conn = engine.connect()
    _save_batch(conn, job)
    conn.close()

    job.thread = threading.Thread(target=_run_loop, args=(job,), daemon=True)
    job.thread.start()
    return {"batch_id": batch_id, "total": job.total, "arms": arms, "n_prompts": len(prompts)}


def _arm_config(tool: str, arm: str):
    return engine.CONFIGS / ("mcp-arm-a.json" if arm == "A" else "mcp-arm-bito.json")


def _run_loop(job: Job) -> None:
    c = job.config
    # Build a per-arm adapter cache so we don't re-instantiate on every prompt.
    arm_tools: dict = c.get("arm_tools") or {}
    _arm_adapters: dict = {}
    for _arm in c["arms"]:
        _tid = arm_tools.get(_arm, c["tool"])
        _a = get_adapter(_tid)
        if _a is None:
            _a = get_adapter(c["tool"])  # fallback to global default
        _arm_adapters[_arm] = _a

    prompts = c["_prompts"]
    conn = engine.connect()
    job.status = "running"
    _save_batch(conn, job)
    job.emit("batch_start", batch_id=job.batch_id, total=job.total, arms=c["arms"],
             n_prompts=len(prompts), tool=c["tool"])

    # Bito skills live in ~/.claude/skills/ (installed by Bito's one-command installer);
    # there's no plugin to refresh before a run.
    # Resolve the Bito skills directory once per batch (not per arm/prompt).
    # Rules: Claude Code skills take priority when present; fall back to
    # ~/.copilot/skills/ when the selected tool is copilot and Claude is absent.
    from . import skills as _skills_svc
    from . import bito_oauth
    from pathlib import Path as _Path
    _sk = _skills_svc.skill_status(tool_id=c["tool"])
    _skills_dir: _Path = _Path(_sk["skills_dir"])

    try:
        for arm in c["arms"]:
            adapter = _arm_adapters[arm]
            arm_tool_id = (c.get("arm_tools") or {}).get(arm, c["tool"])
            mcp_config = _arm_config(arm_tool_id, arm)
            for item in prompts:
                pid = item["id"]
                for r in range(c["n_runs"]):
                    if job.stop_flag.is_set():
                        raise _Stopped()
                    suffix = f"#r{r+1}" if c["n_runs"] > 1 else ""
                    stored_pid = f"{job.batch_id}:{pid}{suffix}"
                    label = f"{pid}{suffix}"
                    job.emit("run_start", arm=arm, prompt_id=pid, label=label,
                             index=job.progress + 1, total=job.total,
                             runner=arm_tool_id)

                    # Keep Bito auth fresh for arms B/C right before each run: refresh the
                    # OAuth token (if any) and re-stamp it onto the config THIS CLI reads —
                    # mcp-arm-bito.json for Claude, or the live ~/.copilot/mcp-config.json for
                    # Copilot (read directly each run, so a stale/expired bearer or a CLI
                    # rewrite would silently drop Bito). Static-token setups are a no-op.
                    if arm in ("B", "C"):
                        try:
                            bito_oauth.ensure_run_bito_authed(arm_tool_id)
                        except Exception:
                            pass

                    eff_prompt = harness.effective_prompt(
                        arm, item["prompt"],
                        workspace_mode=c.get("workspace_mode", "fresh-clone"),
                        skills_dir=_skills_dir,
                    )
                    if arm == "B":
                        eff_prompt += "\n\nBe concise in your response."
                    work_dir = harness.RUNS / "work" / arm_tool_id / job.batch_id / arm / label
                    stream_path = harness.RUNS / job.batch_id / arm / f"{label}.stream.jsonl"

                    # Retry transient failures (flaky empty results / errors) with
                    # backoff — mirroring the CLI path, which the UI previously lacked.
                    # A real success or an account usage-limit stops immediately; Stop bails.
                    # After a clean success, also rerun up to SKILL_RERUN_LIMIT times when
                    # the arm's skill policy was violated (e.g. Arm C used no bito-* skill).
                    from ..adapters.base import RunResult

                    res = RunResult(exit_code=-1, error="run did not execute")
                    max_attempts = len(harness.RETRY_BACKOFFS_SEC) + 1 + harness.SKILL_RERUN_LIMIT
                    for attempt in range(max_attempts):
                        if job.stop_flag.is_set():
                            raise _Stopped()
                        if c.get("workspace_mode") == "local-repo":
                            harness.seed_workspace_from_repo(work_dir, c["local_repo_path"])
                        else:
                            harness.reset_workspace(work_dir)
                        try:
                            res = adapter.run(
                                prompt_text=eff_prompt,
                                mcp_config_path=mcp_config,
                                model=c["model"],
                                max_turns=c["max_turns"],
                                work_dir=work_dir,
                                stream_path=stream_path,
                                on_proc=lambda p: setattr(job, "active_proc", p),
                                # Arm A: hard-deny Bito MCP tools by name so a plugin-
                                # registered BitoAIArchitect can't slip past the config.
                                disallowed_tools=(harness.bito_disallow_patterns() if arm == "A" else None),
                            )
                        except Exception as e:  # adapter blew up — treat as a failed attempt
                            res = RunResult(exit_code=-1, error=f"{type(e).__name__}: {e}")

                        ok_run = res.exit_code == 0 and not res.error
                        is_limit = bool(res.error and res.error.startswith("usage_limit"))
                        if is_limit or job.stop_flag.is_set():
                            break
                        if ok_run:
                            # Clean run — check skill policy; rerun if violated and attempts remain.
                            violation = harness.skill_policy_violation(arm, res.tool_calls)
                            if violation and attempt < max_attempts - 1:
                                job.emit("run_retry", arm=arm, prompt_id=pid, label=label,
                                         attempt=attempt + 1, attempts=max_attempts,
                                         error=f"skill policy: {violation}",
                                         index=job.progress + 1, total=job.total)
                                time.sleep(harness.INTER_RUN_SLEEP_SEC)
                                continue
                            break
                        if attempt < max_attempts - 1:
                            job.emit("run_retry", arm=arm, prompt_id=pid, label=label,
                                     attempt=attempt + 1, attempts=max_attempts, error=res.error,
                                     index=job.progress + 1, total=job.total)
                            time.sleep(harness.RETRY_BACKOFFS_SEC[min(attempt, len(harness.RETRY_BACKOFFS_SEC) - 1)])

                    job.active_proc = None
                    # If Stop was pressed while this run was in flight, exit now —
                    # don't record or count the killed run.
                    if job.stop_flag.is_set():
                        raise _Stopped()
                    # A transient DB error on one run (e.g. SQLite locked) shouldn't abort
                    # the whole batch — record the failure to save and keep going.
                    try:
                        _insert_run(
                            conn, arm=arm, prompt_id=stored_pid, base_pid=pid, prompt=eff_prompt,
                            res=res, tool=arm_tool_id, repo=c.get("repo"),
                            category=item.get("category"), batch_id=job.batch_id,
                        )
                    except Exception as e:
                        job.emit("run_save_error", arm=arm, prompt_id=pid, label=label,
                                 error=f"{type(e).__name__}: {e}")
                    job.progress += 1
                    ok = res.exit_code == 0 and not res.error
                    is_limit = bool(res.error and res.error.startswith("usage_limit"))
                    # Parse tool-call stats so the UI can show Bito activity per run.
                    import json as _json
                    _tc_raw = _json.dumps([t.__dict__ if hasattr(t, "__dict__") else t for t in (res.tool_calls or [])])
                    _bito = sum(int((t.get("count") or 0)) for t in (res.tool_calls or []) if str(t.get("name","")).startswith("mcp__BitoAIArchitect__"))
                    _skills = [s for t in (res.tool_calls or []) if t.get("name") == "Skill" for s in (t.get("skills") or []) if str(s).startswith("bito-")]
                    job.emit("run_done", arm=arm, prompt_id=pid, label=label, ok=ok,
                             cost=res.total_cost_usd, duration_ms=res.duration_ms,
                             error=res.error, usage_limit=is_limit,
                             runner=arm_tool_id,
                             index=job.progress, total=job.total,
                             bito_calls=_bito, skills=_skills)
                    _save_batch(conn, job)
                    # Account usage/rate limit → stop now; continuing just fails.
                    if is_limit:
                        raise _UsageLimit(res.error.split("usage_limit: ", 1)[-1])
                    time.sleep(harness.INTER_RUN_SLEEP_SEC)
            job.emit("arm_done", arm=arm)

        job.status = "done"
        _save_batch(conn, job)
        job.emit("batch_done", batch_id=job.batch_id, progress=job.progress, total=job.total)

    except _UsageLimit as e:
        job.status = "stopped"
        job.error = f"Stopped: Claude usage limit reached — {e}"
        _kill_active(job)
        _save_batch(conn, job)
        job.emit("usage_limit", message=str(e))
        job.emit("batch_stopped", batch_id=job.batch_id, reason="usage_limit", message=str(e))
    except _Stopped:
        job.status = "stopped"
        _kill_active(job)
        _save_batch(conn, job)
        job.emit("batch_stopped", batch_id=job.batch_id)
    except Exception as e:
        job.status = "error"
        job.error = f"{type(e).__name__}: {e}"
        _save_batch(conn, job)
        job.emit("batch_error", error=job.error)
    finally:
        conn.close()
        _prune_jobs()


class _Stopped(Exception):
    pass


class _UsageLimit(Exception):
    """Raised when a run returns an account usage/rate-limit notice."""


def _kill_active(job: Job) -> None:
    """Kill the active run's ENTIRE process group, so the CLI and its model/MCP
    child processes all die — otherwise the run keeps going after Stop."""
    p = job.active_proc
    if p and p.poll() is None:
        try:
            if os.name != "nt":
                pgid = os.getpgid(p.pid)
                os.killpg(pgid, signal.SIGTERM)
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(pgid, signal.SIGKILL)
            else:
                # Windows: terminate()/kill() only kill the launcher process,
                # orphaning claude's children (model/MCP workers, any git it
                # spawned) — so the run keeps going after Stop. taskkill /T kills
                # the ENTIRE process tree.
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(p.pid)],
                        capture_output=True, timeout=10,
                    )
                except Exception:
                    p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    job.active_proc = None


def stop_batch(batch_id: str) -> dict:
    job = JOBS.get(batch_id)
    if not job:
        raise ValueError("No active run with that id (it may have already finished).")
    job.stop_flag.set()
    _kill_active(job)
    job.emit("stopping")
    return {"ok": True, "batch_id": batch_id}


def rerun_batch(batch_id: str) -> dict:
    """Start a fresh batch with the same configuration as a previous one."""
    conn = engine.connect()
    row = conn.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
    conn.close()
    if not row:
        raise ValueError("Original batch not found.")
    cfg = {
        "tool": row["tool"],
        "repo": row["repo"],
        "prompt_set": row["prompt_set"],
        "arms": json.loads(row["arms"] or '["A","B","C"]'),
        "mode": row["mode"],
        "n_runs": row["n_runs"] or 1,
        "model": row["model"],
        "judge_model": row["judge_model"],
        "label": (row["label"] or "") + " (rerun)" if row["label"] else "rerun",
    }
    return start_batch(cfg)


# ---------------------------------------------------------------------------
# Batch-scoped blind judge (reuses harness primitives)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Reads for the API
# ---------------------------------------------------------------------------
def list_batches() -> list[dict]:
    conn = engine.connect()
    rows = conn.execute("SELECT * FROM batches ORDER BY created_at DESC").fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["arms"] = json.loads(d.get("arms") or "[]")
        # Reflect the live in-memory status if the job is still tracked.
        job = JOBS.get(d["batch_id"])
        # `live` is the authoritative "a worker thread is running this right now"
        # signal — the UI uses it to reconnect to an in-flight run after the user
        # navigates away and back. A DB status of "running" alone isn't enough: a
        # crashed/restarted server can leave a stale "running" row with no job.
        d["live"] = bool(job and job.status in ("queued", "running"))
        if job:
            d["status"] = job.status
            d["progress"] = job.progress
            d["total"] = job.total
        elif d["status"] in ("running", "queued"):
            # No live job (server restarted mid-run) → it can't still be running.
            d["status"] = "interrupted"
        out.append(d)
    return out


def reconcile_orphans() -> int:
    """Flip any 'running'/'queued' batches with no live job to 'interrupted'.
    Called at startup so a server restart doesn't leave phantom runs."""
    conn = engine.connect()
    try:
        rows = conn.execute(
            "SELECT batch_id FROM batches WHERE status IN ('running','queued')"
        ).fetchall()
        n = 0
        for r in rows:
            if r["batch_id"] not in JOBS:
                conn.execute(
                    "UPDATE batches SET status='interrupted', updated_at=? WHERE batch_id=?",
                    (_now(), r["batch_id"]),
                )
                n += 1
        conn.commit()
        return n
    finally:
        conn.close()


def list_run_rows(batch: Optional[str] = None, failures: bool = False) -> list[dict]:
    """Every run (across sessions, or one batch) with status + error, for the
    Logs view. Set ``failures`` to return only runs that errored."""
    conn = engine.connect()
    where = ["batch_id IS NOT NULL"]
    args: list[Any] = []
    if batch:
        where.append("batch_id = ?")
        args.append(batch)
    rows = conn.execute(
        f"""SELECT batch_id, arm, base_prompt_id, prompt_id, tool, exit_code, error,
               total_cost_usd, duration_ms, num_turns, started_at,
               substr(response, 1, 120) AS response_preview
            FROM runs WHERE {' AND '.join(where)}
            ORDER BY started_at DESC, batch_id, base_prompt_id, arm""",
        args,
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["ok"] = r["exit_code"] == 0 and not r["error"]
        # Classify the failure so the UI can label it helpfully.
        if not d["ok"]:
            err = (r["error"] or "").lower()
            resp = (r["response_preview"] or "").lower()
            if "usage_limit" in err or "session limit" in resp or "usage limit" in resp:
                d["fail_kind"] = "usage_limit"
            else:
                d["fail_kind"] = "error"
        else:
            d["fail_kind"] = None
        if failures and d["ok"]:
            continue
        out.append(d)
    return out


def get_batch(batch_id: str) -> Optional[dict]:
    conn = engine.connect()
    row = conn.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
    if not row:
        conn.close()
        return None
    d = dict(row)
    d["arms"] = json.loads(d.get("arms") or "[]")
    runs = conn.execute(
        "SELECT arm, base_prompt_id, prompt_id, exit_code, error, total_cost_usd, "
        "duration_ms, output_tokens FROM runs WHERE batch_id=? ORDER BY base_prompt_id, arm",
        (batch_id,),
    ).fetchall()
    conn.close()
    d["runs"] = [dict(r) for r in runs]
    job = JOBS.get(batch_id)
    if job:
        d["status"] = job.status
        d["progress"] = job.progress
        d["total"] = job.total
    return d
