import { useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import { useAsync } from "../lib/useAsync";
import { ARM_INFO, ARMS, fmtTs, money, num, pct, secs } from "../lib";
import { useEffect, useState } from "react";
import { Async, Card, Empty, Modal } from "../components/ui";

interface ArmM {
  n: number;
  n_success: number;
  n_completed: number;
  n_errors: number;
  n_violations: number;
  success_rate: number | null;
  avg_cost: number | null;
  total_cost: number;
  avg_duration_ms: number | null;
  avg_input_tokens: number | null;
  avg_output_tokens: number | null;
  avg_tool_calls: number | null;
  avg_num_turns: number | null;
  avg_mcp_calls: number | null;
  avg_bito_calls: number | null;
  skills_used: string[];
}

interface MetricsResp {
  batch_id: string | null;
  arms: Record<string, ArmM>;
  n_runs: number;
}

interface BatchRow {
  batch_id: string;
  label: string | null;
  status: string;
  created_at: string;
}

const v = (a: ArmM | undefined, k: keyof ArmM) => (a ? (a[k] as number | null) : null);

interface BadgeConfig {
  key: keyof ArmM;
  label: string;
  icon?: string;
}

const BADGE_METRICS: BadgeConfig[] = [
  { key: "total_cost",      label: "more cost-effective" },
  { key: "avg_num_turns",   label: "fewer turns" },
  { key: "avg_duration_ms", label: "faster" },
];

// Returns rounded improvement % if thisVal < armAVal, regardless of the other arm.
// Never shows if Arm A is already the lowest (armA <= both B and C).
function calcImprovement(
  armAVal: number | null,
  armBVal: number | null,
  armCVal: number | null,
  thisVal: number | null,
): number | null {
  if (armAVal == null || armBVal == null || armCVal == null || thisVal == null) return null;
  if (armAVal <= armBVal && armAVal <= armCVal) return null; // Arm A is already the best — no badge
  if (thisVal >= armAVal) return null; // this arm is not better than baseline
  return Math.round(((armAVal - thisVal) / armAVal) * 100);
}

function ImprovementBadge({ pct, label, icon = "↓" }: { pct: number; label: string; icon?: string }) {
  return (
    <span title={label} style={{ fontSize: 10.5, fontWeight: 700, color: "#16a34a", marginLeft: 5, whiteSpace: "nowrap", cursor: "default" }}>
      {icon} {pct}%
    </span>
  );
}

function AnswerPreviewModal({ batch, arm, onClose }: { batch: string; arm: string; onClose: () => void }) {
  const result = useAsync<string>(
    () => fetch(`/api/runs/${encodeURIComponent(batch)}/logs/${arm}/preview`)
        .then((r) => { if (!r.ok) throw new Error("No data"); return r.text(); }),
    [batch, arm],
  );
  return (
    <Modal title={`Arm ${arm} answers · ${batch}`} onClose={onClose}>
      <Async state={result}>
        {(md) => (
          <>
            <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 12 }}>
              <a
                href={`/api/runs/${encodeURIComponent(batch)}/logs/${arm}/download`}
                className="btn sm"
                download
                style={{ textDecoration: "none" }}
              >
                ↓ Download response
              </a>
            </div>
            <pre style={{
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
              fontSize: 12.5,
              lineHeight: 1.7,
              fontFamily: "inherit",
              background: "var(--surface-raised, var(--surface))",
              border: "1px solid var(--border)",
              borderRadius: 6,
              padding: "14px 16px",
              maxHeight: "60vh",
              overflowY: "auto",
            }}>
              {md}
            </pre>
          </>
        )}
      </Async>
    </Modal>
  );
}

