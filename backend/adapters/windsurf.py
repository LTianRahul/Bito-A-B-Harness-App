"""Windsurf adapter — detection + MCP status only.

Windsurf (by Codeium) is IDE-first; it has no stable headless agent CLI we can
drive for unattended benchmark runs, so ``supports_headless`` is False and the
runner reports it honestly instead of faking a run. MCP servers live in
``~/.codeium/windsurf/mcp_config.json`` on macOS/Linux and
``%APPDATA%\\Codeium\\windsurf\\mcp_config.json`` on Windows.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Optional

from .base import HOME, ToolAdapter, cli_version


def _windsurf_config_dir() -> Path:
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA") or (HOME / "AppData" / "Roaming"))
        return base / "Codeium" / "windsurf"
    return HOME / ".codeium" / "windsurf"


class WindsurfAdapter(ToolAdapter):
    id = "windsurf"
    name = "Windsurf"
    kind = "ide-extension"
    supports_headless = False
    headless_note = "Windsurf has no headless agent CLI, so unattended benchmark runs aren't supported. Detection and MCP status still work."

    def detect(self) -> tuple[bool, Optional[str], str]:
        exe = shutil.which("windsurf")
        cfg = _windsurf_config_dir()
        if exe:
            return True, cli_version("windsurf") or "installed", f"Found Windsurf at {exe}"
        if cfg.exists():
            return True, None, f"Found Windsurf configuration at {cfg}."
        return False, None, "Windsurf not found (no `windsurf` command, no Windsurf config directory)."

    def mcp_config_path(self) -> Optional[Path]:
        return _windsurf_config_dir() / "mcp_config.json"
