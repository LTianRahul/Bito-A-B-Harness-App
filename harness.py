"""A/B/C harness: Claude Code measured across three configurations.

Arms:
  A = baseline: the customer's normal Claude Code (their MCPs + their skills),
      with NO Bito. Hard-instructed not to use any bito-* skill; enforced by
      detect-and-rerun.
  B = lean Bito: Bito AI Architect MCP + ONLY the bito-codebase-explorer skill.
  C = full Bito: Bito AI Architect MCP + free use of any bito-* skill.
Arms B and C share the same MCP set; they differ only by prompt suffix.

Subcommands:
  setup    Build MCP configs from ~/.claude.json:
             configs/mcp-arm-a.json    (your MCPs, Bito removed)      -> Arm A
             configs/mcp-arm-bito.json (your MCPs, Bito added)        -> Arms B, C
  doctor   Preflight: verify claude + the Bito MCP are genuinely live (a real
           tool call, not just a config entry), then list the indexed repos and
           write them to indexed-repos.txt so you can aim your prompts at them
  run      Run all prompts under one arm (A, B, or C), record metrics
  judge    One blind judge call per prompt scores all three answers independently
           on a fixed rubric (no ranking; ties allowed)
  report   Emit reports/summary.md and reports/summary.csv
  all      One command: setup (if needed) -> doctor -> run A/B/C -> judge -> report

Usage examples:
  python harness.py all --prompts prompts.json     # the easy path
  # …or drive each step yourself:
  python harness.py setup
  python harness.py doctor
  python harness.py run --arm A --prompts prompts.json
  python harness.py run --arm B --prompts prompts.json
  python harness.py run --arm C --prompts prompts.json
  python harness.py judge --prompts prompts.json
  python harness.py report
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(it, **_):
        return it


ROOT = Path(__file__).parent
DB_PATH = ROOT / "results.db"
CONFIGS = ROOT / "configs"
RUNS = ROOT / "runs"
JUDGMENTS = ROOT / "judgments"
REPORTS = ROOT / "reports"
GIT_NAMESPACE_PATH = CONFIGS / "git-namespace.txt"

DEFAULT_MODEL = "claude-opus-4-7"
DEFAULT_JUDGE_MODEL = "claude-opus-4-7"
DEFAULT_JUDGE_SEED = 20260608
DEFAULT_MAX_TURNS = 200
DEFAULT_DOCTOR_MAX_TURNS = 15
INTER_RUN_SLEEP_SEC = 0.2
RETRY_BACKOFFS_SEC = [1, 4]
# Reruns when an arm violates its skill policy: Arm A must use NO bito-* skill;
# Arm C must use AT LEAST ONE bito-* skill. A persistent violation is kept + flagged.
SKILL_RERUN_LIMIT = 2
# Universal dimensions are scored on EVERY prompt. Conditional dimensions are
# scored ONLY when the task is a plan/design/change/fix task (else the judge
# returns null), so their sample size (n) is smaller — matching how the report
# averages each dimension over just the prompts where it applies.
UNIVERSAL_DIMS = ["correctness", "completeness", "grounding", "hallucination_resistance", "reasoning"]
CONDITIONAL_DIMS = ["planning_quality", "impact_analysis"]
RUBRIC_DIMS = UNIVERSAL_DIMS + CONDITIONAL_DIMS
# Human-readable labels for the rubric dimensions (used in the judge prompt and report).
RUBRIC_LABELS = {
    "correctness": "factual accuracy, internal consistency, absence of errors",
    "completeness": "coverage of the question and depth (mechanisms, edge cases)",
    "grounding": "specific evidence (named repos, files, symbols, configs) vs. generic claims",
    "hallucination_resistance": "absence of fabricated or unsupported claims (invented repos, "
                                "files, APIs, or facts); higher = nothing made up",
    "reasoning": "quality of reasoning and the logical soundness of the explanation",
    "planning_quality": "ONLY for plan/design/build tasks: the strength, structure, and "
                        "actionability of the proposed plan (else null)",
    "impact_analysis": "ONLY for change/fix tasks: how well it identifies blast radius, affected "
                       "components, and design-change guidance (else null)",
}

# The three arms:
#   A = customer's normal Claude Code with all their non-Bito MCPs and tools, NO Bito.
#   B = lean Bito: non-Bito MCPs + Bito AI Architect MCP + ONLY bito-codebase-explorer.
#   C = full Bito: non-Bito MCPs + Bito AI Architect MCP + full bito-* skill suite.
# Arms B and C share the same MCP set; they differ ONLY by prompt suffix.
# The only controlled variable across all three arms is the Bito MCP + skills.

# Arm A: ban ONLY Bito MCP + bito-* skills; all other MCPs are allowed (GitLab,
# GitHub, Atlassian, Jira, Confluence, Slack, Linear, etc.). Enforced in code:
# cmd_run detects any bito-* Skill or BitoAIArchitect MCP call and reruns.
ARM_A_PROMPT_SUFFIX = (
    "\n\n---\n"
    "CRITICAL CONSTRAINTS FOR THIS ARM — READ CAREFULLY:\n"
    "\n"
    "1. NO BITO MCP OR BITO SKILLS: You must NOT invoke the BitoAIArchitect MCP, "
    "any MCP tool whose name begins with `mcp__BitoAIArchitect__`, or ANY skill "
    "whose name begins with `bito-`. This is a hard rule with no exceptions.\n"
    "\n"
    "2. NON-BITO TOOLS ARE ALLOWED: You may freely use the customer's normal "
    "tools, non-Bito skills, and any non-Bito MCPs available in the config — "
    "including Atlassian, Jira, Confluence, GitHub (gh), GitLab (glab), "
    "Bitbucket, Linear, Slack, or other customer-provided MCPs.\n"
    "\n"
    "3. INVESTIGATION RULE: Use the best available non-Bito tools to answer "
    "the task. You may use non-Bito MCPs, local shell tools, git/glab/gh "
    "commands, grep/rg, file reads, and fresh repo clones when needed.\n"
)

# Arm B: non-Bito MCPs + Bito MCP + the bito-codebase-explorer skill ONLY.
ARM_B_PROMPT_SUFFIX = (
    "\n\n---\n"
    "INSTRUCTIONS FOR THIS TASK:\n"
    "\n"
    "1. You MUST use AI Architect (the BitoAIArchitect MCP) to make at least "
    "2 calls before doing anything else. Start with the Bito index and context, "
    "not by cloning repositories first.\n"
    "\n"
    "2. After those initial BitoAIArchitect calls, rely on what the index "
    "returned. Clone a repo with glab/git ONLY if the index genuinely cannot "
    "answer the question, or if you must make code changes — do NOT clone and "
    "re-grep to re-verify facts the index already provided.\n"
    "\n"
    "3. You may freely use any non-Bito MCPs available in the config — "
    "including Atlassian, Jira, Confluence, GitHub, GitLab, Bitbucket, "
    "Linear, Slack, or other customer-provided MCPs. These represent the "
    "customer's normal Claude Code environment and are allowed in all arms.\n"
    "\n"
    "4. Do NOT spawn research agents or sub-agents until after you have called "
    "BitoAIArchitect at least twice.\n"
    "\n"
    "5. After your initial BitoAIArchitect calls, invoke the "
    "`bito-codebase-explorer` skill via the Skill tool. Run it as leanly as the "
    "task allows: do the minimum steps needed to answer, and skip optional "
    "checkpoints, task-tracking, and sub-agents when the answer is already clear.\n"
    "\n"
    "6. `bito-codebase-explorer` is the ONLY Bito skill you may use — do NOT invoke "
    "any other `bito-*` skill.\n"
    "\n"
    "7. Your final answer must contain ONLY the requested findings. Do NOT add a "
    "closing note about unauthenticated or unused MCP servers (e.g. Atlassian, "
    "Figma, Slack) or any other tool you did not need for this task.\n"
)

# Arm C: non-Bito MCPs + Bito MCP + the full bito-* skill suite, chaining where
# the task calls for it. Match the skill selection to the goal.
ARM_C_PROMPT_SUFFIX = (
    "\n\n---\n"
    "INSTRUCTIONS FOR THIS TASK:\n"
    "\n"
    "1. You MUST use AI Architect (the BitoAIArchitect MCP) to make at least "
    "2 calls before doing anything else. Start with the Bito index and context, "
    "not by cloning repositories first.\n"
    "\n"
    "2. After those initial BitoAIArchitect calls, rely on what the index "
    "returned. Clone a repo with glab/git ONLY if the index genuinely cannot "
    "answer the question, or if you must make code changes — do NOT clone and "
    "re-grep to re-verify facts the index already provided.\n"
    "\n"
    "3. You may freely use any non-Bito MCPs available in the config — "
    "including Atlassian, Jira, Confluence, GitHub, GitLab, Bitbucket, "
    "Linear, Slack, or other customer-provided MCPs. These represent the "
    "customer's normal Claude Code environment and are allowed in all arms.\n"
    "\n"
    "4. Do NOT spawn research agents or sub-agents until after you have called "
    "BitoAIArchitect at least twice.\n"
    "\n"
    "5. After your initial BitoAIArchitect calls, choose and invoke the "
    "`bito-*` skill(s) that fit the task — route based on each skill's stated "
    "purpose in the SKILL list below. Use the MINIMUM number of skills the task "
    "needs (you MUST invoke at least one). Within each skill, do the minimum "
    "steps needed to answer and skip optional checkpoints and task-tracking "
    "when the answer is already clear.\n"
    "\n"
    "6. Only chain multiple skills when the task genuinely spans stages (e.g. "
    "design → plan → implement). For a single lookup or diagnosis, one skill is "
    "enough — do not add skills beyond what the task calls for.\n"
    "\n"
    "7. When ready to implement code, FIRST clone the relevant repo(s) into "
    "your workspace with glab/git and create the required branch(es). Then "
    "make changes on those branches — work against real cloned code, not a "
    "fresh empty directory.\n"
    "\n"
    "8. Proceed through every skill step and checkpoint WITHOUT waiting for "
    "user confirmation. This run is fully non-interactive: make the most "
    "reasonable default choice at each checkpoint and keep going. Output the "
    "full design, plan, or analysis — a bare 'I have completed the design' "
    "summary is not acceptable.\n"
    "\n"
    "9. Your final answer must contain ONLY the requested findings. Do NOT add a "
    "closing note about unauthenticated or unused MCP servers (e.g. Atlassian, "
    "Figma, Slack) or any other tool you did not need for this task.\n"
)

ARM_SUFFIXES = {"A": ARM_A_PROMPT_SUFFIX, "B": ARM_B_PROMPT_SUFFIX, "C": ARM_C_PROMPT_SUFFIX}

# Appended to every prompt in every arm — workspace discipline and no-refusal
# nudge. Identical across arms so the only deltas are the per-arm suffixes.
COMMON_PROMPT_SUFFIX = (
    "\n\n---\n"
    "WORKSPACE RULES FOR THIS TASK:\n"
    "\n"
    "- Treat the current working directory (it starts empty) as your ONLY "
    "local workspace. Do NOT read, list, or rely on files anywhere else on "
    "this machine, including existing repo checkouts elsewhere on disk.\n"
    "\n"
    "- If you need source code, obtain it fresh into the current directory "
    "(e.g. `glab repo clone`, `gh repo clone`, or `git clone`).\n"
    "\n"
    "- You may use the tools available in this session according to the active "
    "arm rules. Non-Bito MCPs are allowed in all arms. Bito MCP and bito-* "
    "skills are allowed only where the active arm explicitly permits them.\n"
    "\n"
    "- Investigate with permitted tools before answering. Do NOT refuse only "
    "because a specific tool or index is unavailable — use the best available "
    "permitted alternative instead.\n"
    "\n"
    "- Only conclude that the question cannot be answered after you have "
    "actually attempted an investigation with the permitted tools you have.\n"
    "\n"
    "- GROUND YOUR ANSWER IN REAL CODE: base every factual claim on evidence you "
    "actually inspected during this run, and cite specific repos, files, functions, "
    "and line numbers. Obtain whatever source you need to verify specifics — read it "
    "with the permitted tools or clone it fresh into the workspace. A generic claim "
    "that is not tied to code you inspected does not count as grounded.\n"
)

# Local-repo workspace mode: the current directory already IS the target repo (a copy
# including the user's uncommitted changes). Used when the goal is to make local code
# changes and compare arms with vs without AI Architect on that same checkout.
COMMON_PROMPT_SUFFIX_LOCAL = (
    "\n\n---\n"
    "WORKSPACE RULES FOR THIS TASK:\n"
    "\n"
    "- The current working directory is a checkout of the TARGET REPOSITORY, including "
    "the user's uncommitted local changes. Work IN PLACE: read and edit files here. Do "
    "NOT clone a fresh copy, and do NOT read or rely on files elsewhere on this machine.\n"
    "\n"
    "- Make the code changes the task requires directly in this working tree. You may "
    "use git here to inspect history, branches, and the current diff.\n"
    "\n"
    "- You may use the tools available in this session according to the active arm "
    "rules. Non-Bito MCPs are allowed in all arms. Bito MCP and bito-* skills are "
    "allowed only where the active arm explicitly permits them.\n"
    "\n"
    "- Investigate with permitted tools before answering. Do NOT refuse only because a "
    "specific tool or index is unavailable — use the best available permitted "
    "alternative instead.\n"
    "\n"
    "- GROUND YOUR ANSWER IN REAL CODE: base every factual claim on evidence you "
    "actually inspected in this working tree, and cite specific files, functions, and "
    "line numbers. A generic claim not tied to code you inspected is not grounded.\n"
)


def resolve_skills_dir() -> Path:
    """Return the best available Bito skills directory for this machine.

    Priority:
      1. ~/.claude/skills/   — Claude Code (primary; Bito's installer targets this)
      2. ~/.copilot/skills/  — GitHub Copilot CLI fallback (when Claude Code absent)

    Returns the first directory that exists and contains at least one bito-* entry,
    falling back to the Claude path (even if missing) so callers get a stable Path.
    Always works on macOS, Linux, and Windows via Path.home().
    """
    home = Path.home()
    claude_dir = home / ".claude" / "skills"
    copilot_dir = home / ".copilot" / "skills"

    def _has_bito(d: Path) -> bool:
        try:
            return any(p.name.startswith("bito-") for p in d.iterdir())
        except OSError:
            return False

    if _has_bito(claude_dir):
        return claude_dir
    if _has_bito(copilot_dir):
        return copilot_dir
    # Neither has skills yet — default to Claude path (Bito installer's primary target).
    return claude_dir


def _skill_description(name: str, skills_dir: Optional[Path] = None) -> str:
    """One-paragraph description (the 'use when / triggers' routing text) from a skill's
    SKILL.md frontmatter. Returns '' if the skill isn't installed or has no description.

    skills_dir: override the resolved skills directory (used by the runner to pass the
    correct path for the selected tool). Defaults to resolve_skills_dir()."""
    base = skills_dir if skills_dir is not None else resolve_skills_dir()
    path = base / name / "SKILL.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    fm = m.group(1) if m else ""
    dm = re.search(r"(?m)^description:\s*(.*)$", fm)
    if not dm:
        return ""
    first = dm.group(1).strip()
    if first in (">", ">-", ">+", "|", "|-", "|+", ""):
        # YAML folded/literal block scalar — gather the indented continuation lines.
        block = []
        for ln in fm[dm.end():].splitlines():
            if ln.strip() == "":
                continue
            if re.match(r"^\S", ln):  # next top-level key → stop
                break
            block.append(ln.strip())
        desc = " ".join(block)
    else:
        desc = first.strip().strip('"').strip("'")
    desc = " ".join(desc.split())  # collapse whitespace
    return desc[:500].rstrip()


def installed_bito_skills(skills_dir: Optional[Path] = None) -> list[str]:
    """Names of bito-* skills installed in the resolved skills directory (sorted).

    skills_dir: override the auto-resolved directory (passed by the runner for the
    selected tool). Defaults to resolve_skills_dir()."""
    d = skills_dir if skills_dir is not None else resolve_skills_dir()
    try:
        return sorted(p.name for p in d.iterdir() if p.is_dir() and p.name.startswith("bito-"))
    except OSError:
        return []


def _bito_skill_block(arm: str, skills_dir: Optional[Path] = None) -> str:
    """Skill-routing guidance built from the installed SKILL.md files, so the prompt
    always reflects what's actually available on this machine.
    - Arm B: only bito-codebase-explorer (the single skill it may use).
    - Arm C: the full installed suite, each with its description, so the model routes
      to the right skill(s).

    skills_dir: directory to scan (resolved automatically when not provided)."""
    if arm == "B":
        desc = _skill_description("bito-codebase-explorer", skills_dir)
        if not desc:
            return ""
        return (
            "\n\nSKILL TO USE — invoke `bito-codebase-explorer` via the Skill tool and "
            "follow its workflow. What it is for:\n"
            f"  {desc}\n"
        )
    if arm == "C":
        lines = []
        for name in installed_bito_skills(skills_dir):
            d = _skill_description(name, skills_dir)
            lines.append(f"- `{name}`: {d}" if d else f"- `{name}`")
        if not lines:
            return ""
        return (
            "\n\nAVAILABLE BITO SKILLS — route to the best fit for this task (chain "
            "several in sequence when it calls for it), and invoke each via the Skill "
            "tool. What each is for:\n" + "\n".join(lines) + "\n"
        )
    return ""


def effective_prompt(
    arm: str,
    prompt: str,
    workspace_mode: str = "fresh-clone",
    skills_dir: Optional[Path] = None,
) -> str:
    """Build the final prompt for a run: task + indexed-repo hint + common workspace
    rules + arm-specific suffix (A = no-Bito, B = codebase-explorer only,
    C = full skill suite).  The repo list gives Arm A concrete targets to clone
    and gives Arms B/C matching hints for Bito queries.

    workspace_mode:
      - "fresh-clone" (default): cwd starts empty; arms obtain source by cloning.
      - "local-repo": cwd is already a copy of the target repo (with uncommitted
        changes); arms work in place and must not clone. Lets a user give a goal and
        compare arms with vs without AI Architect on their own local code.

    skills_dir: override the Bito skills directory used for Arms B/C prompt injection.
      When None, resolve_skills_dir() is called automatically (Claude Code first,
      Copilot CLI fallback). Pass explicitly from the runner when the selected tool
      determines which skills path to use."""
    out = prompt
    local = workspace_mode == "local-repo"

    # Inject the indexed-repo list so every arm has real repo names to work with.
    # Arm A uses them for git/glab clone; Arms B/C use them as Bito query hints.
    repos = load_indexed_repos()
    if repos:
        names = ", ".join(repos[:50]) + ("…" if len(repos) > 50 else "")
        if local:
            # The target repo is already checked out — clone hints would be misleading.
            # Keep the names purely as cross-repo query hints for Bito (Arms B/C).
            out += (
                "\n\n---\n"
                "REPOSITORIES INDEXED IN AI ARCHITECT:\n"
                f"These repositories are indexed and queryable for cross-repo context: {names}.\n"
                "Use their exact names when querying Bito AI Architect. The repository you "
                "are changing is already checked out in your working directory.\n"
            )
        else:
            namespace = load_git_namespace()
            if namespace:
                # User has configured a namespace prefix — provide exact clone commands.
                example = f"`glab repo clone {namespace}/<repo-name>` or `gh repo clone {namespace}/<repo-name>`"
                clone_hint = (
                    f"Clone a repository with: {example} "
                    f"(namespace prefix: `{namespace}`)."
                )
            else:
                # No namespace set — let the model search via glab/gh to find the full path.
                clone_hint = (
                    "If you need to clone, use `glab repo list --search <name>` or "
                    "`gh repo list --json nameWithOwner --search <name>` to find the "
                    "full path first, then `glab repo clone <path>` or `gh repo clone <path>`."
                )
            out += (
                "\n\n---\n"
                "REPOSITORIES AVAILABLE IN THIS WORKSPACE:\n"
                f"The following repositories are indexed and available to investigate: {names}.\n"
                f"{clone_hint}\n"
                "Use their exact names when querying Bito AI Architect.\n"
            )

    common = COMMON_PROMPT_SUFFIX_LOCAL if local else COMMON_PROMPT_SUFFIX
    if common:
        out += common
    out += ARM_SUFFIXES.get(arm.upper(), "")
    # Inject skill descriptions read from the installed SKILL.md files so the model
    # knows what each Bito skill does and when to route to it (B = the one skill it
    # may use; C = the full installed suite).
    out += _bito_skill_block(arm.upper(), skills_dir)
    if local:
        # Arm B/C suffixes tell the model to clone before editing; in local-repo mode the
        # checkout already exists. Override those instructions without rewriting the suffixes.
        out += (
            "\n\nNOTE (local-repo mode): the target repository is ALREADY checked out in "
            "your working directory, including local changes. Disregard any instruction "
            "above about cloning a fresh copy or creating an empty workspace — make your "
            "changes against the existing checkout in the current directory.\n"
        )
    return out


def bito_skills_used(tool_calls: list[dict] | None) -> list[str]:
    """Return names of any bito-* skills invoked (from tool_calls list)."""
    if not tool_calls:
        return []
    found: list[str] = []
    for tc in tool_calls:
        if tc.get("name") == "Skill":
            for s in tc.get("skills") or []:
                if isinstance(s, str) and s.startswith("bito-"):
                    found.append(s)
    return found


def bito_mcp_used(tool_calls: list[dict] | None) -> list[str]:
    """Return names of any BitoAIArchitect MCP tools that were called."""
    if not tool_calls:
        return []
    return [
        str(tc.get("name", ""))
        for tc in tool_calls
        if str(tc.get("name", "")).startswith("mcp__BitoAIArchitect__")
    ]


def skill_policy_violation(arm: str, tool_calls: list[dict] | None) -> str | None:
    """Return a violation reason string if the arm broke its skill policy, else None.

    Arm A: must NOT use any bito-* skill or any BitoAIArchitect MCP tool.
    Arm B: must NOT use any bito-* skill other than bito-codebase-explorer.
    Arm C: must use AT LEAST ONE bito-* skill.
    """
    skills = bito_skills_used(tool_calls)
    mcp_tools = bito_mcp_used(tool_calls)
    if arm == "A":
        if skills:
            return f"used bito skill(s) {sorted(set(skills))} despite the ban"
        if mcp_tools:
            sample = sorted(set(mcp_tools))[:3]
            return f"used BitoAIArchitect MCP tool(s) {sample} despite the ban"
    if arm == "B":
        bad = [s for s in skills if s != "bito-codebase-explorer"]
        if bad:
            return (
                f"used disallowed bito skill(s) {sorted(set(bad))} — "
                "Arm B may only use bito-codebase-explorer"
            )
    if arm == "C" and not skills:
        return "invoked no bito-* skill (Arm C must use at least one)"
    return None


def ensure_glab_on_path() -> None:
    """Prepend the known glab install dir to PATH if glab isn't resolvable,
    so run subprocesses can clone from GitLab."""
    if shutil.which("glab"):
        return
    local = os.environ.get("LOCALAPPDATA")
    if local:
        cand = Path(local) / "Programs" / "glab"
        if (cand / "glab.exe").exists():
            os.environ["PATH"] = str(cand) + os.pathsep + os.environ.get("PATH", "")


def noninteractive_env() -> dict:
    """Environment for headless runs: never pop an interactive auth dialog. The
    benchmark tells the model it MAY clone repos; on Windows an unauthenticated
    `git clone` otherwise triggers Git Credential Manager's GitHub login popup,
    which hangs the run. These vars make any auth-needing clone fail fast instead."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"      # git: never prompt on the terminal
    env["GCM_INTERACTIVE"] = "Never"      # Git Credential Manager: no GUI popup
    env["GH_PROMPT_DISABLED"] = "1"       # gh CLI: no interactive prompts
    return env


def _rmtree(path: Path) -> None:
    """Robust rmtree that clears read-only bits (e.g. files under .git on Windows)."""
    def _onexc(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    try:
        shutil.rmtree(path, onexc=_onexc)  # Python 3.12+
    except TypeError:
        shutil.rmtree(path, onerror=_onexc)


def reset_workspace(path: Path) -> None:
    """(Re)create an empty scratch working directory for a run."""
    if path.exists():
        _rmtree(path)
    path.mkdir(parents=True)


def seed_workspace_from_repo(work_dir: Path, repo_path: str | Path) -> None:
    """local-repo workspace mode: give the run an isolated, editable copy of the user's
    repo INCLUDING uncommitted changes, so every arm starts from identical state and the
    user's real checkout is never touched. Recreated fresh before each attempt for
    reproducibility. The whole tree (incl. .git) is copied so agents can use git/branches."""
    src = Path(repo_path).expanduser()
    if not src.is_dir():
        raise ValueError(
            f"workspace_mode is 'local-repo' but local_repo_path is not a directory: {src}"
        )
    if work_dir.exists():
        _rmtree(work_dir)
    work_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, work_dir, symlinks=True, ignore_dangling_symlinks=True)


def find_claude_cli() -> str:
    """Locate the claude CLI executable, raising a helpful error if missing."""
    candidate = shutil.which("claude") or shutil.which("claude.exe")
    if not candidate and os.name == "nt":
        # Fallback: `where` catches PATH entries the Python process didn't inherit
        # (e.g. Claude installed after server started, or via user-PATH on Windows).
        try:
            import subprocess as _sp
            r = _sp.run(["where", "claude"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                first = r.stdout.strip().splitlines()[0].strip()
                if first:
                    candidate = first
        except Exception:
            pass
    if not candidate:
        sys.exit(
            "ERROR: 'claude' CLI not found on PATH. "
            "Install Claude Code or add its install directory to PATH."
        )
    return candidate


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS runs (
          arm TEXT NOT NULL,
          prompt_id TEXT NOT NULL,
          prompt TEXT,
          response TEXT,
          duration_ms INTEGER,            -- wall-clock from harness
          duration_api_ms INTEGER,        -- as reported by Claude
          num_turns INTEGER,
          input_tokens INTEGER,
          output_tokens INTEGER,
          cache_read_tokens INTEGER,
          cache_creation_tokens INTEGER,
          total_cost_usd REAL,
          model TEXT,
          tool_calls_json TEXT,
          session_id TEXT,
          exit_code INTEGER,
          error TEXT,
          bito_violation INTEGER,         -- Arm A only: 1 if a bito-* skill was used despite the ban
          started_at TEXT,
          PRIMARY KEY (arm, prompt_id)
        );
        CREATE TABLE IF NOT EXISTS judgments (
          prompt_id TEXT PRIMARY KEY,
          scores_a_json TEXT,             -- {dim: 1-5} per arm (0 if refusal)
          scores_b_json TEXT,
          scores_c_json TEXT,
          refusal_a INTEGER,
          refusal_b INTEGER,
          refusal_c INTEGER,
          rationale TEXT,
          presentation_order TEXT,        -- e.g. "CAB": order the 3 answers were shown to the judge
          judge_cost_usd REAL,
          judge_duration_ms INTEGER,
          error TEXT,
          judged_at TEXT
        );
        """
    )
    return conn


# ---------- Helpers to invoke Claude Code headlessly ----------

def win_safe_cmd(cmd: list[str]) -> list[str]:
    """On Windows, a .cmd/.bat shim (how npm installs `claude`, and `npm` itself) can't
    be launched directly via subprocess — CreateProcess raises [WinError 193] "not a
    valid Win32 application". Route those through cmd.exe. No-op on POSIX, for real
    executables, and for already-wrapped commands."""
    if os.name == "nt" and cmd and isinstance(cmd[0], str) \
            and cmd[0].lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", *cmd]
    return cmd


def claude_cmd(
    *,
    prompt: str,
    mcp_config: Path,
    output_format: str,
    model: str,
    max_turns: int | None,
    verbose: bool,
    via_stdin: bool = False,
    disallowed_tools: list[str] | None = None,
) -> list[str]:
    cli = find_claude_cli()
    # When via_stdin, omit the prompt from argv (the caller pipes it to stdin).
    # Windows caps the command line at ~32KB; large prompts (e.g. the 3-answer
    # judge prompt) must go through stdin to avoid "filename or extension is too long".
    cmd = [
        cli,
        "-p", *([] if via_stdin else [prompt]),
        "--output-format", output_format,
        "--mcp-config", str(mcp_config),
        "--dangerously-skip-permissions",
        "--model", model,
    ]
    # NOTE: --strict-mcp-config is deliberately omitted. Without it, the servers in
    # --mcp-config are MERGED with whatever the user already has available — their
    # locally-registered (`claude mcp add`) servers AND their claude.ai account
    # connectors (Google Drive, Datadog, Slack, etc.) — so every arm can use the
    # customer's full connected toolset with zero per-server config-file editing.
    #
    # This means Bito is no longer excluded from Arm A by config-file omission alone.
    # Arm A's Bito ban is now enforced ENTIRELY by name-based tool denial below —
    # this is no longer a "belt and suspenders" backup, it is the primary guard.
    if disallowed_tools:
        cmd += ["--disallowedTools", *disallowed_tools]
    if max_turns is not None:
        cmd += ["--max-turns", str(max_turns)]
    if verbose:
        cmd.append("--verbose")
    return cmd


def run_claude_stream_json(
    *,
    prompt: str,
    mcp_config: Path,
    model: str,
    max_turns: int,
    stream_path: Path,
    cwd: Path | None = None,
    disallowed_tools: list[str] | None = None,
) -> tuple[int, float]:
    """Invoke claude -p with stream-json, writing each line to stream_path.
    Returns (exit_code, wall_seconds).
    """
    cmd = claude_cmd(
        prompt=prompt,
        mcp_config=mcp_config,
        output_format="stream-json",
        model=model,
        max_turns=max_turns,
        verbose=True,
        disallowed_tools=disallowed_tools,
    )
    stream_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    with open(stream_path, "w", encoding="utf-8") as out:
        proc = subprocess.Popen(
            win_safe_cmd(cmd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(cwd) if cwd else None,
            env=noninteractive_env(),
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            out.write(line)
            out.flush()
        # Drain stderr so the process can exit cleanly; append at the end of the file as a JSON comment line.
        stderr = proc.stderr.read() if proc.stderr else ""
        rc = proc.wait()
    elapsed = time.perf_counter() - t0
    if stderr.strip():
        # Persist stderr alongside the stream for debugging — separate file.
        stream_path.with_suffix(".stderr.txt").write_text(stderr, encoding="utf-8")
    return rc, elapsed


def run_claude_json(
    *,
    prompt: str,
    mcp_config: Path,
    model: str,
    max_turns: int | None,
) -> tuple[int, float, dict | None, str]:
    """Invoke claude -p with json output. Returns (exit_code, wall_sec, parsed_obj, stderr)."""
    cmd = claude_cmd(
        prompt=prompt,
        mcp_config=mcp_config,
        output_format="json",
        model=model,
        max_turns=max_turns,
        verbose=False,
        via_stdin=True,
    )
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            win_safe_cmd(cmd),
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=noninteractive_env(),
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        return 1, time.perf_counter() - t0, None, "Timed out after 5 minutes."
    elapsed = time.perf_counter() - t0
    obj: dict | None = None
    if proc.stdout.strip():
        try:
            obj = json.loads(proc.stdout)
        except json.JSONDecodeError:
            # Sometimes the CLI prefixes a couple of stray lines; try last-brace heuristic.
            m = re.search(r"\{.*\}\s*\Z", proc.stdout, re.DOTALL)
            if m:
                try:
                    obj = json.loads(m.group(0))
                except json.JSONDecodeError:
                    obj = None
    return proc.returncode, elapsed, obj, proc.stderr


# ---------- Parsing stream-json into the per-run summary ----------

def parse_stream_jsonl(path: Path) -> dict[str, Any]:
    """Walk a stream-json file and extract:
       - the final `result` event (cost, tokens, duration, num_turns, session_id, result text)
       - tool-call counts across all assistant turns
    """
    result_event: dict[str, Any] | None = None
    tool_counter: Counter[str] = Counter()
    skills_by_tool: dict[str, list[str]] = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = ev.get("type")
            if etype == "result":
                result_event = ev
            elif etype == "assistant":
                msg = ev.get("message") or {}
                content = msg.get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            name = block.get("name", "<unknown>")
                            tool_counter[name] += 1
                            # Capture the skill name behind each Skill invocation so
                            # callers can enforce per-arm skill policy (e.g. Arm A).
                            if name == "Skill":
                                sk = (block.get("input") or {}).get("skill")
                                if isinstance(sk, str):
                                    skills_by_tool.setdefault("Skill", []).append(sk)
    summary: dict[str, Any] = {
        "tool_calls": [
            {"name": n, "count": c, **({"skills": skills_by_tool[n]} if n in skills_by_tool else {})}
            for n, c in tool_counter.most_common()
        ],
    }
    if result_event:
        usage = result_event.get("usage") or {}
        summary.update({
            "result_text": result_event.get("result"),
            "session_id": result_event.get("session_id"),
            "duration_api_ms": result_event.get("duration_ms"),
            "num_turns": result_event.get("num_turns"),
            "total_cost_usd": result_event.get("total_cost_usd"),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cache_read_tokens": usage.get("cache_read_input_tokens"),
            "cache_creation_tokens": usage.get("cache_creation_input_tokens"),
            "model_usage": result_event.get("modelUsage"),
            "subtype": result_event.get("subtype"),
            "is_error": bool(result_event.get("is_error")),
        })
    return summary


def primary_model_from_usage(model_usage: Any, fallback: str) -> str:
    if isinstance(model_usage, dict) and model_usage:
        return max(model_usage.items(), key=lambda kv: (kv[1] or {}).get("output_tokens") or 0)[0]
    return fallback


# Tools that constitute "searching for / reading code" — used to attribute
# discovery effort and the tokens pulled into context from it.
_DISCOVERY_TOOLS = {"Read", "Glob", "Grep", "Bash"}


def _is_discovery_tool(name: str) -> bool:
    return name in _DISCOVERY_TOOLS or name.startswith("mcp__BitoAIArchitect__")


def stream_io_stats(path: Path) -> dict[str, Any]:
    """Re-walk a run's stream file for analytics not stored in the runs table:
    time-to-first-token, tool-call counts, and an approximation of how many
    tokens of content were pulled into context by searching/reading code.

    The token figure is approximate (tool-result characters ÷ 4); the CLI does
    not attribute token usage to individual tool calls.
    """
    ttft_ms = None
    id_to_name: dict[str, str] = {}
    tool_calls_total = 0
    discovery_calls = 0
    result_chars_total = 0
    result_chars_discovery = 0
    skills: list[str] = []          # bito-* (and other) skills, in invocation order
    cloned: list[str] = []          # repo names cloned via glab/git
    branches: list[str] = []        # branches created via git checkout -b
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                etype = ev.get("type")
                if etype == "result":
                    ttft_ms = ev.get("ttft_ms")
                elif etype == "assistant":
                    for b in (ev.get("message") or {}).get("content") or []:
                        if isinstance(b, dict) and b.get("type") == "tool_use":
                            name = b.get("name", "")
                            id_to_name[b.get("id")] = name
                            tool_calls_total += 1
                            if _is_discovery_tool(name):
                                discovery_calls += 1
                            inp = b.get("input") or {}
                            if name == "Skill":
                                sk = inp.get("skill")
                                if isinstance(sk, str):
                                    skills.append(sk)
                            elif name == "Bash":
                                cmd = inp.get("command", "") or ""
                                # Repo = the clone SOURCE after the clone keyword: prefer a
                                # token with a "/" or ".git" (namespace/repo or URL), skipping
                                # flags and their values like "--depth 1".
                                for m in re.finditer(r"(?:glab repo clone|git clone)\s+(.+)", cmd):
                                    toks = m.group(1).split()
                                    src = next((t for t in toks if not t.startswith("-")
                                                and ("/" in t or t.endswith(".git"))), None)
                                    if src is None:
                                        src = next((t for t in toks if not t.startswith("-")
                                                    and not t.isdigit()), None)
                                    if src:
                                        repo = src.rstrip("/").split("/")[-1].replace(".git", "")
                                        if repo and repo not in cloned:
                                            cloned.append(repo)
                                for m in re.findall(r"checkout -b (\S+)", cmd):
                                    if m not in branches:
                                        branches.append(m)
                elif etype == "user":
                    for b in (ev.get("message") or {}).get("content") or []:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            c = b.get("content")
                            ln = len(c) if isinstance(c, str) else len(json.dumps(c))
                            result_chars_total += ln
                            if _is_discovery_tool(id_to_name.get(b.get("tool_use_id"), "")):
                                result_chars_discovery += ln
    except OSError:
        pass
    return {
        "ttft_ms": ttft_ms,
        "tool_calls_total": tool_calls_total,
        "discovery_calls": discovery_calls,
        "tool_tokens_total": result_chars_total // 4,
        "tool_tokens_discovery": result_chars_discovery // 4,
        "skills": skills,
        "cloned": cloned,
        "branches": branches,
    }


# ---------- Subcommand: setup ----------

def cmd_setup(args: argparse.Namespace) -> None:
    """Build the per-arm MCP config files from ~/.claude.json.

    Writes three files into configs/:
      - mcp-arm-a.json    : every MCP from ~/.claude.json EXCEPT the chosen Bito server (Arm A)
      - mcp-arm-bito.json : same set, WITH the Bito server included (Arms B and C)

    Sources merged (in priority order; first wins on key collision):
      1. ~/.claude.json top-level "mcpServers"
      2. ~/.claude.json projects[*].mcpServers (user-added per-project)
      3. Installed Claude Code plugins' .mcp.json files (per
         ~/.claude/plugins/installed_plugins.json)
    """
    home = Path.home()
    claude_json = home / ".claude.json"
    if not claude_json.exists():
        sys.exit(f"ERROR: {claude_json} not found.")
    try:
        data = json.loads(claude_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"ERROR: could not parse {claude_json}: {e}")

    servers, sources = collect_mcp_servers(data, home / ".claude" / "plugins")
    if not servers:
        sys.exit("ERROR: no mcpServers found in ~/.claude.json or installed plugins.")

    bito_candidates = [k for k in servers if "bito" in k.lower() or "architect" in k.lower()]
    if args.bito_server:
        if args.bito_server not in servers:
            sys.exit(
                f"ERROR: --bito-server '{args.bito_server}' not in available MCP servers: {sorted(servers)}"
            )
        bito_key = args.bito_server
    elif len(bito_candidates) == 1:
        bito_key = bito_candidates[0]
    elif len(bito_candidates) > 1:
        sys.exit(
            "Multiple Bito-looking MCP servers found: "
            + ", ".join(bito_candidates)
            + ". Re-run with --bito-server <name>."
        )
    else:
        sys.exit(
            "No MCP server matching 'bito' or 'architect' in ~/.claude.json. "
            f"Available: {sorted(servers)}. Install the Bito MCP first, "
            "or re-run with --bito-server <name> to point at a different server."
        )

    arm_a_servers = {k: v for k, v in servers.items() if k != bito_key}
    arm_bito_servers = dict(servers)  # arm A's set + the Bito server (used by arms B and C)

    CONFIGS.mkdir(parents=True, exist_ok=True)
    arm_a_path = CONFIGS / "mcp-arm-a.json"
    arm_bito_path = CONFIGS / "mcp-arm-bito.json"
    arm_a_path.write_text(json.dumps({"mcpServers": arm_a_servers}, indent=2), encoding="utf-8")
    arm_bito_path.write_text(json.dumps({"mcpServers": arm_bito_servers}, indent=2), encoding="utf-8")

    a_names = sorted(arm_a_servers) or ["(none)"]
    bito_names = sorted(arm_bito_servers)
    print(f"Wrote {arm_a_path}  (Arm A — {len(arm_a_servers)} MCP(s): {', '.join(a_names)})")
    print(f"Wrote {arm_bito_path}  (Arms B & C — {len(arm_bito_servers)} MCP(s): {', '.join(bito_names)})")
    print(f"\nThe MCP delta between Arm A and Arms B/C is exactly one server: '{bito_key}'.")
    print("Arms B and C share this config; they differ only by prompt suffix (skill policy).")

    by_src: dict[str, list[str]] = {}
    for name, src in sources.items():
        by_src.setdefault(src, []).append(name)
    print("\nMCP sources merged:")
    for src in ("top-level", "per-project", "plugin"):
        names = sorted(by_src.get(src, []))
        if names:
            print(f"  {src}: {', '.join(names)}")
    print("\nNext — verify the Bito MCP is actually live and see your indexed repos:")
    print("  python harness.py doctor")


def collect_mcp_servers(
    data: dict, plugins_dir: Path | None = None
) -> tuple[dict[str, dict], dict[str, str]]:
    """Merge MCP servers from every source Claude Code sees:

    1. ~/.claude.json top-level "mcpServers"
    2. ~/.claude.json projects[*].mcpServers
    3. Each currently-installed Claude Code plugin's .mcp.json
       (per ~/.claude/plugins/installed_plugins.json)

    First write wins on name collision. Returns (servers_by_name, source_by_name)
    where source ∈ {"top-level", "per-project", "plugin"}.
    """
    out: dict[str, dict] = {}
    sources: dict[str, str] = {}

    def add(name: str, server: dict, source: str) -> None:
        if name not in out:
            out[name] = server
            sources[name] = source

    top = data.get("mcpServers")
    if isinstance(top, dict):
        for k, v in top.items():
            add(k, v, "top-level")

    projects = data.get("projects")
    if isinstance(projects, dict):
        for proj in projects.values():
            if isinstance(proj, dict):
                ms = proj.get("mcpServers")
                if isinstance(ms, dict):
                    for k, v in ms.items():
                        add(k, v, "per-project")

    if plugins_dir is not None:
        for k, v in _collect_plugin_mcp_servers(plugins_dir).items():
            add(k, v, "plugin")

    return out, sources


def _collect_plugin_mcp_servers(plugins_dir: Path) -> dict[str, dict]:
    """Read .mcp.json from each currently-installed Claude Code plugin.

    Walks ~/.claude/plugins/installed_plugins.json for the canonical
    install paths (handles multiple installed versions per plugin by
    using the entry's recorded installPath). Falls back to scanning
    plugins/cache/**/.mcp.json if the index file is missing.
    """
    out: dict[str, dict] = {}
    index_file = plugins_dir / "installed_plugins.json"
    install_paths: list[Path] = []

    if index_file.exists():
        try:
            idx = json.loads(index_file.read_text(encoding="utf-8"))
            for entries in (idx.get("plugins") or {}).values():
                if isinstance(entries, list):
                    for e in entries:
                        ip = (e or {}).get("installPath")
                        if isinstance(ip, str) and ip:
                            install_paths.append(Path(ip))
        except (OSError, json.JSONDecodeError):
            pass

    if not install_paths:
        cache = plugins_dir / "cache"
        if cache.exists():
            install_paths = [p.parent for p in cache.rglob(".mcp.json")]

    for ip in install_paths:
        manifest = ip / ".mcp.json"
        if not manifest.exists():
            continue
        try:
            servers = (json.loads(manifest.read_text(encoding="utf-8"))
                       .get("mcpServers") or {})
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(servers, dict):
            for k, v in servers.items():
                out.setdefault(k, v)
    return out


# ---------- Subcommand: doctor ----------

# A connectivity probe: the answer text isn't the point — the point is whether a
# real claude run can actually CALL a BitoAIArchitect tool. We key "MCP is live"
# off a tool_use for an mcp__BitoAIArchitect__* tool appearing in the stream, and
# read the indexed-repo list out of the model's JSON reply.
DOCTOR_PROBE_PROMPT = (
    "You are a connectivity probe for the BitoAIArchitect MCP server (also called "
    "AI Architect). Do exactly this:\n"
    "1. Use ToolSearch with query 'select:mcp__BitoAIArchitect__listRepositories' "
    "to load the tool schema. If ToolSearch says 'BitoAIArchitect is still "
    "connecting', call ToolSearch again with the same query — keep retrying until "
    "the tool appears or you have used at least 10 search attempts. Do NOT give up "
    "after 1-2 attempts; the MCP server needs a moment to complete its handshake.\n"
    "2. Once the tool is available, call `mcp__BitoAIArchitect__listRepositories` "
    "with purposeType='discovery', purpose='list indexed repos', "
    "callerPlatform='CLAUDE_CODE'.\n"
    "3. Then reply with ONLY a single-line JSON object — no prose, no markdown:\n"
    '   {"ok": true, "repositories": ["<repo name>", ...]}\n'
    "   listing EVERY repository name the tool returned.\n"
    "Only reply with {\"ok\": false, \"repositories\": []} if after 10+ ToolSearch "
    "retries the tool STILL does not appear (genuine unavailability, not just "
    "slow startup). Never invent repository names."
)

INDEXED_REPOS_PATH = ROOT / "indexed-repos.txt"


def _probe_bito(model: str, max_turns: int) -> dict:
    """Run one headless claude call against the Bito arm config and report whether
    the BitoAIArchitect MCP tools were genuinely reachable, plus the indexed repos.

    Returns {"mcp_called": bool, "repositories": list[str], "result_text": str,
             "exit_code": int}.
    """
    mcp_config = CONFIGS / "mcp-arm-bito.json"
    work = RUNS / "work" / "doctor"
    stream_path = RUNS / "doctor" / "probe.stream.jsonl"
    reset_workspace(work)
    ensure_glab_on_path()
    rc, _wall = run_claude_stream_json(
        prompt=DOCTOR_PROBE_PROMPT,
        mcp_config=mcp_config,
        model=model,
        max_turns=max_turns,
        stream_path=stream_path,
        cwd=work,
    )
    summary = parse_stream_jsonl(stream_path) if stream_path.exists() else {}
    tool_calls = summary.get("tool_calls") or []
    mcp_called = any(
        str(tc.get("name", "")).startswith("mcp__BitoAIArchitect__") for tc in tool_calls
    )
    repos: list[str] = []
    result_text = summary.get("result_text") or ""
    parsed = extract_judge_json(result_text)
    if isinstance(parsed, dict) and isinstance(parsed.get("repositories"), list):
        repos = [str(x).strip() for x in parsed["repositories"] if str(x).strip()]

    # Did the probe run itself complete normally? A run that exited nonzero,
    # produced no result event, or returned an error result (Claude usage/session
    # limit, CLI crash, model error) never got the chance to call the Bito tools.
    # In that case a missing tool call is INCONCLUSIVE — not proof Bito is down —
    # so we must not blame the Bito URL/token. Only when the run completed cleanly
    # is `mcp_called == False` genuine evidence that Bito stayed silent.
    run_ok = (rc == 0) and bool(summary) and not summary.get("is_error")
    failure_reason = "" if run_ok else _classify_probe_failure(rc, result_text, summary)
    return {
        "mcp_called": mcp_called,
        "run_ok": run_ok,
        "failure_reason": failure_reason,
        "repositories": repos,
        "result_text": result_text,
        "exit_code": rc,
    }


def _classify_probe_failure(rc: int, result_text: str, summary: dict) -> str:
    """Turn a failed probe run into a short, honest reason — separating an
    environment failure (usage limit, CLI error) from a Bito problem."""
    low = (result_text or "").lower()
    if "session limit" in low or "usage limit" in low or "rate limit" in low:
        return (
            "Claude usage/session limit was hit during the probe — this is a Claude "
            "account limit, not a Bito problem. Re-run the health check after it resets."
        )
    if not summary:
        return f"The probe run produced no output (claude exit code {rc})."
    if summary.get("is_error"):
        msg = (result_text or "").strip().splitlines()[0] if result_text else ""
        return f"The probe run ended with an error: {msg[:160]}" if msg else \
            "The probe run ended with an error before it could reach Bito."
    return f"The probe run did not complete cleanly (claude exit code {rc})."


def _write_indexed_repos(repos: list[str], bito_servers: list[str]) -> None:
    lines = [
        f"# Repositories indexed in your Bito AI Architect workspace "
        f"({', '.join(bito_servers)}).",
        f"# Generated by `python harness.py doctor` at "
        f"{datetime.now().isoformat(timespec='seconds')}.",
        "# Aim your prompts.json questions at these repos — AI Architect can only "
        "ground answers in what it has indexed.",
        "",
    ]
    lines += repos if repos else ["(none reported)"]
    INDEXED_REPOS_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_indexed_repos() -> list[str]:
    """Read the canonical indexed-repo names from indexed-repos.txt (written by
    `doctor`). Returns [] if the file is absent — the judge then falls back to
    grounding answers on their internal merits only."""
    try:
        lines = INDEXED_REPOS_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [
        ln.strip() for ln in lines
        if ln.strip() and not ln.lstrip().startswith("#") and ln.strip() != "(none reported)"
    ]


def load_git_namespace() -> str:
    """Read the user-configured git namespace/org prefix (e.g. 'myorg' or
    'gitlab.company.com/myteam'). Returns '' if not set."""
    try:
        return GIT_NAMESPACE_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def save_git_namespace(namespace: str) -> None:
    """Persist the git namespace to configs/git-namespace.txt."""
    CONFIGS.mkdir(parents=True, exist_ok=True)
    GIT_NAMESPACE_PATH.write_text(namespace.strip(), encoding="utf-8")


def _run_doctor(model: str, max_turns: int) -> bool:
    """Preflight: verify the harness can run AND the Bito MCP is genuinely live,
    then print + persist the indexed-repo list. Returns True only if every check
    passed and the MCP answered with at least one indexed repo.

    This guards the experiment's single most damaging failure: a missing or
    misconfigured Bito MCP makes Arms B/C silently fall back to baseline, so Bito
    looks like it adds nothing — a false negative the run itself can't surface.
    """
    docs = "https://docs.bito.ai/ai-architect/quick-mcp-integration-with-ai-coding-agents"

    # 1. claude CLI on PATH.
    cli = shutil.which("claude") or shutil.which("claude.exe")
    if not cli:
        print("[FAIL] claude CLI not on PATH. Install Claude Code or add it to PATH.")
        return False
    print(f"[ok]   claude CLI: {cli}")

    # 2. Per-arm configs exist.
    arm_a = CONFIGS / "mcp-arm-a.json"
    arm_bito = CONFIGS / "mcp-arm-bito.json"
    if not (arm_a.exists() and arm_bito.exists()):
        print("[FAIL] Missing arm config(s). Run `python harness.py setup` first.")
        return False
    print(f"[ok]   Arm configs present: {arm_a.name}, {arm_bito.name}")

    # 3. A Bito/AI-Architect server is actually in the bito-arm config.
    bito_servers = [
        n for n in read_mcp_servers(arm_bito)
        if "bito" in n.lower() or "architect" in n.lower()
    ]
    if not bito_servers:
        print("[FAIL] No Bito / AI-Architect server in mcp-arm-bito.json.")
        print(f"       Install the Bito MCP (README Step 0: {docs}), then re-run `setup`.")
        return False
    print(f"[ok]   Bito server configured: {', '.join(bito_servers)}")

    # 4. The real test: can a live run actually call the tools?
    print("\nProbing AI Architect with one short headless claude call…")
    probe = _probe_bito(model=model, max_turns=max_turns)
    if not probe["mcp_called"]:
        print("[FAIL] The BitoAIArchitect MCP tools did NOT respond in a real run.")
        print("       Arms B and C would silently degrade to baseline (Arm A) — an invalid test.")
        print("       Likely causes:")
        print("         • the MCP server URL or token is wrong/expired, or")
        print("         • an OAuth MCP still needs a one-time `/mcp` authentication, or")
        print("         • the Bito MCP isn't installed for this workspace yet.")
        print(f"       Fix: follow README Step 0 ({docs}), then re-run `python harness.py doctor`.")
        if probe["result_text"]:
            print(f"       Probe replied: {probe['result_text'][:200]}")
        return False

    repos = probe["repositories"]
    _write_indexed_repos(repos, bito_servers)
    print("[ok]   AI Architect MCP is LIVE — its tools answered in a real run.")
    if not repos:
        print("[FAIL] The MCP is live but reported ZERO indexed repositories "
              "(or the list could not be read).")
        print("       Arms B/C would have nothing to ground on. Index your repos in the "
              "Bito AI Architect workspace, then re-run `python harness.py doctor`.")
        return False

    print(f"\n✅ {len(repos)} repositories are indexed in your AI Architect workspace:")
    for r in repos:
        print(f"     • {r}")
    print(f"\nWrote this list to {INDEXED_REPOS_PATH}.")
    print("➡️  Write your prompts.json questions around THESE repositories. Asking AI "
          "Architect about a repo it hasn't indexed yields weak, ungrounded answers "
          "and isn't a fair test of what Bito adds.")
    return True


def cmd_doctor(args: argparse.Namespace) -> None:
    ok = _run_doctor(model=args.model, max_turns=args.max_turns)
    print()
    if ok:
        print("doctor: all checks passed — you're ready to run the benchmark.")
    else:
        print("doctor: NOT ready — fix the items above, then re-run `python harness.py doctor`.")
        sys.exit(1)


# ---------- Subcommand: run ----------

def load_prompts(path: Path) -> list[dict]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, list):
        sys.exit("prompts.json must be a JSON array of {id, prompt} objects.")
    seen = set()
    out = []
    for item in obj:
        if not isinstance(item, dict) or "id" not in item or "prompt" not in item:
            sys.exit("Each prompts.json item must have 'id' and 'prompt'.")
        if item["id"] in seen:
            sys.exit(f"Duplicate prompt id: {item['id']}")
        seen.add(item["id"])
        out.append(item)
    return out


def already_succeeded(conn: sqlite3.Connection, arm: str, prompt_id: str) -> bool:
    row = conn.execute(
        "SELECT exit_code, error FROM runs WHERE arm=? AND prompt_id=?", (arm, prompt_id)
    ).fetchone()
    if not row:
        return False
    return row[0] == 0 and not row[1]


def read_mcp_servers(path: Path) -> dict[str, dict]:
    """Read an MCP config file and return its `mcpServers` dict (empty if absent or malformed)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    servers = data.get("mcpServers")
    return servers if isinstance(servers, dict) else {}


def bito_disallow_patterns() -> list[str]:
    """Tool-deny patterns that block EVERY Bito MCP server's tools for Arm A.

    THIS IS ARM A'S PRIMARY BITO GUARD, not a backup. --strict-mcp-config is
    deliberately NOT used (see claude_cmd), so every arm merges in whatever MCP
    servers the user already has — local (`claude mcp add`) servers AND claude.ai
    account connectors — including Bito, if the user happens to have it configured
    that way. Arm A must therefore be denied Bito by tool NAME, unconditionally.

    MCP tools are namespaced `mcp__<serverKey>__<tool>`, so we deny `mcp__<key>__*`
    for every Bito-looking server key the harness knows about (from the arm-bito
    config) plus a set of common literal fallbacks, in case Bito is reachable only
    as a claude.ai connector under a name this harness has never seen locally.

    CAUTION: if an org's Bito connector uses a key name that doesn't contain
    "bito"/"architect" and isn't in the fallback set below, it will NOT be caught.
    Verify the customer's actual connector name (via `/mcp` in an interactive
    session) and add it here explicitly if it doesn't match."""
    keys = {
        k for k in read_mcp_servers(CONFIGS / "mcp-arm-bito.json")
        if "bito" in k.lower() or "architect" in k.lower()
    }
    # Canonical + common literal fallbacks — covers the case where Bito is only
    # reachable as a claude.ai connector (not in any local file this harness reads).
    keys.update({"BitoAIArchitect", "Bito", "bito", "bito-ai-architect", "BitoAI"})
    return [f"mcp__{k}__*" for k in sorted(keys)]


def confirm_arm_run(
    *,
    arm: str,
    mcp_config: Path,
    prompts: list[dict],
    args: argparse.Namespace,
) -> bool:
    """Show the active MCP list for this arm + run shape, then ask the user to confirm."""
    servers = read_mcp_servers(mcp_config)
    names = sorted(servers)
    has_bito = any("bito" in n.lower() or "architect" in n.lower() for n in names)

    print()
    print(f"About to run arm {arm} with these MCPs EXPLICITLY declared in {mcp_config}:")
    if names:
        for n in names:
            print(f"  - {n}")
    else:
        print("  (none listed — but see note below)")
    if has_bito:
        print("(Bito MCP IS explicitly loaded in this arm.)")
    else:
        print(
            "(Bito MCP is not in this file, but --strict-mcp-config is NOT used — "
            "any locally-registered MCPs or claude.ai connectors already available "
            "to this user will ALSO be merged in at runtime. Arm A's Bito ban is "
            "enforced by tool-name denial (bito_disallow_patterns), not by omission "
            "from this file alone.)"
            if arm == "A" else
            "(Bito MCP is not in this file, but will be merged in from the user's "
            "locally-registered MCPs / claude.ai connectors if available there.)"
        )
    print()
    print(
        f"{len(prompts)} prompt(s) x {args.model}, max {args.max_turns} turns each. "
        "Resumable — already-successful (arm, prompt_id) rows will be skipped."
    )
    if COMMON_PROMPT_SUFFIX:
        print()
        print("Common prompt suffix (all arms, appended to every prompt):")
        for line in COMMON_PROMPT_SUFFIX.strip().splitlines():
            print(f"  | {line}")
    arm_suffix = ARM_SUFFIXES.get(arm, "")
    if arm_suffix:
        print()
        print(f"Arm {arm} prompt suffix (appended to every prompt before sending):")
        for line in arm_suffix.strip().splitlines():
            print(f"  | {line}")
    if arm == "A":
        print()
        print(f"Arm A enforcement: any bito-* skill use triggers up to "
              f"{SKILL_RERUN_LIMIT} rerun(s); a persistent violation is kept but flagged.")
    elif arm == "C":
        print()
        print(f"Arm C enforcement: using NO bito-* skill triggers up to "
              f"{SKILL_RERUN_LIMIT} rerun(s); a persistent violation is kept but flagged.")

    if args.yes or os.environ.get("CI"):
        print("--yes / CI set: proceeding without prompt.")
        return True
    try:
        answer = input("Proceed? [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def _stamp_bito_bearer(mcp_config: Path, token: str) -> bool:
    """Standalone fallback: write `Authorization: Bearer <token>` onto the Bito server
    in `mcp_config` (no backend needed). Used for the CLI static-token path."""
    try:
        data = json.loads(mcp_config.read_text(encoding="utf-8"))
        servers = data.get("mcpServers")
    except Exception:
        return False
    if not isinstance(servers, dict):
        return False
    key = next((k for k in servers if "bito" in k.lower() or "architect" in k.lower()), None)
    if not key or not isinstance(servers.get(key), dict):
        return False
    servers[key].setdefault("type", "http")
    servers[key]["headers"] = {"Authorization": f"Bearer {token}"}
    mcp_config.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return True


def _bito_bearer_present(mcp_config: Path) -> dict:
    """Standalone fallback verdict: is a usable Bito bearer already in `mcp_config`?"""
    try:
        data = json.loads(mcp_config.read_text(encoding="utf-8"))
        servers = data.get("mcpServers") or {}
    except Exception:
        return {"ok": False, "detail": f"could not read {mcp_config.name}"}
    key = next((k for k in servers if "bito" in k.lower() or "architect" in k.lower()), None)
    if not key:
        return {"ok": False, "detail": "no Bito server in the MCP config"}
    auth = (servers[key].get("headers") or {}).get("Authorization", "")
    tok = auth[len("Bearer "):].strip() if auth.startswith("Bearer ") else ""
    if not tok or tok.lower() == "none":
        return {"ok": False, "detail": "Bito server has no Authorization bearer"}
    return {"ok": True}


def prepare_bito_auth(mcp_config: Path, static_token: Optional[str]) -> dict:
    """Ensure the Bito server in `mcp_config` carries a usable bearer before a B/C run,
    supporting BOTH modes. Prefers the backend OAuth service (refresh + static +
    passthrough); if the backend can't be imported (pure standalone CLI), stamps an
    explicit static token as-is, or else just verifies a bearer is already present."""
    try:
        from backend.services import bito_oauth
        return bito_oauth.ensure_bito_ready(static_token)
    except Exception:
        pass
    if static_token and static_token.strip():
        _stamp_bito_bearer(mcp_config, static_token.strip())
    return _bito_bearer_present(mcp_config)


def cmd_run(args: argparse.Namespace) -> None:
    arm = args.arm.upper()
    if arm not in {"A", "B", "C"}:
        sys.exit("--arm must be A, B, or C")
    if args.config:
        mcp_config = Path(args.config)
    else:
        # A = no Bito; B and C share the Bito-enabled config (they differ only by prompt suffix).
        mcp_config = CONFIGS / ("mcp-arm-a.json" if arm == "A" else "mcp-arm-bito.json")
    if not mcp_config.exists():
        sys.exit(
            f"ERROR: missing {mcp_config}. Run `python harness.py setup` first, "
            "or pass --config <path> to point at a custom MCP config."
        )

    # Arms B/C need a usable Bito bearer in the config the headless run reads (under
    # --strict-mcp-config there is no ~/.claude.json fallback). Refresh OAuth or accept
    # an explicit static token (--bito-token / $BITO_TOKEN) before launching, and refuse
    # to run if Bito still isn't reachable — so B/C aren't misreported as losing when
    # they simply never reached AI Architect. Skipped for a custom --config.
    if arm in {"B", "C"} and not args.config:
        static_tok = getattr(args, "bito_token", None) or os.environ.get("BITO_TOKEN")
        verdict = prepare_bito_auth(mcp_config, static_tok)
        if not verdict.get("ok"):
            hint = (
                f" (token refresh also failed: {verdict['refresh_error']})"
                if verdict.get("refresh_error") else ""
            )
            sys.exit(
                f"ERROR: Bito is not ready for arm {arm}: "
                f"{verdict.get('detail', 'no usable bearer')}.{hint}\n"
                "Pass a valid static token with --bito-token / $BITO_TOKEN, or connect "
                "Bito (Setup) so an OAuth token can be refreshed. Refusing to run so "
                "B/C aren't misreported as losing when they couldn't reach AI Architect."
            )
        mode = verdict.get("mode")
        if mode:
            print(f"[arm {arm}] Bito auth ready (mode: {mode}).")

    prompts = load_prompts(Path(args.prompts))
    if args.limit:
        prompts = prompts[: args.limit]

    workspace_mode = getattr(args, "workspace_mode", "fresh-clone")
    local_repo = getattr(args, "local_repo", None)
    if workspace_mode == "local-repo":
        if not local_repo or not Path(local_repo).expanduser().is_dir():
            sys.exit(
                "--workspace-mode local-repo requires --local-repo <dir> pointing at an "
                f"existing repo checkout (got {local_repo!r})."
            )

    if not confirm_arm_run(arm=arm, mcp_config=mcp_config, prompts=prompts, args=args):
        sys.exit("Aborted.")

    conn = db()
    arm_dir = RUNS / arm
    arm_dir.mkdir(parents=True, exist_ok=True)
    ensure_glab_on_path()

    # Arm A must never reach Bito. Beyond omitting it from the config, hard-deny the
    # Bito MCP tools by name so a plugin-registered server can't slip past.
    deny_tools = bito_disallow_patterns() if arm == "A" else None

    for item in tqdm(prompts, desc=f"arm {arm}", unit="q"):
        pid = item["id"]
        prompt = effective_prompt(arm, item["prompt"], workspace_mode=workspace_mode)
        if already_succeeded(conn, arm, pid):
            continue

        stream_path = arm_dir / f"{pid}.stream.jsonl"
        result_path = arm_dir / f"{pid}.result.json"
        work_dir = RUNS / "work" / arm / pid
        last_error = None
        rc = -1
        wall = 0.0
        summary: dict[str, Any] = {}
        bito_violation = 0

        # Arms A, B, and C all have a skill policy that triggers reruns on violation.
        # A violation triggers up to SKILL_RERUN_LIMIT reruns, then keep-and-flag.
        max_attempts = len(RETRY_BACKOFFS_SEC) + 1 + SKILL_RERUN_LIMIT
        for attempt in range(max_attempts):
            try:
                # Fresh cwd per attempt: in fresh-clone mode an empty dir (the run can't
                # see the harness, results.db, prior answers, or any project CLAUDE.md);
                # in local-repo mode a fresh copy of the target repo incl. uncommitted changes.
                if workspace_mode == "local-repo":
                    seed_workspace_from_repo(work_dir, local_repo)
                else:
                    reset_workspace(work_dir)
                rc, wall = run_claude_stream_json(
                    prompt=prompt,
                    mcp_config=mcp_config,
                    model=args.model,
                    max_turns=args.max_turns,
                    stream_path=stream_path,
                    cwd=work_dir,
                    disallowed_tools=deny_tools,
                )
                summary = parse_stream_jsonl(stream_path) if stream_path.exists() else {}
                # Require a non-empty result: the CLI can report success with an
                # empty result string when the final turn ends on a tool call.
                if rc == 0 and summary.get("result_text"):
                    reason = skill_policy_violation(arm, summary.get("tool_calls"))
                    if reason:
                        bito_violation = 1
                        # Rerun if we still have attempts left; else keep-and-flag.
                        if attempt < max_attempts - 1:
                            print(f"\n  [arm {arm}] {pid}: {reason} — rerunning ({attempt + 1}/{SKILL_RERUN_LIMIT}).")
                            time.sleep(INTER_RUN_SLEEP_SEC)
                            continue
                        print(f"\n  [arm {arm}] {pid}: still violating skill policy after {SKILL_RERUN_LIMIT} rerun(s) — keeping and flagging.")
                    else:
                        bito_violation = 0
                    last_error = None
                    break
                last_error = f"exit_code={rc} subtype={summary.get('subtype')} result_len={len(summary.get('result_text') or '')}"
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
            if attempt < max_attempts - 1:
                time.sleep(RETRY_BACKOFFS_SEC[min(attempt, len(RETRY_BACKOFFS_SEC) - 1)])

        result_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

        conn.execute(
            """INSERT INTO runs (arm, prompt_id, prompt, response, duration_ms, duration_api_ms,
                num_turns, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
                total_cost_usd, model, tool_calls_json, session_id, exit_code, error,
                bito_violation, started_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(arm, prompt_id) DO UPDATE SET
                 prompt=excluded.prompt, response=excluded.response,
                 duration_ms=excluded.duration_ms, duration_api_ms=excluded.duration_api_ms,
                 num_turns=excluded.num_turns, input_tokens=excluded.input_tokens,
                 output_tokens=excluded.output_tokens, cache_read_tokens=excluded.cache_read_tokens,
                 cache_creation_tokens=excluded.cache_creation_tokens,
                 total_cost_usd=excluded.total_cost_usd, model=excluded.model,
                 tool_calls_json=excluded.tool_calls_json, session_id=excluded.session_id,
                 exit_code=excluded.exit_code, error=excluded.error,
                 bito_violation=excluded.bito_violation,
                 started_at=excluded.started_at""",
            (
                arm,
                pid,
                prompt,
                summary.get("result_text"),
                int(wall * 1000),
                summary.get("duration_api_ms"),
                summary.get("num_turns"),
                summary.get("input_tokens"),
                summary.get("output_tokens"),
                summary.get("cache_read_tokens"),
                summary.get("cache_creation_tokens"),
                summary.get("total_cost_usd"),
                primary_model_from_usage(summary.get("model_usage"), args.model),
                json.dumps(summary.get("tool_calls") or []),
                summary.get("session_id"),
                rc,
                last_error,
                bito_violation,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        time.sleep(INTER_RUN_SLEEP_SEC)

    conn.close()
    print(f"\nArm {arm}: done. Inspect with: sqlite3 {DB_PATH} \"SELECT prompt_id, total_cost_usd, output_tokens FROM runs WHERE arm='{arm}'\"")


# ---------- Subcommand: judge ----------

def _rubric_block() -> str:
    return "\n".join(f"- {d}: {RUBRIC_LABELS[d]}" for d in RUBRIC_DIMS)


JUDGE_PROMPT_TEMPLATE = """You are evaluating THREE answers to the same question. The answers are unlabeled and shown in random order as Answer 1, Answer 2, Answer 3.

IMPORTANT — about tool access:
The answers were produced by another model in separate sessions that may have
had DIFFERENT tools available than you do right now. Do NOT infer an answerer's
tool access from your own session. Treat specific, internally-consistent content
(repo names, file paths, code references) as plausibly real unless it has internal
contradictions or violates well-known facts. You MAY use any tools you have to
spot-check claims, but your job is primarily to score the answers as written for
substance and quality.

{canonical_repos}
REFUSALS:
A "refusal" is an answer that does not substantively attempt the task: it declines
for any reason (claims it lacks tools, context, or access), asks for clarification
instead of answering, only describes how it WOULD answer, or is empty/near-empty.
An answer that substantively attempts the task is NOT a refusal, even if hedged or
partially incomplete. Decide refusal true/false for each answer independently. A
refusal scores 0 on ALL dimensions.

Score each answer INDEPENDENTLY on a 1-5 integer scale for these dimensions:
{rubric}

ANTI-VAGUENESS — do NOT reward an answer for saying little. Caution is not a virtue
when the task asked for substance:
- An answer that does not actually RESOLVE the question — it stays generic, hedges,
  defers, or gives boilerplate that would read the same for any codebase — scores
  LOW on correctness AND completeness, no matter how careful or well-written it is.
- hallucination_resistance is NOT a reward for making no claims. An answer with little
  substantive, checkable content cannot earn a high hallucination_resistance score —
  there is nothing to have gotten right. Reserve 4-5 for answers that make many
  specific, verifiable claims and get them right. A thin or evasive answer should sit
  around 2-3 here, not 5.
- grounding is earned ONLY by specific, verifiable evidence (named repos, files,
  symbols, configs, line references) — never by hedging, disclaimers, or stating the
  obvious. "I don't have access, but generally…" is NOT grounding; score it 1.
- Partial credit must track how much of the question was actually answered with
  substance, not how confidently or safely the answer was phrased.

Dimensions marked "ONLY for …" are conditional: score them 1-5 ONLY when the
QUESTION is that kind of task (a plan/design/build task for planning_quality; a
change/fix task for impact_analysis). If the question is not that kind of task,
return null for that dimension — do NOT invent a score. All other dimensions are
always scored 1-5.

Do NOT rank the answers against each other and do NOT force them apart — score each
on its own merits. Ties are completely fine: if two answers are equally good, give
them the same scores.

Return STRICT JSON (no markdown, no prose outside the JSON), exactly this shape:
{{"answer1": {{"refusal": true|false, {score_keys}}},
  "answer2": {{"refusal": true|false, {score_keys}}},
  "answer3": {{"refusal": true|false, {score_keys}}},
  "rationale": "<two or three sentences comparing the three answers>"}}

QUESTION:
{question}

ANSWER 1:
{answer1}

ANSWER 2:
{answer2}

ANSWER 3:
{answer3}
"""


def extract_judge_json(text: str) -> dict | None:
    if not text:
        return None
    text = text.strip()
    # Strip ```json fences if present
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


def _coerce_scores(raw: dict | None, refusal: bool) -> dict:
    """Return a {dim: int|None} score dict. Universal dims are 0 on a refusal and
    1-5 otherwise. Conditional dims are None when not scored (not applicable, or a
    refusal) so they don't drag down averages — they only count where they apply."""
    if refusal or not isinstance(raw, dict):
        return {**{d: 0 for d in UNIVERSAL_DIMS}, **{d: None for d in CONDITIONAL_DIMS}}
    out: dict = {}
    for d in UNIVERSAL_DIMS:
        v = raw.get(d)
        out[d] = int(v) if isinstance(v, (int, float)) else 0
    for d in CONDITIONAL_DIMS:
        v = raw.get(d)
        out[d] = int(v) if isinstance(v, (int, float)) else None
    return out


def cmd_judge(args: argparse.Namespace) -> None:
    prompts = load_prompts(Path(args.prompts))
    if args.limit:
        prompts = prompts[: args.limit]

    conn = db()
    # The judge gets the Bito-enabled config so it can spot-check claims if it wants;
    # falls back to no-MCP config if setup hasn't produced the Bito one.
    judge_config = CONFIGS / "mcp-arm-bito.json"
    if not judge_config.exists():
        judge_config = CONFIGS / "mcp-none.json"
    rng = random.Random(args.seed)
    arms = ("A", "B", "C")
    score_keys = ", ".join(f'"{d}": N' for d in RUBRIC_DIMS)
    rubric = _rubric_block()

    # Canonical truth-set of repos (from `doctor`'s indexed-repos.txt). Grounding in
    # a repo NOT on this list is treated as unverified / possibly stale, so an arm
    # can't win on specificity that points at non-canonical code (e.g. a stale repo).
    canonical = load_indexed_repos()
    if canonical:
        canonical_repos = (
            "CANONICAL REPOSITORIES (the source of truth):\n"
            "These are the repositories indexed in the organization's AI Architect "
            "knowledge base — the authoritative set of CURRENT, canonical repos:\n"
            + ", ".join(sorted(canonical)) + "\n"
            "When an answer grounds its claims in a repo ON this list, treat that grounding "
            "as verified. When an answer grounds its claims in a repo NOT on this list, treat "
            "that grounding as UNVERIFIED and possibly stale or non-canonical — weight it LOWER "
            "on grounding and correctness, because building on or citing a non-canonical repo "
            "is a real defect even when the answer looks specific. Do not reward apparent "
            "specificity that points at a repo outside this canonical set.\n"
        )
        print(f"judge: using {len(canonical)} canonical repos from {INDEXED_REPOS_PATH.name} as the truth-set.")
    else:
        canonical_repos = (
            "(No canonical repository list is available for this run — run "
            "`python harness.py doctor` to generate one. Judge grounding on its internal "
            "merits, treating specific, internally-consistent references as plausibly real.)\n"
        )
        print(f"judge: no {INDEXED_REPOS_PATH.name} found; judging grounding on internal merits only.")

    for item in tqdm(prompts, desc="judge", unit="q"):
        pid = item["id"]
        question = item["prompt"]
        rows = conn.execute(
            "SELECT arm, response, exit_code, error FROM runs WHERE prompt_id=?", (pid,)
        ).fetchall()
        by_arm = {r[0]: r for r in rows}
        # Need all three arms, each a clean successful run.
        if any(a not in by_arm for a in arms):
            continue
        if any(by_arm[a][2] != 0 or by_arm[a][3] for a in arms):
            continue
        if not args.force:
            existing = conn.execute(
                "SELECT scores_a_json, error FROM judgments WHERE prompt_id=?", (pid,)
            ).fetchone()
            if existing and existing[0] and not existing[1]:
                continue

        answers = {a: (by_arm[a][1] or "") for a in arms}
        # Random presentation order: a permutation of the three arms.
        order = list(arms)
        rng.shuffle(order)
        presentation_order = "".join(order)  # e.g. "CAB"

        judge_prompt = JUDGE_PROMPT_TEMPLATE.format(
            rubric=rubric, score_keys=score_keys, question=question,
            canonical_repos=canonical_repos,
            answer1=answers[order[0]], answer2=answers[order[1]], answer3=answers[order[2]],
        )

        verdict: dict | None = None
        wall = 0.0
        cost = None
        err: str | None = None
        for attempt in range(len(RETRY_BACKOFFS_SEC) + 1):
            rc, wall, obj, stderr = run_claude_json(
                prompt=judge_prompt,
                mcp_config=judge_config,
                model=args.judge_model,
                max_turns=args.max_turns,
            )
            if rc == 0 and obj:
                cost = obj.get("total_cost_usd")
                cand = extract_judge_json(obj.get("result") or "")
                if cand and all(f"answer{i}" in cand for i in (1, 2, 3)):
                    verdict = cand
                    err = None
                    break
                err = "judge returned no parseable verdict"
            else:
                err = f"exit_code={rc} stderr={(stderr or '').strip()[:200]}"
            if attempt < len(RETRY_BACKOFFS_SEC):
                time.sleep(RETRY_BACKOFFS_SEC[attempt])

        scores: dict[str, dict] = {}
        refusals: dict[str, bool | None] = {a: None for a in arms}
        if verdict:
            # Map positional answer{1,2,3} back to arms via the presentation order.
            for pos, arm in enumerate(order, start=1):
                ans = verdict.get(f"answer{pos}") or {}
                ref = bool(ans.get("refusal"))
                refusals[arm] = ref
                scores[arm] = _coerce_scores(ans, ref)
            rationale = verdict.get("rationale") or ""
        else:
            for arm in arms:
                scores[arm] = {}
            rationale = ""

        JUDGMENTS.mkdir(parents=True, exist_ok=True)
        judgment_path = JUDGMENTS / f"{pid}.judge.json"
        judgment_path.write_text(
            json.dumps(
                {
                    "prompt_id": pid,
                    "scores": scores,
                    "refusals": refusals,
                    "rationale": rationale,
                    "presentation_order": presentation_order,
                    "judge_cost_usd": cost,
                    "judge_duration_ms": int(wall * 1000),
                    "raw_verdict": verdict,
                    "error": err,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        conn.execute(
            """INSERT INTO judgments (prompt_id, scores_a_json, scores_b_json, scores_c_json,
                refusal_a, refusal_b, refusal_c, rationale, presentation_order,
                judge_cost_usd, judge_duration_ms, error, judged_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(prompt_id) DO UPDATE SET
                 scores_a_json=excluded.scores_a_json, scores_b_json=excluded.scores_b_json,
                 scores_c_json=excluded.scores_c_json,
                 refusal_a=excluded.refusal_a, refusal_b=excluded.refusal_b,
                 refusal_c=excluded.refusal_c, rationale=excluded.rationale,
                 presentation_order=excluded.presentation_order,
                 judge_cost_usd=excluded.judge_cost_usd,
                 judge_duration_ms=excluded.judge_duration_ms,
                 error=excluded.error, judged_at=excluded.judged_at""",
            (
                pid,
                json.dumps(scores.get("A", {})),
                json.dumps(scores.get("B", {})),
                json.dumps(scores.get("C", {})),
                None if refusals["A"] is None else int(refusals["A"]),
                None if refusals["B"] is None else int(refusals["B"]),
                None if refusals["C"] is None else int(refusals["C"]),
                rationale,
                presentation_order,
                cost,
                int(wall * 1000),
                err,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        time.sleep(INTER_RUN_SLEEP_SEC)

    conn.close()
    print(f"\nJudging done. Inspect with: sqlite3 {DB_PATH} \"SELECT prompt_id, scores_a_json, scores_b_json, scores_c_json FROM judgments\"")


# ---------- Subcommand: report ----------

def _avg(xs: list[float]) -> float:
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else 0.0


ARMS = ("A", "B", "C")
ARM_LABELS = {
    "A": "Arm A (baseline, no Bito)",
    "B": "Arm B (Bito + explorer only)",
    "C": "Arm C (Bito + all skills)",
}


def cmd_report(args: argparse.Namespace) -> None:
    conn = db()
    only_pids: set[str] | None = None
    out_suffix = ""
    if getattr(args, "prompts", None):
        ppath = Path(args.prompts)
        only_pids = {item["id"] for item in load_prompts(ppath)}
        out_suffix = f"-{ppath.stem}"

    rows = conn.execute(
        """SELECT arm, prompt_id, prompt, response, duration_ms, num_turns,
                  input_tokens, output_tokens, total_cost_usd, tool_calls_json,
                  exit_code, error, bito_violation,
                  cache_read_tokens, cache_creation_tokens
           FROM runs"""
    ).fetchall()
    by_pid: dict[str, dict[str, dict]] = {}
    for r in rows:
        arm, pid = r[0], r[1]
        if only_pids is not None and pid not in only_pids:
            continue
        rec = {
            "prompt": r[2], "response": r[3], "duration_ms": r[4], "num_turns": r[5],
            "input_tokens": r[6], "output_tokens": r[7], "total_cost_usd": r[8],
            "tool_calls_json": r[9], "exit_code": r[10], "error": r[11],
            "bito_violation": r[12],
            "cache_read_tokens": r[13], "cache_creation_tokens": r[14],
        }
        # Enrich with analytics recovered from the run's stream file.
        rec.update(stream_io_stats(RUNS / arm / f"{pid}.stream.jsonl"))
        by_pid.setdefault(pid, {})[arm] = rec
    judgments = {}
    for r in conn.execute(
        """SELECT prompt_id, scores_a_json, scores_b_json, scores_c_json,
                  refusal_a, refusal_b, refusal_c, rationale, error FROM judgments"""
    ).fetchall():
        if only_pids is not None and r[0] not in only_pids:
            continue
        judgments[r[0]] = {
            "scores": {"A": json.loads(r[1] or "{}"), "B": json.loads(r[2] or "{}"),
                       "C": json.loads(r[3] or "{}")},
            "refusal": {"A": bool(r[4]), "B": bool(r[5]), "C": bool(r[6])},
            "rationale": r[7], "error": r[8],
        }

    DIMS = RUBRIC_DIMS

    def is_refusal(pid: str, arm: str) -> bool:
        j = judgments.get(pid)
        return bool(j and not j["error"] and j["refusal"].get(arm))

    # A run is "completed" if it succeeded AND the judge did not flag it a refusal.
    # Refusal spend/time still counts toward the arm's totals, so a refusing arm
    # pays for its non-answers in the per-completed metrics.
    def arm_stats(arm: str) -> dict:
        runs = [(pid, d[arm]) for pid, d in by_pid.items() if arm in d]
        success = [(pid, r) for pid, r in runs if r["exit_code"] == 0 and not r["error"]]
        completed = [(pid, r) for pid, r in success if not is_refusal(pid, arm)]
        total_cost = sum(r["total_cost_usd"] or 0 for _, r in success)
        total_wall_ms = sum(r["duration_ms"] or 0 for _, r in success)
        n_completed = len(completed)
        comp_runs = [r for _, r in completed]
        return {
            "n": len(runs), "n_success": len(success), "n_completed": n_completed,
            "n_refusals": len(success) - n_completed,
            "n_violations": sum(1 for _, r in runs if r.get("bito_violation")),
            "completion_rate": (n_completed / len(success)) if success else 0.0,
            "avg_duration_ms": _avg([r["duration_ms"] for r in comp_runs]),
            "avg_ttft_ms": _avg([r.get("ttft_ms") for r in comp_runs]),
            "avg_num_turns": _avg([r["num_turns"] for r in comp_runs]),
            "avg_tool_calls": _avg([r.get("tool_calls_total") for r in comp_runs]),
            "avg_discovery_calls": _avg([r.get("discovery_calls") for r in comp_runs]),
            "avg_input_tokens": _avg([r["input_tokens"] for r in comp_runs]),
            "avg_output_tokens": _avg([r["output_tokens"] for r in comp_runs]),
            "avg_cache_read_tokens": _avg([r.get("cache_read_tokens") for r in comp_runs]),
            "avg_cache_creation_tokens": _avg([r.get("cache_creation_tokens") for r in comp_runs]),
            "avg_tool_tokens_discovery": _avg([r.get("tool_tokens_discovery") for r in comp_runs]),
            "avg_tool_tokens_total": _avg([r.get("tool_tokens_total") for r in comp_runs]),
            "total_cost_usd": total_cost,
            "total_wall_ms": total_wall_ms,
            "cost_per_completed": (total_cost / n_completed) if n_completed else None,
            "time_per_completed_ms": (total_wall_ms / n_completed) if n_completed else None,
        }

    stats = {a: arm_stats(a) for a in ARMS}

    # Per-prompt total score per arm (0 if refusal/missing). "Leader" = arm(s) with
    # the max total on that prompt; ties allowed (an arm shares the lead).
    def prompt_total(pid: str, arm: str) -> int | None:
        j = judgments.get(pid)
        if not j or j["error"]:
            return None
        sc = j["scores"].get(arm) or {}
        return sum(v for v in (sc.get(d) for d in DIMS) if isinstance(v, (int, float)))

    leader_counts = {a: 0 for a in ARMS}
    judged_ok = [pid for pid, j in judgments.items() if not j["error"]]
    for pid in judged_ok:
        totals = {a: prompt_total(pid, a) for a in ARMS}
        best = max((t for t in totals.values() if t is not None), default=None)
        if best is None:
            continue
        for a in ARMS:
            if totals[a] == best:
                leader_counts[a] += 1

    def rubric_avg(arm: str, dim: str) -> float:
        vals = [judgments[pid]["scores"].get(arm, {}).get(dim) for pid in judged_ok]
        return _avg([v for v in vals if isinstance(v, (int, float))])

    def arm_total(arm: str, dim: str | None = None) -> int:
        total = 0
        for pid in judged_ok:
            sc = judgments[pid]["scores"].get(arm, {})
            dims = [dim] if dim else DIMS
            for d in dims:
                v = sc.get(d)
                if isinstance(v, (int, float)):
                    total += v
        return total

    n_judged_ok = len(judged_ok)
    judge_failed = sum(1 for j in judgments.values() if j["error"])
    max_per_dim = n_judged_ok * 5
    max_overall = max_per_dim * len(DIMS)

    def money(v: float | None) -> str:
        return f"${v:.4f}" if v is not None else "n/a"

    def secs(v: float | None) -> str:
        return f"{v / 1000:.1f}" if v is not None else "n/a"

    def row3(label: str, fn) -> str:
        return f"| {label} | {fn('A')} | {fn('B')} | {fn('C')} |"

    REPORTS.mkdir(parents=True, exist_ok=True)
    md = REPORTS / f"summary{out_suffix}.md"
    hdr = "| metric | " + " | ".join(ARM_LABELS[a] for a in ARMS) + " |"
    sep = "|---|---:|---:|---:|"
    md_lines = [
        "# Bito A/B/C Harness Summary" + (f" — {out_suffix.lstrip('-')}" if out_suffix else ""),
        "",
        f"_Generated {datetime.now().isoformat(timespec='seconds')}_",
        "",
        "Arms: **A** = baseline (no Bito). **B** = Bito MCP + only the "
        "bito-codebase-explorer skill. **C** = Bito MCP + free use of all Bito skills.",
        "",
        "## Per-arm aggregates",
        "",
        "A run is **completed** when it succeeded and was not judged a refusal.",
        "Refusal spend and wall-clock still count toward the arm's totals, so the",
        "per-completed metrics charge an arm for its non-answers.",
        "",
        hdr, sep,
        row3("prompts attempted", lambda a: stats[a]["n"]),
        row3("successful runs", lambda a: stats[a]["n_success"]),
        row3("refusals (per judge)", lambda a: stats[a]["n_refusals"]),
        row3("completed (substantive)", lambda a: stats[a]["n_completed"]),
        row3("**completion rate**", lambda a: f"**{stats[a]['completion_rate']:.0%}**"),
        row3("skill-policy violations", lambda a: stats[a]["n_violations"] if a in ("A", "C") else "—"),
        row3("total spend, all attempts", lambda a: money(stats[a]["total_cost_usd"])),
        row3("**cost per completed answer**", lambda a: f"**{money(stats[a]['cost_per_completed'])}**"),
        "",
        "## Speed & effort (completed answers)",
        "",
        "All averaged over completed (non-refusal) answers. \"Reasoning steps\" is the",
        "number of agentic turns (model responses); \"discovery calls\" are tool calls that",
        "search or read code (Read/Glob/Grep/Bash and AI Architect search/read tools).",
        "",
        hdr, sep,
        row3("**wall-clock per answer (s)**", lambda a: f"**{secs(stats[a]['time_per_completed_ms'])}**"),
        row3("avg time to first response (s)", lambda a: secs(stats[a]["avg_ttft_ms"])),
        row3("avg reasoning steps (turns)", lambda a: f"{stats[a]['avg_num_turns']:.1f}"),
        row3("avg tool calls", lambda a: f"{stats[a]['avg_tool_calls']:.1f}"),
        row3("avg discovery (search/read) calls", lambda a: f"{stats[a]['avg_discovery_calls']:.1f}"),
        "",
        "## Token economics (avg per completed answer)",
        "",
        "Tokens are from the API's own accounting. \"Tokens read via discovery\" approximates",
        "how much code/content was pulled into context by search/read tool calls "
        "(tool-result characters ÷ 4; the CLI does not attribute tokens to individual tools).",
        "",
        hdr, sep,
        row3("input tokens", lambda a: f"{stats[a]['avg_input_tokens']:.0f}"),
        row3("output tokens", lambda a: f"{stats[a]['avg_output_tokens']:.0f}"),
        row3("cache-read tokens", lambda a: f"{stats[a]['avg_cache_read_tokens']:.0f}"),
        row3("cache-creation tokens", lambda a: f"{stats[a]['avg_cache_creation_tokens']:.0f}"),
        row3("~tokens read via discovery tools", lambda a: f"{stats[a]['avg_tool_tokens_discovery']:.0f}"),
        row3("~tokens read via all tools", lambda a: f"{stats[a]['avg_tool_tokens_total']:.0f}"),
        "",
        "## Judgments",
        "",
        "Each prompt is scored by ONE blind judge call that sees all three answers in",
        "random order and scores each independently (no ranking; ties allowed). A refusal",
        "scores 0 on every dimension (enforced in code). \"Led\" = had the top total score",
        "on a prompt (shared on ties).",
        "",
        f"- Judged OK: **{n_judged_ok}** / {len(by_pid)}   (judge failures: {judge_failed})",
        row3("prompts led (top score, ties shared)", lambda a: f"**{leader_counts[a]}**"),
        "",
        "## Rubric averages (1-5)",
        "",
        hdr.replace("metric", "dimension"), sep,
    ]
    for d in DIMS:
        md_lines.append(row3(d, lambda a, d=d: f"{rubric_avg(a, d):.2f}"))

    md_lines += [
        "",
        f"## Total rubric scores (summed across {n_judged_ok} judged prompts)",
        "",
        "| dimension | " + " | ".join(ARM_LABELS[a] for a in ARMS) + " | max |",
        "|---|---:|---:|---:|---:|",
    ]
    for d in DIMS:
        md_lines.append(
            f"| {d} | " + " | ".join(str(arm_total(a, d)) for a in ARMS) + f" | {max_per_dim} |"
        )
    md_lines.append(
        "| **OVERALL** | " + " | ".join(f"**{arm_total(a)}**" for a in ARMS) + f" | **{max_overall}** |"
    )

    # Per-question rubric breakdown
    md_lines += [
        "",
        "## Per-question scores",
        "",
        f"Format per arm: `{' / '.join(DIMS)}  (sum)`",
        "",
        "| prompt_id | " + " | ".join(ARM_LABELS[a] for a in ARMS) + " | rationale |",
        "|---|---|---|---|---|",
    ]

    def fmt_scores(scores: dict, refusal: bool) -> str:
        if refusal:
            return "REFUSAL (0)"
        vals = [scores.get(d) for d in DIMS]
        s = " / ".join(str(v) if isinstance(v, (int, float)) else "-" for v in vals)
        nums = [v for v in vals if isinstance(v, (int, float))]
        return f"{s}  ({sum(nums)})" if nums else "-"

    for pid in sorted(by_pid):
        j = judgments.get(pid, {})
        if not j or j.get("error"):
            md_lines.append(f"| {pid} | — | — | — | {j.get('error') or 'no judgment'} |")
            continue
        rationale = (j.get("rationale") or "").replace("|", "\\|").replace("\n", " ")
        cells = " | ".join(fmt_scores(j["scores"].get(a, {}), j["refusal"].get(a)) for a in ARMS)
        md_lines.append(f"| {pid} | {cells} | {rationale} |")

    # Approach per arm — a scannable trace of HOW each answer was produced.
    md_lines += [
        "",
        "## Approach per arm (how each answer was produced)",
        "",
        "Skills invoked (in order), repos cloned, branches created, and effort per run. "
        "Full answers are in the companion `answers" + out_suffix + ".md`.",
        "",
        "| prompt_id | arm | turns | cost | skills (in order) | cloned repos | branches |",
        "|---|---|---:|---:|---|---|---|",
    ]
    for pid in sorted(by_pid):
        for a in ARMS:
            r = by_pid[pid].get(a)
            if not r:
                continue
            skills = " → ".join(r.get("skills") or []) or "—"
            cloned = ", ".join(r.get("cloned") or []) or "—"
            branches = ", ".join(r.get("branches") or []) or "—"
            cost = f"${r['total_cost_usd']:.2f}" if r.get("total_cost_usd") is not None else "—"
            md_lines.append(
                f"| {pid} | {a} | {r.get('num_turns') or '—'} | {cost} | {skills} | {cloned} | {branches} |"
            )

    # Flag runs that broke their arm's skill policy (A used a bito skill; C used none).
    violations = [
        (arm, pid)
        for pid, d in by_pid.items()
        for arm in ("A", "C")
        if d.get(arm, {}).get("bito_violation")
    ]
    if violations:
        md_lines += [
            "",
            "## ⚠️ Skill-policy violations",
            "",
            "Kept after exhausting reruns — treat their scores as contaminated. "
            "Arm A = used a bito-* skill despite the ban; Arm C = used no bito-* skill "
            "despite the requirement.",
            "",
        ]
        md_lines += [f"- Arm {arm}: {pid}" for arm, pid in sorted(violations)]

    md.write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    # Companion answers file: the full text of every arm's answer, with a header
    # line showing how it was produced, so a reviewer can read what was actually said.
    ans_lines = [
        f"# Answers — {out_suffix.lstrip('-') or 'all prompts'} (Arms A / B / C)",
        "",
        "Each prompt below shows the original question and all three arms' full answers.",
        "The header on each answer notes turns, cost, skills invoked, and repos cloned.",
    ]
    for pid in sorted(by_pid):
        any_run = next((by_pid[pid][a] for a in ARMS if a in by_pid[pid]), {})
        raw_prompt = (any_run.get("prompt") or "").split("\n\n---\n")[0]
        ans_lines += ["", "", "=" * 80, f"# {pid}", "", f"**PROMPT:** {raw_prompt}"]
        for a in ARMS:
            r = by_pid[pid].get(a)
            if not r:
                continue
            skills = " → ".join(r.get("skills") or []) or "no skill"
            cloned = ", ".join(r.get("cloned") or []) or "no clone"
            cost = f"${r['total_cost_usd']:.2f}" if r.get("total_cost_usd") is not None else "?"
            ans_lines += [
                "",
                f"## {ARM_LABELS[a]}  ({r.get('num_turns')} turns, {cost}, "
                f"skills: {skills}, cloned: {cloned})",
                "",
                r.get("response") or "(no response)",
            ]
    ans_path = REPORTS / f"answers{out_suffix}.md"
    ans_path.write_text("\n".join(ans_lines) + "\n", encoding="utf-8")

    # CSV per prompt
    csv_path = REPORTS / f"summary{out_suffix}.csv"
    fields = ["prompt_id", "rationale"]
    per_arm_cols = ["refusal", "cost_usd", "duration_ms", "ttft_ms", "num_turns",
                    "tool_calls_total", "discovery_calls", "input_tokens",
                    "output_tokens", "cache_read_tokens", "cache_creation_tokens",
                    "tool_tokens_discovery", "tool_tokens_total", "error"]
    for a in ARMS:
        fields += [f"{a}_{c}" for c in per_arm_cols]
        fields += [f"{a}_{d}" for d in DIMS]
    fields.append("A_bito_violation")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for pid in sorted(by_pid):
            j = judgments.get(pid, {})
            row = {"prompt_id": pid, "rationale": j.get("rationale") or ""}
            for a in ARMS:
                run = by_pid[pid].get(a, {})
                row[f"{a}_refusal"] = (j.get("refusal") or {}).get(a) if j else None
                row[f"{a}_cost_usd"] = run.get("total_cost_usd")
                row[f"{a}_duration_ms"] = run.get("duration_ms")
                row[f"{a}_ttft_ms"] = run.get("ttft_ms")
                row[f"{a}_num_turns"] = run.get("num_turns")
                row[f"{a}_tool_calls_total"] = run.get("tool_calls_total")
                row[f"{a}_discovery_calls"] = run.get("discovery_calls")
                row[f"{a}_input_tokens"] = run.get("input_tokens")
                row[f"{a}_output_tokens"] = run.get("output_tokens")
                row[f"{a}_cache_read_tokens"] = run.get("cache_read_tokens")
                row[f"{a}_cache_creation_tokens"] = run.get("cache_creation_tokens")
                row[f"{a}_tool_tokens_discovery"] = run.get("tool_tokens_discovery")
                row[f"{a}_tool_tokens_total"] = run.get("tool_tokens_total")
                row[f"{a}_error"] = run.get("error") or ""
                for d in DIMS:
                    row[f"{a}_{d}"] = (j.get("scores", {}).get(a) or {}).get(d) if j else None
            row["A_bito_violation"] = by_pid[pid].get("A", {}).get("bito_violation")
            w.writerow(row)

    print(f"Wrote {md}")
    print(f"Wrote {ans_path}")
    print(f"Wrote {csv_path}")
    conn.close()


# ---------- Subcommand: all ----------

def cmd_all(args: argparse.Namespace) -> None:
    """One command for the whole pipeline:
    setup (if needed) → doctor (preflight) → run A/B/C → judge → report.

    This is the path a packaged customer should use: edit prompts.json, run this.
    The doctor preflight refuses to spend tokens if the Bito MCP isn't genuinely
    live, so Arms B/C can never silently collapse into baseline unnoticed.
    """
    from argparse import Namespace

    arm_a = CONFIGS / "mcp-arm-a.json"
    arm_bito = CONFIGS / "mcp-arm-bito.json"

    # 1. Build the per-arm MCP configs from ~/.claude.json (always, unless the
    #    user has them and asked to skip). setup itself exits with guidance if it
    #    can't unambiguously identify the Bito server.
    if not (arm_a.exists() and arm_bito.exists()):
        print("Arm configs not found — generating them from ~/.claude.json (setup)…\n")
        cmd_setup(Namespace(bito_server=args.bito_server))
    elif args.skip_setup:
        print("Using existing arm configs (--skip-setup).")
    else:
        print("Refreshing arm configs from ~/.claude.json (setup)…\n")
        cmd_setup(Namespace(bito_server=args.bito_server))

    # 1b. Make the Bito bearer usable before the preflight probes it (refresh an OAuth
    #     token or stamp an explicit static --bito-token / $BITO_TOKEN), so doctor tests
    #     exactly what the runs will use.
    static_tok = getattr(args, "bito_token", None) or os.environ.get("BITO_TOKEN")
    prepare_bito_auth(arm_bito, static_tok)

    # 2. Preflight — abort before spending tokens if the Bito MCP isn't live.
    if args.skip_doctor:
        print("\nSkipping preflight (--skip-doctor). Arms B/C may silently degrade if the "
              "Bito MCP isn't live.")
    else:
        print("\n=== Preflight (doctor) ===")
        if not _run_doctor(model=args.model, max_turns=args.doctor_max_turns):
            sys.exit(
                "\nAborting: preflight failed, so the Bito arms would not be a fair test. "
                "Fix the issues above (or pass --skip-doctor to override) and re-run."
            )

    prompts = load_prompts(Path(args.prompts))
    n = min(args.limit, len(prompts)) if args.limit else len(prompts)

    # 3. One confirmation gate for the entire pipeline.
    print("\n=== Plan ===")
    print(f"  Prompts:   {args.prompts}  ({n} question(s))")
    print("  Arms:      A (baseline, no Bito) → B (lean Bito) → C (full Bito)")
    print(f"  Model:     {args.model}    Judge: {args.judge_model}")
    print("  Then:      blind 3-way judge → reports/summary.md + summary.csv")
    print("  Resumable: already-successful (arm, prompt) rows are skipped.")
    if not (args.yes or os.environ.get("CI")):
        try:
            if input("\nProceed with the full A/B/C run? [y/N] ").strip().lower() not in {"y", "yes"}:
                sys.exit("Aborted.")
        except EOFError:
            sys.exit("Aborted.")

    # 4. Run all three arms (the single confirmation above covers them → yes=True).
    for arm in ARMS:
        print(f"\n=== Run arm {arm} ===")
        cmd_run(Namespace(
            arm=arm, config=None, prompts=args.prompts, model=args.model,
            max_turns=args.max_turns, limit=args.limit, yes=True,
            workspace_mode=getattr(args, "workspace_mode", "fresh-clone"),
            local_repo=getattr(args, "local_repo", None),
            bito_token=getattr(args, "bito_token", None),
        ))

    # 5. Blind judge.
    print("\n=== Judge ===")
    cmd_judge(Namespace(
        prompts=args.prompts, judge_model=args.judge_model, max_turns=10,
        limit=args.limit, seed=DEFAULT_JUDGE_SEED, force=args.force,
    ))

    # 6. Headline report (reports/summary.md + .csv).
    print("\n=== Report ===")
    cmd_report(Namespace(prompts=None))

    print("\nAll done. See reports/summary.md and reports/summary.csv.")


# ---------- CLI ----------

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser(
        "setup",
        help="Build configs/mcp-arm-a.json (your MCPs minus Bito) and configs/mcp-arm-bito.json (with Bito, used by arms B and C)",
    )
    sp.add_argument(
        "--bito-server",
        help="Exact MCP server key for Bito (overrides the auto-detect heuristic)",
    )
    sp.set_defaults(func=cmd_setup)

    sp = sub.add_parser(
        "doctor",
        help="Preflight: verify claude + the Bito MCP are genuinely live, and list the indexed repos",
    )
    sp.add_argument("--model", default=DEFAULT_MODEL, help="Model for the live probe call")
    sp.add_argument("--max-turns", type=int, default=DEFAULT_DOCTOR_MAX_TURNS,
                    help="Turn cap for the probe call")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser(
        "all",
        help="One command: setup (if needed) → doctor → run A/B/C → judge → report",
    )
    sp.add_argument("--prompts", default="prompts.json")
    sp.add_argument("--model", default=DEFAULT_MODEL)
    sp.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL)
    sp.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS,
                    help="Per-run agentic-turn cap")
    sp.add_argument("--doctor-max-turns", type=int, default=DEFAULT_DOCTOR_MAX_TURNS,
                    help="Turn cap for the preflight probe")
    sp.add_argument("--limit", type=int, help="Only process the first N prompts")
    sp.add_argument("--bito-server", help="Exact Bito MCP server key (passed through to setup)")
    sp.add_argument("--bito-token",
                    help="Static Bito bearer token for arms B/C (stamped as-is, no OAuth "
                         "refresh). Falls back to $BITO_TOKEN.")
    sp.add_argument("--skip-setup", action="store_true",
                    help="Use existing configs instead of regenerating them")
    sp.add_argument("--skip-doctor", action="store_true",
                    help="Skip the preflight MCP check (not recommended)")
    sp.add_argument("--workspace-mode", choices=["fresh-clone", "local-repo"],
                    default="fresh-clone",
                    help="fresh-clone (default) or local-repo (run all arms against a copy "
                         "of --local-repo, incl. uncommitted changes).")
    sp.add_argument("--local-repo",
                    help="Path to the local repo checkout (required for "
                         "--workspace-mode local-repo).")
    sp.add_argument("--force", action="store_true",
                    help="Re-judge prompts that already have a verdict")
    sp.add_argument("--yes", "-y", action="store_true",
                    help="Skip the single confirmation gate (also skipped when env CI is set)")
    sp.set_defaults(func=cmd_all)

    sp = sub.add_parser("run", help="Run all prompts under one arm (A, B, or C)")
    sp.add_argument("--arm", required=True, choices=["A", "B", "C", "a", "b", "c"])
    sp.add_argument(
        "--config",
        help="Path to an MCP config file (overrides the default per-arm file: "
             "configs/mcp-arm-a.json for A, configs/mcp-arm-bito.json for B and C)",
    )
    sp.add_argument("--prompts", default="prompts.json")
    sp.add_argument("--model", default=DEFAULT_MODEL)
    sp.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
    sp.add_argument("--limit", type=int, help="Only run the first N prompts (for smoke tests)")
    sp.add_argument("--workspace-mode", choices=["fresh-clone", "local-repo"],
                    default="fresh-clone",
                    help="fresh-clone (default): empty cwd, arm clones source. "
                         "local-repo: run against a copy of --local-repo (incl. uncommitted "
                         "changes) so the arm can make local code changes in place.")
    sp.add_argument("--local-repo",
                    help="Path to the local repo checkout to test (required for "
                         "--workspace-mode local-repo).")
    sp.add_argument("--bito-token",
                    help="Static Bito bearer token for arms B/C (stamped as-is, no "
                         "OAuth refresh). Falls back to $BITO_TOKEN. If omitted, an "
                         "OAuth session is refreshed instead when available.")
    sp.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip the pre-run confirmation prompt (also skipped when env var CI is set)",
    )
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("judge", help="Blind 3-way judge: score each arm's answer per prompt")
    sp.add_argument("--prompts", default="prompts.json")
    sp.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL)
    sp.add_argument("--max-turns", type=int, default=10,
                    help="Turn cap for the judge (it may use MCP tools to spot-check claims)")
    sp.add_argument("--limit", type=int)
    sp.add_argument("--seed", type=int, default=DEFAULT_JUDGE_SEED, help="Seed for blind presentation order")
    sp.add_argument("--force", action="store_true", help="Re-judge even if a verdict already exists")
    sp.set_defaults(func=cmd_judge)

    sp = sub.add_parser("report", help="Emit reports/summary.md and reports/summary.csv")
    sp.add_argument(
        "--prompts",
        help="Restrict the report to the prompt ids in this file; output goes to "
             "reports/summary-<stem>.md/.csv instead of summary.md/.csv",
    )
    sp.set_defaults(func=cmd_report)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
