"""Bito AI Architect MCP — OAuth (authorization-code + PKCE) from the UI.

Hosted Bito (mcp.bito.ai) speaks the standard MCP OAuth flow:
  protected-resource metadata → authorization-server metadata →
  dynamic client registration (RFC 7591) → /authorize (PKCE S256) →
  /token (code → access+refresh) → refresh as needed.

We run that flow here: the user clicks Connect, approves in the browser, and we
store the tokens locally and auto-refresh them. The access token is written as a
``Authorization: Bearer`` header on the Bito MCP server in the tool configs, so
headless ``claude`` runs authenticate with no interactive step.

Self-hosted Bito (a static bearer token) is handled by ``bito_auth.configure``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

from .. import engine
from ..adapters.base import HOME, looks_like_bito, read_json

harness = engine.harness

TOKENS_PATH = engine.CONFIGS / ".bito_oauth.json"
DEFAULT_SERVER_KEY = "BitoAIArchitect"

# Tools whose MCP config the UI can manage independently. Each maps to the
# canonical config file the CLI reads (created on first connect if absent):
#   claude  -> ~/.claude.json                (top-level "mcpServers")
#   copilot -> ~/.copilot/mcp-config.json    (GitHub Copilot CLI, "mcpServers")
# The OAuth access token is a single app-wide credential; these entries control
# WHICH CLIs carry the Bito server, so each tool can be connected/disconnected
# on its own from the Setup page.
_TOOL_CONFIG_PATHS: dict[str, Path] = {
    "claude": HOME / ".claude.json",
    "copilot": HOME / ".copilot" / "mcp-config.json",
}

# state token -> pending auth context (in memory, short-lived)
_PENDING: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Small HTTP helpers (stdlib only)
# ---------------------------------------------------------------------------
def _get_json(url: str, timeout: int = 15) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _post(url: str, *, json_body: dict | None = None, form: dict | None = None, timeout: int = 20) -> dict:
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        ctype = "application/json"
    else:
        data = urllib.parse.urlencode(form or {}).encode("utf-8")
        ctype = "application/x-www-form-urlencoded"
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": ctype, "Accept": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise ValueError(f"{e.code} from {url}: {detail[:300]}")


# ---------------------------------------------------------------------------
# Discovery  (cached so the Connect button opens the browser instantly)
# ---------------------------------------------------------------------------
def mcp_url(workspace_id: str) -> str:
    return f"https://mcp.bito.ai/{workspace_id.strip()}/mcp"


def resolve_mcp_url(identifier: str) -> str:
    """Turn the user's input into a Bito MCP endpoint URL. Accepts EITHER:
      - a hosted workspace ID  ("<WORKSPACE_ID>" -> https://mcp.bito.ai/<WORKSPACE_ID>/mcp), OR
      - a full URL for a self-hosted / custom Bito, used EXACTLY as given (any host
        or path — we never reshape it or assume a ``/mcp`` suffix; only a trailing
        slash is trimmed so well-known lookups don't double up).
    Empty input returns "" so callers can guard on it."""
    v = (identifier or "").strip()
    if not v:
        return ""
    if v.startswith("http://") or v.startswith("https://"):
        return v.rstrip("/")
    return mcp_url(v)


def workspace_from(identifier: str) -> Optional[str]:
    """Best-effort workspace id for display: the segment after ``mcp.bito.ai/`` for
    hosted ids/URLs, else None for a custom host (which has no workspace id)."""
    v = (identifier or "").strip()
    if not v:
        return None
    if v.startswith("http://") or v.startswith("https://"):
        if "mcp.bito.ai/" in v:
            try:
                return v.split("mcp.bito.ai/", 1)[1].split("/", 1)[0] or None
            except Exception:
                return None
        return None
    return v


# In-process cache: workspace_id → {endpoints, ts}.  TTL = 10 min.
# The first call (triggered by the frontend on workspace-ID change) warms this
# cache; clicking Connect reuses it so no network round-trips happen on click.
_DISCOVERY_CACHE: dict[str, dict] = {}
_DISCOVERY_TTL = 600   # seconds


def discover(workspace_id: str) -> dict:
    """Resolve the OAuth endpoints for a workspace via the MCP well-known docs.

    Results are cached for _DISCOVERY_TTL seconds so repeated calls (e.g. the
    frontend pre-warming on blur + the Connect button itself) hit the network
    at most once per workspace per 10 minutes.
    """
    import time as _time
    cached = _DISCOVERY_CACHE.get(workspace_id)
    if cached and (_time.time() - cached["ts"]) < _DISCOVERY_TTL:
        return cached["endpoints"]

    resource = resolve_mcp_url(workspace_id)
    pr = _get_json(f"{resource}/.well-known/oauth-protected-resource")
    servers = pr.get("authorization_servers") or [resource]
    auth_server = servers[0]
    meta = _get_json(f"{auth_server}/.well-known/oauth-authorization-server")
    endpoints = {
        "resource": resource,
        "authorization_endpoint": meta["authorization_endpoint"],
        "token_endpoint": meta["token_endpoint"],
        "registration_endpoint": meta.get("registration_endpoint"),
        "scopes": " ".join(pr.get("scopes_supported") or ["mcp"]),
    }
    _DISCOVERY_CACHE[workspace_id] = {"endpoints": endpoints, "ts": _time.time()}
    return endpoints


def _register(registration_endpoint: str, redirect_uri: str) -> str:
    reg = _post(registration_endpoint, json_body={
        "client_name": "A/B Testing Benchmark Harness",
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": "mcp",
    })
    cid = reg.get("client_id")
    if not cid:
        raise ValueError("Dynamic client registration did not return a client_id.")
    return cid


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------
def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


# ---------------------------------------------------------------------------
# Flow
# ---------------------------------------------------------------------------
def start(workspace_id: str, redirect_uri: str, tools: Optional[list[str]] = None) -> dict:
    """Begin OAuth: discover, register, build the authorize URL. The caller opens
    the returned URL in a browser.

    ``tools`` is the list of tool ids (e.g. ["copilot"]) whose config should
    receive the Bito server once sign-in completes. When omitted we fall back to
    every tool already configured (or Claude Code on a fresh machine)."""
    workspace_id = (workspace_id or "").strip()
    if not workspace_id:
        raise ValueError("Workspace ID or MCP URL is required.")
    endpoints = discover(workspace_id)
    if not endpoints.get("registration_endpoint"):
        raise ValueError("This Bito workspace does not advertise OAuth registration.")
    client_id = _register(endpoints["registration_endpoint"], redirect_uri)
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(24)
    _PENDING[state] = {
        "workspace_id": workspace_id,
        "endpoints": endpoints,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "verifier": verifier,
        "tools": _valid_tools(tools),
        "ts": time.time(),
    }
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "scope": endpoints["scopes"],
        "resource": endpoints["resource"],
    }
    authorize_url = endpoints["authorization_endpoint"] + "?" + urllib.parse.urlencode(params)
    return {"authorize_url": authorize_url, "state": state}


def handle_callback(code: str, state: str) -> dict:
    ctx = _PENDING.pop(state, None)
    if not ctx:
        raise ValueError("This sign-in link expired or was already used. Click Connect again.")
    ep = ctx["endpoints"]
    tok = _post(ep["token_endpoint"], form={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": ctx["redirect_uri"],
        "client_id": ctx["client_id"],
        "code_verifier": ctx["verifier"],
        "resource": ep["resource"],
    })
    _store_tokens(ctx["workspace_id"], ctx["client_id"], ep, tok, tools=ctx.get("tools"))
    return {"connected": True, "workspace_id": ctx["workspace_id"]}


def _store_tokens(
    workspace_id: str,
    client_id: str,
    endpoints: dict,
    tok: dict,
    tools: Optional[list[str]] = None,
) -> None:
    access = tok.get("access_token")
    if not access:
        raise ValueError("Token exchange did not return an access token.")
    expires_in = int(tok.get("expires_in") or 3600)
    record = {
        "workspace_id": workspace_id,
        "client_id": client_id,
        "endpoints": endpoints,
        "access_token": access,
        "refresh_token": tok.get("refresh_token"),
        "token_type": tok.get("token_type", "Bearer"),
        "expires_at": time.time() + expires_in - 60,
        "obtained_at": time.time(),
    }
    engine.CONFIGS.mkdir(parents=True, exist_ok=True)
    TOKENS_PATH.write_text(json.dumps(record, indent=2), encoding="utf-8")
    _write_token_to_configs(workspace_id, access, tools=tools)


def _load() -> Optional[dict]:
    return read_json(TOKENS_PATH) if TOKENS_PATH.exists() else None


def refresh() -> bool:
    rec = _load()
    if not rec or not rec.get("refresh_token"):
        return False
    ep = rec["endpoints"]
    try:
        tok = _post(ep["token_endpoint"], form={
            "grant_type": "refresh_token",
            "refresh_token": rec["refresh_token"],
            "client_id": rec["client_id"],
            "resource": ep["resource"],
        })
    except ValueError:
        return False
    # Some servers omit a new refresh_token on refresh — keep the old one.
    tok.setdefault("refresh_token", rec["refresh_token"])
    try:
        _store_tokens(rec["workspace_id"], rec["client_id"], ep, tok)
    except Exception:
        # Got a fresh token but couldn't persist it — report failure rather than
        # silently leaving the stale token in place for the next call.
        return False
    return True


def ensure_fresh() -> Optional[str]:
    """Return a valid access token, refreshing if it's expired. Rewrites configs
    when refreshed. Returns None if not connected via OAuth."""
    rec = _load()
    if not rec:
        return None
    if time.time() >= rec.get("expires_at", 0):
        if not refresh():
            return None
        rec = _load()
    return rec.get("access_token") if rec else None


def current_token() -> Optional[str]:
    """The stored OAuth access token WITHOUT refreshing (None if not connected).
    Used inside config-write paths so we never trigger a refresh from there (which
    would recurse through _store_tokens → _write_token_to_configs)."""
    rec = _load()
    return rec.get("access_token") if rec else None


def _stamp_arm_bito_header(token: str) -> bool:
    """Write `Authorization: Bearer <token>` onto the Bito server inside
    configs/mcp-arm-bito.json — the file every benchmark run and probe reads. This
    is the app-controlled source of truth, so it self-heals a header the claude CLI
    may have stripped from ~/.claude.json. Rebuilds the arm config first if missing."""
    path = engine.CONFIGS / "mcp-arm-bito.json"
    if not path.exists():
        try:
            from . import detection
            detection.build_arm_configs()
        except Exception:
            return False
    data = read_json(path) if path.exists() else None
    if not isinstance(data, dict):
        return False
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        return False
    key = next((k for k in servers if looks_like_bito(k)), None)
    if not key or not isinstance(servers.get(key), dict):
        return False
    servers[key].setdefault("type", "http")
    servers[key]["headers"] = {"Authorization": f"Bearer {token}"}
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return True


def arm_bito_auth_status() -> dict:
    """Inspect configs/mcp-arm-bito.json — the file headless B/C runs explicitly
    declare — and report whether the Bito server carries a usable bearer. Covers
    BOTH auth modes: OAuth stamps the header here, and the self-hosted static-token
    path (bito_auth.configure) writes it directly. We require an explicit bearer
    here rather than relying on whatever inherited/global Bito auth might happen to
    be reachable, since that would be nondeterministic across machines and could
    let B/C silently degrade to baseline. Returns a structured verdict the runner
    uses to fail loud before wasting a batch."""
    path = engine.CONFIGS / "mcp-arm-bito.json"
    if not path.exists():
        return {"ok": False, "reason": "missing_config",
                "detail": f"{path.name} does not exist — Bito is not configured."}
    data = read_json(path)
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict) or not servers:
        return {"ok": False, "reason": "no_servers",
                "detail": f"{path.name} has no MCP servers."}
    key = next((k for k in servers if looks_like_bito(k)), None)
    if not key:
        return {"ok": False, "reason": "no_bito_server",
                "detail": "No Bito (AI Architect) server found in the arm config."}
    entry = servers.get(key) or {}
    auth = (entry.get("headers") or {}).get("Authorization", "")
    token = auth[len("Bearer "):].strip() if auth.startswith("Bearer ") else ""
    if not token or token.lower() == "none":
        return {"ok": False, "reason": "no_bearer", "server": key,
                "detail": "Bito server has no Authorization bearer — sign-in/token is "
                          "missing or expired. Headless B/C runs will not reach Bito."}
    return {"ok": True, "server": key}


def _ws_token_from_servers(servers: dict) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Pull (workspace_id, bearer token, mcp url) from a Bito server entry."""
    key = next((k for k in (servers or {}) if looks_like_bito(k)), None)
    if not key:
        return None, None, None
    entry = servers.get(key) or {}
    url = entry.get("url") or ""
    ws = None
    if "mcp.bito.ai/" in url:
        try:
            ws = url.split("mcp.bito.ai/", 1)[1].split("/", 1)[0] or None
        except Exception:
            ws = None
    auth = (entry.get("headers") or {}).get("Authorization", "")
    tok = auth[len("Bearer "):].strip() if auth.startswith("Bearer ") else None
    return ws, (tok or None), (url or None)


def _find_bito_credential() -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Best (workspace_id, token, mcp_url) to health-check with, covering all setups:
    1) the app's own OAuth token store, 2) the stamped arm-bito config,
    3) the user's existing ~/.claude.json (the 'reuse what they already have' case)."""
    rec = _load()
    if rec:
        tok = ensure_fresh()
        ws = rec.get("workspace_id")
        if tok and ws:
            return ws, tok, resolve_mcp_url(ws)
    ws, tok, url = _ws_token_from_servers(harness.read_mcp_servers(engine.CONFIGS / "mcp-arm-bito.json"))
    if url:
        return ws, tok, url
    data = read_json(HOME / ".claude.json")
    if isinstance(data, dict):
        return _ws_token_from_servers(data.get("mcpServers") or {})
    return None, None, None


