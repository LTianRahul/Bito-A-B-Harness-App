"""In-UI command-line endpoints (run harness subcommands, stream output)."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..services import cli as svc

router = APIRouter(prefix="/api/cli", tags=["cli"])


class ExecRequest(BaseModel):
    args: str


@router.get("/reference")
def reference() -> dict:
    return {"reference": svc.REFERENCE, "allowed": sorted(svc.ALLOWED)}


@router.post("/exec")
def exec_cmd(req: ExecRequest) -> dict:
    try:
        return svc.exec_command(req.args)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{job_id}/stop")
def stop(job_id: str) -> dict:
    svc.stop(job_id)
    return {"ok": True}


@router.get("/{job_id}/events")
async def events(job_id: str):
    job = svc.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="No such command.")

    async def gen():
        sent = 0
        while True:
            with job.lock:
                pending = job.lines[sent:]
                sent = len(job.lines)
            for line in pending:
                yield f"data: {json.dumps({'line': line})}\n\n"
            if job.status in ("done", "error") and sent >= len(job.lines):
                yield f"data: {json.dumps({'done': True, 'status': job.status, 'code': job.returncode})}\n\n"
                return
            await asyncio.sleep(0.3)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
