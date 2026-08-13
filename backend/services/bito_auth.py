"""Bito AI Architect authentication.

Flow (Req #2):
  1. User enters their Workspace ID (and optionally a token).
  2. We write the Bito MCP server entry — ``https://mcp.bito.ai/<id>/mcp`` — into
     the selected tool config(s), merging safely (we back up first and never
     clobber other servers).
  3. We rebuild the per-arm configs and run a live probe to *validate* that the
     MCP actually answers, then report connected status + indexed repos.

Honesty note: token auth is fully self-serve here. For OAuth-based MCPs the
one-time browser sign-in is performed by the tool itself (e.g. Claude Code's
``/mcp``); we can't complete that handshake headlessly, so we write the config,
tell the user how to finish sign-in, and let the live check confirm it.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .. import engine
from ..adapters.base import read_json
from ..adapters.registry import all_adapters, get_adapter
from . import detection
from .bito_oauth import resolve_mcp_url

harness = engine.harness

DEFAULT_SERVER_KEY = "BitoAIArchitect"


def mcp_url(workspace_id: str) -> str:
    """Accepts a hosted workspace ID or a full custom MCP URL (see resolve_mcp_url)."""
    return resolve_mcp_url(workspace_id)


def _server_entry(workspace_id: str, token: Optional[str]) -> dict[str, Any]:
    entry: dict[str, Any] = {"type": "http", "url": mcp_url(workspace_id)}
    if token:
        entry["headers"] = {"Authorization": f"Bearer {token.strip()}"}
    return entry


def _backup(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = path.with_suffix(path.suffix + f".bak-{stamp}")
    shutil.copy2(path, bak)
    return str(bak)


def _write_into_tool(tool_id: str, workspace_id: str, token: Optional[str]) -> dict[str, Any]:
    """Merge the Bito server into one tool's MCP config, preserving everything
    else. Returns a per-tool result dict."""
    adapter = get_adapter(tool_id)
    if not adapter:
        return {"tool": tool_id, "ok": False, "detail": "Unknown tool."}

    path = adapter.mcp_config_path()
    if path is None:
        return {"tool": tool_id, "ok": False, "detail": "No MCP config location for this tool."}

    path.parent.mkdir(parents=True, exist_ok=True)
    data = read_json(path) if path.exists() else None
    if data is None:
        data = {}
    if not isinstance(data.get("mcpServers"), dict):
        data["mcpServers"] = {}

    # Reuse an existing Bito key if present, else use the default name.
    existing_key = next(
        (k for k in data["mcpServers"] if "bito" in k.lower() or "architect" in k.lower()),
        None,
    )
    key = existing_key or DEFAULT_SERVER_KEY

    backup = _backup(path)
    data["mcpServers"][key] = _server_entry(workspace_id, token)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return {
        "tool": tool_id,
        "ok": True,
        "server_key": key,
        "path": str(path),
        "backup": backup,
        "auth": "token" if token else "oauth",
    }


def configure(workspace_id: str, token: Optional[str], tools: Optional[list[str]]) -> dict[str, Any]:
    """Write the Bito MCP into the requested tools (default: every detected tool
    that has a config location), then rebuild per-arm configs."""
    workspace_id = (workspace_id or "").strip()
    if not workspace_id:
        raise ValueError("Workspace ID or MCP URL is required.")

    if tools:
        targets = tools
    else:
        targets = [a.id for a in all_adapters() if a.detect()[0] and a.mcp_config_path()]
    if not targets:
        targets = ["claude"]

    results = [_write_into_tool(t, workspace_id, token) for t in targets]

    # Rebuild the benchmark arm configs so the new Bito server is included.
    rebuilt = None
    rebuild_error = None
    try:
        rebuilt = detection.build_arm_configs()
    except ValueError as e:
        rebuild_error = str(e)

    return {
        "ok": any(r.get("ok") for r in results),
        "workspace_id": workspace_id,
        "url": mcp_url(workspace_id),
        "auth": "token" if token else "oauth",
        "written": results,
        "arm_configs": rebuilt,
        "rebuild_error": rebuild_error,
    }


def status() -> dict[str, Any]:
    """Current Bito connection for the UI, from the config shape (no network call).
    The on-demand 'Run health check' is the live verification."""
    adapter = get_adapter("claude")
    info = adapter.info() if adapter else None
    mcp = info.mcp if info else None
    return {
        "configured": bool(mcp and mcp.state in ("configured",)),
        "state": mcp.state if mcp else "missing",
        "workspace_id": mcp.workspace_id if mcp else None,
        "url": mcp.url if mcp else None,
        "detail": mcp.detail if mcp else "Claude Code not detected.",
        "configs_built": detection.configs_exist(),
        # auth_kind == "token" only when a real bearer/static token is actually
        # present in the config. A bare URL (no token, OAuth not signed in) is
        # "oauth"/"none" — so the UI must not claim "Token configured" for it.
        "auth_kind": mcp.auth_kind if mcp else None,
        "has_token": bool(mcp and mcp.auth_kind == "token"),
    }


def validate(
    model: Optional[str] = None,
    max_turns: Optional[int] = None,
    tool_id: str = "claude",
) -> dict[str, Any]:
    """Live validation through ONE tool's own CLI: a real probe that proves that
    tool's Bito MCP answers, plus indexed repos."""
    # Claude's probe reads configs/mcp-arm-bito.json; build it if missing. Copilot
    # reads its own ~/.copilot/mcp-config.json, so it doesn't need the arm configs.
    if tool_id == "claude" and not detection.configs_exist():
        try:
            detection.build_arm_configs()
        except ValueError as e:
            return {"connected": False, "error": str(e), "repositories": []}

    doc = detection.run_doctor(model=model, max_turns=max_turns, tool_id=tool_id)
    connected = doc["ready"] or any(
        c["name"].startswith("Bito MCP answered") and c["ok"] for c in doc["checks"]
    )
    return {
        "connected": connected,
        "ready": doc["ready"],
        "checks": doc["checks"],
        "repositories": doc["repositories"],
        "probe_text": doc.get("probe_text", ""),
    }
