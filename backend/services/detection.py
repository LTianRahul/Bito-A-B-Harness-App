"""Environment detection + setup actions.

Wraps the harness's config readers and live-probe so the Setup page can:
  - list every known tool with install + Bito-MCP status,
  - build the per-arm MCP configs (mirrors ``cmd_setup`` without stdin/exit),
  - run a structured doctor (real Bito probe) without the CLI's prints.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Optional

from .. import engine
from ..adapters.base import HOME, looks_like_bito, pick_bito_key, read_json
from ..adapters.registry import all_adapters

harness = engine.harness


# ---------------------------------------------------------------------------
# Tool scan
# ---------------------------------------------------------------------------
def scan_tools() -> list[dict[str, Any]]:
    return [a.info().to_dict() for a in all_adapters()]


# ---------------------------------------------------------------------------
# Build per-arm MCP configs (mirror of cmd_setup, no stdin / no sys.exit)
# ---------------------------------------------------------------------------
def build_arm_configs(
    bito_server: Optional[str] = None, static_token: Optional[str] = None
) -> dict[str, Any]:
    """Read ~/.claude.json (+ plugins), choose the Bito server, and write
    configs/mcp-arm-a.json and configs/mcp-arm-bito.json. Returns a summary or
    raises ValueError with a user-friendly message.

    If ``static_token`` is given it is stamped onto the arm-bito config as-is
    (static-token mode); otherwise the app's STORED OAuth token is used (without
    refreshing — refresh happens in ``bito_oauth.ensure_bito_ready``, outside this
    write path). When neither exists, any bearer already on the source server entry
    (e.g. copied from ~/.claude.json) is preserved."""
    # Try Claude Code config first; fall back to Copilot CLI config for users who
    # only have Copilot installed (no Claude Code on this machine).
    claude_json   = HOME / ".claude.json"
    copilot_json  = HOME / ".copilot" / "mcp-config.json"

    if claude_json.exists():
        data = read_json(claude_json)
        if data is None:
            raise ValueError("Could not parse ~/.claude.json.")
        plugins_dir = HOME / ".claude" / "plugins"
        servers, sources = harness.collect_mcp_servers(data, plugins_dir)
        if not servers:
            raise ValueError("No MCP servers found in ~/.claude.json or installed plugins.")
    elif copilot_json.exists():
        data = read_json(copilot_json)
        if data is None:
            raise ValueError("Could not parse ~/.copilot/mcp-config.json.")
        # Copilot stores servers directly under mcpServers — no plugins dir.
        servers = data.get("mcpServers", {})
        sources = {k: "copilot-mcp-config" for k in servers}
        if not servers:
            raise ValueError(
                "No MCP servers found in ~/.copilot/mcp-config.json. "
                "Connect Bito MCP via the Setup step first."
            )
    else:
        raise ValueError(
            "Neither ~/.claude.json nor ~/.copilot/mcp-config.json found. "
            "Install Claude Code or GitHub Copilot CLI and connect Bito MCP first."
        )

    # The benchmark uses exactly ONE Bito server — the canonical "BitoAIArchitect".
    # Any other bito-ish servers (e.g. BitoAIArchitectSelf, "Bito AIInternal") are
    # IGNORED: dropped from BOTH arms so they can't quietly give Arm A Bito powers.
    bito_key = bito_server or pick_bito_key(servers)
    if bito_server and bito_server not in servers:
        raise ValueError(f"'{bito_server}' is not one of the detected MCP servers.")
    if not bito_key:
        raise ValueError(
            "No Bito AI Architect MCP found in ~/.claude.json. "
            "Connect Bito on the Setup step first."
        )

    # Every other bito-ish server (not the chosen one) is excluded from the experiment.
    ignored_bito = [k for k in servers if k != bito_key and looks_like_bito(k)]
    non_bito = {k: v for k, v in servers.items() if not looks_like_bito(k)}

    # Arm A = customer's normal Claude Code with all non-Bito MCPs (GitLab, GitHub,
    # Atlassian, Jira, Confluence, Slack, Linear, etc.) but WITHOUT Bito.
    # Arm B/C = same non-Bito MCPs + the canonical Bito server.
    # The only controlled variable is the Bito MCP (and skills in the prompt suffix).
    arm_a_servers = dict(non_bito)
    arm_bito_servers = {**non_bito, bito_key: servers[bito_key]}  # + exactly the canonical Bito

    # Make the Bito server's auth self-sufficient: stamp the app-controlled OAuth
    # bearer directly onto the arm-bito config, so it never depends on ~/.claude.json
    # keeping the header (the claude CLI rewrites that file and drops it). Uses the
    # STORED token without refreshing, to avoid recursing through the token-write path.
    try:
        from . import bito_oauth
        # Explicit static token wins; else fall back to the stored OAuth token.
        tok = (static_token or "").strip() or bito_oauth.current_token()
        if tok:
            entry = dict(arm_bito_servers[bito_key])
            entry.setdefault("type", "http")
            entry["headers"] = {"Authorization": f"Bearer {tok}"}
            arm_bito_servers[bito_key] = entry
    except Exception:
        pass

    engine.CONFIGS.mkdir(parents=True, exist_ok=True)
    (engine.CONFIGS / "mcp-arm-a.json").write_text(
        json.dumps({"mcpServers": arm_a_servers}, indent=2), encoding="utf-8"
    )
    (engine.CONFIGS / "mcp-arm-bito.json").write_text(
        json.dumps({"mcpServers": arm_bito_servers}, indent=2), encoding="utf-8"
    )

    by_src: dict[str, list[str]] = {}
    for name, src in sources.items():
        by_src.setdefault(src, []).append(name)

    return {
        "bito_server": bito_key,
        "arm_a_servers": sorted(arm_a_servers),
        "arm_bito_servers": sorted(arm_bito_servers),
        "ignored_bito_servers": sorted(ignored_bito),
        "sources": {k: sorted(v) for k, v in by_src.items()},
        "configs_dir": str(engine.CONFIGS),
    }


def configs_exist() -> bool:
    return (engine.CONFIGS / "mcp-arm-a.json").exists() and (
        engine.CONFIGS / "mcp-arm-bito.json"
    ).exists()


# ---------------------------------------------------------------------------
# Structured doctor (real Bito probe, no prints)
# ---------------------------------------------------------------------------
def run_doctor(
    model: Optional[str] = None,
    max_turns: Optional[int] = None,
    tool_id: str = "claude",
) -> dict[str, Any]:
    """Structured live preflight for ONE tool. Each CLI's Bito connection is
    independent (Claude Code reads mcp-arm-bito.json; GitHub Copilot CLI reads its
    own ~/.copilot/mcp-config.json), so we probe through that tool's own CLI."""
    import shutil

    model = model or harness.DEFAULT_MODEL
    max_turns = max_turns or harness.DEFAULT_DOCTOR_MAX_TURNS

    # Non-Claude tools (Copilot) run a generic per-tool doctor that probes through
    # their own CLI + config. Claude keeps the original, battle-tested path below.
    tool_id = tool_id if tool_id in ("claude", "copilot") else "claude"
    if tool_id != "claude":
        return _run_doctor_tool(tool_id, model, max_turns)

    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str, status: Optional[str] = None) -> bool:
        checks.append({
            "name": name,
            "ok": ok,
            "detail": detail,
            "status": status or ("ok" if ok else "fail"),
        })
        return ok

    # Check all headless-capable adapters — works whether user has Claude Code,
    # Copilot CLI, or both.  Show each found tool with its actual path.
    # For consistency with the Setup page, delegate detection to each adapter's
    # own detect() method (which already includes Windows direct-path probing).
    _found_clis: list[tuple[str, str]] = []   # (display_name, path)
    _missing_clis: list[str] = []
    for _adapter in all_adapters():
        if not _adapter.supports_headless:
            continue
        _installed, _version, _detail = _adapter.detect()
        if _installed:
            # Extract the path from the detail string ("Found … at <path>")
            _path = _detail.split(" at ", 1)[-1] if " at " in _detail else (_version or _detail)
            _found_clis.append((_adapter.name, _path))
        else:
            _missing_clis.append(_adapter.name)

    if _found_clis:
        _cli_detail = "  |  ".join(f"{n}: {p}" for n, p in _found_clis)
        add("Code-gen CLI on PATH", True, _cli_detail)
    else:
        _names = " or ".join(n for n in _missing_clis) or "a supported code-gen CLI"
        add("Code-gen CLI on PATH", False,
            f"No supported CLI found on PATH. Install {_names}.")
        return _doctor_result(checks, ready=False, repos=[])

    arm_bito = engine.CONFIGS / "mcp-arm-bito.json"
    if not add(
        "Benchmark MCP configs built",
        configs_exist(),
        "Found arm configs" if configs_exist() else "Click 'Build configs' first.",
    ):
        return _doctor_result(checks, ready=False, repos=[])

    # Self-heal auth before probing: refresh the OAuth token (or keep the static one)
    # and re-stamp it onto the arm-bito config, so the probe tests exactly what a real
    # B/C run will use — not a stale snapshot.
    try:
        from . import bito_oauth
        bito_oauth.ensure_bito_ready()
    except Exception:
        pass

    bito_servers = [
        n for n in harness.read_mcp_servers(arm_bito)
        if looks_like_bito(n)
    ]
    if not add(
        "Bito server present in config",
        bool(bito_servers),
        ", ".join(bito_servers) if bito_servers else "No Bito server — connect Bito first.",
    ):
        return _doctor_result(checks, ready=False, repos=[])

    # Check our app's own auth token for Bito.
    # Claude Code CLI manages a separate MCP OAuth session of its own; the probe
    # below may pass via that session even when our app's token is absent.  This
    # check surfaces the distinction so the user understands the auth picture.
    from . import bito_oauth as _oauth_svc
    import time as _t
    _oauth_rec = _oauth_svc._load()
    _arm_cfg = read_json(arm_bito) or {}
    _arm_bito_entry = next(
        (v for k, v in (_arm_cfg.get("mcpServers") or {}).items() if looks_like_bito(k)),
        None,
    )
    _has_static = bool(
        _arm_bito_entry
        and isinstance(_arm_bito_entry.get("headers"), dict)
        and _arm_bito_entry["headers"].get("Authorization")
    )
    if _oauth_rec and not (_t.time() >= _oauth_rec.get("expires_at", 0)):
        add("Bito app token", True, "OAuth token valid and current.")
    elif _oauth_rec:
        add(
            "Bito app token", True,
            "OAuth token expired — reconnect above so this app can auto-refresh. "
            "Claude Code's own session may still pass the probe below.",
            status="warn",
        )
    elif _has_static:
        add("Bito app token", True, "Static bearer token configured.")
    else:
        add(
            "Bito app token", True,
            "No app token — Claude Code's own MCP session will be used for the probe. "
            "Connect via OAuth above so this app can auto-refresh your credentials.",
            status="warn",
        )

    # Check Bito skills — Claude Code path first, Copilot CLI fallback.
    # skill_status() auto-resolves the right directory for this machine.
    from . import skills as _skills_svc
    sk = _skills_svc.skill_status()
    installed_skills = sk["installed"]
    skills_source = sk.get("skills_source", "none")
    skills_dir_label = (
        "~/.claude/skills" if skills_source == "claude"
        else "~/.copilot/skills" if skills_source == "copilot"
        else "skills directory"
    )
    _INSTALL_HINT = ("Install with Bito's one-command installer, then restart: "
                     "curl -fsSL https://mcp-setup.bito.ai/install.sh | bash  "
                     "(Windows: irm https://mcp-setup.bito.ai/install.ps1 | iex)")

    if not sk["arm_b_ok"]:
        add(
            "Bito skills installed (Arms B & C)",
            False,
            f"'bito-codebase-explorer' not found in {skills_dir_label} — Arm B requires "
            f"it and Arms B/C would fall back to MCP-only behavior without it. {_INSTALL_HINT}",
        )
        return _doctor_result(checks, ready=False, repos=[])

    source_note = f" (from {skills_dir_label})" if skills_source != "claude" else ""
    if not sk["arm_c_ok"]:
        add(
            "Bito skills installed (Arms B & C)",
            True,
            f"Only {len(installed_skills)} bito-* skill(s) found{source_note} "
            f"({', '.join(installed_skills)}). Arm B is OK; Arm C works best with the "
            f"full suite. {_INSTALL_HINT}",
            status="warn",
        )
    else:
        preview = ", ".join(installed_skills[:4]) + ("…" if len(installed_skills) > 4 else "")
        add(
            "Bito skills installed (Arms B & C)",
            True,
            f"{len(installed_skills)} bito-* skills ready{source_note}: {preview}",
        )

    # The real test: a live headless call that must actually reach Bito's tools.
    probe = harness._probe_bito(model=model, max_turns=max_turns)
    if not probe["mcp_called"]:
        # Separate "the probe run itself failed" (usage limit, CLI error — not a
        # Bito problem) from "the run completed but Bito stayed silent" (a real
        # URL/token/auth issue). Blaming the Bito config in the former case sends
        # the user chasing the wrong fix.
        if not probe.get("run_ok", True):
            add(
                "Bito MCP answered in a live run",
                False,
                "Couldn't verify Bito — the probe run itself didn't complete. "
                + (probe.get("failure_reason") or "")
                + " Bito may be fine; re-run once the run can complete.",
                status="warn",
            )
        else:
            add(
                "Bito MCP answered in a live run",
                False,
                "Tools did NOT respond — arms B/C would silently fall back to baseline. "
                "Check the URL/token or re-authenticate.",
            )
        return _doctor_result(checks, ready=False, repos=[], probe_text=probe.get("result_text"))
    add("Bito MCP answered in a live run", True, "AI Architect tools responded.")

    repos = probe["repositories"]
    if repos:
        harness._write_indexed_repos(repos, bito_servers)
    add(
        "Workspace has indexed repositories",
        bool(repos),
        f"{len(repos)} repositories indexed." if repos else "Live but ZERO indexed repos — index some first.",
    )
    return _doctor_result(checks, ready=bool(repos), repos=repos, probe_text=probe.get("result_text"))


