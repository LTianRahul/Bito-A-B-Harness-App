import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError } from "../api/client";
import { useAsync } from "../lib/useAsync";
import { ARM_INFO, ARMS } from "../lib";
import { mcpBadge, type ToolInfo } from "../lib/status";
import { Async, Badge, Banner, Card, Spinner } from "../components/ui";

interface GitToolEntry {
  available: boolean;
  authed: boolean | null;
  path: string | null;
  detail: string;
}
interface GitToolsStatus {
  glab: GitToolEntry;
  gh: GitToolEntry;
  git: GitToolEntry;
}

interface ToolsResp {
  tools: ToolInfo[];
  configs_built: boolean;
}
interface OAuthStatus {
  connected: boolean;
  mode: string | null;
  workspace_id?: string | null;
  url?: string | null;
  expired?: boolean;
}
interface PerToolEntry {
  configured: boolean;
  has_token: boolean;
  workspace_id: string | null;
  url: string | null;
  installed: boolean;
  config_path: string;
}
interface PerToolStatus {
  tools: Record<string, PerToolEntry>;
  oauth_live: boolean;
}
interface BitoStatus {
  configured: boolean;
  state: string;
  workspace_id: string | null;
  url: string | null;
  detail: string;
  has_token?: boolean;
  auth_kind?: string | null;
  oauth: OAuthStatus;
  per_tool?: PerToolStatus;
}

// CLIs whose Bito MCP config the Setup page manages independently.
const MANAGED_TOOLS: { id: string; label: string }[] = [
  { id: "claude", label: "Claude Code" },
  { id: "copilot", label: "GitHub Copilot CLI" },
];
const TOOL_LABEL: Record<string, string> = Object.fromEntries(
  MANAGED_TOOLS.map((t) => [t.id, t.label]),
);
interface ValidateResult {
  connected?: boolean;
  checks: { name: string; ok: boolean; detail: string; status?: "ok" | "fail" | "warn" }[];
  repositories: string[];
}
interface SingleToolSkills {
  installed: string[];
  count: number;
  arm_b_ok: boolean;
  arm_c_ok: boolean;
  skills_dir?: string;
  skills_source?: string;
}
interface SkillsStatus {
  claude: SingleToolSkills;
  copilot: SingleToolSkills;
}

