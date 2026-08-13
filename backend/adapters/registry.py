"""Registry of known tool adapters."""

from __future__ import annotations

import os

from .base import ToolAdapter
from .claude_code import ClaudeCodeAdapter
from .cline import ClineAdapter
from .copilot import GitHubCopilotAdapter
from .cursor import CursorAdapter
from .windsurf import WindsurfAdapter

# Order = display order on the Setup page. Claude Code first (fully supported).
_ADAPTERS: list[ToolAdapter] = [
    ClaudeCodeAdapter(),
    GitHubCopilotAdapter(),
    CursorAdapter(),
    WindsurfAdapter(),
    ClineAdapter(),
]

_BY_ID = {a.id: a for a in _ADAPTERS}


def enabled_tool_ids() -> set[str] | None:
    """Restrict which tools the app shows/manages, via HARNESS_TOOLS (comma-
    separated ids, e.g. "claude"). Unset (the default everywhere except the
    Docker image, which sets it to "claude") means every tool is enabled — this
    only narrows the Docker build, it doesn't change the base app's behavior."""
    raw = os.environ.get("HARNESS_TOOLS", "").strip()
    if not raw:
        return None
    return {t.strip() for t in raw.split(",") if t.strip()}


def all_adapters() -> list[ToolAdapter]:
    enabled = enabled_tool_ids()
    if enabled is None:
        return list(_ADAPTERS)
    return [a for a in _ADAPTERS if a.id in enabled]


def get_adapter(tool_id: str) -> ToolAdapter | None:
    return _BY_ID.get(tool_id)
