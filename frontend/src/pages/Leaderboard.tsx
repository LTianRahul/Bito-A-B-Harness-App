import { useState } from "react";
import { api } from "../api/client";
import { useAsync } from "../lib/useAsync";
import { ARM_INFO, categoryLabel, money, num, pct, secs, tokens, type Arm } from "../lib";
import { Async, Card, Empty } from "../components/ui";

interface Entry {
  tool: string;
  arm: string;
  n: number;
  n_compared: number;
  success_rate: number | null;
  total_cost: number | null;
  avg_cost: number | null;
  total_duration_ms: number | null;
  avg_duration_ms: number | null;
  total_output_tokens: number | null;
  avg_output_tokens: number | null;
  token_efficiency: number | null;
  avg_bito_calls: number | null;
  avg_mcp_calls: number | null;
  avg_num_turns: number | null;
}

function calcImprovement(armAVal: number | null, armBVal: number | null, armCVal: number | null, thisVal: number | null): number | null {
  if (armAVal == null || armBVal == null || armCVal == null || thisVal == null) return null;
  if (armAVal <= armBVal && armAVal <= armCVal) return null; // Arm A is already the best — no badge
  if (thisVal >= armAVal) return null; // this arm is not better than baseline
  return Math.round(((armAVal - thisVal) / armAVal) * 100);
}

function ImprovementBadge({ pct, label, icon = "↓" }: { pct: number; label: string; icon?: string }) {
  return (
    <span style={{ fontSize: 11, fontWeight: 600, color: "#16a34a", marginLeft: 6, whiteSpace: "nowrap" }}>
      {icon} {pct}% {label}
    </span>
  );
}
interface Pick {
  tool: string;
  arm: string;
  value: number;
  tie?: boolean;
  tied_arms?: string[];
}
interface LbResp {
  entries: Entry[];
  best_by: Record<string, Pick | null>;
  n_runs: number;
}

const CHIPS: { key: string; label: string; icon: string; fmt: (v: number) => string; metricKey: keyof Entry; badgeLabel: string; badgeIcon?: string }[] = [
  { key: "cost",  label: "Lowest cost",  icon: "$",  fmt: money,             metricKey: "avg_cost",        badgeLabel: "lower cost" },
  { key: "speed", label: "Fastest",      icon: "⚡", fmt: secs,              metricKey: "avg_duration_ms", badgeLabel: "faster",      badgeIcon: "⚡" },
  { key: "turns", label: "Fewest turns", icon: "↩",  fmt: (v) => num(v, 1), metricKey: "avg_num_turns",   badgeLabel: "fewer turns" },
];

export default function Leaderboard({ embedded = false }: { embedded?: boolean }) {
  const fil = useAsync<{ tools: string[]; repos: string[]; categories: string[] }>(
    () => api.get("/leaderboard/filters"),
  );
  const [tool, setTool] = useState("");
  const [repo, setRepo] = useState("");
  const [category, setCategory] = useState("");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");

  const qs = new URLSearchParams();
  if (tool) qs.set("tool", tool);
  if (repo) qs.set("repo", repo);
  if (category) qs.set("category", category);
  if (dateFrom) qs.set("date_from", dateFrom);
  if (dateTo) qs.set("date_to", dateTo);
  const query = qs.toString();

  const data = useAsync<LbResp>(() => api.get(`/leaderboard${query ? `?${query}` : ""}`), [query]);

  return (
    <div className="stack">
      {!embedded && (
        <div className="page-head">
          <h2>Leaderboard</h2>
          <p>Compare every arm across your runs. Filter by tool, repository, prompt type, or date.</p>
        </div>
      )}

      <Async state={fil}>
        {(f) => (
          <Card>
            <div className="row wrap" style={{ gap: 14 }}>
              <Filter label="Tool" value={tool} set={setTool} options={f.tools} />
              <Filter label="Repository" value={repo} set={setRepo} options={f.repos} />
              <Filter
                label="Prompt type"
                value={category}
                set={setCategory}
                options={f.categories}
                render={categoryLabel}
              />
              <div className="field" style={{ margin: 0 }}>
                <label>From</label>
                <input type="date" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)} />
              </div>
              <div className="field" style={{ margin: 0 }}>
                <label>To</label>
                <input type="date" value={dateTo} onChange={(e) => setDateTo(e.target.value)} />
              </div>
              {query && (
                <button
                  className="btn ghost"
                  style={{ alignSelf: "flex-end" }}
                  onClick={() => {
                    setTool(""); setRepo(""); setCategory(""); setDateFrom(""); setDateTo("");
                  }}
                >
                  Clear filters
                </button>
              )}
            </div>
          </Card>
        )}
      </Async>

      <Async state={data}>
        {(d) => {
          if (d.n_runs === 0)
            return (
              <Card>
                <Empty icon="🏆" title="Nothing to rank yet">
                  Run an A/B test to populate the leaderboard.
                </Empty>
              </Card>
            );

          const armLabel = (p: Pick) => `Arm ${p.arm} · ${p.tool}`;
          const entryFor = (arm: string) => d.entries.find((e) => e.arm === arm);
          const armA = entryFor("A");
          const armB = entryFor("B");
          const armC = entryFor("C");
          return (
            <>
              <div className="faint" style={{ fontSize: 12, marginBottom: 4 }}>
                All values are averages per completed prompt across all runs for that arm.
              </div>
              <div className="grid grid-3">
                {CHIPS.map((c) => {
                  const p = d.best_by[c.key];
                  const badge = (p && p.arm !== "A" && !p.tie)
                    ? calcImprovement(
                        armA ? (armA[c.metricKey] as number | null) : null,
                        armB ? (armB[c.metricKey] as number | null) : null,
                        armC ? (armC[c.metricKey] as number | null) : null,
                        p.value,
                      )
                    : null;
                  return (
                    <Card key={c.key} title={c.label}>
                      {p ? (
                        <div className="row">
                          <span
                            className={`pill ${p.tie ? "tie" : p.arm.toLowerCase()}`}
                            style={{ fontSize: 14, padding: "4px 12px" }}
                          >
                            {c.icon} {p.tie ? `Tie (${(p.tied_arms || []).join("/")})` : `Arm ${p.arm}`}
                          </span>
                          <div>
                            <div style={{ fontWeight: 720, fontSize: 18 }}>{c.fmt(p.value)}</div>
                            <div className="faint" style={{ fontSize: 11.5 }}>
                              {p.tie ? "all arms equal" : p.tool}
                            </div>
                            {badge !== null && (
                              <ImprovementBadge pct={badge} label={c.badgeLabel} icon={c.badgeIcon} />
                            )}
                          </div>
                        </div>
                      ) : (
                        <span className="faint">n/a</span>
                      )}
                    </Card>
                  );
                })}
              </div>

              <Card title="All entries" sub={`${d.entries.length} arm(s) across ${d.n_runs} runs. Cost & time are compared only on prompts where the baseline (Arm A) succeeded. Totals shown alongside averages; "best" ranks by average.`}>
                <LbTable entries={d.entries} />
              </Card>
            </>
          );
        }}
      </Async>
    </div>
  );
}