def _health_url(mcp_server_url: str) -> str:
    """Bito exposes a dedicated health endpoint at `/health`, a sibling of the `/mcp`
    path (e.g. https://mcp.bito.ai/<ws>/mcp → https://mcp.bito.ai/<ws>/health).
    Derived from the actual server URL so self-hosted instances work too."""
    base = (mcp_server_url or "").rstrip("/")
    if base.endswith("/mcp"):
        base = base[: -len("/mcp")]
    return base + "/health"


def mcp_health(timeout: int = 8) -> dict:
    """Lightweight check that the configured token ACTUALLY AUTHENTICATES — a single MCP
    `initialize` POST to the `/mcp` endpoint itself (no model tokens, ~instant). We probe
    /mcp, NOT the unauthenticated `/health` sibling, because /health can return 200 for a
    dead token and falsely show "connected"; only /mcp validates the bearer.

    FAIL-SAFE: only an explicit 401/403 (token rejected) or a connection failure / 5xx
    marks Bito down; any other response means the server accepted auth and is up, so we
    never falsely block a working Bito.  reason ∈ ok | reachable | unauthorized |
    unreachable | no_token
    """
    ws, token, url = _find_bito_credential()
    if not url and ws:
        url = resolve_mcp_url(ws)
    if not url:
        return {"ok": False, "reason": "no_token",
                "detail": "Bito is not configured yet — connect it below."}
    if not token:
        # No bearer in our config: we require an explicit token rather than relying
        # on inherited/global Bito auth, so treat as not-yet-usable (not a hard "down").
        return {"ok": False, "reason": "no_token",
                "detail": "No Bito token in the benchmark config — connect/enter a token below."}
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "ab-harness-health", "version": "1.0"}},
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return {"ok": True, "reason": "ok",
                    "detail": f"Bito MCP authenticated (HTTP {getattr(r, 'status', 200)})."}
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {"ok": False, "reason": "unauthorized",
                    "detail": "Bito rejected the token (HTTP %d) — it's invalid or expired. "
                              "Reconnect / enter a fresh token." % e.code}
        if e.code >= 500:
            return {"ok": False, "reason": "unreachable",
                    "detail": f"Bito MCP server error (HTTP {e.code})."}
        # Non-auth 4xx (e.g. 400/406 over protocol shape) — auth was accepted, server is
        # up; treat as reachable so we don't block a working token.
        return {"ok": True, "reason": "reachable",
                "detail": f"Bito MCP accepted auth (HTTP {e.code})."}
    except Exception as e:
        return {"ok": False, "reason": "unreachable",
                "detail": f"Bito MCP isn't responding ({type(e).__name__}). Check your connection."}


