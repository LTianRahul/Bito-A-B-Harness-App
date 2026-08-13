"""Benchmark runner endpoints (Phase 4)."""

from __future__ import annotations

import asyncio
import json
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse, Response, StreamingResponse

from .. import engine
from ..models import StartRunRequest
from ..services import logs as logs_svc
from ..services import runner

router = APIRouter(prefix="/api", tags=["runs"])


@router.post("/runs")
def start_run(req: StartRunRequest) -> dict:
    try:
        return runner.start_batch(req.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/runs")
def list_runs() -> dict:
    return {"batches": runner.list_batches()}


@router.get("/runlog")
def runlog(batch: Optional[str] = None, failures: bool = False) -> dict:
    """All runs (or one session) with status + error, for the Logs view."""
    return {"rows": runner.list_run_rows(batch, failures)}


@router.get("/runs/{batch_id}")
def get_run(batch_id: str) -> dict:
    b = runner.get_batch(batch_id)
    if not b:
        raise HTTPException(status_code=404, detail="Batch not found.")
    return b


@router.get("/runs/{batch_id}/transcript")
def transcript(batch_id: str, arm: str, prompt_id: str) -> dict:
    """Parsed step-by-step transcript of one run (tool calls, decisions, results)."""
    return logs_svc.transcript(batch_id, arm.upper(), prompt_id)


@router.get("/runs/{batch_id}/transcript/raw")
def transcript_raw(batch_id: str, arm: str, prompt_id: str):
    raw = logs_svc.raw_stream(batch_id, arm.upper(), prompt_id)
    if raw is None:
        raise HTTPException(status_code=404, detail="No transcript found for that run.")
    return PlainTextResponse(
        raw,
        headers={"Content-Disposition": f'attachment; filename="{batch_id}-{arm}-{prompt_id}.jsonl"'},
    )


def _build_arm_md(batch_id: str, arm: str) -> Optional[str]:
    """Build a markdown document of final answers for one arm. Returns None if no data."""
    conn = engine.connect()
    try:
        batch_row = conn.execute(
            "SELECT created_at FROM batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        rows = conn.execute(
            "SELECT prompt_id, prompt, response, total_cost_usd, duration_ms, num_turns, tool_calls_json "
            "FROM runs WHERE batch_id=? AND arm=? AND exit_code=0 AND (error IS NULL OR error='') "
            "ORDER BY prompt_id",
            (batch_id, arm),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return None

    arm_names = {"A": "Vanilla (no Bito)", "B": "With Bito MCP + Skill", "C": "Bito MCP + all Skills"}

    def fmt_duration(ms):
        if ms is None:
            return "n/a"
        s = int(ms / 1000)
        return f"{s // 60}m {s % 60}s" if s >= 60 else f"{s}s"

    def extract_skills(tool_calls_json):
        try:
            calls = json.loads(tool_calls_json or "[]")
        except Exception:
            calls = []
        skills = []
        for c in calls:
            if c.get("name") == "Skill" and isinstance(c.get("skills"), list):
                skills.extend(c["skills"])
        return sorted(set(skills))

    def base_pid(full_id: str) -> str:
        # strip batch prefix if present: "batch_id:prompt-slug" -> "prompt-slug"
        return full_id.split(":", 1)[-1] if ":" in full_id else full_id

    created = (batch_row["created_at"] or batch_id) if batch_row else batch_id
    lines = [
        f"# Benchmark Results — Arm {arm} · {arm_names.get(arm, arm)}",
        f"**Session:** {batch_id}  ",
        f"**Date:** {created.replace('T', ' ')[:19]}  ",
        f"**Arm:** {arm} — {arm_names.get(arm, arm)}",
        "",
    ]

    for i, r in enumerate(rows, 1):
        pid = base_pid(r["prompt_id"])
        skills = extract_skills(r["tool_calls_json"])
        cost = f"${r['total_cost_usd']:.4f}" if r["total_cost_usd"] is not None else "n/a"
        meta_parts = [
            f"Cost: {cost}",
            f"Time: {fmt_duration(r['duration_ms'])}",
            f"Turns: {r['num_turns'] or 'n/a'}",
        ]
        if skills:
            meta_parts.append(f"Skills: {', '.join(skills)}")

        lines += [
            f"---",
            f"",
            f"## {i}. {pid}",
            f"",
            f"**Prompt:**",
            f"> {(r['prompt'] or '').splitlines()[0][:300]}",
            f"",
            f"_{' · '.join(meta_parts)}_",
            f"",
            f"**Answer:**",
            f"",
            (r["response"] or "_No response recorded._"),
            f"",
        ]

    return "\n".join(lines)


@router.get("/runs/{batch_id}/logs/{arm}/preview")
def preview_arm_logs(batch_id: str, arm: str) -> Response:
    """Return markdown text of final answers for UI preview (no download header)."""
    arm = arm.upper()
    md = _build_arm_md(batch_id, arm)
    if md is None:
        raise HTTPException(status_code=404, detail=f"No completed runs found for Arm {arm} in batch {batch_id}.")
    return Response(content=md, media_type="text/plain; charset=utf-8")


@router.get("/runs/{batch_id}/logs/{arm}/download")
def download_arm_logs(batch_id: str, arm: str) -> Response:
    """Download final answers for one arm as a markdown file."""
    arm = arm.upper()
    md = _build_arm_md(batch_id, arm)
    if md is None:
        raise HTTPException(status_code=404, detail=f"No completed runs found for Arm {arm} in batch {batch_id}.")
    filename = f"{batch_id}-arm-{arm}-answers.md"
    return Response(
        content=md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/runs/{batch_id}/stop")
def stop_run(batch_id: str) -> dict:
    try:
        return runner.stop_batch(batch_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/runs/{batch_id}/rerun")
def rerun(batch_id: str) -> dict:
    try:
        return runner.rerun_batch(batch_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/runs/{batch_id}/events")
async def events(batch_id: str):
    """Server-Sent Events: live progress for a running batch.

    Replays any events already buffered (so a late subscriber catches up), then
    tails new ones until the job reaches a terminal state.
    """
    job = runner.JOBS.get(batch_id)
    if not job:
        raise HTTPException(status_code=404, detail="No active run with that id.")

    async def gen():
        sent = 0
        while True:
            # Snapshot any new events.
            with job.lock:
                pending = job.events[sent:]
                sent = len(job.events)
            for ev in pending:
                yield f"data: {json.dumps(ev)}\n\n"
            terminal = job.status in ("done", "stopped", "error")
            if terminal and sent >= len(job.events):
                yield f"data: {json.dumps({'type': 'closed', 'status': job.status})}\n\n"
                return
            await asyncio.sleep(0.4)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
