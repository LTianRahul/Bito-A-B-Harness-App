"""Tool-adapter interface + shared data types.

Each code-gen tool (Claude Code, Cursor, Windsurf, Cline, …) gets a subclass of
``ToolAdapter``. Every adapter implements **detection** and **MCP status** so the
Setup page can show all tools uniformly. Adapters whose tool exposes a real
headless interface also implement ``run()`` (Phase 4); the rest set
``supports_headless = False`` and the runner skips them honestly.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

HOME = Path.home()


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------
@dataclass
class McpStatus:
    """How a tool's Bito AI Architect MCP looks in that tool's own config."""

    # configured | missing | invalid | needs-auth
    state: str = "missing"
    server_key: Optional[str] = None      # the MCP server name in the config
    url: Optional[str] = None             # the MCP URL, if any
    workspace_id: Optional[str] = None    # parsed from a mcp.bito.ai/<id>/mcp URL
    via_one_command: bool = False         # looks like Bito's one-command setup
    auth_kind: Optional[str] = None       # token | oauth | none
    detail: str = ""                      # human-friendly explanation / next step
    other_servers: list[str] = field(default_factory=list)


@dataclass
class ToolInfo:
    id: str                               # stable id e.g. "claude"
    name: str                             # display name e.g. "Claude Code"
    kind: str                             # "cli" | "ide-extension"
    installed: bool = False
    version: Optional[str] = None
    detect_detail: str = ""               # how we detected (or why not)
    supports_headless: bool = False       # can we run benchmarks through it?
    headless_note: str = ""               # why not, if not
    config_path: Optional[str] = None     # where its MCP config lives
    mcp: McpStatus = field(default_factory=McpStatus)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


@dataclass
class RunResult:
    """Normalized result of one (arm, prompt) run, matching the columns the
    harness ``runs`` table already stores. Nullable where a tool doesn't report
    a metric (the UI shows 'n/a')."""

    response: Optional[str] = None
    exit_code: int = -1
    error: Optional[str] = None
    duration_ms: Optional[int] = None
    duration_api_ms: Optional[int] = None
    num_turns: Optional[int] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    cache_creation_tokens: Optional[int] = None
    total_cost_usd: Optional[float] = None
    model: Optional[str] = None
    tool_calls: list[dict] = field(default_factory=list)
    session_id: Optional[str] = None
    ttft_ms: Optional[int] = None
    stream_path: Optional[str] = None     # raw transcript for analytics


# ---------------------------------------------------------------------------
# Bito MCP heuristics (shared by all adapters)
# ---------------------------------------------------------------------------
def looks_like_bito(name: str) -> bool:
    n = name.lower()
    return "bito" in n or "architect" in n


def classify_bito_server(key: str, server: dict) -> McpStatus:
    """Given an MCP server entry, decide configured/invalid/needs-auth and pull
    out the workspace id. Pure heuristic over config shape — the only *proof*
    the MCP works is the live probe (doctor)."""
    url = server.get("url") or server.get("serverUrl") or ""
    headers = server.get("headers") or {}
    env = server.get("env") or {}
    has_token = bool(
        headers.get("Authorization")
        or headers.get("authorization")
        or server.get("token")
        or any("TOKEN" in k.upper() or "KEY" in k.upper() for k in env)
    )
    workspace_id = None
    via_one_command = False
    if "mcp.bito.ai/" in url:
        via_one_command = True
        try:
            # https://mcp.bito.ai/<workspace-id>/mcp
            tail = url.split("mcp.bito.ai/", 1)[1]
            raw_id = tail.split("/", 1)[0] or None
            # Reject unexpanded env-var placeholders like ${BITO_WORKSPACE_ID}
            # or %BITO_WORKSPACE_ID% that some setup scripts leave behind.
            if raw_id and not (raw_id.startswith("${") or raw_id.startswith("%")):
                workspace_id = raw_id
        except Exception:
            workspace_id = None

    st = McpStatus(
        server_key=key,
        url=url or None,
        workspace_id=workspace_id,
        via_one_command=via_one_command,
        auth_kind="token" if has_token else ("oauth" if url else "none"),
    )
    if not url:
        st.state = "invalid"
        st.detail = "A Bito server is present but has no URL. Re-run setup or re-enter your Workspace ID."
        return st
    # URL contains an unexpanded env-var placeholder — treat same as missing.
    if "${" in url or (url.count("%") >= 2 and url.upper() == url):
        st.state = "invalid"
        st.workspace_id = None
        st.detail = ("Workspace ID is an unexpanded placeholder (e.g. ${BITO_WORKSPACE_ID}). "
                     "Enter your real Workspace ID and reconnect.")
        return st
    # URL is present → treat as configured. We can't reliably tell from the
    # config file alone whether an OAuth sign-in has happened (Claude Code stores
    # OAuth tokens outside this file), so config-shape is only a hint — the live
    # check is the real authority on whether the MCP answers.
    st.state = "configured"
    if has_token:
        st.detail = "Bito MCP is configured with a token. Run a live check to confirm it's reachable."
    else:
        st.auth_kind = "oauth"
        st.detail = "Bito MCP is configured (sign-in handled by the tool). Run a live check to confirm."
    return st


# The one canonical Bito server the benchmark uses. If several bito-ish servers
# exist (e.g. self-hosted, internal), we use ONLY this exact key and ignore the rest.
BITO_SERVER_KEY = "BitoAIArchitect"


def pick_bito_key(servers: dict[str, dict]) -> Optional[str]:
    """Choose THE Bito server: prefer the exact ``BitoAIArchitect`` key, else fall
    back to any bito/architect-named server, else any server whose URL is bito.ai."""
    if BITO_SERVER_KEY in servers:
        return BITO_SERVER_KEY
    named = [k for k in servers if looks_like_bito(k)]
    if named:
        return named[0]
    url_based = [k for k, v in servers.items() if "mcp.bito.ai/" in str(v.get("url", ""))]
    return url_based[0] if url_based else None


def bito_status_from_servers(servers: dict[str, dict]) -> McpStatus:
    """Pick the Bito server out of a tool's MCP servers and classify it."""
    key = pick_bito_key(servers)
    others = [k for k in servers if k != key]
    if not key:
        st = McpStatus(state="missing", detail="No Bito AI Architect MCP found for this tool.")
        st.other_servers = others
        return st
    st = classify_bito_server(key, servers[key])
    st.other_servers = others
    return st


