"""Metrics & evaluation endpoints (Phase 5)."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter
from fastapi.responses import Response

from ..services import metrics as svc

router = APIRouter(prefix="/api", tags=["metrics"])


@router.get("/metrics")
def metrics(batch: Optional[str] = None) -> dict:
    """All metrics for a batch, or across all UI batches if no batch given."""
    return svc.compute(batch)


@router.get("/metrics/export.csv")
def export_csv(batch: Optional[str] = None):
    csv_text = svc.to_csv(batch)
    fname = f"ab-metrics-{batch or 'all'}.csv"
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/metrics/export.html")
def export_html(batch: Optional[str] = None):
    html = svc.to_html(batch)
    fname = f"ab-metrics-{batch or 'all'}.html"
    return Response(
        content=html,
        media_type="text/html; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
