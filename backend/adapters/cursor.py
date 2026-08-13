"""Cursor adapter.

Cursor ships a headless agent CLI (`cursor-agent`) with a print mode, so it can
be driven for benchmarks (run() lands in Phase 4). MCP servers live in
``~/.cursor/mcp.json``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Optional

from .base import HOME, RunResult, ToolAdapter, cli_version, read_json, run_streaming, win_safe_cmd


def _cursor_model_args(model: Optional[str]) -> list[str]:
    """Only pass --model to cursor-agent when it's a Cursor-valid model. The
    benchmark's default is a Claude Code id (e.g. claude-opus-4-7) which Cursor
    does NOT understand, so in that case we omit --model and let Cursor use its
    configured default."""
    if not model:
        return []
    m = model.lower()
    if m.startswith("claude") or m.startswith("us.anthropic") or "opus-4-7" in m:
        return []
    return ["--model", model]


# Flags that make a headless cursor-agent run actually work for benchmarking:
#  -p/--output-format json : non-interactive, parseable output
#  --force                  : don't prompt for tool/command permission
#  --approve-mcps           : load & approve MCP servers (so Bito actually activates)
#  --trust                  : trust the (fresh, empty) workspace without prompting
_CURSOR_HEADLESS = ["--output-format", "json", "--force", "--approve-mcps", "--trust"]


class CursorAdapter(ToolAdapter):
    id = "cursor"
    name = "Cursor"
    kind = "cli"
    supports_headless = True
    headless_note = "Runs via the `cursor-agent` headless CLI."

    def detect(self) -> tuple[bool, Optional[str], str]:
        agent = shutil.which("cursor-agent")
        if agent:
            return True, cli_version("cursor-agent") or "installed", f"Found cursor-agent at {agent}"
        if (HOME / ".cursor").exists():
            return (
                True,
                None,
                "Found ~/.cursor (install the cursor-agent CLI to enable headless runs).",
            )
        app = shutil.which("cursor")
        if app:
            return True, None, "Found Cursor app (cursor-agent CLI not detected)."
        return False, None, "Cursor not found (no cursor-agent CLI, no ~/.cursor)."

    def mcp_config_path(self) -> Optional[Path]:
        # Global MCP config; project-level .cursor/mcp.json also exists but the
        # global file is what applies across folders.
        p = HOME / ".cursor" / "mcp.json"
        return p

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
        disallowed_tools: Optional[list[str]] = None,  # accepted for interface parity (cursor isolates via empty mcp.json)
    ) -> RunResult:
        """Best-effort headless run via `cursor-agent`.

        Isolation: we drop the per-arm MCP config (same {mcpServers:{}} shape the
        harness produces) into ``work_dir/.cursor/mcp.json`` so Arm A runs without
        Bito and Arms B/C with it, then invoke cursor-agent inside that folder.

        cursor-agent's exact flags vary by version; this uses the documented
        print + json output and parses defensively. If the CLI isn't present we
        return a clean error rather than a fake result.
        """
        exe = shutil.which("cursor-agent")
        if not exe:
            return RunResult(exit_code=127, error="cursor-agent CLI not found on PATH.")

        # Place the arm's MCP config where cursor-agent will pick it up.
        cur_dir = work_dir / ".cursor"
        cur_dir.mkdir(parents=True, exist_ok=True)
        servers = read_json(mcp_config_path) or {"mcpServers": {}}
        (cur_dir / "mcp.json").write_text(json.dumps(servers, indent=2), encoding="utf-8")

        cmd = [exe, "-p", prompt_text, *_CURSOR_HEADLESS, *_cursor_model_args(model)]
        rc, wall = run_streaming(cmd, cwd=work_dir, stream_path=stream_path, on_proc=on_proc)

        # Parse cursor-agent output defensively: try whole-file JSON, then the
        # last JSON object/line; fall back to raw text as the response.
        text = ""
        try:
            text = stream_path.read_text(encoding="utf-8")
        except OSError:
            pass
        response, cost, in_tok, out_tok = self._parse_output(text)

        return RunResult(
            response=response,
            exit_code=rc,
            error=None if (rc == 0 and response) else f"exit_code={rc}",
            duration_ms=int(wall * 1000),
            total_cost_usd=cost,
            input_tokens=in_tok,
            output_tokens=out_tok,
            model=model,
            tool_calls=[],
            stream_path=str(stream_path),
        )

    def complete(self, *, prompt: str, mcp_config_path: Path, model: str, max_turns: int) -> dict:
        """One-shot prompt → text, used for the blind judge so a Cursor benchmark
        is judged by Cursor (no Claude tokens). Runs in a throwaway temp dir."""
        exe = shutil.which("cursor-agent")
        if not exe:
            return {"ok": False, "text": None, "cost": None, "error": "cursor-agent CLI not found."}
        with tempfile.TemporaryDirectory() as d:
            cur = Path(d) / ".cursor"
            cur.mkdir(parents=True, exist_ok=True)
            (cur / "mcp.json").write_text(
                json.dumps(read_json(mcp_config_path) or {"mcpServers": {}}), encoding="utf-8"
            )
            cmd = [exe, "-p", prompt, *_CURSOR_HEADLESS, *_cursor_model_args(model)]
            try:
                out = subprocess.run(
                    win_safe_cmd(cmd), cwd=d, capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=300,
                )
            except subprocess.TimeoutExpired:
                return {"ok": False, "text": None, "cost": None, "error": "cursor-agent timed out."}
            response, cost, _i, _o = self._parse_output(out.stdout or "")
            ok = out.returncode == 0 and bool(response)
            return {
                "ok": ok, "text": response, "cost": cost,
                "error": None if ok else f"exit_code={out.returncode} {(out.stderr or '')[:150]}",
            }

    @staticmethod
    def _parse_output(text: str):
        """Pull (response, cost, input_tokens, output_tokens) out of whatever
        cursor-agent emitted. Tolerant of plain text or JSON shapes."""
        if not text.strip():
            return None, None, None, None
        obj = None
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            for line in reversed(text.strip().splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        obj = json.loads(line)
                        break
                    except json.JSONDecodeError:
                        continue
        if isinstance(obj, dict):
            response = (
                obj.get("result")
                or obj.get("response")
                or obj.get("text")
                or obj.get("content")
            )
            usage = obj.get("usage") or {}
            return (
                response if isinstance(response, str) else json.dumps(response) if response else None,
                obj.get("total_cost_usd") or obj.get("cost"),
                usage.get("input_tokens"),
                usage.get("output_tokens"),
            )
        # Plain text transcript.
        return text.strip(), None, None, None
