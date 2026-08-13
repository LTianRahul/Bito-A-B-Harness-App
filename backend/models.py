"""Pydantic request/response schemas shared by routers."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


# ---- Setup ----
class BuildConfigsRequest(BaseModel):
    bito_server: Optional[str] = None


class DoctorRequest(BaseModel):
    model: Optional[str] = None
    max_turns: Optional[int] = None
    # Which CLI to health-check (its own MCP connection): "claude" or "copilot".
    tool: Optional[str] = "claude"


# ---- Bito auth ----
class BitoAuthRequest(BaseModel):
    workspace_id: str
    token: Optional[str] = None
    tools: Optional[list[str]] = None   # which tool configs to write into; default all detected


# ---- Prompts ----
class Prompt(BaseModel):
    id: str
    prompt: str
    title: Optional[str] = None
    category: Optional[str] = None


class PromptSetSave(BaseModel):
    name: str
    prompts: list[Prompt]


class ImportRequest(BaseModel):
    prompts: list[Prompt]
    replace: bool = False


class GenerateRequest(BaseModel):
    topic: Optional[str] = None
    count: int = 6
    categories: Optional[list[str]] = None
    ground: bool = True
    model: Optional[str] = None


# ---- Runs ----
class StartRunRequest(BaseModel):
    tool: str = "claude"
    # Optional per-arm tool override.  Keys are "A", "B", "C"; values are tool
    # IDs (e.g. "copilot", "claude", "cursor").  When present for an arm, it
    # takes priority over the global `tool` field for that arm only.
    # Example: {"A": "copilot"} runs Arm A with GitHub Copilot CLI while Arms
    # B/C use the global `tool` (Claude Code with Bito).
    arm_tools: Optional[dict] = None
    repo: Optional[str] = None
    prompt_set: Optional[str] = None     # name of a saved set; default = working prompts
    arms: list[str] = ["A", "B", "C"]
    mode: str = "standard"
    n_runs: int = 1
    model: Optional[str] = None
    max_turns: Optional[int] = None
    label: Optional[str] = None
    # Workspace: "fresh-clone" (empty cwd, arms clone source) or "local-repo" (run against
    # a copy of local_repo_path incl. uncommitted changes so arms make local code changes).
    workspace_mode: str = "fresh-clone"
    local_repo_path: Optional[str] = None
