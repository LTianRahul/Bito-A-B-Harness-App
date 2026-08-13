"""Claude Code adapter — the one tool the harness drives today.

Detection + MCP status reuse the harness's own config readers so the UI sees
exactly what a benchmark run would see (top-level + per-project + plugin MCPs).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Optional

from .. import engine
from .base import (
    HOME,
    McpStatus,
    RunResult,
    ToolAdapter,
    bito_status_from_servers,
    cli_version,
    run_streaming,
)


class ClaudeCodeAdapter(ToolAdapter):
    id = "claude"
    name = "Claude Code"
    kind = "cli"
    supports_headless = True
    headless_note = "Runs via `claude -p` (headless)."

    def detect(self) -> tuple[bool, Optional[str], str]:
        try:
            cli = engine.harness.find_claude_cli()
        except SystemExit:
            cli = None
        except Exception:
            cli = None
        if cli:
            return True, cli_version("claude") or "installed", f"Found CLI at {cli}"
        if (HOME / ".claude.json").exists():
            return True, None, "Found ~/.claude.json (CLI not on PATH)"
        return False, None, "`claude` CLI not found on PATH."

    def mcp_config_path(self) -> Optional[Path]:
        p = HOME / ".claude.json"
        return p if p.exists() else None

    def read_mcp_servers(self) -> dict[str, dict]:
        # Reuse the harness's merge of top-level + per-project + plugin MCPs.
        claude_json = HOME / ".claude.json"
        data = {}
        if claude_json.exists():
            from .base import read_json

            data = read_json(claude_json) or {}
        servers, _sources = engine.harness.collect_mcp_servers(
            data, HOME / ".claude" / "plugins"
        )
        return servers

    def mcp_status(self) -> McpStatus:
        return bito_status_from_servers(self.read_mcp_servers())

    def complete(self, *, prompt: str, mcp_config_path, model: str, max_turns: int) -> dict:
        """One-shot judge call via `claude -p --output-format json`."""
        h = engine.harness
        rc, _wall, obj, stderr = h.run_claude_json(
            prompt=prompt, mcp_config=mcp_config_path, model=model, max_turns=max_turns,
        )
        if rc == 0 and obj:
            return {"ok": True, "text": obj.get("result"), "cost": obj.get("total_cost_usd"), "error": None}
        return {"ok": False, "text": None, "cost": None, "error": f"exit_code={rc} {(stderr or '')[:150]}"}

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
        """One headless `claude -p` run, parsed into a normalized RunResult.

        Reuses the harness's command builder + stream parser so a UI run is
        identical to a CLI run (same isolation: fresh cwd, strict MCP config)."""
        h = engine.harness
        cmd = h.claude_cmd(
            prompt=prompt_text,
            mcp_config=mcp_config_path,
            output_format="stream-json",
            model=model,
            max_turns=max_turns,
            verbose=True,
            disallowed_tools=disallowed_tools,
        )
        rc, wall = run_streaming(cmd, cwd=work_dir, stream_path=stream_path, on_proc=on_proc)

        summary = h.parse_stream_jsonl(stream_path) if stream_path.exists() else {}
        io = h.stream_io_stats(stream_path) if stream_path.exists() else {}

        from .base import detect_usage_limit

        result_text = summary.get("result_text")
        limit = detect_usage_limit(result_text)
        if limit:
            error = f"usage_limit: {limit}"
        elif rc == 0 and result_text:
            error = None
        else:
            error = (
                f"exit_code={rc} subtype={summary.get('subtype')} "
                f"result_len={len(result_text or '')}"
            )

        return RunResult(
            response=result_text,
            exit_code=rc,
            error=error,
            duration_ms=int(wall * 1000),
            duration_api_ms=summary.get("duration_api_ms"),
            num_turns=summary.get("num_turns"),
            input_tokens=summary.get("input_tokens"),
            output_tokens=summary.get("output_tokens"),
            cache_read_tokens=summary.get("cache_read_tokens"),
            cache_creation_tokens=summary.get("cache_creation_tokens"),
            total_cost_usd=summary.get("total_cost_usd"),
            model=h.primary_model_from_usage(summary.get("model_usage"), model),
            tool_calls=summary.get("tool_calls") or [],
            session_id=summary.get("session_id"),
            ttft_ms=io.get("ttft_ms"),
            stream_path=str(stream_path),
        )