export default function Metrics({ embedded = false }: { embedded?: boolean }) {
  const [preview, setPreview] = useState<{ batch: string; arm: string } | null>(null);
  const [params, setParams] = useSearchParams();
  const batchParam = params.get("batch");
  const batches = useAsync<{ batches: BatchRow[] }>(() => api.get("/runs"));

  const latest = batches.data?.batches?.[0]?.batch_id;
  useEffect(() => {
    if (batchParam === null && latest) setParams({ batch: latest }, { replace: true });
  }, [batchParam, latest, setParams]);

  const selectValue = batchParam ?? latest ?? "";
  const batch = batchParam && batchParam !== "all" ? batchParam : "";
  const data = useAsync<MetricsResp>(
    () => api.get(`/metrics${batch ? `?batch=${encodeURIComponent(batch)}` : ""}`),
    [batch],
  );

  return (
    <div className="stack">
      {!embedded && (
        <div className="page-head">
          <h2>Scores</h2>
          <p>How each arm performed. Lower cost and time with more Bito usage is the win.</p>
        </div>
      )}

      <Async state={batches}>
        {(b) => (
          <div className="row wrap">
            <label style={{ fontWeight: 600, fontSize: 13 }}>Showing</label>
            <select
              value={selectValue}
              style={{ width: 320 }}
              onChange={(e) => setParams({ batch: e.target.value })}
            >
              {b.batches.map((x, i) => (
                <option key={x.batch_id} value={x.batch_id}>
                  {fmtTs(x.created_at)}{x.label ? ` · ${x.label}` : ""} ({x.status}){i === 0 ? " — latest" : ""}
                </option>
              ))}
              <option value="all">All benchmark sessions (aggregate)</option>
            </select>
            <div className="spacer" />
            <div className="export-group">
              <a
                className="btn sm export export-csv"
                href={`/api/metrics/export.csv${batch ? `?batch=${encodeURIComponent(batch)}` : ""}`}
                download
              >
                <span className="export-chip">CSV</span> Export CSV
              </a>
              <button
                className="btn sm export export-html"
                onClick={async () => {
                  const url = `/api/metrics/export.html${batch ? `?batch=${encodeURIComponent(batch)}` : ""}`;
                  const blob = await fetch(url).then((r) => r.blob());
                  const a = document.createElement("a");
                  a.href = URL.createObjectURL(blob);
                  a.download = `ab-metrics-${batch || "all"}.html`;
                  a.click();
                  URL.revokeObjectURL(a.href);
                }}
              >
                <span className="export-chip">HTML</span> Export HTML
              </button>
            </div>
          </div>
        )}
      </Async>

      <Async state={data}>
        {(d) => {
          if (d.n_runs === 0)
            return (
              <Card>
                <Empty icon="📊" title="No results yet">
                  Run an A/B test, then come back to see scores.
                </Empty>
              </Card>
            );

          const cardStats: { label: string; value: (am: ArmM) => string; badgeKey?: keyof ArmM; badgeLabel?: string; badgeIcon?: string }[] = [
            { label: "completed",      value: (am) => `${am.n_completed}/${am.n}` },
            { label: "completion",     value: (am) => pct(am.success_rate) },
            { label: "violations",     value: (am) => num(am.n_violations) },
            { label: "total spend",    value: (am) => money(am.total_cost),     badgeKey: "total_cost",      badgeLabel: "lower cost" },
            { label: "cost / answer",  value: (am) => money(am.avg_cost),          badgeKey: "avg_cost",        badgeLabel: "lower cost" },
            { label: "avg turns",      value: (am) => num(am.avg_num_turns, 1), badgeKey: "avg_num_turns",   badgeLabel: "fewer turns" },
            { label: "avg tool calls", value: (am) => num(am.avg_tool_calls, 1), badgeKey: "avg_tool_calls",  badgeLabel: "fewer tool calls" },
            { label: "avg time",       value: (am) => secs(am.avg_duration_ms),  badgeKey: "avg_duration_ms", badgeLabel: "faster", badgeIcon: "⚡" },
            { label: "avg bito mcp calls", value: (am) => num(am.avg_bito_calls, 1) },
          ];

          // Pre-compute badge eligibility: Arm A must beat BOTH B and C for each metric.
          const armA = d.arms["A"];
          const armB = d.arms["B"];
          const armC = d.arms["C"];

          return (
            <>
              {/* ── Arm summary cards ── */}
              <div className="grid grid-3">
                {ARMS.map((a) => {
                  const am = d.arms[a];
                  return (
                    <div key={a} className="card card-pad">
                      <div className="row" style={{ marginBottom: 10, alignItems: "center", gap: 8 }}>
                        <span className={`pill ${a.toLowerCase()}`}>Arm {a}</span>
                        <span style={{ fontWeight: 650, fontSize: 13 }}>{ARM_INFO[a].name}</span>
                      </div>
                      {am ? (
                        <>
                          <div style={{ display: "grid", gap: 5 }}>
                            {cardStats.map((r) => {
                              const badge = (a !== "A" && r.badgeKey && r.badgeLabel)
                                ? calcImprovement(
                                    armA ? (armA[r.badgeKey] as number | null) : null,
                                    armB ? (armB[r.badgeKey] as number | null) : null,
                                    armC ? (armC[r.badgeKey] as number | null) : null,
                                    am[r.badgeKey] as number | null,
                                  )
                                : null;
                              return (
                              <div
                                key={r.label}
                                className="row"
                                style={{ justifyContent: "space-between", fontSize: 12.5 }}
                              >
                                <span className="muted">{r.label}</span>
                                <span style={{ fontWeight: 600, display: "flex", alignItems: "center", flexWrap: "wrap", justifyContent: "flex-end" }}>
                                  {r.value(am)}
                                  {badge !== null && r.badgeLabel && (
                                    <ImprovementBadge pct={badge} label={r.badgeLabel} icon={r.badgeIcon} />
                                  )}
                                </span>
                              </div>
                            )})}
                          </div>
                          <div className="divider" style={{ margin: "10px 0 8px" }} />
                          <div style={{ fontSize: 11, color: "var(--muted)", marginBottom: 6, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.04em" }}>
                            Skills used
                          </div>
                          {(am.skills_used || []).length ? (
                            <div>
                              {am.skills_used.map((s) => (
                                <span className="tag" key={s} style={{ marginRight: 4, marginBottom: 4 }}>{s}</span>
                              ))}
                            </div>
                          ) : (
                            <span className="faint" style={{ fontSize: 11.5 }}>no skills used</span>
                          )}
                          {batch && (
                            <div style={{ display: "flex", gap: 6, marginTop: 10 }}>
                              <button
                                className="btn sm"
                                style={{ flex: 1 }}
                                onClick={() => setPreview({ batch, arm: a })}
                              >
                                👁 Preview answers
                              </button>
                              <a
                                href={`/api/runs/${batch}/logs/${a}/download`}
                                className="btn sm"
                                style={{ flex: 1, textAlign: "center", textDecoration: "none" }}
                                download
                              >
                                ↓ Download response
                              </a>
                            </div>
                          )}
                        </>
                      ) : (
                        <span className="faint" style={{ fontSize: 12 }}>No data</span>
                      )}
                    </div>
                  );
                })}
              </div>

              {/* ── Key metrics card ── */}
              <Card title="Key metrics">
                <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 12 }}>
                  All metrics are execution-derived. Lower cost, faster time, and more Bito MCP calls indicate Bito's impact.
                </div>
                {(
                  [
                    { label: "Success rate",      key: "success_rate" as keyof ArmM,      fmt: pct,   better: true },
                    { label: "Cost / answer",      key: "avg_cost" as keyof ArmM,           fmt: money, better: false },
                    { label: "Avg time",           key: "avg_duration_ms" as keyof ArmM,    fmt: secs,  better: false },
                    { label: "Avg MCP calls",      key: "avg_mcp_calls" as keyof ArmM,      fmt: (v: number | null) => num(v, 1), better: true },
                    { label: "Avg Bito MCP calls", key: "avg_bito_calls" as keyof ArmM,     fmt: (v: number | null) => num(v, 1), better: true },
                    { label: "Avg turns",          key: "avg_num_turns" as keyof ArmM,      fmt: (v: number | null) => num(v, 1), better: false },
                    { label: "Avg tool calls",     key: "avg_tool_calls" as keyof ArmM,     fmt: (v: number | null) => num(v, 1), better: false },
                  ] as const
                ).map(({ label, key, fmt, better }) => {
                  const vals = ARMS.map((a) => v(d.arms[a], key));
                  const nums = vals.filter((x): x is number => x != null);
                  const best = nums.length > 1 ? (better ? Math.max(...nums) : Math.min(...nums)) : null;
                  return (
                    <div key={label} style={{ display: "grid", gridTemplateColumns: "140px 1fr 1fr 1fr", gap: 6, fontSize: 12.5, alignItems: "center", marginBottom: 6 }}>
                      <span className="muted">{label}</span>
                      {ARMS.map((a, i) => (
                        <span
                          key={a}
                          style={{
                            fontWeight: best !== null && vals[i] === best ? 800 : 500,
                            color: best !== null && vals[i] === best ? ARM_INFO[a].color : undefined,
                            textAlign: "right",
                          }}
                        >
                          {fmt(vals[i] as any)}
                        </span>
                      ))}
                    </div>
                  );
                })}
                <div style={{ display: "grid", gridTemplateColumns: "140px 1fr 1fr 1fr", gap: 6, fontSize: 11, marginTop: 8 }}>
                  <span />
                  {ARMS.map((a) => (
                    <span key={a} className="faint" style={{ textAlign: "right" }}>Arm {a}</span>
                  ))}
                </div>
              </Card>
              {/* ── Metric glossary ── */}
              <Card title="Metric definitions">
                <div style={{ display: "grid", gap: 6 }}>
                  {[
                    { term: "Completed",       def: "Number of prompts that finished without an error or timeout." },
                    { term: "Completion %",    def: "Percentage of prompts that completed successfully out of total attempted." },
                    { term: "Violations",      def: "Runs where Arm A accidentally used a Bito tool/skill despite the ban — these are flagged and re-run." },
                    { term: "Total spend",     def: "Sum of API costs (USD) across all completed prompts for this arm." },
                    { term: "Cost / answer",   def: "Average API cost per completed prompt. Lower means the arm is cheaper per task." },
                    { term: "Avg turns",       def: "Average number of conversation turns (user + assistant exchanges) per run. Fewer turns generally means a more direct path to the answer." },
                    { term: "Avg tool calls",  def: "Average number of tool invocations per run (Bash, Read, Write, MCP calls, etc. combined). Fewer calls generally means a leaner, more direct investigation." },
                    { term: "Avg time",        def: "Average wall-clock time from prompt submission to final answer. Lower is faster." },
                    { term: "Avg Bito MCP calls",  def: "Average number of BitoAIArchitect MCP tool calls per run. Only Arms B and C make these; Arm A is always 0." },
                    { term: "Avg MCP calls",   def: "Average total MCP tool calls per run across all MCP servers (Bito + any others like GitLab, Jira, etc.)." },
                    { term: "Skills used",     def: "Distinct Bito skills invoked at least once across all runs in this arm (e.g. bito-codebase-explorer, bito-production-triage)." },
                  ].map(({ term, def }) => (
                    <div key={term} style={{ display: "grid", gridTemplateColumns: "160px 1fr", gap: 8, fontSize: 12.5, alignItems: "start" }}>
                      <span style={{ fontWeight: 600, color: "var(--fg)" }}>{term}</span>
                      <span style={{ color: "var(--muted)", lineHeight: 1.5 }}>{def}</span>
                    </div>
                  ))}
                </div>
              </Card>
            </>
          );
        }}
      </Async>

      {preview && (
        <AnswerPreviewModal
          batch={preview.batch}
          arm={preview.arm}
          onClose={() => setPreview(null)}
        />
      )}
    </div>
  );
}
