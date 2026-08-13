"""Report generation endpoints (Phase 7)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, Response

from ..services import reports as svc

router = APIRouter(prefix="/api", tags=["reports"])


@router.get("/reports/{batch_id}")
def report(batch_id: str) -> dict:
    try:
        return svc.build(batch_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/reports/{batch_id}/export.html")
def export_html(batch_id: str):
    try:
        html_str = svc.render_html(batch_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return Response(
        content=html_str,
        media_type="text/html",
        headers={"Content-Disposition": f'attachment; filename="ab-report-{batch_id}.html"'},
    )


@router.get("/reports/{batch_id}/export.md")
def export_markdown(batch_id: str):
    try:
        md = svc.render_markdown(batch_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return Response(
        content=md,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="ab-report-{batch_id}.md"'},
    )
