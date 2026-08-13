"""GitHub Copilot CLI adapter (standalone `copilot` binary).

Mirrors ClaudeCodeAdapter as closely as possible, using the standalone
`copilot` binary that ships with GitHub Copilot CLI (not the `gh copilot`
extension).

Headless execution pattern (equivalent to `claude -p`):
    copilot -p "<prompt>" \\
        --output-format json \\
        --allow-all-tools --allow-all-paths --allow-all-urls \\
        --no-ask-user --no-auto-update

MCP / Bito isolation:
    Copilot CLI stores MCP servers in ~/.copilot/mcp-config.json (macOS/Linux)
    or %USERPROFILE%\\.copilot\\mcp-config.json (Windows).  BitoAIArchitect
    configured there via `/mcp add` is available to Arms B and C automatically.
    For Arm A we pass --disable-mcp-server BitoAIArchitect so Bito is blocked
    at the tool level — same isolation intent as the --mcp-config arm-a.json
    approach used by ClaudeCodeAdapter.

Windows + macOS compatible throughout: shutil.which(), Path.home(), win_safe_cmd().
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

from .base import (
    HOME,
    McpStatus,
    RunResult,
    ToolAdapter,
    bito_status_from_servers,
    detect_usage_limit,
    looks_like_bito,
    read_json,
    run_streaming,
    win_safe_cmd,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _win_find_copilot() -> Optional[str]:
    """Windows-only: locate copilot.exe across every known install method.

    Install methods covered:
      winget   — %LOCALAPPDATA%\\Microsoft\\WinGet\\Links\\copilot.exe
                 %LOCALAPPDATA%\\Microsoft\\WinGet\\Packages\\GitHub.CopilotCLI*\\copilot.exe
      scoop    — %USERPROFILE%\\scoop\\shims\\copilot.exe
                 %USERPROFILE%\\scoop\\apps\\copilot\\current\\copilot.exe
                 %SCOOP%\\shims\\copilot.exe  (custom SCOOP env var)
      choco    — %ProgramData%\\chocolatey\\bin\\copilot.exe
      direct   — %LOCALAPPDATA%\\Programs\\GitHub Copilot CLI\\copilot.exe
                 %PROGRAMFILES%\\GitHub Copilot CLI\\copilot.exe
                 %PROGRAMFILES(X86)%\\GitHub Copilot CLI\\copilot.exe
      portable — %USERPROFILE%\\bin\\copilot.exe
                 %USERPROFILE%\\.local\\bin\\copilot.exe
    """
    import glob as _glob

    local    = os.environ.get("LOCALAPPDATA", "")
    appdata  = os.environ.get("APPDATA", "")           # %ROAMING%
    prog64   = os.environ.get("PROGRAMFILES", "")
    prog86   = os.environ.get("PROGRAMFILES(X86)", "")
    progdata = os.environ.get("PROGRAMDATA", "")
    userprof = os.environ.get("USERPROFILE", str(HOME))
    scoop_root = os.environ.get("SCOOP", os.path.join(userprof, "scoop"))

    candidates: list[str] = []

    if local:
        # winget — symlink dir (on PATH after install)
        candidates.append(os.path.join(local, "Microsoft", "WinGet", "Links", "copilot.exe"))
        # winget — package folder (hash suffix → glob)
        candidates += _glob.glob(os.path.join(
            local, "Microsoft", "WinGet", "Packages", "GitHub.CopilotCLI*", "copilot.exe"
        ))
        # direct download / MSI to LocalAppData\Programs
        candidates += [
            os.path.join(local, "Programs", "GitHub Copilot CLI", "copilot.exe"),
            os.path.join(local, "Programs", "copilot", "copilot.exe"),
            os.path.join(local, "Programs", "copilot.exe"),
        ]

    # scoop shim (covers custom SCOOP root too)
    candidates += [
        os.path.join(scoop_root, "shims", "copilot.exe"),
        os.path.join(scoop_root, "apps", "copilot", "current", "copilot.exe"),
    ]

    # chocolatey
    if progdata:
        candidates.append(os.path.join(progdata, "chocolatey", "bin", "copilot.exe"))

    # system-wide installs
    for _d in [prog64, prog86]:
        if _d:
            candidates.append(os.path.join(_d, "GitHub Copilot CLI", "copilot.exe"))

    # portable / manual install — user puts the binary in ~/bin or ~/.local/bin
    candidates += [
        os.path.join(userprof, "bin", "copilot.exe"),
        os.path.join(userprof, ".local", "bin", "copilot.exe"),
    ]

    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def _copilot_exe() -> Optional[str]:
    """Locate the standalone `copilot` binary.

    shutil.which() only sees the PATH the Python process inherited at startup.
    On macOS/Linux that covers most installs; a few extra dirs are probed below.
    On Windows, `where` also uses the inherited PATH snapshot, so we additionally
    probe every known install location via _win_find_copilot().
    """
    path = shutil.which("copilot")
    if not path:
        if os.name == "nt":
            # 1. `where` — works when PATH was current at server-start time.
            try:
                r = subprocess.run(
                    ["where", "copilot"], capture_output=True, text=True, timeout=5
                )
                if r.returncode == 0:
                    first = r.stdout.strip().splitlines()[0].strip()
                    if first:
                        path = first
            except Exception:
                pass
            # 2. Direct filesystem probing — covers installs that happened after
            #    the server started, or that land in directories not on PATH.
            if not path:
                path = _win_find_copilot()
        else:
            # macOS / Linux: check common user-local install dirs
            for candidate in [
                HOME / ".local" / "bin" / "copilot",
                Path("/usr/local/bin/copilot"),
                Path("/opt/homebrew/bin/copilot"),
            ]:
                if candidate.is_file():
                    path = str(candidate)
                    break
    return path


def _copilot_version() -> Optional[str]:
    exe = _copilot_exe()
    if not exe:
        return None
    try:
        r = subprocess.run(
            win_safe_cmd([exe, "--version"]),
            capture_output=True, text=True, timeout=10,
        )
        text = (r.stdout or r.stderr or "").strip()
        return text.splitlines()[0] if text else "installed"
    except Exception:
        return None


def _mcp_config_path() -> Path:
    """~/.copilot/mcp-config.json — same location on macOS, Linux, Windows."""
    return HOME / ".copilot" / "mcp-config.json"


def _read_mcp_servers() -> dict[str, dict]:
    """Parse ~/.copilot/mcp-config.json → {server_name: server_dict}.

    Copilot CLI uses the standard {"mcpServers": {...}} shape.  We also try
    {"servers": {...}} as a fallback in case the format changes in future
    versions.
    """
    p = _mcp_config_path()
    if not p.exists():
        return {}
    data = read_json(p) or {}
    for key in ("mcpServers", "servers"):
        servers = data.get(key)
        if isinstance(servers, dict):
            return servers
    return {}


def _arm_a_disallow(disallowed_tools: Optional[list[str]]) -> bool:
    """True when the caller passed Bito disallow patterns → this is Arm A."""
    if not disallowed_tools:
        return False
    return any(
        "BitoAIArchitect" in t or "bito" in t.lower()
        for t in disallowed_tools
    )


# ---------------------------------------------------------------------------
# JSONL output parser
# ---------------------------------------------------------------------------

def _parse_copilot_jsonl(path: Path) -> dict:
    """Extract a normalised summary from copilot --output-format json output.

    copilot emits one JSON object per line (JSONL).  The exact schema differs
    from Claude's stream-json but carries similar concepts: assistant messages,
    tool_use / tool_result events, and a final result/stats line.  We parse
    defensively so any unknown lines are silently skipped.

    Returns a dict with keys that mirror harness.parse_stream_jsonl() where
    available: result_text, tool_calls, num_turns, duration_ms, model,
    input_tokens, output_tokens, total_cost_usd.
    """
    if not path.exists():
        return {}

    result_text: Optional[str] = None
    tool_calls: list[dict] = []
    num_turns: Optional[int] = None
    duration_ms: Optional[int] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_cost_usd: Optional[float] = None
    model_used: Optional[str] = None
    text_chunks: list[str] = []

    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue

        etype = obj.get("type", "")
        # Copilot nests payload under "data"; flatten it for uniform access below.
        data = obj.get("data") if isinstance(obj.get("data"), dict) else {}

        # --- Copilot "assistant.message" — primary text + tool-request carrier ---
        if etype == "assistant.message":
            content = data.get("content", "")
            if isinstance(content, str) and content.strip():
                text_chunks.append(content)
            # tool calls come as toolRequests list inside data
            for req in data.get("toolRequests") or []:
                if not isinstance(req, dict):
                    continue
                tool_calls.append({
                    "name": req.get("name", ""),
                    "input": req.get("arguments") or req.get("input") or {},
                    "count": 1,
                })
            if not model_used:
                model_used = data.get("model")

        # --- generic assistant message shapes (other adapters / future formats) ---
        elif etype in ("assistant", "message") or (
            etype not in ("result", "completion", "done") and "content" in obj
        ):
            content = obj.get("content") or obj.get("message", {}).get("content", [])
            if isinstance(content, str):
                text_chunks.append(content)
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type", "")
                    if btype == "text":
                        text_chunks.append(block.get("text", ""))
                    elif btype in ("tool_use", "tool_call"):
                        tool_calls.append({
                            "name": block.get("name", ""),
                            "input": block.get("input") or block.get("parameters") or {},
                            "count": 1,
                        })

        # --- tool call event ---
        elif etype in ("tool_use", "tool_call"):
            tool_calls.append({
                "name": obj.get("name", ""),
                "input": obj.get("input") or obj.get("parameters") or {},
                "count": 1,
            })

        # --- final result / stats line ---
        # Copilot's result event has exitCode + usage but NO text (text came via
        # assistant.message events above).  We still extract usage/duration here.
        elif etype in ("result", "completion", "done"):
            result_text = (
                obj.get("result")
                or obj.get("text")
                or obj.get("response")
                or obj.get("content")
                or result_text
            )
            stats = obj.get("usage") or obj.get("stats") or obj
            num_turns = _int(stats.get("num_turns") or stats.get("turns"))
            duration_ms = _int(
                stats.get("totalApiDurationMs")
                or stats.get("duration_ms")
                or stats.get("duration")
                or (stats.get("duration_s") and int(stats["duration_s"] * 1000))
            )
            input_tokens = _int(stats.get("input_tokens") or stats.get("prompt_tokens"))
            output_tokens = _int(stats.get("output_tokens") or stats.get("completion_tokens"))
            total_cost_usd = _float(stats.get("total_cost_usd") or stats.get("cost"))
            model_used = obj.get("model") or stats.get("model") or model_used

        # --- plain text / response line ---
        elif "text" in obj or "response" in obj or "result" in obj:
            candidate = obj.get("text") or obj.get("response") or obj.get("result")
            if isinstance(candidate, str) and candidate.strip():
                result_text = candidate

        # --- model info ---
        if not model_used:
            model_used = obj.get("model") or data.get("model")

    # Assemble result_text from collected text chunks if no explicit result line.
    if not result_text and text_chunks:
        result_text = "\n".join(t for t in text_chunks if t.strip()).strip() or None

    return {
        "result_text": result_text,
        "tool_calls": tool_calls,
        "num_turns": num_turns,
        "duration_ms": duration_ms,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_cost_usd": total_cost_usd,
        "model": model_used,
    }


def _int(v) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _float(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class GitHubCopilotAdapter(ToolAdapter):
    id = "copilot"
    name = "GitHub Copilot CLI"
    kind = "cli"
    supports_headless = True
    headless_note = "Runs via `copilot -p` (headless), the standalone GitHub Copilot CLI binary."

    # ------------------------------------------------------------------ detect

    def detect(self) -> tuple[bool, Optional[str], str]:
        exe = _copilot_exe()
        if not exe:
            return (
                False, None,
                "`copilot` binary not found on PATH. "
                "Install GitHub Copilot CLI from https://docs.github.com/copilot/how-tos/copilot-cli",
            )
        version = _copilot_version() or "installed"
        return True, version, f"Found copilot CLI at {exe}"

    # ----------------------------------------------------------------- mcp cfg

    def mcp_config_path(self) -> Optional[Path]:
        """~/.copilot/mcp-config.json — present on macOS, Linux, and Windows."""
        return _mcp_config_path()

    def read_mcp_servers(self) -> dict[str, dict]:
        return _read_mcp_servers()

    def mcp_status(self) -> McpStatus:
        """Detect Bito AI Architect status from ~/.copilot/mcp-config.json."""
        servers = _read_mcp_servers()
        if not servers:
            p = _mcp_config_path()
            if not p.exists():
                return McpStatus(
                    state="missing",
                    detail=(
                        "No MCP config found at ~/.copilot/mcp-config.json. "
                        "Open GitHub Copilot CLI, run /mcp add, and add the "
                        "Bito AI Architect server URL."
                    ),
                )
            return McpStatus(
                state="missing",
                detail=(
                    "No MCP servers configured in ~/.copilot/mcp-config.json. "
                    "Open GitHub Copilot CLI, run /mcp add, and add the "
                    "Bito AI Architect server URL."
                ),
            )
        return bito_status_from_servers(servers)

    # -------------------------------------------------------------------- run

    def run(
        self,
        *,
        prompt_text: str,
        mcp_config_path: Path,          # accepted for interface parity; isolation
        model: str,                     # passed to --model when provided
        max_turns: int,                 # not directly supported; kept for parity
        work_dir: Path,
        stream_path: Path,
        on_proc: Optional[Callable[[subprocess.Popen], None]] = None,
        disallowed_tools: Optional[list[str]] = None,
    ) -> RunResult:
        """Headless `copilot -p` run, parsed into a normalised RunResult.

        MCP / Bito isolation:
          Arm A  → --disable-mcp-server BitoAIArchitect  (blocks Bito from
                   the user's ~/.copilot/mcp-config.json for this run only)
          Arms B/C → no extra flag; Bito from ~/.copilot/mcp-config.json is
                     used automatically (user must have added it via /mcp add).
        """
        exe = _copilot_exe()
        if not exe:
            return RunResult(exit_code=127, error="`copilot` binary not found on PATH.")

        work_dir.mkdir(parents=True, exist_ok=True)

        is_arm_a = _arm_a_disallow(disallowed_tools)
        # Arm A isolation must disable Bito by its ACTUAL server name(s) — the user may
        # have added it under a non-canonical key (e.g. "BitoAIArchitectSelf"), and a
        # hardcoded "BitoAIArchitect" would miss it and leak Bito into the baseline arm.
        bito_servers = (
            [k for k in _read_mcp_servers() if looks_like_bito(k)] if is_arm_a else []
        )
        cmd = _build_cmd(
            exe=exe,
            prompt=prompt_text,
            model=model,
            is_arm_a=is_arm_a,
            bito_servers=bito_servers,
        )

        rc, wall = run_streaming(
            win_safe_cmd(cmd),
            cwd=work_dir,
            stream_path=stream_path,
            on_proc=on_proc,
        )

        summary = _parse_copilot_jsonl(stream_path) if stream_path.exists() else {}

        result_text = summary.get("result_text")
        limit = detect_usage_limit(result_text)
        if limit:
            error: Optional[str] = f"usage_limit: {limit}"
        elif rc == 0 and result_text:
            error = None
        else:
            error = (
                f"exit_code={rc}"
                + (f" result_len={len(result_text or '')}" if result_text else "")
            )

        return RunResult(
            response=result_text,
            exit_code=rc,
            error=error,
            duration_ms=int(wall * 1000),
            duration_api_ms=summary.get("duration_ms"),
            num_turns=summary.get("num_turns"),
            input_tokens=summary.get("input_tokens"),
            output_tokens=summary.get("output_tokens"),
            total_cost_usd=summary.get("total_cost_usd"),
            model=summary.get("model") or "github-copilot",
            tool_calls=summary.get("tool_calls") or [],
            stream_path=str(stream_path),
        )

    # ----------------------------------------------------------------- complete

    def complete(
        self, *, prompt: str, mcp_config_path: Path, model: str, max_turns: int
    ) -> dict:
        """One-shot judge call via `copilot -p`, used by the blind judge."""
        exe = _copilot_exe()
        if not exe:
            return {"ok": False, "text": None, "cost": None, "error": "`copilot` binary not found."}

        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as tmp:
            out_path = pathlib.Path(tmp) / "out.jsonl"
            cmd = win_safe_cmd(_build_cmd(exe=exe, prompt=prompt, model=model, is_arm_a=False))
            env = {
                **os.environ,
                "GIT_TERMINAL_PROMPT": "0",
                "GCM_INTERACTIVE": "Never",
                "NO_COLOR": "1",
            }
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True, text=True,
                    encoding="utf-8", errors="replace",
                    cwd=tmp, env=env, timeout=300,
                )
            except subprocess.TimeoutExpired:
                return {"ok": False, "text": None, "cost": None, "error": "copilot timed out"}
            except Exception as exc:
                return {"ok": False, "text": None, "cost": None, "error": str(exc)}

            # Parse stdout directly as JSONL.
            out_path.write_text(result.stdout or "", encoding="utf-8")
            summary = _parse_copilot_jsonl(out_path)
            response = summary.get("result_text")
            ok = result.returncode == 0 and bool(response)
            return {
                "ok": ok,
                "text": response,
                "cost": summary.get("total_cost_usd"),
                "error": None if ok else f"exit_code={result.returncode} {(result.stderr or '')[:150]}",
            }


# ---------------------------------------------------------------------------
# Command builder
# ---------------------------------------------------------------------------

def _build_cmd(
    *,
    exe: str,
    prompt: str,
    model: str,
    is_arm_a: bool,
    bito_servers: Optional[list[str]] = None,
) -> list[str]:
    """Build the `copilot -p` command list for a headless benchmark run.

    Flags chosen to mirror `claude -p --output-format stream-json`:
      -p                  non-interactive prompt mode (exits after completion)
      --output-format json  JSONL output for structured parsing
      --allow-all-tools   required for non-interactive; no permission prompts
      --allow-all-paths   allow file access without prompts
      --allow-all-urls    allow URL access without prompts
      --no-ask-user       disable ask_user tool so agent runs autonomously
      --no-auto-update    skip update check during benchmarks (saves time)
      --no-color          clean output for log files

    Arm A isolation:
      --disable-mcp-server <name>  blocks Bito for this run only, by its ACTUAL
      server name(s) in ~/.copilot/mcp-config.json (Bito stays in the file; only
      this session skips it). Falls back to the canonical "BitoAIArchitect" when
      the caller didn't resolve a name, so isolation never silently no-ops.
    """
    cmd: list[str] = [
        exe,
        "-p", prompt,
        "--output-format", "json",
        "--allow-all-tools",
        "--allow-all-paths",
        "--allow-all-urls",
        "--no-ask-user",
        "--no-auto-update",
        "--no-color",
    ]

    # Only pass --model when the caller explicitly set a non-Claude model ID.
    # When the harness default (claude-opus-4-7 etc.) is passed, skip --model
    # entirely so Copilot uses its own default for the user's plan/subscription.
    if model and not _is_claude_model_id(model):
        cmd += ["--model", model]

    # Arm A: disable every Bito MCP server (by real name) so it can't reach AI Architect.
    if is_arm_a:
        names = bito_servers or ["BitoAIArchitect"]
        for name in names:
            cmd += ["--disable-mcp-server", name]

    return cmd


def _is_claude_model_id(model: str) -> bool:
    """True for Claude model IDs that Copilot CLI won't understand."""
    m = model.lower()
    return (
        m.startswith("claude")
        or m.startswith("us.anthropic")
        or "anthropic" in m
        or "opus" in m
        or "sonnet" in m
        or "haiku" in m
    )
