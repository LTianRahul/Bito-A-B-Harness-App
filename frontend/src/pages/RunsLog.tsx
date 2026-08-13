import { useState } from "react";
import { api } from "../api/client";
import { useAsync } from "../lib/useAsync";
import { fmtTs, money, secs } from "../lib";
import { Async, Badge, Card, Empty } from "../components/ui";
import TranscriptModal from "../components/TranscriptModal";

interface RunRow {
  batch_id: string;
  arm: string;
  base_prompt_id: string;
  tool: string;
  exit_code: number;
  error: string | null;
  total_cost_usd: number | null;
  duration_ms: number | null;
  num_turns: number | null;
  started_at: string | null;
  ok: boolean;
  fail_kind: string | null;
  response_preview: string | null;
}
interface BatchRow {
  batch_id: string;
  label: string | null;
  status: string;
  created_at: string;
}

// Logs / Runs explorer: every run with its status, error, and a click-through
// to the full transcript — so devs can debug failures (not a black box).
export default function RunsLog() {
  const batches = useAsync<{ batches: BatchRow[] }>(() => api.get("/runs"));
  const [batch, setBatch] = useState("");
  const [failuresOnly, setFailuresOnly] = useState(false);
  const [open, setOpen] = useState<{ batch: string; arm: string; pid: string } | null>(null);

  const qs = new URLSearchParams();
  if (batch) qs.set("batch", batch);
  if (failuresOnly) qs.set("failures", "true");
  const data = useAsync<{ rows: RunRow[] }>(
    () => api.get(`/runlog${qs.toString() ? `?${qs}` : ""}`),
    [batch, failuresOnly],
  );

  return (
    <div className="stack">
      <Async state={batches}>
        {(b) => (
          <div className="row wrap">
            <label style={{ fontWeight: 600, fontSize: 13 }}>Session</label>
            <select value={batch} style={{ width: 300 }} onChange={(e) => setBatch(e.target.value)}>
              <option value="">All sessions</option>
              {b.batches.map((x) => (
                <option key={x.batch_id} value={x.batch_id}>
                  {fmtTs(x.created_at)}{x.label ? ` · ${x.label}` : ""} ({x.status})
                </option>
              ))}
            </select>
            <label className="row" style={{ gap: 6, fontSize: 13 }}>
              <input type="checkbox" checked={failuresOnly} style={{ width: "auto" }} onChange={(e) => setFailuresOnly(e.target.checked)} />
              Only failures
            </label>
          </div>
        )}
      </Async>

      <Async state={data}>
        {(d) =>
          d.rows.length === 0 ? (
            <Card>
              <Empty icon="🧾" title={failuresOnly ? "No failures 🎉" : "No runs yet"}>
                {failuresOnly ? "Every run completed cleanly." : "Run an A/B test to see logs here."}
              </Empty>
            </Card>
          ) : (
            <Card title="Runs" sub="Click any run to open its full transcript (tool calls, decisions, errors).">
              <table className="table">
                <thead>
                  <tr>
                    <th>Session</th>
                    <th>Arm</th>
                    <th>Prompt</th>
                    <th>Status</th>
                    <th className="num">Cost</th>
                    <th className="num">Time</th>
                    <th>Detail</th>
                  </tr>
                </thead>
                <tbody>
                  {d.rows.map((r, i) => (
                    <tr
                      key={i}
                      style={{ cursor: "pointer" }}
                      onClick={() => setOpen({ batch: r.batch_id, arm: r.arm, pid: r.base_prompt_id })}
                    >
                      <td className="mono faint" style={{ fontSize: 11 }}>{r.batch_id}</td>
                      <td><span className={`pill ${r.arm.toLowerCase()}`}>{r.arm}</span></td>
                      <td className="mono">{r.base_prompt_id}</td>
                      <td>
                        {r.ok ? (
                          <Badge kind="ok">ok</Badge>
                        ) : r.fail_kind === "usage_limit" ? (
                          <Badge kind="warn">usage limit</Badge>
                        ) : (
                          <Badge kind="err">error</Badge>
                        )}
                      </td>
                      <td className="num">{money(r.total_cost_usd)}</td>
                      <td className="num">{secs(r.duration_ms)}</td>
                      <td className="muted" style={{ fontSize: 12, maxWidth: 320 }}>
                        {r.ok ? (r.response_preview || "—") : (r.error || r.response_preview || "—")}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>
          )
        }
      </Async>

      {open && (
        <TranscriptModal batch={open.batch} arm={open.arm} promptId={open.pid} onClose={() => setOpen(null)} />
      )}
    </div>
  );
}
