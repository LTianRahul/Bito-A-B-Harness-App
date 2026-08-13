import { Fragment, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError, subscribe } from "../api/client";
import { useAsync } from "../lib/useAsync";
import { ARM_INFO, ARMS, MODES, fmtTs, money, secs, type Arm } from "../lib";
import { type ToolInfo } from "../lib/status";
import { Async, Badge, Banner, Card, Progress, Spinner } from "../components/ui";
import CliConsole from "../components/CliConsole";

interface BatchRow {
  batch_id: string;
  label: string | null;
  tool: string;
  repo: string | null;
  mode: string;
  arms: string[];
  n_runs: number;
  status: string;
  progress: number;
  total: number;
  created_at: string;
  live?: boolean;
}

interface BatchDetail {
  status: string;
  progress: number;
  total: number;
  runs: {
    arm: string;
    base_prompt_id: string;
    exit_code: number | null;
    error: string | null;
    total_cost_usd: number | null;
    duration_ms: number | null;
  }[];
}

interface RunEvent {
  type: string;
  arm?: string;
  label?: string;
  prompt_id?: string;
  ok?: boolean;
  cost?: number;
  duration_ms?: number;
  error?: string;
  index?: number;
  total?: number;
  status?: string;
  message?: string;
  usage_limit?: boolean;
  attempt?: number;
  attempts?: number;
  updated?: boolean;
  from_version?: string;
  to_version?: string;
  detail?: string;
  bito_calls?: number;
  skills?: string[];
  runner?: string;  // tool id that executed this arm (e.g. "copilot", "claude")
}

