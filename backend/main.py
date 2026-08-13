"""FastAPI application: serves the built React SPA and the JSON/SSE API.

Run via ``python app.py`` (preferred) or ``uvicorn backend.main:app``.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import engine
from .routers import auth, cli, leaderboard, metrics, prompts, reports, runs, setup

app = FastAPI(title="A/B Testing Benchmark Harness", version="1.0.0")

# During development the Vite dev server runs on :5173 and proxies /api itself,
# but allow cross-origin just in case the SPA is opened directly.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    # Load a Setup-page-saved Anthropic API key into the process env before anything
    # else runs, so headless `claude` subprocess calls have it from the first request.
    from .services import anthropic_auth

    anthropic_auth.load_persisted_into_env()

    # A server restart can't leave a benchmark "running" — mark orphans interrupted.
    from .services import runner

    runner.reconcile_orphans()

    # Re-stamp the Bito OAuth token onto the benchmark config in case the claude CLI
    # rewrote ~/.claude.json since last launch — so the connection is live on startup
    # without the user having to reconnect.
    try:
        from .services import bito_oauth

        bito_oauth.ensure_arm_bito_authed()
    except Exception:
        pass


@app.on_event("shutdown")
def _shutdown() -> None:
    # On Ctrl+C, stop any in-flight benchmark and kill its child processes so we exit
    # cleanly and don't orphan claude/MCP workers.
    from .services import runner

    runner.stop_all()


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "service": "ab-testing-harness", **engine.db_health()}


# Mount feature routers.
app.include_router(setup.router)
app.include_router(auth.router)
app.include_router(prompts.router)
app.include_router(runs.router)
app.include_router(metrics.router)
app.include_router(leaderboard.router)
app.include_router(reports.router)
app.include_router(cli.router)


# ---------------------------------------------------------------------------
# Serve the built SPA. In production frontend/dist exists (committed). In dev it
# may not — then API still works and the root returns a helpful hint.
# ---------------------------------------------------------------------------
DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"


# Any /api/* that didn't match a real route above → clear 404 for EVERY method
# (registered after the routers, so it only catches misses). Without this, a POST
# to an unknown /api path falls through to the GET-only SPA route and returns a
# confusing "405 Method Not Allowed" — usually a sign of a stale running server.
@app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"], include_in_schema=False)
def api_not_found(rest: str):
    return JSONResponse(
        {"detail": f"Unknown API endpoint: /api/{rest}. If you just updated the app, "
                   "restart it (python app.py) to load new endpoints."},
        status_code=404,
    )


if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        # API routes are handled above; everything else serves index.html so the
        # client-side router can take over (deep links work on refresh).
        if full_path.startswith("api/"):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        candidate = DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(DIST / "index.html")

else:  # pragma: no cover - dev convenience

    @app.get("/")
    def no_build():
        return JSONResponse(
            {
                "detail": "Frontend not built. Run `npm install && npm run build` in "
                "frontend/, or use the Vite dev server (npm run dev) on port 5173.",
                "api": "/api/health",
            }
        )
