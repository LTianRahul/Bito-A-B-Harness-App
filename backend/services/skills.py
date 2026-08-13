"""Detect which Bito `bito-*` skills are installed and which directory to use.

Skills directories (per tool):
  ~/.claude/skills/   — Claude Code (Bito's installer primary target)
  ~/.copilot/skills/  — GitHub Copilot CLI

Each tool's skills are cached separately in:
  configs/skills-claude.json
  configs/skills-copilot.json

The cache is built at app startup and revalidated before each batch run using a
content hash of the installed SKILL.md files, so Bito installer updates (which
silently push new/updated skills to the user's machine) are picked up automatically.

Works on macOS, Linux, and Windows via Path.home().
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from ..adapters.base import HOME
from .. import engine

_SKILL_DIRS: dict[str, Path] = {
    "claude":  HOME / ".claude"  / "skills",
    "copilot": HOME / ".copilot" / "skills",
}


# ---------------------------------------------------------------------------
# Directory scanning
# ---------------------------------------------------------------------------

def _bito_skills_in(d: Path) -> list[str]:
    """Sorted list of bito-* skill names found in directory d."""
    try:
        found: set[str] = set()
        for p in d.iterdir():
            name = p.name
            if p.is_file() and "." in name:
                name = name.rsplit(".", 1)[0]
            if name.startswith("bito-"):
                found.add(name)
        return sorted(found)
    except (OSError, PermissionError):
        return []


def _compute_dir_hash(d: Path) -> str:
    """SHA-256 of all bito-*/SKILL.md content (sorted by path).

    This hash changes whenever the Bito installer adds, removes, or edits a skill,
    so stale-cache detection is reliable without a file-system watcher.
    """
    h = hashlib.sha256()
    try:
        paths = sorted(d.glob("bito-*/SKILL.md"))
    except (OSError, PermissionError):
        return ""
    for p in paths:
        try:
            h.update(p.name.encode())
            h.update(p.read_bytes())
        except (OSError, PermissionError):
            pass
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Cache file helpers
# ---------------------------------------------------------------------------

def _cache_path(tool: str) -> Path:
    return engine.CONFIGS / f"skills-{tool}.json"


def _load_cache(tool: str) -> dict | None:
    try:
        data = json.loads(_cache_path(tool).read_text(encoding="utf-8"))
        if isinstance(data, dict) and "skills" in data and "hash" in data:
            return data
    except Exception:
        pass
    return None


def _save_cache(tool: str, skills: list[str], h: str) -> None:
    try:
        engine.CONFIGS.mkdir(parents=True, exist_ok=True)
        _cache_path(tool).write_text(
            json.dumps({"skills": skills, "hash": h, "built_at": time.time()}, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def _is_stale(d: Path, cached_hash: str) -> bool:
    return _compute_dir_hash(d) != cached_hash


# ---------------------------------------------------------------------------
# Cache build
# ---------------------------------------------------------------------------

def build_skills_cache(tool: str) -> list[str]:
    """Scan the tool's skills directory, save cache, return list of skill names."""
    d = _SKILL_DIRS.get(tool, HOME / f".{tool}" / "skills")
    skills = _bito_skills_in(d)
    h = _compute_dir_hash(d)
    _save_cache(tool, skills, h)
    return skills


def build_all_caches() -> None:
    """Build (or refresh) skill caches for all known tools. Called at app startup."""
    for tool in _SKILL_DIRS:
        try:
            build_skills_cache(tool)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _skill_status_for_tool(tool: str) -> dict[str, Any]:
    """Skill status for a single tool, using the cache if fresh."""
    d = _SKILL_DIRS.get(tool, HOME / f".{tool}" / "skills")

    cached = _load_cache(tool)
    if cached and not _is_stale(d, cached["hash"]):
        installed = cached["skills"]
    else:
        # Cache missing or stale — rebuild now.
        installed = build_skills_cache(tool)

    return {
        "installed":     installed,
        "count":         len(installed),
        "arm_b_ok":      "bito-codebase-explorer" in installed,
        "arm_c_ok":      len(installed) >= 3,
        "skills_dir":    str(d),
        "skills_source": tool if installed else "none",
    }


def skill_status(tool_id: str | None = None) -> dict[str, Any]:
    """Which bito-* skills are present, and in which directory.

    tool_id: 'claude', 'copilot', or None.
      - When specified, returns ONLY that tool's skills directory (strict — no
        cross-tool fallback). This is used by the runner to ensure each arm uses
        exactly the skills that belong to the selected tool.
      - When None, falls back to priority: claude first, then copilot, so callers
        that don't know the tool still get a useful result.

    Returns dict with keys: installed, count, arm_b_ok, arm_c_ok, skills_dir, skills_source
    """
    if tool_id in ("claude", "copilot"):
        return _skill_status_for_tool(tool_id)

    # No tool hint — priority order (existing behaviour for callers that don't know).
    for tool in ("claude", "copilot"):
        st = _skill_status_for_tool(tool)
        if st["installed"]:
            return st
    # Neither has skills.
    return _skill_status_for_tool("claude")


def skills_status_all() -> dict[str, dict[str, Any]]:
    """Return skill status for both tools independently.

    Used by the Setup page to show per-tool skill rows so users can see whether
    Claude Code and Copilot CLI each have the required skills installed.
    """
    return {
        "claude":  _skill_status_for_tool("claude"),
        "copilot": _skill_status_for_tool("copilot"),
    }


def resolve_skills_dir() -> tuple[Path, str]:
    """Return (skills_dir, source) for the best available tool.

    source is 'claude', 'copilot', or 'none'.
    Kept for backward compatibility with callers that don't know the tool_id.
    """
    for tool in ("claude", "copilot"):
        st = _skill_status_for_tool(tool)
        if st["installed"]:
            return Path(st["skills_dir"]), tool
    return _SKILL_DIRS["claude"], "none"
