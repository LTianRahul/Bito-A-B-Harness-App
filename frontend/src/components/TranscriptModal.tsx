import { api } from "../api/client";
import { useAsync } from "../lib/useAsync";
import { Async, Badge, Modal } from "./ui";

interface Step {
  kind: "thought" | "tool_call" | "tool_result";
  text?: string;
  name?: string;
  skill?: string | null;
  input?: string;
  is_error?: boolean;
}
interface Transcript {
  found: boolean;
  arm: string;
  prompt_id: string;
  prompt: string | null;
  steps: Step[];
  summary: Record<string, any>;
  stream_rel: string | null;
}

// Per-run logs: the full think → tool-call → result trail, so devs can see
// exactly what the model did (not a black box).
export default function TranscriptModal({
  batch,
  arm,
  promptId,
  onClose,
}: {
  batch: string;
  arm: string;
  promptId: string;
  onClose: () => void;
}) {
  const t = useAsync<Transcript>(
    () => api.get(`/runs/${encodeURIComponent(batch)}/transcript?arm=${arm}&prompt_id=${encodeURIComponent(promptId)}`),
    [batch, arm, promptId],
  );

  return (
    <Modal title={`Run logs · Arm ${arm} · ${promptId}`} onClose={onClose}>
      <Async state={t}>
        {(d) =>
          !d.found ? (
            <p className="muted">No transcript found for this run.</p>
          ) : (
            <>
              <div className="row wrap" style={{ gap: 8, marginBottom: 12 }}>
                <Badge kind="neutral">{d.summary.tool_calls ?? 0} tool calls</Badge>
                <Badge kind="info">{d.summary.bito_calls ?? 0} Bito calls</Badge>
                <Badge kind="neutral">{d.summary.num_turns ?? "?"} turns</Badge>
                {d.summary.is_error ? (
                  <Badge kind="err">error{d.summary.api_error_status ? ` ${d.summary.api_error_status}` : ""}</Badge>
                ) : (
                  <Badge kind="ok">ok</Badge>
                )}
                {d.stream_rel && (
                  <a
                    className="btn ghost sm"
                    style={{ marginLeft: "auto" }}
                    href={`/api/runs/${encodeURIComponent(batch)}/transcript/raw?arm=${arm}&prompt_id=${encodeURIComponent(promptId)}`}
                    target="_blank"
                    rel="noreferrer"
                  >
                    ↓ Download raw JSONL
                  </a>
                )}
              </div>

              {d.prompt && (
                <>
                  <div style={{ fontWeight: 600, fontSize: 12, color: "var(--muted)", textTransform: "uppercase", letterSpacing: "0.04em", marginBottom: 6 }}>Prompt</div>
                  <div style={{ background: "var(--surface-raised, var(--surface))", border: "1px solid var(--border)", borderRadius: 6, padding: "10px 12px", fontSize: 12.5, whiteSpace: "pre-wrap", lineHeight: 1.6, marginBottom: 14, color: "var(--fg)" }}>
                    {d.prompt}
                  </div>
                </>
              )}

              <div className="steps">
                {d.steps.map((s, i) => (
                  <div key={i} className={`step ${s.kind}${s.is_error ? " err" : ""}`}>
                    {s.kind === "thought" && (
                      <>
                        <div className="sname">💭 thinking</div>
                        <div className="sbody">{s.text}</div>
                      </>
                    )}
                    {s.kind === "tool_call" && (
                      <>
                        <div className="sname">
                          ▸ {s.name}
                          {s.skill ? ` · ${s.skill}` : ""}
                        </div>
                        {s.input && <div className="sbody">{s.input}</div>}
                      </>
                    )}
                    {s.kind === "tool_result" && (
                      <>
                        <div className="sname">{s.is_error ? "⚠" : "←"} {s.name} result</div>
                        <div className="sbody">{s.text}</div>
                      </>
                    )}
                  </div>
                ))}
              </div>

              {d.summary.result_text && (
                <>
                  <div className="divider" />
                  <div style={{ fontWeight: 600, marginBottom: 6 }}>Final answer</div>
                  <div className="sbody" style={{ whiteSpace: "pre-wrap", fontSize: 13 }}>
                    {d.summary.result_text}
                  </div>
                </>
              )}
            </>
          )
        }
      </Async>
    </Modal>
  );
}