def ensure_arm_bito_authed() -> bool:
    """Guarantee the benchmark's Bito config carries a fresh, valid OAuth bearer —
    independent of ~/.claude.json, which the claude CLI rewrites and strips. Refreshes
    the token if expired, then stamps it onto configs/mcp-arm-bito.json. Call before
    every run/probe and at startup. Returns False when not OAuth-connected (e.g. the
    self-hosted static-token path, which manages its own header)."""
    token = ensure_fresh()
    if not token:
        return False
    return _stamp_arm_bito_header(token)


def ensure_bito_ready(static_token: Optional[str] = None) -> dict:
    """Make configs/mcp-arm-bito.json carry a usable Bito bearer, supporting BOTH
    auth modes, then return the verdict callers should fail-loud on. Call before
    every run/probe.

    Precedence:
      1. static_token passed explicitly -> stamp it as-is, no refresh   ("static").
      2. an OAuth session is stored      -> refresh if expired, re-stamp ("oauth").
      3. neither                         -> leave the bearer already in the config
         (written by bito_auth.configure or copied from ~/.claude.json) ("passthrough").

    Returns arm_bito_auth_status()'s verdict (ok/reason/detail/server) plus "mode",
    and — when an OAuth refresh raised — "refresh_error". Never raises.
    """
    mode = "passthrough"
    refresh_error: Optional[str] = None
    tok = (static_token or "").strip()
    if tok:
        mode = "static"
        _stamp_arm_bito_header(tok)
    elif _load():
        mode = "oauth"
        try:
            ensure_arm_bito_authed()
        except Exception as e:  # network/refresh failure — surface it, don't crash
            refresh_error = str(e)
    verdict = arm_bito_auth_status()
    verdict["mode"] = mode
    if refresh_error:
        verdict["refresh_error"] = refresh_error
    return verdict


