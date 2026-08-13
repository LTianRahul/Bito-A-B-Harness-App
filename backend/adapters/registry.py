"""Registry of known tool adapters."""

from __future__ import annotations

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


def all_adapters() -> list[ToolAdapter]:
    return list(_ADAPTERS)


def get_adapter(tool_id: str) -> ToolAdapter | None:
    return _BY_ID.get(tool_id)