def _is_bito_tool_call(name: str) -> bool:
    """Detect a Bito AI Architect MCP tool call across CLIs. Claude names them
    ``mcp__BitoAIArchitect__<tool>``; Copilot/others may use ``BitoAIArchitect-<tool>``
    or similar — so we match the server name loosely."""
    n = (name or "").lower()
    return name.startswith("mcp__BitoAIArchitect__") or "bito" in n


def _doctor_probe_prompt(platform: str) -> str:
    """CLI-agnostic version of harness.DOCTOR_PROBE_PROMPT (no Claude-only tool names
    or callerPlatform hardcoded)."""
    return (
        "You are a connectivity probe for the BitoAIArchitect MCP server (also called "
        "AI Architect). Do exactly this:\n"
        "1. Locate and call the BitoAIArchitect tool that lists indexed repositories "
        "(listRepositories). It may appear as mcp__BitoAIArchitect__listRepositories, "
        "BitoAIArchitect-listRepositories, or under the BitoAIArchitect server. If the "
        "server is 'still connecting', retry a few times — it needs a moment to finish "
        "its handshake.\n"
        f"2. Once available, call listRepositories with purposeType='discovery', "
        f"purpose='list indexed repos', callerPlatform='{platform}'.\n"
        "3. Then reply with ONLY a single-line JSON object — no prose, no markdown:\n"
        '   {"ok": true, "repositories": ["<repo name>", ...]}\n'
        "   listing EVERY repository name the tool returned.\n"
        "Only reply with {\"ok\": false, \"repositories\": []} if the tool genuinely never "
        "becomes available. Never invent repository names."
    )


