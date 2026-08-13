"""Per-run transcript / logs.

Devs want to see what actually happened in a run — not just the final score.
This parses a run's raw ``stream-json`` file into an ordered list of steps
(assistant text, tool calls with their inputs, tool results, the final result,
and any errors) so the UI can show the full decision trail, plus exposes the
raw JSONL for download.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .. import engine

harness = engine.harness


def _stream_path_for(batch_id: str, arm: str, base_pid: str, n_runs_suffix: str = "") -> Optional[Path]:
    """Find the stream file for a run. Labels are '<pid>' or '<pid>#rN'."""
    arm_dir = harness.RUNS / batch_id / arm
    if not arm_dir.exists():
        return None
    # Exact first, then any repeat (#r1, …).
    exact = arm_dir / f"{base_pid}.stream.jsonl"
    if exact.exists():
        return exact
    matches = sorted(arm_dir.glob(f"{base_pid}#r*.stream.jsonl"))
    return matches[0] if matches else None


def _short(obj: Any, limit: int = 600) -> str:
    s = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return s if len(s) <= limit else s[:limit] + f"… (+{len(s) - limit} chars)"


def transcript(batch_id: str, arm: str, base_pid: str) -> dict[str, Any]:
    """Parse one run's stream into ordered steps + summary counters."""
    path = _stream_path_for(batch_id, arm, base_pid)
    if not path or not path.exists():
        return {"found": False, "steps": [], "summary": {}, "stream_rel": None}

    id_to_name: dict[str, str] = {}
    steps: list[dict] = []
    counts = {"assistant": 0, "tool_calls": 0, "mcp_calls": 0, "bito_calls": 0, "errors": 0}
    result_event: dict | None = None

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            et = ev.get("type")

            if et == "assistant":
                for b in (ev.get("message") or {}).get("content") or []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text" and b.get("text", "").strip():
                        counts["assistant"] += 1
                        steps.append({"kind": "thought", "text": _short(b["text"], 1200)})
                    elif b.get("type") == "tool_use":
                        name = b.get("name", "?")
                        id_to_name[b.get("id")] = name
                        counts["tool_calls"] += 1
                        if name.startswith("mcp__"):
                            counts["mcp_calls"] += 1
                        if name.startswith("mcp__BitoAIArchitect__"):
                            counts["bito_calls"] += 1
                        inp = b.get("input") or {}
                        # Surface the most useful input field per tool.
                        hint = (
                            inp.get("command")
                            or inp.get("pattern")
                            or inp.get("file_path")
                            or inp.get("skill")
                            or inp.get("query")
                            or inp.get("prompt")
                        )
                        steps.append({
                            "kind": "tool_call",
                            "name": name,
                            "skill": inp.get("skill") if name == "Skill" else None,
                            "input": _short(hint or inp, 400),
                        })
            elif et == "user":
                for b in (ev.get("message") or {}).get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        name = id_to_name.get(b.get("tool_use_id"), "")
                        is_err = bool(b.get("is_error"))
                        if is_err:
                            counts["errors"] += 1
                        c = b.get("content")
                        text = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
                        steps.append({
                            "kind": "tool_result",
                            "name": name,
                            "is_error": is_err,
                            "text": _short(text, 700),
                        })
            elif et == "result":
                result_event = ev

    summary = {**counts}
    if result_event:
        summary.update({
            "subtype": result_event.get("subtype"),
            "is_error": result_event.get("is_error"),
            "api_error_status": result_event.get("api_error_status"),
            "num_turns": result_event.get("num_turns"),
            "total_cost_usd": result_event.get("total_cost_usd"),
            "duration_ms": result_event.get("duration_ms"),
            "result_text": _short(result_event.get("result") or "", 4000),
        })

    # Look up the clean user-facing prompt from prompts.json by id.
    prompt_text: Optional[str] = None
    try:
        prompts_file = engine.ROOT / "prompts.json"
        if prompts_file.exists():
            all_prompts = json.loads(prompts_file.read_text(encoding="utf-8"))
            match = next((p for p in all_prompts if p.get("id") == base_pid), None)
            if match:
                prompt_text = match.get("prompt")
    except Exception:
        pass

    return {
        "found": True,
        "arm": arm,
        "prompt_id": base_pid,
        "prompt": prompt_text,
        "steps": steps,
        "summary": summary,
        "stream_rel": path.relative_to(harness.RUNS).as_posix(),
    }


def raw_stream(batch_id: str, arm: str, base_pid: str) -> Optional[str]:
    path = _stream_path_for(batch_id, arm, base_pid)
    if not path or not path.exists():
        return None
    return path.read_text(encoding="utf-8")
