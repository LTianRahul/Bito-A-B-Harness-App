"""Anthropic API key setup — the in-browser alternative to `claude /login`.

`claude /login` is an interactive browser OAuth flow that doesn't translate well
into a container: it needs a local callback port, and on macOS the resulting
token lives in the Keychain rather than a plain file, so it can't be reused by
a Linux container anyway. `claude` (and the Anthropic SDKs it wraps) natively
accept an `ANTHROPIC_API_KEY` environment variable instead — no browser step.

This mirrors the existing static-bearer-token pattern already used for
self-hosted Bito (see ``bito_auth.configure``): the user pastes a credential
into a Setup-page field, we persist it under ``configs/`` (covered by the same
per-machine-state volume as everything else), and stamp it into the running
process's environment so every subprocess the harness spawns inherits it.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

from .. import engine

KEY_PATH = engine.CONFIGS / "anthropic_key.json"


def _masked(key: str) -> str:
    key = key.strip()
    if len(key) <= 8:
        return "•" * len(key)
    return f"{key[:6]}…{key[-4:]}"


def save(api_key: str) -> dict[str, Any]:
    api_key = (api_key or "").strip()
    if not api_key:
        raise ValueError("API key is required.")
    engine.CONFIGS.mkdir(parents=True, exist_ok=True)
    KEY_PATH.write_text(json.dumps({"api_key": api_key}), encoding="utf-8")
    os.environ["ANTHROPIC_API_KEY"] = api_key
    return status()


def clear() -> dict[str, Any]:
    if KEY_PATH.exists():
        KEY_PATH.unlink()
    # Only unset the env var if WE set it (a key persisted here). An
    # ANTHROPIC_API_KEY provided by the launch environment (e.g. `docker run -e`)
    # is outside the app's control and is left alone.
    os.environ.pop("ANTHROPIC_API_KEY", None)
    return status()


def load_persisted_into_env() -> None:
    """Call once at startup: if the process wasn't launched with an explicit
    ANTHROPIC_API_KEY, load whatever was saved via the Setup page."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return
    if not KEY_PATH.exists():
        return
    try:
        data = json.loads(KEY_PATH.read_text(encoding="utf-8"))
        key = (data or {}).get("api_key", "").strip()
    except Exception:
        return
    if key:
        os.environ["ANTHROPIC_API_KEY"] = key


def status() -> dict[str, Any]:
    live = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    persisted = KEY_PATH.exists()
    return {
        "configured": bool(live),
        # "setup-ui": saved through this app (removable here). "environment": set
        # by whoever launched the process (e.g. `docker run -e`, not removable here).
        "source": "setup-ui" if persisted else ("environment" if live else None),
        "masked": _masked(live) if live else None,
    }
