"""Setup & environment detection endpoints (Phase 1)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..models import BuildConfigsRequest, DoctorRequest
from .. import engine
from ..services import detection
from ..services import skills as skills_svc

router = APIRouter(prefix="/api", tags=["setup"])


@router.get("/tools")
def list_tools() -> dict:
    """Every known code-gen tool with install + Bito-MCP status."""
    return {"tools": detection.scan_tools(), "configs_built": detection.configs_exist()}


@router.get("/setup/cwd")
def get_cwd() -> dict:
    """The server's current working directory — used to pre-fill the local-repo path
    in the Runner when 'local-repo' workspace mode is selected."""
    import os
    return {"cwd": os.getcwd()}


@router.post("/setup/configs")
def build_configs(req: BuildConfigsRequest) -> dict:
    """Build the per-arm MCP config files from ~/.claude.json."""
    try:
        return {"ok": True, **detection.build_arm_configs(req.bito_server)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/setup/skills")
def get_skills() -> dict:
    """Return bito-* skill status for Claude Code and Copilot CLI independently."""
    return skills_svc.skills_status_all()


@router.get("/setup/skills-debug")
def debug_skills() -> dict:
    """Show what's under ~/.claude/skills so skill detection can be diagnosed."""
    from pathlib import Path
    home = Path.home()
    claude = home / ".claude"

    def ls(p: Path) -> list:
        try:
            return sorted(x.name for x in p.iterdir()) if p.exists() else []
        except Exception as e:
            return [f"<error: {e}>"]

    return {
        "home": str(home),
        "claude_dir_exists": claude.exists(),
        "skills_dir": str(claude / "skills"),
        "skills_contents": ls(claude / "skills"),
        "skill_status": skills_svc.skills_status_all(),
    }


@router.get("/setup/git-tools")
def git_tools_status() -> dict:
    """Check which git hosting CLIs are on PATH and whether they are authenticated.
    Used by the Setup page to guide users through glab/gh auth for Arm A cloning."""
    import os
    import shutil
    import subprocess as _sp

    # Prevent any interactive popup or browser launch during the probe.
    # GIT_TERMINAL_PROMPT=0  → git never prompts for credentials on the terminal
    # GCM_INTERACTIVE=Never  → Git Credential Manager never opens a GUI (Windows)
    # GH_PROMPT_DISABLED=1   → gh CLI disables interactive prompts
    # GLAB_TOKEN / CI=true   → glab skips interactive flows when CI is set
    _env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never",
            "GH_PROMPT_DISABLED": "1", "CI": "true"}

    def _probe(cmd: str, auth_args: list) -> dict:
        # shutil.which resolves .exe on Windows via PATHEXT.
        # Fallback: on Windows run `where <cmd>` in case the installer added the
        # binary to a PATH entry that the current Python process didn't inherit
        # (common when gh/glab are installed after the server was started, or when
        # installed via winget/scoop into a user-PATH that uvicorn didn't pick up).
        path = shutil.which(cmd)
        if not path and os.name == "nt":
            try:
                r = _sp.run(["where", cmd], capture_output=True, text=True, timeout=5)
                if r.returncode == 0:
                    first = r.stdout.strip().splitlines()[0].strip()
                    if first:
                        path = first
            except Exception:
                pass
        if not path:
            return {"available": False, "authed": None, "path": None,
                    "detail": "Not found on PATH"}
        try:
            r = _sp.run([path, *auth_args], capture_output=True, text=True,
                        timeout=8, env=_env)
            authed = r.returncode == 0
            detail = (r.stdout + r.stderr).strip()[:300]
        except _sp.TimeoutExpired:
            authed = None
            detail = "Auth check timed out — CLI may be waiting for input."
        except Exception as e:
            authed = None
            detail = str(e)[:200]
        return {"available": True, "authed": authed, "path": path, "detail": detail}

    git_path = shutil.which("git")
    if not git_path and os.name == "nt":
        try:
            r = _sp.run(["where", "git"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                first = r.stdout.strip().splitlines()[0].strip()
                if first:
                    git_path = first
        except Exception:
            pass
    return {
        "glab": _probe("glab", ["auth", "status"]),
        "gh":   _probe("gh",   ["auth", "status"]),
        "git":  {
            "available": bool(git_path),
            "authed": None,
            "path": git_path,
            "detail": "git itself needs no login — auth is managed by glab or gh.",
        },
        "namespace": engine.harness.load_git_namespace(),
    }


@router.get("/setup/git-namespace")
def get_git_namespace() -> dict:
    """Return the saved git namespace prefix (e.g. 'myorg' or 'gitlab.host/group')."""
    return {"namespace": engine.harness.load_git_namespace()}


@router.post("/setup/git-namespace")
def save_git_namespace(body: dict) -> dict:
    """Save the git namespace prefix so effective_prompt can build clone commands."""
    ns = str(body.get("namespace") or "").strip()
    engine.harness.save_git_namespace(ns)
    return {"ok": True, "namespace": ns}


@router.post("/setup/doctor")
def doctor(req: DoctorRequest) -> dict:
    """Run the live preflight: a real Bito probe + indexed-repo discovery.

    This makes one headless model call, so it can take ~15-60s.
    """
    try:
        return detection.run_doctor(model=req.model, max_turns=req.max_turns)
    except Exception as e:  # surface a clean message rather than a 500 stack
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")