export default function Runner() {
  const tools = useAsync<{ tools: ToolInfo[] }>(() => api.get("/tools"));
  const sets = useAsync<{ sets: { name: string; count: number }[] }>(() => api.get("/prompt-sets"));
  const promptCount = useAsync<{ prompts: any[] }>(() => api.get("/prompts"));
  const batches = useAsync<{ batches: BatchRow[] }>(() => api.get("/runs"));
  const cwd = useAsync<{ cwd: string }>(() => api.get("/setup/cwd"));

  const [tool, setTool] = useState("claude");
  const [repo, setRepo] = useState("");
  const [promptSet, setPromptSet] = useState("");
  const [mode, setMode] = useState("standard");
  const [nRuns, setNRuns] = useState(1);
  const [workspaceMode, setWorkspaceMode] = useState("fresh-clone");
  const [localRepoPath, setLocalRepoPath] = useState("");
  const [arms, setArms] = useState<Record<Arm, boolean>>({ A: true, B: true, C: true });
  const [startErr, setStartErr] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);

  // live run state
  const [active, setActive] = useState<{ batch_id: string; total: number } | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [runMap, setRunMap] = useState<Record<string, RunEvent>>({});
  const [phase, setPhase] = useState<string>("");
  const [progress, setProgress] = useState({ done: 0, total: 0 });
  const [limitMsg, setLimitMsg] = useState<string | null>(null);
  const [showCli, setShowCli] = useState(false);
  const unsubRef = useRef<null | (() => void)>(null);
  const logRef = useRef<HTMLDivElement>(null);
  const reconnectTriedRef = useRef(false);
  const [detailId, setDetailId] = useState<string | null>(null);
  const [detail, setDetail] = useState<BatchDetail | null>(null);
  const detailPollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  async function fetchDetail(id: string) {
    try {
      setDetail(await api.get<BatchDetail>(`/runs/${id}`));
    } catch {
      setDetail({ status: "error", progress: 0, total: 0, runs: [] });
    }
  }

  function stopDetailPoll() {
    if (detailPollRef.current) { clearInterval(detailPollRef.current); detailPollRef.current = null; }
  }

  async function viewDetails(id: string) {
    if (detailId === id) { setDetailId(null); setDetail(null); stopDetailPoll(); return; }
    stopDetailPoll();
    setDetailId(id);
    setDetail(null);
    await fetchDetail(id);
  }

  // Auto-refresh the expanded detail row every 3s while that session is live.
  useEffect(() => {
    stopDetailPoll();
    if (!detailId) return;
    const row = batches.data?.batches.find((b) => b.batch_id === detailId);
    if (!row || (row.status !== "running" && row.status !== "queued" && !row.live)) return;
    detailPollRef.current = setInterval(() => fetchDetail(detailId), 3000);
    return stopDetailPoll;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detailId, batches.data]);

  useEffect(() => () => unsubRef.current?.(), []);

  // Reconnect on mount: a run keeps going server-side even if you navigate away
  // from this page. When you come back, find any in-flight batch and resubscribe.
  // The events endpoint replays its buffered history, so live progress + phase
  // catch up immediately instead of looking stuck at 0.
  useEffect(() => {
    if (reconnectTriedRef.current) return;
    const list = batches.data?.batches;
    if (!list) return; // wait for the batch list to load
    reconnectTriedRef.current = true;
    if (active || unsubRef.current) return; // already tracking a run
    const live = list.find((b) => b.live);
    if (live) listen(live.batch_id, live.total);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [batches.data]);
  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [events]);

  // Pre-fill the local-repo path with the server's working directory, so picking
  // "local-repo" doesn't require typing a path from scratch. Only fills when empty,
  // and deps are stable after the one-time fetch, so it never clobbers user edits.
  useEffect(() => {
    if (cwd.data?.cwd && !localRepoPath) setLocalRepoPath(cwd.data.cwd);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cwd.data]);

  // While a run is active, refresh the "Recent runs" table every few seconds so it
  // reflects the real backend state (status + progress) in real time.
  useEffect(() => {
    if (phase !== "running") return;
    const t = setInterval(() => batches.refresh(), 3000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase]);

  // Safety net: reconcile the live phase against the authoritative batch status.
  // SSE can drop (laptop sleep, proxy idle timeout, a long judge step) and the
  // terminal batch_done/closed event may never arrive — leaving the banner stuck on
  // "running" while the backend is actually finished. The batches list (polled every
  // 3s above) carries the real status + `live` flag, so when the tracked batch is no
  // longer live, converge the phase to its final status and close the dangling stream.
  useEffect(() => {
    if (!active) return;
    if (phase !== "running") return;
    const row = batches.data?.batches.find((b) => b.batch_id === active.batch_id);
    if (!row || row.live) return; // still running, or not loaded yet
    const st = row.status;
    if (st === "running" || st === "queued") return; // transitional
    setPhase(st === "done" ? "done" : st === "error" ? "error" : st === "interrupted" ? "stopped" : st);
    unsubRef.current?.();
    unsubRef.current = null;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [batches.data, active, phase]);

  function listen(batchId: string, total: number) {
    setActive({ batch_id: batchId, total });
    setEvents([]);
    setRunMap({});
    setPhase("running");
    setProgress({ done: 0, total });
    setLimitMsg(null);
    unsubRef.current?.();
    unsubRef.current = subscribe(`/runs/${batchId}/events`, (ev: RunEvent) => {
      setEvents((prev) => [...prev, ev]);
      if (ev.type === "run_start" || ev.type === "run_done") {
        setRunMap((m) => ({ ...m, [`${ev.arm}:${ev.label}`]: ev }));
        if (ev.index && ev.total) setProgress({ done: ev.index, total: ev.total });
      }
      if (ev.type === "usage_limit") setLimitMsg((ev as any).message || "Usage limit reached.");
      if (ev.type === "batch_done") { setPhase("done"); batches.refresh(); unsubRef.current?.(); unsubRef.current = null; }
      if (ev.type === "batch_stopped") { setPhase("stopped"); batches.refresh(); unsubRef.current?.(); unsubRef.current = null; }
      if (ev.type === "batch_error") { setPhase("error"); batches.refresh(); unsubRef.current?.(); unsubRef.current = null; }
      if (ev.type === "closed") {
        // The server appends a terminal `closed` event with the final status before
        // ending the stream. Honor it (so we converge even if batch_done was missed)
        // and close the EventSource so the browser doesn't auto-reconnect and replay.
        const st = (ev as any).status;
        if (st === "done") setPhase("done");
        else if (st === "stopped") setPhase("stopped");
        else if (st === "error") setPhase("error");
        else if (st) setPhase(st);
        batches.refresh();
        unsubRef.current?.();
        unsubRef.current = null;
      }
    });
  }

  async function start() {
    setStarting(true);
    setStartErr(null);
    try {
      const selectedArms = ARMS.filter((a) => arms[a]);
      const r = await api.post<{ batch_id: string; total: number }>("/runs", {
        tool,
        repo: repo.trim() || null,
        prompt_set: promptSet || null,
        arms: selectedArms,
        mode,
        n_runs: nRuns,
        workspace_mode: workspaceMode,
        local_repo_path: workspaceMode === "local-repo" ? localRepoPath.trim() || null : null,
      });
      batches.refresh();
      listen(r.batch_id, r.total);
    } catch (e) {
      setStartErr(e instanceof ApiError ? String(e.detail) : String(e));
    } finally {
      setStarting(false);
    }
  }

  async function stop() {
    if (!active) return;
    setPhase("stopped"); // reflect immediately; SSE will confirm
    try {
      await api.post(`/runs/${active.batch_id}/stop`);
    } catch {
      /* ignore — already stopping/finished */
    }
    batches.refresh();
  }

  async function rerun(id: string) {
    try {
      const r = await api.post<{ batch_id: string; total: number }>(`/runs/${id}/rerun`);
      listen(r.batch_id, r.total);
    } catch (e) {
      setStartErr(e instanceof ApiError ? String(e.detail) : String(e));
    }
  }

  const running = phase === "running";

  // Live preview of how many runs "Start" will launch: arms × prompts × repeats.
  // The prompt count follows the selected set (or the working list by default), so
  // the number matches exactly what the backend computes — making it obvious why a
  // run is, say, 12 vs 18 (different prompt count), not a silent platform quirk.
  const selectedArmCount = ARMS.filter((a) => arms[a]).length;
  const selectedPromptCount = promptSet
    ? sets.data?.sets.find((x) => x.name === promptSet)?.count ?? 0
    : promptCount.data?.prompts.length ?? 0;
  const plannedTotal = selectedArmCount * selectedPromptCount * nRuns;
  const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? "" : "s"}`;

  return (
    <div className="stack">
      <div className="page-head">
        <h2>A/B Testing</h2>
        <p>
          Run the same tasks three ways and compare. <strong>Arm A</strong> is the tool alone,{" "}
          <strong>Arm B</strong> adds Bito AI Architect, and <strong>Arm C</strong> adds Bito + its
          Skills. We score the answers and show you what Bito adds.
        </p>
      </div>

      {/* Live progress */}
      {active && (
        <Card
          title={`Run ${active.batch_id}`}
          sub={
            phase === "running"
              ? "Running benchmarks…"
              : phase === "done"
              ? "Complete"
              : phase === "stopped"
              ? "Stopped"
              : "Error"
          }
          right={
            running ? (
              <button className="btn danger sm" onClick={stop}>
                ■ Stop
              </button>
            ) : (
              <div className="row">
                <Badge kind={phase === "done" ? "ok" : phase === "error" ? "err" : "warn"}>
                  {phase}
                </Badge>
                <Link className="btn sm" to="/leaderboard">
                  Compare →
                </Link>
              </div>
            )
          }
        >
          {limitMsg && (
            <div style={{ marginBottom: 14 }}>
              <Banner kind="warn" title="Claude usage limit reached">
                {limitMsg} The run was stopped so it doesn’t keep failing. When your limit resets
                (or you add credits / upgrade), use <strong>Rerun</strong> on this session to finish
                the remaining answers.
              </Banner>
            </div>
          )}
          {/* Per-arm summary cards */}
          {(() => {
            const armTotals = active ? Math.ceil(active.total / ARMS.filter((_a, i) => i < 3).length) : 0;
            const armEntries = ARMS.map((a) => {
              const runs = Object.entries(runMap)
                .filter(([k]) => k.startsWith(`${a}:`))
                .map(([, ev]) => ev);
              const done = runs.filter((ev) => ev.type === "run_done");
              const inFlight = runs.filter((ev) => ev.type === "run_start");
              const totalCost = done.reduce((s, ev) => s + (ev.cost || 0), 0);
              const bitoTotal = done.reduce((s, ev) => s + (ev.bito_calls || 0), 0);
              const skillsUsed = [...new Set(done.flatMap((ev) => ev.skills || []))];
              return { arm: a, done: done.length, inFlight: inFlight.length, totalCost, bitoTotal, skillsUsed };
            });
            return (
              <div className="grid grid-3" style={{ marginBottom: 14 }}>
                {armEntries.map(({ arm, done, inFlight, totalCost, bitoTotal, skillsUsed }) => (
                  <div key={arm} className="card card-pad" style={{ borderColor: ARM_INFO[arm].color, padding: "10px 14px" }}>
                    <div className="row" style={{ marginBottom: 6 }}>
                      <span className={`pill ${arm.toLowerCase()}`}>Arm {arm}</span>
                      <span style={{ fontSize: 12, fontWeight: 600 }}>{ARM_INFO[arm].name}</span>
                      {inFlight > 0 && running && <Spinner />}
                    </div>
                    <div style={{ fontSize: 13, fontWeight: 700 }}>
                      {done}/{armTotals || "?"} runs
                    </div>
                    <div className="muted" style={{ fontSize: 11.5 }}>{money(totalCost)} spent</div>
                    {(bitoTotal > 0 || skillsUsed.length > 0) && (
                      <div style={{ marginTop: 5, fontSize: 11, color: "var(--accent)" }}>
                        {bitoTotal > 0 && <div>Bito MCP calls: {bitoTotal}</div>}
                        {skillsUsed.length > 0 && <div>Skills: {skillsUsed.join(", ")}</div>}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            );
          })()}

          <div className="row" style={{ marginBottom: 6 }}>
            <span className="muted" style={{ fontSize: 13 }}>
              {progress.done} / {progress.total} runs
            </span>
            {running && <Spinner />}
          </div>
          <Progress value={progress.done} total={progress.total} />

          {/* per-run status rows */}
          <div style={{ marginTop: 14, display: "grid", gap: 5 }}>
            {Object.entries(runMap)
              .sort()
              .map(([k, ev]) => (
                <div className="row" key={k} style={{ fontSize: 12.5, gap: 8 }}>
                  <span className={`pill ${ev.arm?.toLowerCase()}`}>{ev.arm}</span>
                  {ev.runner && ev.runner !== "claude" && (
                    <span style={{ fontSize: 11, color: "var(--muted)", fontStyle: "italic" }}>{ev.runner}</span>
                  )}
                  <span className="mono" style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{ev.label}</span>
                  {ev.type === "run_start" && running ? (
                    <Spinner />
                  ) : ev.type === "run_start" ? (
                    <span style={{ color: "var(--warn)", fontSize: 11 }}>stopped</span>
                  ) : ev.ok ? (
                    <span className="muted" style={{ whiteSpace: "nowrap" }}>
                      {money(ev.cost)} · {secs(ev.duration_ms)}
                      {(ev.bito_calls || 0) > 0 && (
                        <span style={{ color: "var(--accent)", marginLeft: 6 }}>
                          [{ev.bito_calls} Bito{ev.skills && ev.skills.length > 0 ? ` · ${ev.skills.join(", ")}` : ""}]
                        </span>
                      )}
                      {" "}<span style={{ color: "var(--ok)" }}>✓</span>
                    </span>
                  ) : ev.usage_limit ? (
                    <span style={{ color: "var(--warn)" }} title={ev.error}>usage limit</span>
                  ) : (
                    <span style={{ color: "var(--err)" }} title={ev.error}>failed</span>
                  )}
                </div>
              ))}
          </div>

          {/* live log */}
          {events.length > 0 && (
            <div className="log" ref={logRef} style={{ marginTop: 14, maxHeight: 180 }}>
              {events.map((e, i) => (
                <div key={i}>
                  {e.type === "skills_updating" && "⤓ pulling the latest Bito skills…"}
                  {e.type === "skills_updated" &&
                    (e.ok === false
                      ? `⚠ couldn't update Bito skills: ${e.detail || ""}`
                      : e.updated
                      ? `⤓ Bito skills updated ${e.from_version || ""} → ${e.to_version || ""}`
                      : `✓ Bito skills already latest (${e.to_version || "?"})`)}
                  {e.type === "run_start" && `▶ ${e.arm} · ${e.label} (run ${e.index}/${e.total})`}
                  {e.type === "run_retry" &&
                    `↻ ${e.arm} · ${e.label} — retrying (attempt ${(e.attempt || 1) + 1}/${e.attempts || "?"})`}
                  {e.type === "run_done" && (() => {
                    const runnerTag = e.runner && e.runner !== "claude" ? ` [${e.runner}]` : "";
                    const base = `${e.ok ? "✓" : "✕"} ${e.arm}${runnerTag} · ${e.label}`;
                    if (!e.ok) return `${base}  ${e.error || ""}`;
                    const bitoInfo = (e.bito_calls || 0) > 0
                      ? `  [Bito: ${e.bito_calls} calls${e.skills && e.skills.length > 0 ? ` · ${e.skills.join(", ")}` : ""}]`
                      : "";
                    return `${base}  ${money(e.cost)} · ${secs(e.duration_ms)}${bitoInfo}`;
                  })()}
                  {e.type === "arm_done" && `— arm ${e.arm} complete —`}
                  {e.type === "usage_limit" && `⚠ usage limit: ${e.message || ""}`}
                  {e.type === "batch_done" && "✅ benchmark complete"}
                  {e.type === "batch_stopped" && "■ stopped"}
                  {e.type === "batch_error" && `✕ error: ${e.error}`}
                </div>
              ))}
            </div>
          )}
        </Card>
      )}

      {/* Config form */}
      {!running && (
        <Card title="New A/B test" sub="Pick what to test, then start.">
          {startErr && (
            <div style={{ marginBottom: 14 }}>
              <Banner kind="err" title="Couldn’t start">{startErr}</Banner>
            </div>
          )}
          <Async state={tools}>
            {(t) => (
              <div className="grid grid-2">
                <div className="field">
                  <label>Code-gen tool</label>
                  <div className="help">Which assistant to benchmark.</div>
                  <select value={tool} onChange={(e) => setTool(e.target.value)}>
                    {t.tools.map((x) => (
                      <option key={x.id} value={x.id} disabled={!x.installed || !x.supports_headless}>
                        {x.name}
                        {!x.installed ? " (not installed)" : !x.supports_headless ? " (no headless runs)" : ""}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="field">
                  <label>Repository / folder</label>
                  <div className="help">A label for what you’re testing against (optional).</div>
                  <input type="text" value={repo} placeholder="e.g. billing-service" onChange={(e) => setRepo(e.target.value)} />
                </div>
              </div>
            )}
          </Async>

          <div className="grid grid-2">
            <div className="field">
              <label>Prompt set</label>
              <div className="help">
                <Async state={promptCount}>
                  {(p) => <>Default uses your {p.prompts.length} working prompt(s).</>}
                </Async>
              </div>
              <Async state={sets}>
                {(s) => (
                  <select value={promptSet} onChange={(e) => setPromptSet(e.target.value)}>
                    <option value="">Working prompts (default)</option>
                    {s.sets.map((x) => (
                      <option key={x.name} value={x.name}>
                        {x.name} ({x.count})
                      </option>
                    ))}
                  </select>
                )}
              </Async>
            </div>
            <div className="field">
              <label>Benchmark mode</label>
              <div className="help">{MODES.find((m) => m.id === mode)?.hint}</div>
              <select value={mode} onChange={(e) => setMode(e.target.value)}>
                {MODES.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.label}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div className="field">
            <label>Workspace</label>
            <div className="help">
              Fresh clone: each arm starts in an empty dir and clones source itself. Local
              repo: run against a copy of your checkout (including uncommitted changes) so
              arms make code changes in place — compare results with vs without AI Architect.
            </div>
            <select value={workspaceMode} onChange={(e) => setWorkspaceMode(e.target.value)}>
              <option value="fresh-clone">Fresh clone (default)</option>
              <option value="local-repo">Local repo (use my checkout)</option>
            </select>
            {workspaceMode === "local-repo" && (
              <input
                type="text"
                style={{ marginTop: 8 }}
                value={localRepoPath}
                placeholder="Absolute path to repo checkout, e.g. /Users/me/code/billing-service"
                onChange={(e) => setLocalRepoPath(e.target.value)}
              />
            )}
          </div>

          <div className="field">
            <label>Arms to run</label>
            <div className="help">Run at least A and B to measure what Bito adds. (Scoring needs all three.)</div>
            <div className="grid grid-3">
              {ARMS.map((a) => (
                <label
                  key={a}
                  className="card card-pad"
                  style={{ cursor: "pointer", borderColor: arms[a] ? ARM_INFO[a].color : undefined }}
                >
                  <div className="row">
                    <input
                      type="checkbox"
                      checked={arms[a]}
                      style={{ width: "auto" }}
                      onChange={(e) => setArms({ ...arms, [a]: e.target.checked })}
                    />
                    <span className={`pill ${a.toLowerCase()}`}>Arm {a}</span>
                  </div>
                  <div style={{ fontWeight: 600, marginTop: 8 }}>{ARM_INFO[a].name}</div>
                  <div className="muted" style={{ fontSize: 12 }}>{ARM_INFO[a].blurb}</div>
                </label>
              ))}
            </div>
          </div>

          <div className="grid grid-2">
            <div className="field">
              <label>Runs per prompt</label>
              <div className="help">Repeat each task to average out variance.</div>
              <input type="number" min={1} max={10} value={nRuns} onChange={(e) => setNRuns(Math.max(1, +e.target.value))} />
            </div>
          </div>

          <Banner kind="info">
            Each run makes real model calls and costs tokens. A standard A/B/C run is{" "}
            {ARMS.filter((a) => arms[a]).length} answer(s) per prompt plus one scoring call.
          </Banner>
          <div className="row" style={{ marginTop: 14, gap: 14, alignItems: "center" }}>
            <button className="btn primary" onClick={start} disabled={starting || plannedTotal === 0}>
              {starting ? <Spinner /> : "▶"} Start A/B test
            </button>
            {selectedPromptCount > 0 && (
              <span className="muted" style={{ fontSize: 13 }}>
                {plural(selectedArmCount, "arm")} × {plural(selectedPromptCount, "prompt")} ×{" "}
                {plural(nRuns, "repeat")} = <strong>{plural(plannedTotal, "run")}</strong>
              </span>
            )}
          </div>
        </Card>
      )}

      {/* History */}
      <Card title="Recent runs" sub="Your benchmark sessions.">
        <Async state={batches}>
          {(b) =>
            b.batches.length === 0 ? (
              <p className="muted">No runs yet. Start one above.</p>
            ) : (
              <table className="table">
                <thead>
                  <tr>
                    <th>Session</th>
                    <th>Tool</th>
                    <th>Arms</th>
                    <th>Status</th>
                    <th className="num">Progress</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {b.batches.map((row) => (
                    <Fragment key={row.batch_id}>
                    <tr>
                      <td>
                        <div style={{ fontWeight: 600 }}>{fmtTs(row.created_at)}{row.label ? ` · ${row.label}` : ""}</div>
                        <div className="faint" style={{ fontSize: 11 }}>
                          {row.repo || "—"} · {row.mode}
                        </div>
                      </td>
                      <td>{row.tool}</td>
                      <td>{row.arms.join(" ")}</td>
                      <td>
                        <Badge
                          kind={
                            row.status === "done"
                              ? "ok"
                              : row.status === "running"
                              ? "info"
                              : row.status === "error"
                              ? "err"
                              : "neutral"
                          }
                        >
                          {row.status}
                        </Badge>
                      </td>
                      <td className="num">
                        {row.progress}/{row.total}
                      </td>
                      <td style={{ textAlign: "right" }}>
                        <button className="btn ghost sm" onClick={() => viewDetails(row.batch_id)}>
                          {detailId === row.batch_id ? "Hide" : "Show"}
                        </button>
                        <button className="btn ghost sm" onClick={() => rerun(row.batch_id)}>
                          Rerun
                        </button>
                        <Link className="btn ghost sm" to={`/metrics?batch=${row.batch_id}`}>
                          Metrics
                        </Link>
                      </td>
                    </tr>
                    {detailId === row.batch_id && (
                      <tr>
                        <td colSpan={6} style={{ background: "var(--surface-2)" }}>
                          {!detail ? (
                            <Spinner />
                          ) : detail.runs.length === 0 ? (
                            <span className="faint">No per-run data recorded.</span>
                          ) : (
                            <div className="log" style={{ maxHeight: 220 }}>
                              {detail.runs.map((r, i) => {
                                const ok = r.exit_code === 0 && !r.error;
                                return (
                                  <div key={i}>
                                    {ok ? "✓" : "✕"} <span className={`pill ${r.arm.toLowerCase()}`}>{r.arm}</span>{" "}
                                    {r.base_prompt_id}
                                    {ok
                                      ? `  ·  ${money(r.total_cost_usd)} · ${secs(r.duration_ms)}`
                                      : `  ·  ${r.error || "failed"}`}
                                  </div>
                                );
                              })}
                            </div>
                          )}
                        </td>
                      </tr>
                    )}
                    </Fragment>
                  ))}
                </tbody>
              </table>
            )
          }
        </Async>
      </Card>

      {/* Command line (advanced) */}
      <Card
        title="Command line"
        sub="For developers — run the harness directly and watch the output."
        right={
          <button className="btn ghost sm" onClick={() => setShowCli((s) => !s)}>
            {showCli ? "Hide" : "Show"}
          </button>
        }
      >
        {showCli ? (
          <CliConsole />
        ) : (
          <p className="muted" style={{ fontSize: 13 }}>
            Prefer the terminal? Run the same benchmark with{" "}
            <span className="mono">python harness.py all --prompts prompts.json</span>. Click{" "}
            <strong>Show</strong> to run commands here and stream their output.
          </p>
        )}
      </Card>
    </div>
  );
}
