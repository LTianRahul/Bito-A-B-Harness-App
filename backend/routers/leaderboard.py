"""Leaderboard endpoints (Phase 6)."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter

from ..services import leaderboard as svc

router = APIRouter(prefix="/api", tags=["leaderboard"])


@router.get("/leaderboard/filters")
def filters() -> dict:
    return svc.filters()


@router.get("/leaderboard")
def leaderboard(
    tool: Optional[str] = None,
    repo: Optional[str] = None,
    category: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> dict:
    return svc.compute(tool, repo, category, date_from, date_to)