_USAGE_LIMIT_MARKERS = (
    "session limit",
    "usage limit",
    "rate limit",
    "you've hit your",
    "you have hit your",
    "resets ",
    "upgrade to increase your usage",
)


def detect_usage_limit(text: Optional[str]) -> Optional[str]:
    """If a model response is actually an account usage/rate-limit notice rather
    than a real answer, return the notice text; else None. Lets the runner stop
    the batch and the UI explain it clearly instead of showing a generic fail."""
    if not text:
        return None
    low = text.strip().lower()
    if len(low) < 240 and any(m in low for m in _USAGE_LIMIT_MARKERS):
        return text.strip()
    return None


def read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def win_safe_cmd(cmd: list[str]) -> list[str]:
    """On Windows, a .cmd/.bat shim (how npm installs `claude`, and `npm`/`cursor-agent`
    themselves) can't be launched directly via subprocess — CreateProcess raises
    [WinError 193]. Route those through cmd.exe. No-op on POSIX, for real executables,
    and for already-wrapped commands."""
    if os.name == "nt" and cmd and isinstance(cmd[0], str) \
            and cmd[0].lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", *cmd]
    return cmd


def run_streaming(
    cmd: list[str],
    cwd: Path,
    stream_path: Path,
    on_proc: Optional[Callable[[subprocess.Popen], None]] = None,
) -> tuple[int, float]:
    """Run a subprocess, writing stdout line-by-line to ``stream_path``.

    Like ``harness.run_claude_stream_json`` but exposes the live ``Popen`` via
    ``on_proc`` so the runner can terminate it for a Stop action. Returns
    (exit_code, wall_seconds).
    """
    stream_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    # Never pop an interactive auth dialog (e.g. Windows GitHub-login popup when the
    # model runs `git clone`) — a headless benchmark must fail fast, not hang.
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",   # git: never prompt on the terminal
        "GCM_INTERACTIVE": "Never",   # Git Credential Manager: no GUI popup
        "GH_PROMPT_DISABLED": "1",    # gh CLI: no interactive prompts
    }
    with open(stream_path, "w", encoding="utf-8") as out:
        proc = subprocess.Popen(
            win_safe_cmd(cmd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(cwd),
            env=env,
            # New session/process group so Stop can kill the WHOLE tree (the CLI
            # plus the model/MCP child processes), not just the launcher.
            start_new_session=(os.name != "nt"),
        )
        if on_proc:
            on_proc(proc)
        assert proc.stdout is not None
        for line in proc.stdout:
            out.write(line)
            out.flush()
        stderr = proc.stderr.read() if proc.stderr else ""
        rc = proc.wait()
    elapsed = time.perf_counter() - t0
    if stderr.strip():
        stream_path.with_suffix(".stderr.txt").write_text(stderr, encoding="utf-8")
    return rc, elapsed


def cli_version(binary: str, *args: str) -> Optional[str]:
    """Best-effort `<binary> --version`. Returns trimmed first line or None."""
    exe = shutil.which(binary)
    if not exe:
        return None
    try:
        out = subprocess.run(
            win_safe_cmd([exe, *(args or ("--version",))]),
            capture_output=True, text=True, timeout=8,
        )
        text = (out.stdout or out.stderr or "").strip()
        return text.splitlines()[0] if text else exe
    except Exception:
        return exe


# ---------------------------------------------------------------------------
# Adapter interface
# ---------------------------------------------------------------------------
class ToolAdapter:
    id: str = "tool"
    name: str = "Tool"
    kind: str = "cli"
    supports_headless: bool = False
    headless_note: str = ""

    # --- detection ---
    def detect(self) -> tuple[bool, Optional[str], str]:
        """Return (installed, version, detail)."""
        raise NotImplementedError

    def mcp_config_path(self) -> Optional[Path]:
        """Where this tool stores its MCP servers (may not exist)."""
        return None

    def read_mcp_servers(self) -> dict[str, dict]:
        """Parse the tool's MCP config into {name: server}. Empty if none."""
        path = self.mcp_config_path()
        if not path or not path.exists():
            return {}
        data = read_json(path) or {}
        servers = data.get("mcpServers")
        return servers if isinstance(servers, dict) else {}

    def mcp_status(self) -> McpStatus:
        return bito_status_from_servers(self.read_mcp_servers())

    def info(self) -> ToolInfo:
        installed, version, detail = self.detect()
        path = self.mcp_config_path()
        return ToolInfo(
            id=self.id,
            name=self.name,
            kind=self.kind,
            installed=installed,
            version=version,
            detect_detail=detail,
            supports_headless=self.supports_headless,
            headless_note=self.headless_note,
            config_path=str(path) if path else None,
            mcp=self.mcp_status() if installed else McpStatus(
                state="missing", detail="Tool not detected on this machine."
            ),
        )

    # --- running (Phase 4); only headless-capable adapters override ---
    def run(
        self,
        *,
        prompt_text: str,
        mcp_config_path: Path,
        model: str,
        max_turns: int,
        work_dir: Path,
        stream_path: Path,
        on_proc: Optional[Callable[[subprocess.Popen], None]] = None,
        disallowed_tools: Optional[list[str]] = None,
    ) -> RunResult:
        """Execute one prompt headlessly under the given per-arm MCP config and
        return a normalized RunResult. Overridden by headless-capable adapters.

        disallowed_tools: optional tool-deny patterns (e.g. ["mcp__BitoAIArchitect__*"]
        for Arm A) passed through to the CLI where supported."""
        raise NotImplementedError(
            f"{self.name} does not support headless benchmark runs."
        )

    def complete(self, *, prompt: str, mcp_config_path: Path, model: str, max_turns: int) -> dict:
        """One-shot prompt → text, used by the blind judge so the benchmark's
        selected tool is also the judge (keeps token usage on that tool). Returns
        {ok, text, cost, error}. Overridden by tools that support it."""
        raise NotImplementedError(f"{self.name} cannot act as the judge.")