export default function Setup() {
  const tools = useAsync<ToolsResp>(() => api.get("/tools"));
  const status = useAsync<BitoStatus>(() => api.get("/bito/status"));
  // Lightweight availability probe of the configured Bito MCP (no model tokens).
  // Drives reuse-vs-reconnect: a configured-but-unreachable/unauthorized Bito must
  // not show as "connected".
  const health = useAsync<{ ok: boolean; reason: string; detail: string }>(() => api.get("/bito/health"));

  // Bito's one-command installer for MCP + skills, per OS (Windows uses PowerShell).
  const isWindows = typeof navigator !== "undefined" && /Win/i.test(navigator.userAgent);
  const claudeSkillsDir  = isWindows ? "%USERPROFILE%\\.claude\\skills\\" : "~/.claude/skills/";
  const copilotSkillsDir = isWindows ? "%USERPROFILE%\\.copilot\\skills\\" : "~/.copilot/skills/";
  const bitoInstallCmd = isWindows
    ? "irm https://mcp-setup.bito.ai/install.ps1 | iex"
    : "curl -fsSL https://mcp-setup.bito.ai/install.sh | bash";
  const prompts = useAsync<{ prompts: { id: string; prompt: string }[] }>(() => api.get("/prompts"));
  const skillsStatus = useAsync<SkillsStatus>(() => api.get("/setup/skills"));

  const [workspaceId, setWorkspaceId] = useState("");
  const [token, setToken] = useState("");
  const [showSelfHosted, setShowSelfHosted] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  // Health check is per-tool: each CLI is probed through its own MCP connection.
  const [checking, setChecking] = useState<string | null>(null); // tool id in flight
  const [check, setCheck] = useState<ValidateResult | null>(null);
  const [checkErr, setCheckErr] = useState<string | null>(null);
  const [checkedTool, setCheckedTool] = useState<string | null>(null);
  // Which tool the OAuth popup is signing in for, so the success message names it.
  const oauthToolRef = React.useRef<string | null>(null);


  const gitTools = useAsync<GitToolsStatus & { namespace?: string }>(() => api.get("/setup/git-tools"));
  const [gitNamespace, setGitNamespace] = useState("");
  const [namespaceSaved, setNamespaceSaved] = useState<boolean | null>(null);

  // Prefill namespace from the fetched status
  useEffect(() => {
    if (gitTools.data?.namespace !== undefined && !gitNamespace) {
      setGitNamespace(gitTools.data.namespace);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [gitTools.data]);

  async function saveNamespace() {
    try {
      await api.post("/setup/git-namespace", { namespace: gitNamespace });
      setNamespaceSaved(true);
      setTimeout(() => setNamespaceSaved(null), 2500);
    } catch {
      setNamespaceSaved(false);
    }
  }

  // Show the intro automatically ONLY on the very first open. We mark it "seen"
  // immediately on mount, so a refresh (or returning later) won't auto-show it —
  // the user can always reopen it with the "What is this?" link.
  const [showIntro, setShowIntro] = useState(() => localStorage.getItem("ab_intro_seen") !== "1");
  useEffect(() => {
    localStorage.setItem("ab_intro_seen", "1");
  }, []);
  // Keep the connection status live: re-fetch periodically and whenever the
  // window/tab regains focus, so e.g. an expired OAuth token shows up here
  // without a manual reload.
  useEffect(() => {
    const refresh = () => {
      status.refresh();
      tools.refresh();
      health.refresh();
    };
    const t = setInterval(refresh, 12000);
    window.addEventListener("focus", refresh);
    return () => {
      clearInterval(t);
      window.removeEventListener("focus", refresh);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function dismissIntro() {
    setShowIntro(false);
  }

  // Prefill the workspace id from whatever Bito is already configured (active OAuth, or
  // the workspace detected in the existing MCP config). A truly fresh machine has none,
  // so the field stays empty there; a configured machine shows its real workspace.
  useEffect(() => {
    const ws = status.data?.oauth?.workspace_id || status.data?.workspace_id;
    // Don't pre-fill with unexpanded env-var placeholders like ${BITO_WORKSPACE_ID}
    if (ws && !workspaceId && !ws.startsWith("${") && !ws.startsWith("%")) setWorkspaceId(ws);
  }, [status.data]);

  // Listen for the OAuth popup telling us it finished.
  useEffect(() => {
    function onMsg(e: MessageEvent) {
      if (e.data && typeof e.data === "object" && "bitoOAuth" in e.data) {
        setBusy(null);
        if (e.data.bitoOAuth) {
          // Name the CLI we just connected (the one whose Connect started this sign-in).
          const t = oauthToolRef.current;
          setMsg({ ok: true, text: t ? `Connected ${TOOL_LABEL[t] ?? t}.` : "Connected." });
        }
        oauthToolRef.current = null;
        status.reload();
        tools.reload();
        health.reload();
      }
    }
    window.addEventListener("message", onMsg);
    return () => window.removeEventListener("message", onMsg);
  }, []);

  // Accept EITHER a hosted workspace ID or a full custom URL — mirror the backend's
  // resolve_mcp_url so the preview matches what gets written to config. A full URL is
  // used exactly as given (any host/path); only a bare ID expands to the hosted form.
  function resolveMcpUrl(v: string): string {
    const s = v.trim();
    if (!s) return "";
    if (/^https?:\/\//i.test(s)) return s.replace(/\/+$/, "");
    return `https://mcp.bito.ai/${s}/mcp`;
  }
  const mcpUrl = workspaceId.trim() ? resolveMcpUrl(workspaceId) : "—";

  async function connectOAuth() {
    if (!workspaceId.trim()) return setMsg({ ok: false, text: "Enter your Workspace ID or MCP URL first." });
    setBusy("oauth");
    setMsg(null);
    try {
      const r = await api.post<{ authorize_url: string }>("/bito/oauth/start", {
        workspace_id: workspaceId.trim(),
      });
      window.open(r.authorize_url, "bito-oauth", "width=520,height=720");
      // The popup posts back on completion; also stop the spinner after a while.
      setTimeout(() => setBusy((b) => (b === "oauth" ? null : b)), 90000);
    } catch (e) {
      setBusy(null);
      setMsg({ ok: false, text: e instanceof ApiError ? String(e.detail) : String(e) });
    }
  }

  async function disconnect() {
    setBusy("disconnect");
    try {
      await api.post("/bito/oauth/disconnect");
      setMsg({ ok: true, text: "Disconnected." });
      setCheck(null);
      status.reload();
      tools.reload();
      health.reload();
    } finally {
      setBusy(null);
    }
  }

  // Connect Bito MCP for ONE CLI. Reuses the app's existing OAuth token when we
  // already have one (instant, no browser); otherwise kicks off the browser sign-in
  // targeting just this tool. The OAuth popup posts back and reloads status.
  async function connectTool(toolId: string) {
    if (!workspaceId.trim()) return setMsg({ ok: false, text: "Enter your Workspace ID or MCP URL first." });
    setBusy("connect:" + toolId);
    setMsg(null);
    try {
      const r = await api.post<{ ok: boolean; needs_oauth: boolean }>("/bito/tool/connect", {
        tool: toolId,
        workspace_id: workspaceId.trim(),
        // Optional static bearer — when present, connects without OAuth (self-hosted /
        // OAuth-disabled). When blank, the backend falls back to the OAuth flow.
        token: token.trim() || undefined,
      });
      if (r.needs_oauth) {
        oauthToolRef.current = toolId; // so the completion message names this CLI
        const o = await api.post<{ authorize_url: string }>("/bito/oauth/start", {
          workspace_id: workspaceId.trim(),
          tools: [toolId],
        });
        window.open(o.authorize_url, "bito-oauth", "width=520,height=720");
        // The popup posts back on completion; also stop the spinner after a while.
        setTimeout(() => setBusy((b) => (b === "connect:" + toolId ? null : b)), 90000);
        return;
      }
      setMsg({ ok: true, text: `Connected ${TOOL_LABEL[toolId] ?? toolId}.` });
      status.reload();
      tools.reload();
      health.reload();
      setBusy(null);
    } catch (e) {
      setBusy(null);
      setMsg({ ok: false, text: e instanceof ApiError ? String(e.detail) : String(e) });
    }
  }

  // Remove Bito MCP from ONE CLI's config (leaves the other CLI untouched).
  async function disconnectTool(toolId: string) {
    setBusy("disc:" + toolId);
    try {
      await api.post("/bito/tool/disconnect", { tool: toolId });
      setMsg({ ok: true, text: `Disconnected ${TOOL_LABEL[toolId] ?? toolId}.` });
      setCheck(null);
      status.reload();
      tools.reload();
      health.reload();
    } catch (e) {
      setMsg({ ok: false, text: e instanceof ApiError ? String(e.detail) : String(e) });
    } finally {
      setBusy(null);
    }
  }

  async function saveToken() {
    if (!workspaceId.trim()) return setMsg({ ok: false, text: "Enter your Workspace ID or MCP URL first." });
    if (!token.trim()) return setMsg({ ok: false, text: "Enter a bearer token." });
    setBusy("token");
    setMsg(null);
    try {
      await api.post("/bito/configure", { workspace_id: workspaceId.trim(), token: token.trim(), tools: null });
      setMsg({ ok: true, text: "Saved token. Run a health check to confirm." });
      setToken("");
      status.reload();
      tools.reload();
    } catch (e) {
      setMsg({ ok: false, text: e instanceof ApiError ? String(e.detail) : String(e) });
    } finally {
      setBusy(null);
    }
  }


  // Live health check for ONE CLI — probes Bito through that tool's own MCP
  // connection (Claude via claude, Copilot via copilot), since they're independent.
  async function healthCheck(toolId: string) {
    setChecking(toolId);
    setCheck(null);
    setCheckErr(null);
    setCheckedTool(toolId);
    try {
      setCheck(await api.post<ValidateResult>("/bito/validate", { tool: toolId }));
      // Use refresh() (silent, no loading spinner) so the Async block doesn't
      // temporarily unmount the doctor results while status refetches.
      status.refresh();
    } catch (e) {
      setCheckErr(e instanceof ApiError ? String(e.detail) : String(e));
    } finally {
      setChecking(null);
    }
  }

  return (
    <div className="stack">
      {showIntro ? (
        <div className="card intro-hero">
          <div className="row" style={{ alignItems: "flex-start" }}>
            <div style={{ flex: 1 }}>
              <h2 style={{ fontSize: 20 }}>Does Bito AI Architect actually make your coding assistant better?</h2>
              <p style={{ marginTop: 8, maxWidth: 760 }}>
                This tool runs the <strong>same tasks three ways</strong> — your code-gen assistant on
                its own, then with <strong>Bito AI Architect</strong>, then with Bito + its Skills — and
                you get a clear, side-by-side picture of what Bito adds: cost, speed, and Bito usage.
              </p>
            </div>
            <button className="btn ghost sm" onClick={dismissIntro}>✕ Dismiss</button>
          </div>

          <div className="grid grid-3" style={{ marginTop: 16 }}>
            {ARMS.map((a) => (
              <div key={a} className="card card-pad" style={{ background: "var(--surface)" }}>
                <span className={`pill ${a.toLowerCase()}`}>Arm {a}</span>
                <div style={{ fontWeight: 600, marginTop: 6 }}>{ARM_INFO[a].name}</div>
                <div className="muted" style={{ fontSize: 12 }}>{ARM_INFO[a].blurb}</div>
              </div>
            ))}
          </div>

          <div className="divider" />
          <div className="row wrap" style={{ gap: 18, fontSize: 13 }}>
            <span className="faint" style={{ fontWeight: 700 }}>HOW IT WORKS</span>
            <span><strong>1.</strong> Connect Bito (here on Setup)</span>
            <span className="faint">→</span>
            <span><strong>2.</strong> <Link to="/prompts">Choose prompts</Link></span>
            <span className="faint">→</span>
            <span><strong>3.</strong> <Link to="/run">Run the A/B test</Link></span>
            <span className="faint">→</span>
            <span><strong>4.</strong> <Link to="/results">See results &amp; report</Link></span>
          </div>
        </div>
      ) : null}

      <div className="page-head">
        <h2>Setup</h2>
        <p>
          Connect your tools and Bito AI Architect to get started.{" "}
          {!showIntro && (
            <a style={{ cursor: "pointer" }} onClick={() => setShowIntro(true)}>What is this?</a>
          )}
        </p>
      </div>

      {/* Guided getting-started checklist — auto-reflects what's already done. */}
      <Async state={tools}>
        {(t) => {
          const claude = t.tools.find((x) => x.id === "claude");
          const claudeOk = !!claude?.installed;
          // Any installed tool that supports headless running counts for step 1.
          const anyHeadlessTool = t.tools.find((x) => x.installed && x.supports_headless);
          const anyToolOk = !!anyHeadlessTool;
          const oauth = status.data?.oauth;
          // Bito counts as configured if EITHER Claude Code or Copilot CLI has it.
          const configured = t.tools.some(
            (x) => (x.id === "claude" || x.id === "copilot") && x.mcp.state === "configured",
          );
          const bitoExpired = !!oauth?.connected && !!oauth?.expired;
          // "Connected" requires a LIVE-verified auth, not just a bearer string in the
          // config (has_token alone can be a stale/invalid token that 401s — it must not
          // claim connected). A current OAuth session counts; a static token is proven by
          // the /health auth probe (reachable) or the live probe (probeOk) below.
          const bitoAuthed = !!(oauth?.connected && !oauth?.expired);
          // The live probe is the ground truth: if it passed recently, treat Bito as
          // connected even if our app's own token is absent (Claude Code may have its
          // own MCP session).
          const probeOk = !!check?.checks?.find(
            (c) => c.name.startsWith("Bito MCP answered") && c.ok
          );
          // The lightweight /health probe: ok === true means the configured MCP is
          // reachable (reusing the user's existing Bito counts as connected). An
          // explicit failure (token rejected / unreachable) blocks; "no_token" or a
          // not-yet-loaded result is fail-safe and does not block.
          const reachable = health.data?.ok === true;
          const healthBad = !!health.data && health.data.ok === false &&
            health.data.reason !== "no_token";
          const bitoOk = configured && (bitoAuthed || probeOk || reachable) && !bitoExpired && !healthBad;
          const promptList = prompts.data?.prompts ?? [];
          const usingExamples =
            promptList.length === 0 || promptList.some((p) => /replace me/i.test(p.prompt));
          const promptsOk = promptList.length > 0 && !usingExamples;

          const gt = gitTools.data;
          const glabOk = !!gt?.glab?.available && gt.glab.authed !== false;
          const ghOk   = !!gt?.gh?.available   && gt.gh.authed   !== false;
          const gitOk  = glabOk || ghOk;
          const sk = skillsStatus.data;
          // Either Claude or Copilot having skills counts as OK for the checklist.
          const claudeSk = sk?.claude;
          const copilotSk = sk?.copilot;
          const skillsOk = !!(claudeSk?.arm_b_ok || copilotSk?.arm_b_ok);
          const skillsWarn = skillsOk && !(claudeSk?.arm_c_ok || copilotSk?.arm_c_ok);
          const totalSkillCount = Math.max(claudeSk?.count ?? 0, copilotSk?.count ?? 0);

          // The single next action to highlight.
          let next: { text: string; to?: string; scroll?: string } | null = null;
          if (!anyToolOk) next = { text: "Install a code-gen tool CLI (Claude Code or GitHub Copilot CLI) to run benchmarks" };
          else if (bitoExpired) next = { text: "Reconnect Bito — your sign-in expired", scroll: "bito-connect" };
          else if (!bitoOk) next = { text: "Connect Bito just below", scroll: "bito-connect" };
          else if (!skillsOk) next = { text: "Install Bito Skills so Arms B & C work correctly", scroll: "bito-skills" };
          else if (!promptsOk) next = { text: "Edit your prompts to match your repos", to: "/prompts" };
          else next = { text: "You're all set — start your first A/B test", to: "/run" };

          // Collect all installed headless tools for the checklist description.
          const installedHeadless = t.tools.filter((x) => x.installed && x.supports_headless);

          const steps = [
            {
              done: anyToolOk,
              t: "Your coding tool is installed",
              d: anyToolOk
                ? installedHeadless.map((x) => (
                    <span key={x.id} style={{ display: "block" }}>
                      <strong>{x.name}</strong>
                      {x.detect_detail ? (
                        <span className="faint mono" style={{ fontSize: 11.5, marginLeft: 6 }}>
                          {x.detect_detail.replace(/^Found \S+ (CLI )?at /, "").replace(/^Found /, "")}
                        </span>
                      ) : null}
                    </span>
                  ))
                : "Install Claude Code or GitHub Copilot CLI (standalone binary), then reopen this app.",
              warn: !anyToolOk,
            },
            {
              done: bitoOk,
              warn: bitoExpired,
              t: bitoOk ? "Bito AI Architect is connected" : "Connect Bito AI Architect",
              d: bitoExpired
                ? "Your OAuth sign-in expired — reconnect in the card below."
                : bitoOk
                ? `Connected${claude?.mcp.workspace_id ? ` (workspace ${claude.mcp.workspace_id})` : ""} — handled for you.`
                : "Connect it in the card below (one click + browser sign-in).",
              action: !bitoOk ? { label: "Connect below", scroll: "bito-connect" } : undefined,
            },
            {
              done: skillsOk && !skillsWarn,
              warn: skillsWarn,
              t: "Bito Skills installed (Arms B & C)",
              d: !sk
                ? "Checking…"
                : !skillsOk
                ? "bito-codebase-explorer not found — Arm B needs it. See the install command below."
                : skillsWarn
                ? `${totalSkillCount} skill(s) installed — Arm B OK, but Arm C works best with the full suite.`
                : `${totalSkillCount} skills ready — both Arms B & C are set.`,
              action: !skillsOk ? { label: "Install below", scroll: "bito-skills" } : undefined,
            },
            {
              done: gitOk,
              warn: !gt,
              t: "Git hosting CLI authenticated (Arm A)",
              d: !gt
                ? "Checking…"
                : gitOk
                ? `${glabOk ? "glab" : ""}${glabOk && ghOk ? " + " : ""}${ghOk ? "gh" : ""} authenticated — Arm A can clone repos.`
                : "glab or gh not authenticated — Arm A can't clone private repos. See the card below.",
              action: !gitOk && gt ? { label: "Configure below", scroll: "git-credentials" } : undefined,
            },
            {
              done: promptsOk,
              warn: usingExamples && promptList.length > 0,
              t: "Your test prompts are ready",
              d: promptsOk
                ? `${promptList.length} prompt(s) ready.`
                : promptList.length === 0
                ? "Add the tasks you want to test."
                : "You're still using the example \"Replace me…\" prompts — edit them for your repositories.",
              action: { label: "Edit prompts", to: "/prompts" },
            },
            {
              done: false,
              t: "Run your first A/B test",
              d: "We compare your tool with & without Bito and score the answers.",
              action: { label: "Go to Run", to: "/run", disabled: !(anyToolOk && bitoOk && skillsOk) },
            },
          ];

          return (
            <Card
              title="Getting started"
              sub="Most of this is automatic — the main thing you do is edit your prompts."
            >
              {next && (
                <div className="banner info" style={{ marginBottom: 14 }}>
                  <span aria-hidden>👉</span>
                  <div>
                    <span className="b-title">Next: </span>
                    {next.to ? <Link to={next.to}>{next.text}</Link> : next.text}
                  </div>
                </div>
              )}
              <div className="checklist">
                {steps.map((s, i) => (
                  <div className="check-row" key={i}>
                    <div className={`check-icon ${s.done ? "done" : (s as any).warn ? "fail" : "todo"}`}>
                      {s.done ? "✓" : i + 1}
                    </div>
                    <div className="check-body">
                      <div className="t">{s.t}</div>
                      <div className="d">{s.d as React.ReactNode}</div>
                    </div>
                    {(s as any).action &&
                      ((s as any).action.to ? (
                        <Link
                          className={`btn sm${(s as any).action.disabled ? " " : ""}`}
                          to={(s as any).action.disabled ? "#" : (s as any).action.to}
                          style={(s as any).action.disabled ? { opacity: 0.5, pointerEvents: "none" } : undefined}
                        >
                          {(s as any).action.label}
                        </Link>
                      ) : (
                        <button
                          className="btn sm"
                          onClick={() => {
                            const el = document.getElementById((s as any).action.scroll ?? "bito-connect");
                            el?.scrollIntoView({ behavior: "smooth", block: "center" });
                          }}
                        >
                          {(s as any).action.label}
                        </button>
                      ))}
                  </div>
                ))}
              </div>
            </Card>
          );
        }}
      </Async>

      {/* Bito MCP connection */}
      <div id="bito-connect" />
      <Card
        title="Bito AI Architect MCP"
        sub="Arms B & C inject this MCP server."
        right={
          <a
            href="https://docs.bito.ai/ai-architect/quick-mcp-integration-with-ai-coding-agents"
            target="_blank"
            rel="noreferrer"
            style={{ fontSize: 13 }}
          >
            Integration guide ↗
          </a>
        }
      >
        <Async state={status}>
          {(s) => {
            const oauth = s.oauth || { connected: false, mode: null };
            const reachable = health.data?.ok === true;
            const probePassed = !!check?.checks?.find(
              (c) => c.name.startsWith("Bito MCP answered") && c.ok
            );

            // Per-tool config state (Claude Code + GitHub Copilot CLI). Each CLI is
            // connected/disconnected on its own; the OAuth token is shared across them.
            const per = s.per_tool;
            const toolView = MANAGED_TOOLS.map((mt) => {
              const e = per?.tools?.[mt.id];
              const installed =
                !!e?.installed || !!tools.data?.tools?.find((x) => x.id === mt.id)?.installed;
              const configured = !!e?.configured;
              const hasToken = !!e?.has_token;
              return {
                ...mt,
                installed,
                configured,
                hasToken,
                // Connected = a Bito entry with a bearer in this CLI's config.
                connected: configured && hasToken,
                cfg: isWindows
                  ? mt.id === "claude"
                    ? "%USERPROFILE%\\.claude.json"
                    : "%USERPROFILE%\\.copilot\\mcp-config.json"
                  : mt.id === "claude"
                  ? "~/.claude.json"
                  : "~/.copilot/mcp-config.json",
              };
            });
            // Show a row for any installed CLI, or any tool that still has a stale entry.
            const rows = toolView.filter((t) => t.installed || t.configured);
            const shown = rows.length ? rows : toolView; // fresh machine: still offer both
            const allConnected = shown.length > 0 && shown.every((t) => t.connected);

            // Connected (for the skills banners / diagnostics below) = any CLI connected,
            // a live OAuth session, the live probe passed, or the /health probe reached Bito.
            const connectedAny =
              shown.some((t) => t.connected) ||
              (oauth.connected && !oauth.expired) ||
              probePassed ||
              reachable;

            return (
              <>
                {/* Workspace ID or custom MCP URL — needed to connect any CLI. */}
                {!allConnected && (
                  <div className="field">
                    <label>Workspace ID or MCP URL</label>
                    <input
                      type="text"
                      placeholder="e.g. 123456  or  https://mcp.bito.ai/123456/mcp"
                      value={workspaceId}
                      onChange={(e) => setWorkspaceId(e.target.value)}
                      onBlur={() => {
                        const id = workspaceId.trim();
                        if (id && !id.startsWith("${")) {
                          api.post("/bito/oauth/pre-warm", { workspace_id: id }).catch(() => {});
                        }
                      }}
                    />
                    <div className="faint mono" style={{ fontSize: 11.5, marginTop: 5 }}>MCP URL: {mcpUrl}</div>
                    <div className="faint" style={{ fontSize: 11.5, marginTop: 3 }}>
                      Hosted Bito: enter your workspace ID. Self-hosted / custom: paste your full MCP URL (used exactly as entered).
                    </div>

                    {/* Bearer token — optional. Required for instances where OAuth isn't
                        enabled (self-hosted / custom); leave blank to sign in with OAuth. */}
                    <div style={{ marginTop: 10 }}>
                      <label>Bearer token <span className="faint" style={{ fontWeight: 400 }}>(optional)</span></label>
                      <input
                        type="password"
                        placeholder="Paste a static token, or leave blank to use OAuth"
                        value={token}
                        onChange={(e) => setToken(e.target.value)}
                        style={{ width: "100%" }}
                      />
                      <div className="faint" style={{ fontSize: 11.5, marginTop: 3 }}>
                        Provide this if your Bito doesn't support OAuth — the token is written into the
                        CLI you Connect below. Leave blank and Connect opens a browser OAuth sign-in.
                      </div>
                    </div>
                  </div>
                )}

                {/* Per-tool connect rows — one Connect/Disconnect button per CLI. */}
                <div className="stack" style={{ gap: 8, marginBottom: 6 }}>
                  {shown.map((t) => {
                    const connecting = busy === "connect:" + t.id;
                    const disconnecting = busy === "disc:" + t.id;
                    return (
                      <div
                        key={t.id}
                        className="card card-pad"
                        style={{
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "space-between",
                          gap: 10,
                          padding: "10px 12px",
                        }}
                      >
                        <div style={{ minWidth: 0 }}>
                          <div style={{ fontWeight: 600, fontSize: 13 }}>{t.label}</div>
                          <div className="faint mono" style={{ fontSize: 11 }}>
                            {t.cfg}
                            {!t.installed && <span style={{ marginLeft: 6 }}>· CLI not detected</span>}
                          </div>
                        </div>
                        <div className="row" style={{ gap: 8, alignItems: "center" }}>
                          {t.connected ? (
                            <>
                              <Badge kind="ok">Connected</Badge>
                              <button
                                className="btn sm danger"
                                onClick={() => disconnectTool(t.id)}
                                disabled={disconnecting}
                              >
                                {disconnecting ? <Spinner /> : null} Disconnect
                              </button>
                            </>
                          ) : t.configured && !t.hasToken ? (
                            <>
                              <Badge kind="warn">Configured · no token</Badge>
                              <button
                                className="btn sm primary"
                                onClick={() => connectTool(t.id)}
                                disabled={connecting}
                              >
                                {connecting ? <Spinner /> : null} Connect
                              </button>
                              <button
                                className="btn sm danger"
                                onClick={() => disconnectTool(t.id)}
                                disabled={disconnecting}
                              >
                                {disconnecting ? <Spinner /> : null} Remove
                              </button>
                            </>
                          ) : (
                            <>
                              <Badge kind="neutral">Not connected</Badge>
                              <button
                                className="btn sm primary"
                                onClick={() => connectTool(t.id)}
                                disabled={connecting}
                              >
                                {connecting ? <Spinner /> : "🔑"} Connect
                              </button>
                            </>
                          )}
                          {/* Per-tool live check — probes Bito through THIS CLI. */}
                          {t.installed && t.configured && (
                            <button
                              className="btn sm"
                              onClick={() => healthCheck(t.id)}
                              disabled={checking === t.id}
                              title={`Live-check Bito through ${t.label}`}
                            >
                              {checking === t.id ? <Spinner /> : "✓"} Health check
                            </button>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>

                {/* Skills warning: MCP is connected but skills are missing — both needed for Arms B/C */}
                {connectedAny && skillsStatus.data && !(skillsStatus.data.claude?.arm_b_ok || skillsStatus.data.copilot?.arm_b_ok) && (
                  <div className="banner warn" style={{ marginBottom: 8, marginTop: 4 }}>
                    <span aria-hidden>⚠️</span>
                    <div>
                      <span className="b-title">Skills also needed: </span>
                      Bito MCP is connected, but <code>bito-codebase-explorer</code> is not installed.
                      Arms B & C need both the MCP <em>and</em> the skills to produce meaningful results.{" "}
                      <button
                        className="btn ghost sm"
                        style={{ padding: "1px 8px", fontSize: 12 }}
                        onClick={() => document.getElementById("bito-skills")?.scrollIntoView({ behavior: "smooth", block: "center" })}
                      >
                        See install steps ↓
                      </button>
                    </div>
                  </div>
                )}
                {connectedAny && (skillsStatus.data?.claude?.arm_b_ok || skillsStatus.data?.copilot?.arm_b_ok) && !(skillsStatus.data?.claude?.arm_c_ok || skillsStatus.data?.copilot?.arm_c_ok) && (
                  <div className="banner info" style={{ marginBottom: 8, marginTop: 4 }}>
                    <span aria-hidden>ℹ️</span>
                    <div>
                      <span className="b-title">Partial skills: </span>
                      Arm B is ready, but Arm C works best with the full skill suite.{" "}
                      <button
                        className="btn ghost sm"
                        style={{ padding: "1px 8px", fontSize: 12 }}
                        onClick={() => document.getElementById("bito-skills")?.scrollIntoView({ behavior: "smooth", block: "center" })}
                      >
                        Install full suite ↓
                      </button>
                    </div>
                  </div>
                )}

                {/* Diagnostic only when NOT connected and the probe explained why
                    (token rejected / server unreachable) — never contradicts "Connected". */}
                {!connectedAny && health.data && health.data.ok === false &&
                  health.data.reason !== "no_token" && (
                  <div style={{ marginBottom: 6 }}>
                    <Badge kind="err">MCP unavailable</Badge>{" "}
                    <span className="faint" style={{ fontSize: 12 }}>{health.data.detail}</span>
                  </div>
                )}

                <p className="faint" style={{ fontSize: 12 }}>
                  Hosted Bito (<span className="mono">mcp.bito.ai</span>) uses OAuth — click <strong>Connect</strong>
                  on a CLI, approve in the browser once, and the harness writes the Bito server into that CLI's
                  MCP config (<span className="mono">~/.claude.json</span> or <span className="mono">~/.copilot/mcp-config.json</span>)
                  and stores &amp; auto-refreshes the token. The same sign-in is reused, so connecting the second CLI
                  needs no extra browser step. Each <strong>Disconnect</strong> only removes that one CLI. Each row's{" "}
                  <strong>Health check</strong> probes Bito live through <em>that</em> CLI — Claude Code via{" "}
                  <span className="mono">claude</span>, Copilot via <span className="mono">copilot</span> — since their MCP
                  connections are independent.
                </p>

                {msg && <div style={{ marginTop: 12 }}><Banner kind={msg.ok ? "ok" : "err"}>{msg.text}</Banner></div>}

                {(checking || check || checkErr) && (
                  <div style={{ marginTop: 14 }}>
                    {checkedTool && (
                      <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 8 }}>
                        Health check — {TOOL_LABEL[checkedTool] ?? checkedTool}
                      </div>
                    )}
                    {checking && <p className="muted"><Spinner /> Probing Bito through {TOOL_LABEL[checking] ?? checking}… up to a minute.</p>}
                    {checkErr && <Banner kind="err" title="Check failed">{checkErr}</Banner>}
                    {check && (
                      <>
                        <div className="checklist">
                          {check.checks.map((c, i) => {
                            const status = c.status ?? (c.ok ? "ok" : "fail");
                            const icon = status === "ok" ? "✓" : status === "warn" ? "!" : "✕";
                            const iconClass = status === "ok" ? "done" : status === "warn" ? "warn" : "fail";
                            return (
                            <div className="check-row" key={i}>
                              <div className={`check-icon ${iconClass}`}>{icon}</div>
                              <div className="check-body">
                                <div className="t">{c.name}</div>
                                <div className="d">{c.detail}</div>
                              </div>
                            </div>
                            );
                          })}
                        </div>
                        {check.repositories.length > 0 && (
                          <>
                            <div className="divider" />
                            <div style={{ fontWeight: 600, marginBottom: 8 }}>
                              Indexed repositories ({check.repositories.length})
                            </div>
                            <div className="row wrap">
                              {check.repositories.map((r) => <span className="tag" key={r}>{r}</span>)}
                            </div>
                          </>
                        )}
                      </>
                    )}
                  </div>
                )}
              </>
            );
          }}
        </Async>
      </Card>

      {/* Bito Skills card */}
      <div id="bito-skills" />
      <Card
        title="Bito Skills"
        sub="Arms B & C invoke the bito-* skills installed locally. Both arms need them present to produce meaningful results."
        right={
          <a
            href="https://docs.bito.ai/ai-architect/agent-skills"
            target="_blank"
            rel="noreferrer"
            style={{ fontSize: 13 }}
          >
            Agent skills docs ↗
          </a>
        }
      >
        <Async state={skillsStatus}>
          {(sk) => {
            const anyArmBOk  = !!(sk.claude.arm_b_ok || sk.copilot.arm_b_ok);
            const anyArmCOk  = !!(sk.claude.arm_c_ok || sk.copilot.arm_c_ok);
            const totalCount = Math.max(sk.claude.count, sk.copilot.count);

            const ToolSkillRow = ({ label, toolSk, dirLabel }: {
              label: string;
              toolSk: typeof sk.claude;
              dirLabel: string;
            }) => {
              const grid = toolSk.installed.map((name) => ({
                name,
                arm: name === "bito-codebase-explorer" ? "B + C" : "C",
                required: name === "bito-codebase-explorer",
              }));
              return (
                <div style={{ marginBottom: 16 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
                    <strong style={{ fontSize: 13 }}>{label}</strong>
                    <code className="faint" style={{ fontSize: 11 }}>{dirLabel}</code>
                    {toolSk.arm_b_ok
                      ? <Badge kind="ok">{toolSk.count} skills</Badge>
                      : <Badge kind="err">No skills found</Badge>}
                  </div>
                  {toolSk.installed.length > 0 ? (
                    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(240px, 1fr))", gap: 6 }}>
                      {grid.map(({ name, arm, required }) => (
                        <div
                          key={name}
                          className="card card-pad"
                          style={{
                            display: "flex", alignItems: "center", gap: 8, padding: "6px 10px",
                            borderColor: required ? "var(--green)" : "var(--border)",
                          }}
                        >
                          <span style={{ fontSize: 14, lineHeight: 1, color: "var(--green)" }}>✓</span>
                          <div style={{ flex: 1, minWidth: 0 }}>
                            <div style={{ fontSize: 12, fontWeight: 600, fontFamily: "monospace" }}>{name}</div>
                            <div className="faint" style={{ fontSize: 11 }}>Arm {arm}{required ? " · required" : ""}</div>
                          </div>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <p className="faint" style={{ fontSize: 12, margin: 0 }}>
                      No bito-* skills found in <code>{dirLabel}</code>
                    </p>
                  )}
                </div>
              );
            };

            return (
              <>
                {/* Status banner */}
                {!anyArmBOk ? (
                  <Banner kind="err" title="Skills missing — Arms B & C cannot run properly">
                    <code>bito-codebase-explorer</code> is not installed in either{" "}
                    <code>{claudeSkillsDir}</code> or <code>{copilotSkillsDir}</code>. Without it, Arms B &
                    C fall back to MCP-only behavior and the benchmark result is invalid. Install with the
                    one command below, then restart the app.
                  </Banner>
                ) : !anyArmCOk ? (
                  <Banner kind="warn" title="Partial install — Arm C may be limited">
                    Only {totalCount} bito-* skill(s) detected. Arm B is OK, but Arm C works best with the
                    full suite — re-run the install command below, then restart.
                  </Banner>
                ) : (
                  <Banner kind="ok">
                    {totalCount} bito-* skills installed — Arms B & C are ready.
                  </Banner>
                )}

                {/* One-command install — shown when skills aren't installed yet */}
                {!anyArmBOk && (
                  <div style={{ margin: "12px 0" }}>
                    <p style={{ fontSize: 13, marginBottom: 6 }}>
                      Install Bito MCP + skills with one command, then restart this app:
                    </p>
                    <code style={{ userSelect: "all", display: "block", padding: "10px 12px", borderRadius: 6, fontSize: 12.5 }}>
                      {bitoInstallCmd}
                    </code>
                    <p className="faint" style={{ fontSize: 12, marginTop: 6 }}>
                      Installs both the Bito AI Architect MCP and the bito-* skills into{" "}
                      <code>{claudeSkillsDir}</code> and <code>{copilotSkillsDir}</code>. When it finishes,
                      stop this app (Ctrl+C) and run the launcher again.
                    </p>
                  </div>
                )}

                {/* Per-tool skill rows */}
                <div style={{ marginTop: 14 }}>
                  <ToolSkillRow label="Claude Code" toolSk={sk.claude} dirLabel={claudeSkillsDir} />
                  <ToolSkillRow label="GitHub Copilot CLI" toolSk={sk.copilot} dirLabel={copilotSkillsDir} />
                </div>
              </>
            );
          }}
        </Async>
      </Card>

      {/* Git hosting credentials */}
      <div id="git-credentials" />
      <Card
        title="Git hosting credentials (Arm A)"
        sub="Arm A investigates repos using glab/gh/git. Authenticate these CLIs so it can access your private repositories."
      >
        <Async state={gitTools}>
          {(gt) => {
            const cliTools = [
              {
                id: "glab",
                label: "GitLab CLI (glab)",
                t: gt.glab,
                loginCmd: "glab auth login",
                note: "Required for GitLab repos.",
              },
              {
                id: "gh",
                label: "GitHub CLI (gh)",
                t: gt.gh,
                loginCmd: "gh auth login",
                note: "Required for GitHub repos.",
              },
              {
                id: "git",
                label: "git",
                t: gt.git,
                loginCmd: null,
                note: "Auth is managed by glab or gh — no separate login needed.",
              },
            ];

            const anyUnauthed = (gt.glab.available && gt.glab.authed === false)
                             || (gt.gh.available   && gt.gh.authed   === false);
            const anyMissing  = !gt.glab.available || !gt.gh.available;
            const allOk       = !anyUnauthed && !anyMissing;

            return (
              <>
                {anyUnauthed ? (
                  <Banner kind="warn" title="Git CLI not authenticated">
                    Arm A clones repositories directly — an unauthenticated CLI means it
                    can't reach private repos and may refuse the task. Run the login
                    command shown below, then reload the page to recheck.
                  </Banner>
                ) : anyMissing ? (
                  <Banner kind="info">
                    Some git hosting CLIs are not installed. Install the ones you use so
                    Arm A can clone your private repositories.
                  </Banner>
                ) : (
                  <Banner kind="ok">
                    git hosting CLIs are installed and authenticated — Arm A can clone repos.
                  </Banner>
                )}

                {/* CLI status grid */}
                <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))", gap: 8, margin: "14px 0 10px" }}>
                  {cliTools.map(({ id, label, t, loginCmd, note }) => {
                    const statusKind: "ok" | "warn" | "err" | "neutral" =
                      !t.available ? "neutral" :
                      t.authed === false ? "err" :
                      t.authed === true  ? "ok" : "warn";
                    const statusText =
                      !t.available ? "Not installed" :
                      t.authed === false ? "Not authenticated" :
                      t.authed === true  ? "Authenticated" : "Installed";
                    const showLogin = loginCmd && (!t.available || t.authed === false);

                    return (
                      <div key={id} className="card card-pad" style={{ padding: "10px 12px" }}>
                        <div className="row" style={{ justifyContent: "space-between", marginBottom: 4 }}>
                          <span style={{ fontWeight: 600, fontSize: 13 }}>{label}</span>
                          <Badge kind={statusKind}>{statusText}</Badge>
                        </div>
                        <div className="faint" style={{ fontSize: 11.5 }}>
                          {t.available && t.detail ? t.detail.split("\n")[0] : note}
                        </div>
                        {showLogin && (
                          <div style={{ marginTop: 7 }}>
                            <div className="faint" style={{ fontSize: 11, marginBottom: 2 }}>
                              {t.available ? "Authenticate in your terminal:" : "Install + authenticate:"}
                            </div>
                            <code style={{ display: "block", fontSize: 12, padding: "3px 7px", background: "var(--surface)", borderRadius: 4, userSelect: "all" }}>
                              {loginCmd}
                            </code>
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>

                <div className="divider" />

                {/* Git namespace */}
                <div style={{ marginTop: 2 }}>
                  <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 4 }}>
                    Repository namespace / org prefix
                  </div>
                  <p className="faint" style={{ fontSize: 12, marginBottom: 8 }}>
                    Set this once so Arm A knows exactly how to clone your repos without needing
                    full URLs in every prompt. Leave blank and the model will search via glab/gh
                    to resolve names automatically.
                  </p>
                  <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                    <input
                      type="text"
                      placeholder="e.g.  myorg   or   gitlab.company.com/backend-team"
                      value={gitNamespace}
                      onChange={(e) => setGitNamespace(e.target.value)}
                      style={{ flex: 1, minWidth: 260, fontFamily: "monospace", fontSize: 13 }}
                    />
                    <button className="btn" onClick={saveNamespace}>Save</button>
                    {namespaceSaved === true && <Badge kind="ok">Saved</Badge>}
                    {namespaceSaved === false && <Badge kind="err">Save failed</Badge>}
                  </div>
                  {gitNamespace.trim() && (
                    <p className="faint" style={{ fontSize: 12, marginTop: 7 }}>
                      Arm A will clone with:{" "}
                      <code style={{ fontSize: 12 }}>glab repo clone {gitNamespace.trim()}/&lt;repo-name&gt;</code>
                    </p>
                  )}
                  {!gitNamespace.trim() && (
                    <p className="faint" style={{ fontSize: 12, marginTop: 7 }}>
                      No prefix set — Arm A will run{" "}
                      <code style={{ fontSize: 12 }}>glab repo list --search &lt;name&gt;</code>{" "}
                      to find each repo's full path before cloning.
                    </p>
                  )}
                </div>

                <p className="faint" style={{ fontSize: 12, marginTop: 12 }}>
                  After authenticating, reload this page to refresh the CLI status. Arms B and C
                  use the Bito AI Architect index and are unaffected by git CLI auth.
                </p>
              </>
            );
          }}
        </Async>
      </Card>

      {/* Tools table */}
      <Card title="Detected tools" sub="Code-gen tools on this computer and their Bito connection.">
        <Async state={tools}>
          {(data) => (
            <>
              <table className="table">
                <thead>
                  <tr>
                    <th>Tool</th>
                    <th>Installed</th>
                    <th>Benchmark runs</th>
                    <th>Bito connection</th>
                    <th>Details</th>
                  </tr>
                </thead>
                <tbody>
                  {data.tools.map((t) => {
                    const b = mcpBadge(t.mcp.state);
                    return (
                      <tr key={t.id}>
                        <td>
                          <div style={{ fontWeight: 600 }}>{t.name}</div>
                          <div className="faint" style={{ fontSize: 11.5 }}>{t.version ?? t.kind}</div>
                        </td>
                        <td><Badge kind={t.installed ? "ok" : "neutral"}>{t.installed ? "Yes" : "No"}</Badge></td>
                        <td>
                          {t.supports_headless ? (
                            <Badge kind="info">Supported</Badge>
                          ) : (
                            <span className="muted" title={t.headless_note}>Detection only</span>
                          )}
                        </td>
                        <td><Badge kind={b.kind}>{b.label}</Badge></td>
                        <td style={{ maxWidth: 280 }}>
                          <span className="muted" style={{ fontSize: 12.5 }}>{t.installed ? t.mcp.detail : t.detect_detail}</span>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              <Banner kind="info">
                "Detection only" tools (Windsurf, Cline) have no unattended command-line mode, so the
                benchmark can't drive them automatically — but Bito connection still shows for them.
              </Banner>
            </>
          )}
        </Async>
      </Card>
    </div>
  );
}
