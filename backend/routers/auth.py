"""Bito authentication endpoints (Phase 2 + OAuth)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from typing import List, Optional

from ..models import BitoAuthRequest, DoctorRequest
from ..services import bito_auth, bito_oauth

router = APIRouter(prefix="/api/bito", tags=["auth"])


class OAuthStartRequest(BaseModel):
    workspace_id: str
    # Which CLIs should receive the Bito server once sign-in completes
    # (e.g. ["copilot"]). Omitted => fall back to already-configured tools.
    tools: Optional[List[str]] = None


class ToolConnectRequest(BaseModel):
    tool: str
    workspace_id: Optional[str] = ""
    # Optional static bearer (self-hosted Bito or when OAuth isn't enabled).
    # When omitted, the OAuth flow is used instead.
    token: Optional[str] = None


class ToolDisconnectRequest(BaseModel):
    tool: str


@router.get("/status")
def status() -> dict:
    base = bito_auth.status()
    base["oauth"] = bito_oauth.status()
    # Per-tool Bito MCP config state (Claude Code + GitHub Copilot CLI), so the
    # Setup page can show and control each CLI's connection independently.
    base["per_tool"] = bito_oauth.per_tool_status()
    # NOTE: this endpoint reports CONFIG SHAPE only (has_token = a bearer is present),
    # NOT whether that token authenticates. Authoritative live connection state comes
    # from GET /api/bito/health (token-validating auth probe) and POST /api/bito/validate
    # (full live tool probe). The UI gates "Connected" on those, not on has_token.
    return base


@router.get("/health")
def health() -> dict:
    """Lightweight availability probe of the configured Bito MCP (no model tokens).
    Used by Setup to decide reuse-vs-reconnect before claiming 'connected'."""
    return bito_oauth.mcp_health()


@router.post("/configure")
def configure(req: BitoAuthRequest) -> dict:
    """Self-hosted / static bearer-token path."""
    try:
        return bito_auth.configure(req.workspace_id, req.token, req.tools)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/validate")
def validate(req: DoctorRequest) -> dict:
    try:
        tool_id = req.tool if req.tool in ("claude", "copilot") else "claude"
        # Refresh the OAuth token AND re-stamp it onto the config THIS CLI reads, so the
        # live probe authenticates even if the CLI rewrote/stripped its own config:
        # mcp-arm-bito.json for Claude, ~/.copilot/mcp-config.json for Copilot.
        bito_oauth.ensure_run_bito_authed(tool_id)
        result = bito_auth.validate(model=req.model, max_turns=req.max_turns, tool_id=tool_id)
        # Note: prompts are NOT auto-generated here. Users start with an empty prompt
        # list and opt in via the "Generate with AI" button on the Prompts page.
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


# ---- OAuth (hosted Bito) ----
@router.post("/oauth/pre-warm")
def oauth_pre_warm(req: OAuthStartRequest) -> dict:
    """Pre-fetch the Bito OAuth discovery endpoints and cache them.

    Called by the frontend when the user finishes typing a workspace ID (on blur).
    The cache means the subsequent /oauth/start call returns instantly — the browser
    popup opens as soon as the user clicks Connect, with no visible network delay.
    """
    try:
        bito_oauth.discover(req.workspace_id)
        return {"ok": True}
    except Exception:
        return {"ok": False}   # silent — this is best-effort pre-warming


@router.post("/oauth/start")
def oauth_start(req: OAuthStartRequest, request: Request) -> dict:
    redirect_uri = str(request.base_url).rstrip("/") + "/api/bito/oauth/callback"
    try:
        return bito_oauth.start(req.workspace_id, redirect_uri, tools=req.tools)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Couldn't start sign-in: {e}")


# ---- Per-tool MCP connect / disconnect (Claude Code + GitHub Copilot CLI) ----
@router.post("/tool/connect")
def tool_connect(req: ToolConnectRequest) -> dict:
    """Write the Bito MCP server into one CLI's config (~/.claude.json or
    ~/.copilot/mcp-config.json) using the app's live OAuth token. When no token
    exists yet, returns {"needs_oauth": true} and the UI starts the browser flow."""
    try:
        return bito_oauth.connect_tool(req.tool, req.workspace_id or "", token=req.token)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/tool/disconnect")
def tool_disconnect(req: ToolDisconnectRequest) -> dict:
    """Remove the Bito MCP server from one CLI's config only."""
    try:
        return bito_oauth.disconnect_tool(req.tool)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/oauth/callback")
def oauth_callback(code: str = "", state: str = "", error: str = ""):
    """Browser redirect target. Closes itself and notifies the opener."""
    if error:
        msg, ok = f"Sign-in failed: {error}", False
    else:
        try:
            bito_oauth.handle_callback(code, state)
            msg, ok = "Bito is connected. You can close this tab.", True
        except Exception as e:
            msg, ok = str(e), False
    color = "#1f9d57" if ok else "#d8434f"
    html = f"""<!doctype html><html><head><meta charset=utf-8><title>Bito sign-in</title>
<style>body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;display:grid;place-items:center;
height:100vh;margin:0;background:#f4f6fb}}.box{{text-align:center;background:#fff;padding:36px 44px;
border-radius:14px;box-shadow:0 4px 24px rgba(20,30,60,.1)}}.ico{{font-size:40px;color:{color}}}
h2{{margin:12px 0 4px}}p{{color:#64708a}}</style></head>
<body><div class=box><div class=ico>{'✓' if ok else '✕'}</div>
<h2>{'Connected' if ok else 'Could not connect'}</h2><p>{msg}</p></div>
<script>try{{if(window.opener)window.opener.postMessage({{bitoOAuth:{str(ok).lower()}}},'*');}}catch(e){{}}
setTimeout(function(){{window.close()}},{1500 if ok else 6000});</script></body></html>"""
    return HTMLResponse(html)


@router.post("/oauth/disconnect")
def oauth_disconnect() -> dict:
    bito_oauth.disconnect()
    return {"ok": True}