def status() -> dict:
    rec = _load()
    if not rec:
        return {"connected": False, "mode": None}
    return {
        "connected": True,
        "mode": "oauth",
        "workspace_id": rec.get("workspace_id"),
        "url": resolve_mcp_url(rec.get("workspace_id", "")),
        "expires_at": rec.get("expires_at"),
        "expired": time.time() >= rec.get("expires_at", 0),
    }


def disconnect() -> None:
    rec = _load()
    # Delete the app's OAuth token store first.
    if TOKENS_PATH.exists():
        TOKENS_PATH.unlink()
    # Always remove the Bito server entry (even with no OAuth record — the "reusing an
    # existing ~/.claude.json Bito" case), so the app shows fully disconnected afterwards.
    _remove_token_from_configs(rec.get("workspace_id") if rec else None)


# ---------------------------------------------------------------------------
# Write/remove the bearer header in the tool configs + arm configs
# ---------------------------------------------------------------------------
def _bito_key(servers: dict) -> Optional[str]:
    return next((k for k in servers if looks_like_bito(k)), None)


# ---------------------------------------------------------------------------
# Per-tool config helpers  (claude / copilot — see _TOOL_CONFIG_PATHS)
# ---------------------------------------------------------------------------
def _valid_tools(tools: Optional[list[str]]) -> Optional[list[str]]:
    """Keep only tool ids we know how to configure, preserving order. None stays
    None so callers can distinguish 'not specified' from 'empty list'."""
    if tools is None:
        return None
    return [t for t in tools if t in _TOOL_CONFIG_PATHS]


