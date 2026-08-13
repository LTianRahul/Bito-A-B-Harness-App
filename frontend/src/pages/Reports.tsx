import { useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import { useAsync } from "../lib/useAsync";
import { ARMS, fmtTs, money, num, pct, secs } from "../lib";
import { Async, Banner, Card, Empty } from "../components/ui";

interface BatchRow {
  batch_id: string;
  label: string | null;
  status: string;
  created_at: string;
}

const ARM_NAMES: Record<string, string> = { A: "Vanilla tool", B: "With Bito", C: "Bito + Skills" };

export default function Reports({ embedded = false }: { embedded?: boolean }) {
  const batches = useAsync<{ batches: BatchRow[] }>(() => api.get("/runs"));
  const [params, setParams] = useSearchParams();
  const batch = params.get("batch") || "";
  const setBatch = (v: string) => setParams(v ? { batch: v } : {});

  const report = useAsync<any>(
    () => (batch ? api.get(`/reports/${encodeURIComponent(batch)}`) : Promise.resolve(null)),
    [batch],
  );

  return (
    <div className="stack">
      {!embedded && (
        <div className="page-head">
          <h2>Reports</h2>
          <p>A clean, shareable summary of a benchmark session. Export it as HTML or Markdown to share.</p>
        </div>
      )}

      <Async state={batches}>
        {(b) => {
          if (!b.batches.length)
            return (
              <Card>
                <Empty icon="📄" title="No runs to report on yet">
                  Run an A/B test first.
                </Empty>
              </Card>
            );
          return (
            <Card>
              <div className="row wrap">
                <div className="field" style={{ margin: 0, minWidth: 320 }}>
                  <label>Benchmark session</label>
                  <select value={batch} onChange={(e) => setBatch(e.target.value)}>
                    <option value="">Choose a session…</option>
                    {b.batches.map((x) => (
                      <option key={x.batch_id} value={x.batch_id}>
                        {fmtTs(x.created_at)}{x.label ? ` · ${x.label}` : ""} ({x.status})
                      </option>
                    ))}
                  </select>
                </div>
                {batch && (
                  <div className="export-group" style={{ alignSelf: "flex-end" }}>
                    <a
                      className="btn sm export export-md"
                      href={`/api/reports/${encodeURIComponent(batch)}/export.md`}
                      target="_blank"
                      rel="noreferrer"
                    >
                      <span className="export-chip">MD</span> Export Markdown
                    </a>
                    <a
                      className="btn sm export export-html"
                      href={`/api/reports/${encodeURIComponent(batch)}/export.html`}
                      target="_blank"
                      rel="noreferrer"
                    >
                      <span className="export-chip">HTML</span> Export HTML
                    </a>
                  </div>
                )}
              </div>
            </Card>
          );
        }}
      </Async>

      {batch && (
        <Async state={report}>
          {(r) =>
            !r ? null : (
              <>
                {/* Summary cards */}
                <Card title="Summary">
                  <div className="grid grid-3">
                    {ARMS.map((a) => (
                      <div className="card card-pad" key={a}>
                        <span className={`pill ${a.toLowerCase()}`}>Arm {a}</span>
                        <div style={{ fontWeight: 600, marginTop: 6 }}>{ARM_NAMES[a]}</div>
                        <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
                          {pct(r.arms[a].success_rate)} success · {money(r.arms[a].avg_cost)}/answer · {secs(r.arms[a].avg_duration_ms)}
                        </div>
                        <div style={{ marginTop: 8, fontSize: 12 }}>
                          <span className="muted">Skills: </span>
                          {(r.arms[a].skills_used || []).length
                            ? r.arms[a].skills_used.map((s: string) => (
                                <span className="tag" key={s} style={{ marginRight: 3 }}>{s}</span>
                              ))
                            : <span className="faint">none</span>
                          }
                        </div>
                      </div>
                    ))}
                  </div>
                </Card>

                {/* Recommendation */}
                <Card title="Recommendation">
                  <Banner kind={r.recommendation.verdict === "adopt" ? "ok" : "info"}>
                    <span style={{ fontSize: 15 }}>{r.recommendation.text}</span>
                  </Banner>
                </Card>

                {/* Comparison table */}
                <Card title="Arm A / B / C comparison">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>Metric</th>
                        {ARMS.map((a) => <th className="num" key={a}>Arm {a}</th>)}
                      </tr>
                    </thead>
                    <tbody>
                      {(
                        [
                          ["Task success", "success_rate", (v: any) => pct(v), true],
                          ["Avg cost / answer", "avg_cost", money, false],
                          ["Total cost", "total_cost", money, false],
                          ["Avg time / answer", "avg_duration_ms", secs, false],
                          ["Avg output tokens", "avg_output_tokens", (v: any) => num(v, 0), false],
                          ["Avg tool calls", "avg_tool_calls", (v: any) => num(v, 1), false],
                          ["Avg MCP calls", "avg_mcp_calls", (v: any) => num(v, 1), false],
                          ["Bito MCP calls", "avg_bito_calls", (v: any) => num(v, 1), true],
                          ["Avg turns", "avg_num_turns", (v: any) => num(v, 1), false],
                          ["Errors", "n_errors", (v: any) => num(v), false],
                          ["Violations", "n_violations", (v: any) => num(v), false],
                        ] as [string, string, (v: any) => string, boolean][]
                      ).map(([label, key, fmt, higher]) => {
                        const vals = ARMS.map((a) => r.arms[a][key]);
                        const nums = vals.filter((x: any) => typeof x === "number") as number[];
                        const best = nums.length > 1 ? (higher ? Math.max(...nums) : Math.min(...nums)) : null;
                        return (
                          <tr key={key}>
                            <td>{label}</td>
                            {vals.map((vv: any, i: number) => (
                              <td className={`num ${best !== null && vv === best ? "best" : ""}`} key={i}>
                                {fmt(vv)}
                              </td>
                            ))}
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </Card>
              </>
            )
          }
        </Async>
      )}
    </div>
  );
}