function Filter({
  label,
  value,
  set,
  options,
  render,
}: {
  label: string;
  value: string;
  set: (v: string) => void;
  options: string[];
  render?: (v: string) => string;
}) {
  return (
    <div className="field" style={{ margin: 0, minWidth: 150 }}>
      <label>{label}</label>
      <select value={value} onChange={(e) => set(e.target.value)}>
        <option value="">All</option>
        {options.map((o) => (
          <option key={o} value={o}>
            {render ? render(o) : o}
          </option>
        ))}
      </select>
    </div>
  );
}

function LbTable({ entries }: { entries: Entry[] }) {
  // Determine best per column.
  // `noBest` columns (the totals, and the compared count) are shown for context but
  // not highlighted as a "winner": lowest total just means fewest completed runs.
  const cols: { key: keyof Entry; label: string; fmt: (v: any) => string; higher: boolean; noBest?: boolean }[] = [
    { key: "success_rate", label: "Success", fmt: (v) => pct(v), higher: true },
    { key: "n_compared", label: "Compared", fmt: (v) => num(v, 0), higher: true, noBest: true },
    { key: "total_cost", label: "Total cost", fmt: money, higher: false, noBest: true },
    { key: "avg_cost", label: "Avg cost", fmt: money, higher: false },
    { key: "total_duration_ms", label: "Total time", fmt: secs, higher: false, noBest: true },
    { key: "avg_duration_ms", label: "Avg time", fmt: secs, higher: false },
    { key: "avg_output_tokens", label: "Avg tokens", fmt: tokens, higher: false },
    { key: "token_efficiency", label: "Tok-eff", fmt: (v) => num(v, 2), higher: true },
    { key: "avg_num_turns", label: "Avg turns", fmt: (v) => num(v, 1), higher: false },
    { key: "avg_bito_calls", label: "Bito MCP calls", fmt: (v) => num(v, 1), higher: true },
    { key: "avg_mcp_calls", label: "MCP calls", fmt: (v) => num(v, 1), higher: true },
  ];
  const bestVal: Record<string, number | null> = {};
  for (const c of cols) {
    if (c.noBest) { bestVal[c.key as string] = null; continue; }
    const nums = entries.map((e) => e[c.key]).filter((x): x is number => typeof x === "number");
    bestVal[c.key as string] = nums.length ? (c.higher ? Math.max(...nums) : Math.min(...nums)) : null;
  }

  return (
    <table className="table">
      <thead>
        <tr>
          <th>Tool</th>
          <th>Arm</th>
          <th className="num">Runs</th>
          {cols.map((c) => (
            <th className="num" key={c.key as string}>
              {c.label}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {entries.map((e, i) => (
          <tr key={i}>
            <td>{e.tool}</td>
            <td>
              <span className={`pill ${e.arm.toLowerCase()}`}>{e.arm}</span>{" "}
              <span className="faint" style={{ fontSize: 11 }}>{ARM_INFO[e.arm as Arm]?.name}</span>
            </td>
            <td className="num">{e.n}</td>
            {cols.map((c) => {
              const val = e[c.key];
              const isBest =
                typeof val === "number" && bestVal[c.key as string] === val && entries.length > 1;
              return (
                <td className={`num ${isBest ? "best" : ""}`} key={c.key as string}>
                  {c.fmt(val)}
                </td>
              );
            })}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