def _probe_bito_tool(tool_id: str, model: str, max_turns: int) -> dict[str, Any]:
    """Live Bito connectivity probe through ONE tool's own CLI + MCP config. Claude
    reuses the harness probe (isolated mcp-arm-bito.json); other tools (Copilot) run
    their own headless CLI against their own config. Same shape as harness._probe_bito."""
    if tool_id == "claude":
        return harness._probe_bito(model=model, max_turns=max_turns)

    from ..adapters.registry import get_adapter
    adapter = get_adapter(tool_id)
    if not adapter:
        return {"mcp_called": False, "run_ok": False,
                "failure_reason": f"Unknown tool '{tool_id}'.",
                "repositories": [], "result_text": "", "exit_code": -1}

    platform = {"copilot": "COPILOT"}.get(tool_id, tool_id.upper())
    work = harness.RUNS / "work" / "doctor" / tool_id
    stream_path = harness.RUNS / "doctor" / f"probe-{tool_id}.stream.jsonl"
    try:
        harness.reset_workspace(work)
    except Exception:
        work.mkdir(parents=True, exist_ok=True)
    try:
        harness.ensure_glab_on_path()
    except Exception:
        pass
    try:
        res = adapter.run(
            prompt_text=_doctor_probe_prompt(platform),
            mcp_config_path=adapter.mcp_config_path(),
            model=model,
            max_turns=max_turns,
            work_dir=work,
            stream_path=stream_path,
        )
    except Exception as e:
        return {"mcp_called": False, "run_ok": False,
                "failure_reason": f"{type(e).__name__}: {e}",
                "repositories": [], "result_text": "", "exit_code": -1}

    tool_calls = res.tool_calls or []
    mcp_called = any(_is_bito_tool_call(str(tc.get("name", ""))) for tc in tool_calls)
    result_text = res.response or ""
    repos: list[str] = []
    parsed = harness.extract_judge_json(result_text)
    if isinstance(parsed, dict) and isinstance(parsed.get("repositories"), list):
        repos = [str(x).strip() for x in parsed["repositories"] if str(x).strip()]
    run_ok = (res.exit_code == 0) and bool(result_text)
    failure_reason = "" if run_ok else (
        res.error or f"The probe run did not complete cleanly (exit code {res.exit_code})."
    )
    return {
        "mcp_called": mcp_called, "run_ok": run_ok, "failure_reason": failure_reason,
        "repositories": repos, "result_text": result_text, "exit_code": res.exit_code,
    }


