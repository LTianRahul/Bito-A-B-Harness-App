"""Prompt management.

The *working* prompt list is ``prompts.json`` (the file the harness runs by
default). Named snapshots live in ``prompt_sets/<name>.json``. Every prompt is
``{id, prompt, title?, category?}``; old flat ``[{id, prompt}]`` files load
unchanged (the extra fields are optional).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .. import engine

WORKING = engine.ROOT / "prompts.json"
SETS_DIR = engine.PROMPT_SETS


def _normalize(items: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        pid = str(it.get("id") or "").strip()
        prompt = str(it.get("prompt") or "").strip()
        if not pid or not prompt:
            raise ValueError("Every prompt needs a non-empty id and prompt text.")
        if pid in seen:
            raise ValueError(f"Duplicate prompt id: {pid}")
        seen.add(pid)
        entry: dict[str, Any] = {"id": pid, "prompt": prompt}
        if it.get("title"):
            entry["title"] = str(it["title"]).strip()
        if it.get("category"):
            entry["category"] = str(it["category"]).strip()
        out.append(entry)
    return out


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"{path.name} is not valid JSON: {e}")
    if not isinstance(data, list):
        raise ValueError(f"{path.name} must be a JSON array of prompts.")
    return _normalize(data)


def _write(path: Path, items: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, indent=2) + "\n", encoding="utf-8")


# ---- working prompts ----
def list_prompts() -> list[dict]:
    return _read(WORKING)


def save_all(items: list[dict]) -> list[dict]:
    norm = _normalize(items)
    _write(WORKING, norm)
    return norm


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "prompt"


def add(prompt: dict) -> list[dict]:
    items = list_prompts()
    if not prompt.get("id"):
        base = _slug(prompt.get("title") or prompt.get("prompt", "prompt"))[:24]
        existing = {p["id"] for p in items}
        pid, n = base, 2
        while pid in existing:
            pid = f"{base}-{n}"
            n += 1
        prompt = {**prompt, "id": pid}
    items.append(prompt)
    return save_all(items)


def update(pid: str, prompt: dict) -> list[dict]:
    items = list_prompts()
    idx = next((i for i, p in enumerate(items) if p["id"] == pid), None)
    if idx is None:
        raise ValueError(f"No prompt with id {pid}.")
    # Allow id change but keep it unique.
    new_id = str(prompt.get("id") or pid).strip()
    if new_id != pid and any(p["id"] == new_id for p in items):
        raise ValueError(f"Prompt id {new_id} already exists.")
    items[idx] = {**prompt, "id": new_id}
    return save_all(items)


def delete(pid: str) -> list[dict]:
    items = [p for p in list_prompts() if p["id"] != pid]
    return save_all(items)


def duplicate(pid: str) -> list[dict]:
    items = list_prompts()
    src = next((p for p in items if p["id"] == pid), None)
    if src is None:
        raise ValueError(f"No prompt with id {pid}.")
    existing = {p["id"] for p in items}
    new_id, n = f"{pid}-copy", 2
    while new_id in existing:
        new_id = f"{pid}-copy-{n}"
        n += 1
    copy = {**src, "id": new_id}
    if copy.get("title"):
        copy["title"] = f"{copy['title']} (copy)"
    items.append(copy)
    return save_all(items)


def import_prompts(items: list[dict], replace: bool) -> list[dict]:
    incoming = _normalize(items)
    if replace:
        return save_all(incoming)
    current = list_prompts()
    by_id = {p["id"]: p for p in current}
    for p in incoming:
        by_id[p["id"]] = p  # upsert by id
    return save_all(list(by_id.values()))


# ---- prompt sets (named snapshots) ----
def list_sets() -> list[dict]:
    SETS_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for f in sorted(SETS_DIR.glob("*.json")):
        try:
            n = len(_read(f))
        except ValueError:
            n = -1
        out.append({"name": f.stem, "count": n})
    return out


def save_set(name: str, items: list[dict]) -> dict:
    safe = _slug(name)
    norm = _normalize(items)
    _write(SETS_DIR / f"{safe}.json", norm)
    return {"name": safe, "count": len(norm)}


def load_set(name: str) -> list[dict]:
    path = SETS_DIR / f"{_slug(name)}.json"
    if not path.exists():
        raise ValueError(f"No prompt set named {name}.")
    return _read(path)


def delete_set(name: str) -> None:
    path = SETS_DIR / f"{_slug(name)}.json"
    if path.exists():
        path.unlink()


def get_prompts_for_run(set_name: str | None) -> list[dict]:
    """Resolve the prompts a benchmark run should use."""
    if set_name:
        return load_set(set_name)
    return list_prompts()


# Prompts are never auto-generated. Users start with an empty list and opt in via the
# "Generate with AI" button on the Prompts page (the /prompts/generate endpoint), or
# add their own (prompts.example.json is a reference for the format).
