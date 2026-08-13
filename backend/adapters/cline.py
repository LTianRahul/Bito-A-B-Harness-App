"""Cline adapter — detection + MCP status only.

Cline is a VS Code extension with no stable headless agent CLI, so
``supports_headless`` is False. Its MCP servers live in the extension's
globalStorage settings file, whose location is OS-specific.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from .base import HOME, ToolAdapter


def _vscode_globalstorage_dirs() -> list[Path]:
    """Candidate VS Code (and variants) User dirs across OSes.

    On Linux we probe the standard XDG path plus Snap and Flatpak sandboxes,
    which store Code data in different locations.
    """
    variants = ["Code", "Code - Insiders", "VSCodium", "Cursor"]
    if sys.platform == "darwin":
        base = HOME / "Library" / "Application Support"
        return [base / v / "User" / "globalStorage" for v in variants]
    if sys.platform.startswith("win"):
        import os
        base = Path(os.environ.get("APPDATA") or (HOME / "AppData" / "Roaming"))
        return [base / v / "User" / "globalStorage" for v in variants]
    # Linux: XDG standard + Snap + Flatpak sandboxes
    xdg_config = HOME / ".config"
    snap_base = HOME / "snap" / "code" / "current" / ".config"
    flatpak_base = HOME / ".var" / "app" / "com.visualstudio.code" / "config"
    dirs: list[Path] = []
    for base in (xdg_config, snap_base, flatpak_base):
        dirs.extend(base / v / "User" / "globalStorage" for v in variants)
    return dirs


# Cline's extension id (publisher.name).
_CLINE_EXT = "saoudrizwan.claude-dev"


class ClineAdapter(ToolAdapter):
    id = "cline"
    name = "Cline"
    kind = "ide-extension"
    supports_headless = False
    headless_note = "Cline is a VS Code extension with no headless CLI, so unattended benchmark runs aren't supported. Detection and MCP status still work."

    def _settings_file(self) -> Optional[Path]:
        for gs in _vscode_globalstorage_dirs():
            f = gs / _CLINE_EXT / "settings" / "cline_mcp_settings.json"
            if f.exists():
                return f
        return None

    def _ext_dir(self) -> Optional[Path]:
        for gs in _vscode_globalstorage_dirs():
            d = gs / _CLINE_EXT
            if d.exists():
                return d
        return None

    def detect(self) -> tuple[bool, Optional[str], str]:
        d = self._ext_dir()
        if d:
            return True, None, f"Found Cline extension data at {d}"
        return False, None, "Cline (VS Code extension) data not found."

    def mcp_config_path(self) -> Optional[Path]:
        return self._settings_file()
