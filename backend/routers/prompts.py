"""Prompt management endpoints (Phase 3)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..models import GenerateRequest, ImportRequest, Prompt, PromptSetSave
from ..services import prompt_gen
from ..services import prompts as svc

router = APIRouter(prefix="/api", tags=["prompts"])


def _guard(fn):
    try:
        return fn()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/prompts")
def list_prompts() -> dict:
    return {"prompts": svc.list_prompts()}


@router.post("/prompts")
def add_prompt(p: Prompt) -> dict:
    return {"prompts": _guard(lambda: svc.add(p.model_dump(exclude_none=True)))}


@router.put("/prompts/{pid}")
def update_prompt(pid: str, p: Prompt) -> dict:
    return {"prompts": _guard(lambda: svc.update(pid, p.model_dump(exclude_none=True)))}


@router.delete("/prompts/{pid}")
def delete_prompt(pid: str) -> dict:
    return {"prompts": _guard(lambda: svc.delete(pid))}


@router.post("/prompts/{pid}/duplicate")
def duplicate_prompt(pid: str) -> dict:
    return {"prompts": _guard(lambda: svc.duplicate(pid))}


@router.post("/prompts/import")
def import_prompts(req: ImportRequest) -> dict:
    items = [p.model_dump(exclude_none=True) for p in req.prompts]
    return {"prompts": _guard(lambda: svc.import_prompts(items, req.replace))}


@router.get("/prompts/export")
def export_prompts() -> list:
    return svc.list_prompts()


@router.post("/prompts/generate")
def generate(req: GenerateRequest) -> dict:
    """Draft benchmark prompts with AI (grounded in indexed repos when available).
    Makes one model call; returns candidates for the user to review and import."""
    try:
        return prompt_gen.generate(
            topic=req.topic, count=req.count, categories=req.categories,
            ground=req.ground, model=req.model,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---- prompt sets ----
@router.get("/prompt-sets")
def list_sets() -> dict:
    return {"sets": svc.list_sets()}


@router.post("/prompt-sets")
def save_set(req: PromptSetSave) -> dict:
    items = [p.model_dump(exclude_none=True) for p in req.prompts]
    return _guard(lambda: svc.save_set(req.name, items))


@router.get("/prompt-sets/{name}")
def load_set(name: str) -> dict:
    return {"prompts": _guard(lambda: svc.load_set(name))}


@router.delete("/prompt-sets/{name}")
def delete_set(name: str) -> dict:
    svc.delete_set(name)
    return {"ok": True}
