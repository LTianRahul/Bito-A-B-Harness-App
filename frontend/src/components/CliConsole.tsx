import { useEffect, useRef, useState } from "react";
import { api, ApiError, subscribe } from "../api/client";
import { useAsync } from "../lib/useAsync";
import { Async, Badge, Spinner } from "./ui";

interface Ref {
  reference: { cmd: string; desc: string }[];
  allowed: string[];
}

// A working command line: run the harness's own subcommands and stream output,
// the same as a terminal, for devs who want direct control.
export default function CliConsole() {
  const ref = useAsync<Ref>(() => api.get("/cli/reference"));
  const [cmd, setCmd] = useState("doctor");
  const [lines, setLines] = useState<string[]>([]);
  const [running, setRunning] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const unsub = useRef<null | (() => void)>(null);
  const logRef = useRef<HTMLDivElement>(null);

  useEffect(() => () => unsub.current?.(), []);
  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [lines]);

  async function run() {
    setErr(null);
    setLines([]);
    setRunning(true);
    try {
      const r = await api.post<{ id: string }>("/cli/exec", { args: cmd });
      setJobId(r.id);
      unsub.current?.();
      unsub.current = subscribe(`/cli/${r.id}/events`, (ev: any) => {
        if (ev.line !== undefined) setLines((p) => [...p, ev.line]);
        if (ev.done) {
          setRunning(false);
        }
      });
    } catch (e) {
      setErr(e instanceof ApiError ? String(e.detail) : String(e));
      setRunning(false);
    }
  }

  async function stop() {
    if (jobId) await api.post(`/cli/${jobId}/stop`);
  }

  return (
    <div className="stack">
      <Async state={ref}>
        {(d) => (
          <>
            <div className="muted" style={{ fontSize: 12.5 }}>
              Runs <span className="mono">python harness.py &lt;command&gt;</span> on this machine and
              streams the output. Allowed: {d.allowed.map((a) => <span className="tag" key={a} style={{ marginRight: 4 }}>{a}</span>)}
            </div>

            <div className="row">
              <span className="mono faint" style={{ fontSize: 13 }}>harness.py</span>
              <input
                type="text"
                value={cmd}
                onChange={(e) => setCmd(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && !running && run()}
                placeholder="doctor"
                style={{ fontFamily: "var(--mono)", flex: 1 }}
              />
              {running ? (
                <button className="btn danger" onClick={stop}>■ Stop</button>
              ) : (
                <button className="btn primary" onClick={run}>▶ Run</button>
              )}
            </div>

            {err && <div className="banner err">{err}</div>}

            {(lines.length > 0 || running) && (
              <div className="log" ref={logRef}>
                {lines.join("\n")}
                {running && <div className="row" style={{ marginTop: 6 }}><Spinner /></div>}
              </div>
            )}

            <div>
              <div className="faint" style={{ fontSize: 11.5, textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 6 }}>
                Common commands
              </div>
              <table className="table">
                <tbody>
                  {d.reference.map((r) => (
                    <tr key={r.cmd}>
                      <td className="mono" style={{ width: 320 }}>harness.py {r.cmd}</td>
                      <td className="muted" style={{ fontSize: 12.5 }}>{r.desc}</td>
                      <td style={{ textAlign: "right" }}>
                        <button className="btn ghost sm" onClick={() => setCmd(r.cmd)}>Use</button>
                        <button
                          className="btn ghost sm"
                          onClick={() => navigator.clipboard?.writeText(`python harness.py ${r.cmd}`)}
                        >
                          Copy
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </Async>
    </div>
  );
}