def _tool_config_path(tool_id: str) -> Optional[Path]:
    return _TOOL_CONFIG_PATHS.get(tool_id)


def _read_tool_servers(tool_id: str) -> dict:
    """Parse a tool's MCP config → {server_name: entry}. Empty if absent/unreadable.
    Both Claude Code and Copilot CLI use the standard ``mcpServers`` map; we also
    accept ``servers`` as a forward-compatible fallback."""
    p = _tool_config_path(tool_id)
    if not p or not p.exists():
        return {}
    data = read_json(p) or {}
    if not isinstance(data, dict):
        return {}
    for key in ("mcpServers", "servers"):
        servers = data.get(key)
        if isinstance(servers, dict):
            return servers
    return {}


def _backup(path: Path) -> None:
    """Timestamped backup before we rewrite a user's CLI config (best-effort)."""
    if not path.exists():
        return
    try:
        import shutil
        bak = path.with_suffix(path.suffix + f".bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, bak)
    except Exception:
        pass


def _stamp_tool_config(tool_id: str, workspace_id: str, token: Optional[str]) -> bool:
    """Merge the Bito MCP server (URL + optional bearer) into one tool's config,
    creating the file/dirs if needed and preserving every other server. Reuses an
    existing Bito key when present, else the canonical ``BitoAIArchitect`` name
    (which Arm A isolation relies on, e.g. copilot's --disable-mcp-server)."""
    p = _tool_config_path(tool_id)
    if not p:
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    data = read_json(p) if p.exists() else {}
    if not isinstance(data, dict):
        data = {}
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        data["mcpServers"] = servers
    key = _bito_key(servers) or DEFAULT_SERVER_KEY
    entry: dict = {"type": "http", "url": resolve_mcp_url(workspace_id)}
    if token:
        entry["headers"] = {"Authorization": f"Bearer {token}"}
    servers[key] = entry
    _backup(p)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return True


def remove_bito_from_tool(tool_id: str) -> bool:
    """Drop every Bito server entry from one tool's config. Returns True if any
    were removed. The file and all other servers are left intact."""
    p = _tool_config_path(tool_id)
    if not p or not p.exists():
        return False
    data = read_json(p)
    if not isinstance(data, dict):
        return False
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        return False
    bito_keys = [k for k in servers if looks_like_bito(k)]
    if not bito_keys:
        return False
    _backup(p)
    for k in bito_keys:
        servers.pop(k, None)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return True


def _tools_with_bito() -> list[str]:
    """Tool ids whose config currently carries a Bito server."""
    return [t for t in _TOOL_CONFIG_PATHS if _bito_key(_read_tool_servers(t))]


def _tool_installed(tool_id: str) -> bool:
    try:
        from ..adapters.registry import get_adapter
        adapter = get_adapter(tool_id)
        return bool(adapter and adapter.detect()[0])
    except Exception:
        return False


def _write_token_to_configs(
    workspace_id: str, access_token: str, tools: Optional[list[str]] = None
) -> None:
    """Put the OAuth bearer on the Bito server in each target tool's config, then
    rebuild the per-arm configs so headless runs use it.

    ``tools`` selects which CLIs to write. When None (e.g. a token refresh) we
    re-stamp every tool that already has a Bito entry, so all configured CLIs stay
    current; on a fresh machine with nothing configured we default to Claude Code."""
    targets = _valid_tools(tools)
    if not targets:
        targets = _tools_with_bito() or ["claude"]
    for tid in targets:
        _stamp_tool_config(tid, workspace_id, access_token)
    # Regenerate arm configs from the updated tool config(s).
    try:
        from . import detection
        detection.build_arm_configs()
    except Exception:
        pass


def _remove_token_from_configs(workspace_id: Optional[str], tools: Optional[list[str]] = None) -> None:
    # Remove the Bito server ENTRY (not just its token header) from the target
    # tools' configs and the arm-bito config, so the app shows disconnected and the
    # Connect UI reappears. ``tools`` defaults to every tool we manage (full sign-out).
    for tid in (_valid_tools(tools) or list(_TOOL_CONFIG_PATHS)):
        remove_bito_from_tool(tid)

    # Remove the Bito server from mcp-arm-bito.json as well.
    from .. import engine
    arm_bito = engine.CONFIGS / "mcp-arm-bito.json"
    if arm_bito.exists():
        try:
            cfg = read_json(arm_bito) or {}
            servers = cfg.get("mcpServers") or {}
            for k in [k for k in servers if looks_like_bito(k)]:
                servers.pop(k, None)
            arm_bito.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Per-tool connect / disconnect / status  (drives the Setup page tool rows)
# ---------------------------------------------------------------------------
def connect_tool(tool_id: str, workspace_id: str = "", token: Optional[str] = None) -> dict:
    """Write the Bito MCP server into one CLI's config.

    Two auth modes:
      - static bearer: when ``token`` is given, stamp it directly (self-hosted Bito
        or any instance where OAuth isn't enabled). No browser step.
      - OAuth: with no token, reuse the app's stored OAuth access token (refreshing
        if expired) so a second tool connects with no extra sign-in. When there is
        no usable token yet, returns ``{"needs_oauth": True}`` and the caller kicks
        off the browser OAuth flow (which targets this same tool)."""
    if tool_id not in _TOOL_CONFIG_PATHS:
        raise ValueError(f"Unknown tool '{tool_id}'.")
    workspace_id = (workspace_id or "").strip()
    token = (token or "").strip()

    if token:
        # Static-bearer path — no OAuth required.
        if not workspace_id:
            raise ValueError("Workspace ID or MCP URL is required.")
        _stamp_tool_config(tool_id, workspace_id, token)
        _rebuild_arm_configs()
        return {"ok": True, "needs_oauth": False, "tool": tool_id, "auth": "static"}

    live = ensure_fresh()  # live OAuth token (refreshes if needed), else None
    if not live:
        return {"ok": False, "needs_oauth": True, "tool": tool_id}

    if not workspace_id:
        rec = _load()
        workspace_id = (rec or {}).get("workspace_id") or ""
    if not workspace_id:
        raise ValueError("Workspace ID or MCP URL is required.")

    _stamp_tool_config(tool_id, workspace_id, live)
    _rebuild_arm_configs()
    return {"ok": True, "needs_oauth": False, "tool": tool_id, "auth": "oauth"}


def _rebuild_arm_configs() -> None:
    try:
        from . import detection
        detection.build_arm_configs()
    except Exception:
        pass


def _restamp_tool_header(tool_id: str, token: str) -> bool:
    """Refresh ONLY the Authorization bearer on the existing Bito entry in a tool's
    config, preserving its URL (hosted or custom) and every other server. Cheap, no
    backup — meant to be called repeatedly (e.g. before each benchmark run)."""
    p = _tool_config_path(tool_id)
    if not p or not p.exists():
        return False
    data = read_json(p)
    if not isinstance(data, dict):
        return False
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        return False
    key = _bito_key(servers)
    if not key or not isinstance(servers.get(key), dict):
        return False
    servers[key].setdefault("type", "http")
    servers[key]["headers"] = {"Authorization": f"Bearer {token}"}
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return True


def ensure_tool_bito_authed(tool_id: str) -> bool:
    """Refresh the app's OAuth token (if expired) and re-stamp it onto THIS tool's
    own live MCP config, so a long batch can't drift to a stale/stripped bearer.
    Returns False (no-op) for static-token setups — the existing bearer is kept."""
    token = ensure_fresh()
    if not token:
        return False
    return _restamp_tool_header(tool_id, token)


def ensure_run_bito_authed(tool_id: str) -> bool:
    """Make the Bito bearer fresh in the config the given CLI will actually read for
    a B/C run: ``mcp-arm-bito.json`` for Claude Code (isolated, passed via
    --mcp-config), or the live ``~/.copilot/mcp-config.json`` for Copilot (read
    directly each run). Safe to call before every run; static-token setups no-op."""
    if tool_id == "claude":
        return ensure_arm_bito_authed()
    return ensure_tool_bito_authed(tool_id)


def disconnect_tool(tool_id: str) -> dict:
    """Remove Bito from one CLI's config. When the last configured tool is removed,
    also clear the app's stored OAuth token so the app reads as fully signed out."""
    if tool_id not in _TOOL_CONFIG_PATHS:
        raise ValueError(f"Unknown tool '{tool_id}'.")
    removed = remove_bito_from_tool(tool_id)
    if not _tools_with_bito() and TOKENS_PATH.exists():
        try:
            TOKENS_PATH.unlink()
        except Exception:
            pass
    # Keep the benchmark arm configs in step with the remaining tools.
    try:
        from . import detection
        detection.build_arm_configs()
    except Exception:
        pass
    return {"ok": True, "tool": tool_id, "removed": removed}


def per_tool_status() -> dict:
    """Per-tool Bito MCP config state for the Setup page.

    For each managed CLI we report whether it is installed, whether a Bito server
    is present in its config (``configured``), and whether that entry carries a
    bearer (``has_token``). ``oauth_live`` is the app-wide OAuth session state used
    to render an accurate per-tool 'Connected' vs 'Configured (no token)' badge."""
    rec = _load()
    oauth_live = bool(rec) and not (time.time() >= rec.get("expires_at", 0))
    tools: dict[str, dict] = {}
    for tid in _TOOL_CONFIG_PATHS:
        servers = _read_tool_servers(tid)
        key = _bito_key(servers)
        ws, tok, url = _ws_token_from_servers(servers) if key else (None, None, None)
        tools[tid] = {
            "configured": key is not None,
            "has_token": bool(tok),
            "workspace_id": ws,
            "url": url,
            "installed": _tool_installed(tid),
            "config_path": str(_TOOL_CONFIG_PATHS[tid]),
        }
    return {"tools": tools, "oauth_live": oauth_live}