def _run_doctor_tool(tool_id: str, model: str, max_turns: int) -> dict[str, Any]:
    """Per-tool structured doctor for non-Claude CLIs (Copilot). Mirrors run_doctor's
    checks but scoped to one tool: its CLI, its own MCP config, its skills dir, and a
    live probe through its own CLI."""
    from ..adapters.registry import get_adapter
    from . import bito_oauth, skills as _skills_svc

    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str, status: Optional[str] = None) -> bool:
        checks.append({"name": name, "ok": ok, "detail": detail,
                       "status": status or ("ok" if ok else "fail")})
        return ok

    adapter = get_adapter(tool_id)
    label = adapter.name if adapter else tool_id

    # 1. This specific CLI is installed.
    installed, version, detail = adapter.detect() if adapter else (False, None, "Unknown tool.")
    if not installed:
        add(f"{label} on PATH", False, detail or f"{label} not found on PATH.")
        return _doctor_result(checks, ready=False, repos=[])
    _path = detail.split(" at ", 1)[-1] if " at " in (detail or "") else (version or "")
    add(f"{label} on PATH", True, f"{label}: {_path}" if _path else label)

    # 2. Bito is connected in THIS tool's config.
    pts = bito_oauth.per_tool_status()
    entry = pts.get("tools", {}).get(tool_id, {})
    if not entry.get("configured"):
        add(f"Bito connected for {label}", False,
            f"No Bito server in {label}'s MCP config — click Connect for {label} above.")
        return _doctor_result(checks, ready=False, repos=[])
    _url = entry.get("url")
    add(f"Bito connected for {label}", True,
        f"Bito server present in {label}'s config" + (f" ({_url})" if _url else "") + ".")

    # 3. Make the bearer fresh in the file this CLI reads, then report auth.
    try:
        bito_oauth.ensure_run_bito_authed(tool_id)
    except Exception:
        pass
    if entry.get("has_token") or pts.get("oauth_live"):
        add("Bito auth token", True,
            "OAuth session active." if pts.get("oauth_live")
            else "Static bearer token configured.")
    else:
        add("Bito auth token", True,
            f"No bearer in {label}'s config — its own MCP sign-in may still answer the "
            "probe. Connect via OAuth above so the app can auto-refresh.", status="warn")

    # 4. Bito skills for THIS tool.
    sk = _skills_svc.skill_status(tool_id)
    skills_dir_label = "~/.claude/skills" if tool_id == "claude" else "~/.copilot/skills"
    INSTALL_HINT = ("Install with Bito's one-command installer, then restart: "
                    "curl -fsSL https://mcp-setup.bito.ai/install.sh | bash  "
                    "(Windows: irm https://mcp-setup.bito.ai/install.ps1 | iex)")
    installed_skills = sk["installed"]
    if not sk["arm_b_ok"]:
        add("Bito skills installed (Arms B & C)", False,
            f"'bito-codebase-explorer' not found in {skills_dir_label} — Arm B requires it "
            f"and Arms B/C fall back to MCP-only behavior without it. {INSTALL_HINT}")
        return _doctor_result(checks, ready=False, repos=[])
    if not sk["arm_c_ok"]:
        add("Bito skills installed (Arms B & C)", True,
            f"Only {len(installed_skills)} bito-* skill(s) in {skills_dir_label} "
            f"({', '.join(installed_skills)}). Arm B OK; Arm C works best with the full "
            f"suite. {INSTALL_HINT}", status="warn")
    else:
        preview = ", ".join(installed_skills[:4]) + ("…" if len(installed_skills) > 4 else "")
        add("Bito skills installed (Arms B & C)", True,
            f"{len(installed_skills)} bito-* skills ready in {skills_dir_label}: {preview}")

    # 5. The real test: a live headless run through THIS tool that must reach Bito.
    probe = _probe_bito_tool(tool_id, model, max_turns)
    if not probe["mcp_called"]:
        if not probe.get("run_ok", True):
            add(f"Bito MCP answered in a live {label} run", False,
                "Couldn't verify Bito — the probe run itself didn't complete. "
                + (probe.get("failure_reason") or "")
                + " Bito may be fine; re-run once the run can complete.", status="warn")
        else:
            add(f"Bito MCP answered in a live {label} run", False,
                "Tools did NOT respond — arms B/C would silently fall back to baseline. "
                "Check the URL/token or re-authenticate.")
        return _doctor_result(checks, ready=False, repos=[], probe_text=probe.get("result_text"))
    add(f"Bito MCP answered in a live {label} run", True, "AI Architect tools responded.")

    repos = probe["repositories"]
    if repos:
        bito_servers = [n for n in bito_oauth._read_tool_servers(tool_id) if looks_like_bito(n)]
        try:
            harness._write_indexed_repos(repos, bito_servers or ["BitoAIArchitect"])
        except Exception:
            pass
    add("Workspace has indexed repositories", bool(repos),
        f"{len(repos)} repositories indexed." if repos
        else "Live but ZERO indexed repos — index some first.")
    return _doctor_result(checks, ready=bool(repos), repos=repos, probe_text=probe.get("result_text"))


def _doctor_result(checks, ready, repos, probe_text=None) -> dict[str, Any]:
    return {
        "ready": ready,
        "checks": checks,
        "repositories": repos,
        "probe_text": (probe_text or "")[:400],
    }
