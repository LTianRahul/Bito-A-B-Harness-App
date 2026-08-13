import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError } from "../api/client";
import { useAsync } from "../lib/useAsync";
import { CATEGORIES, categoryLabel } from "../lib";
import { Async, Badge, Banner, Card, Empty, Modal, Spinner } from "../components/ui";

interface PromptT {
  id: string;
  prompt: string;
  title?: string;
  category?: string;
}

const BLANK: PromptT = { id: "", prompt: "", title: "", category: "single-repo" };

export default function Prompts() {
  const state = useAsync<{ prompts: PromptT[] }>(() => api.get("/prompts"));
  const [editing, setEditing] = useState<PromptT | null>(null);
  const [isNew, setIsNew] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [importText, setImportText] = useState("");
  const [importReplace, setImportReplace] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  // AI generation
  const [genOpen, setGenOpen] = useState(false);
  const [genTopic, setGenTopic] = useState("");
  const [genCount, setGenCount] = useState(6);
  const [genGround, setGenGround] = useState(true);
  const [generating, setGenerating] = useState(false);
  const [genResults, setGenResults] = useState<PromptT[] | null>(null);
  const [genErr, setGenErr] = useState<string | null>(null);

  // prompt sets
  const sets = useAsync<{ sets: { name: string; count: number }[] }>(() => api.get("/prompt-sets"));
  const [setName, setSetName] = useState("");

  function flash(ok: boolean, text: string) {
    setMsg({ ok, text });
    setTimeout(() => setMsg(null), 4000);
  }

  async function save() {
    if (!editing) return;
    setBusy(true);
    try {
      const body = { ...editing, title: editing.title || undefined, category: editing.category || undefined };
      const r = isNew
        ? await api.post<{ prompts: PromptT[] }>("/prompts", body)
        : await api.put<{ prompts: PromptT[] }>(`/prompts/${encodeURIComponent(editing.id)}`, body);
      state.setData(r);
      setEditing(null);
      flash(true, isNew ? "Prompt added." : "Prompt saved.");
    } catch (e) {
      flash(false, e instanceof ApiError ? String(e.detail) : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function remove(pid: string) {
    if (!confirm("Delete this prompt?")) return;
    const r = await api.del<{ prompts: PromptT[] }>(`/prompts/${encodeURIComponent(pid)}`);
    state.setData(r);
    flash(true, "Prompt deleted.");
  }

  async function dup(pid: string) {
    const r = await api.post<{ prompts: PromptT[] }>(`/prompts/${encodeURIComponent(pid)}/duplicate`);
    state.setData(r);
    flash(true, "Prompt duplicated.");
  }

  function exportJson(items: PromptT[]) {
    const blob = new Blob([JSON.stringify(items, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "prompts.json";
    a.click();
    URL.revokeObjectURL(url);
  }

  async function doImport() {
    setBusy(true);
    try {
      const parsed = JSON.parse(importText);
      const arr = Array.isArray(parsed) ? parsed : parsed.prompts;
      const r = await api.post<{ prompts: PromptT[] }>("/prompts/import", {
        prompts: arr,
        replace: importReplace,
      });
      state.setData(r);
      setImportOpen(false);
      setImportText("");
      flash(true, `Imported ${arr.length} prompt(s).`);
    } catch (e) {
      flash(false, e instanceof ApiError ? String(e.detail) : "Could not parse JSON. " + String(e));
    } finally {
      setBusy(false);
    }
  }

  async function generate() {
    setGenerating(true);
    setGenErr(null);
    setGenResults(null);
    const ctrl = new AbortController();
    const tid = setTimeout(() => ctrl.abort(), 330_000); // 5.5 min client-side guard
    try {
      const r = await fetch("/api/prompts/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ topic: genTopic.trim() || null, count: genCount, ground: genGround }),
        signal: ctrl.signal,
      });
      clearTimeout(tid);
      if (!r.ok) {
        const text = await r.text();
        const data = text ? JSON.parse(text) : null;
        throw new ApiError(r.status, (data && (data.detail ?? data)) ?? r.statusText);
      }
      const data = await r.json();
      setGenResults(data.prompts);
    } catch (e: unknown) {
      clearTimeout(tid);
      if (e instanceof Error && e.name === "AbortError") {
        setGenErr("Request timed out — the model took too long. Try again with fewer prompts.");
      } else {
        setGenErr(e instanceof ApiError ? String(e.detail) : String(e));
      }
    } finally {
      setGenerating(false);
    }
  }

  async function addGenerated() {
    if (!genResults?.length) return;
    setBusy(true);
    try {
      const r = await api.post<{ prompts: PromptT[] }>("/prompts/import", {
        prompts: genResults,
        replace: false,
      });
      state.setData(r);
      setGenOpen(false);
      setGenResults(null);
      setGenTopic("");
      flash(true, `Added ${genResults.length} generated prompt(s).`);
    } catch (e) {
      flash(false, String(e));
    } finally {
      setBusy(false);
    }
  }

  function onFile(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    if (!f) return;
    f.text().then((t) => {
      setImportText(t);
      setImportOpen(true);
    });
    e.target.value = "";
  }

  async function saveSet(items: PromptT[]) {
    if (!setName.trim()) return flash(false, "Name your prompt set first.");
    try {
      await api.post("/prompt-sets", { name: setName.trim(), prompts: items });
      setSetName("");
      sets.reload();
      flash(true, "Prompt set saved.");
    } catch (e) {
      flash(false, String(e));
    }
  }

  async function loadSet(name: string) {
    const r = await api.get<{ prompts: PromptT[] }>(`/prompt-sets/${encodeURIComponent(name)}`);
    const imp = await api.post<{ prompts: PromptT[] }>("/prompts/import", {
      prompts: r.prompts,
      replace: true,
    });
    state.setData(imp);
    flash(true, `Loaded set “${name}” (${r.prompts.length} prompts).`);
  }

  return (
    <div className="stack">
      <div className="page-head" style={{ display: "flex", alignItems: "flex-start", gap: 16 }}>
        <div style={{ flex: 1 }}>
          <h2>Prompts</h2>
          <p>
            These are the tasks each benchmark runs. Organize them by category, import an existing
            set, or build your own. Aim them at repositories Bito has indexed.
          </p>
        </div>
        <Link className="btn primary" to="/run" style={{ marginTop: 4, whiteSpace: "nowrap" }}>
          Run A/B test →
        </Link>
      </div>

      {msg && <Banner kind={msg.ok ? "ok" : "err"}>{msg.text}</Banner>}

      <Card>
        <div className="row wrap">
          <button
            className="btn primary"
            onClick={() => {
              setEditing({ ...BLANK });
              setIsNew(true);
            }}
          >
            + Add prompt
          </button>
          <button className="btn" onClick={() => { setGenOpen(true); setGenResults(null); setGenErr(null); }}>
            ✨ Generate with AI
          </button>
          <button className="btn" onClick={() => fileRef.current?.click()}>
            ↑ Import JSON
          </button>
          <input ref={fileRef} type="file" accept="application/json" hidden onChange={onFile} />
          <button className="btn" onClick={() => state.data && exportJson(state.data.prompts)}>
            ↓ Export JSON
          </button>
          <div className="spacer" />
          <Async state={sets}>
            {(s) => (
              <div className="row">
                <input
                  type="text"
                  placeholder="Save current as set…"
                  value={setName}
                  style={{ width: 180 }}
                  onChange={(e) => setSetName(e.target.value)}
                />
                <button className="btn sm" onClick={() => state.data && saveSet(state.data.prompts)}>
                  Save set
                </button>
                {s.sets.length > 0 && (
                  <select
                    defaultValue=""
                    onChange={(e) => e.target.value && loadSet(e.target.value)}
                    style={{ width: 170 }}
                  >
                    <option value="">Load a set…</option>
                    {s.sets.map((x) => (
                      <option key={x.name} value={x.name}>
                        {x.name} ({x.count})
                      </option>
                    ))}
                  </select>
                )}
              </div>
            )}
          </Async>
        </div>
      </Card>

      <Async state={state}>
        {(data) => {
          if (!data.prompts.length)
            return (
              <Card>
                <Empty icon="✎" title="No prompts yet">
                  Add one, or import a JSON file to get started.
                </Empty>
              </Card>
            );

          // Group by category (in CATEGORIES order, then uncategorized).
          const m = new Map<string, PromptT[]>();
          for (const p of data.prompts) {
            const k = p.category || "_none";
            if (!m.has(k)) m.set(k, []);
            m.get(k)!.push(p);
          }
          const groups: [string, PromptT[]][] = [];
          for (const c of CATEGORIES) if (m.has(c.id)) groups.push([c.id, m.get(c.id)!]);
          if (m.has("_none")) groups.push(["_none", m.get("_none")!]);

          return (
            <>
              <div className="muted" style={{ fontSize: 13 }}>
                {data.prompts.length} prompt{data.prompts.length === 1 ? "" : "s"} across{" "}
                {groups.length} categor{groups.length === 1 ? "y" : "ies"}.
              </div>
              {groups.map(([cat, items]) => (
                <Card
                  key={cat}
                  title={cat === "_none" ? "Uncategorized" : categoryLabel(cat)}
                  sub={`${items.length} prompt${items.length === 1 ? "" : "s"}`}
                  pad={false}
                >
                  <table className="table">
                    <tbody>
                      {items.map((p) => (
                        <tr key={p.id}>
                          <td style={{ width: 150 }}>
                            <span className="mono faint">{p.id}</span>
                          </td>
                          <td>
                            {p.title && <div style={{ fontWeight: 600 }}>{p.title}</div>}
                            <div className="muted" style={{ fontSize: 12.5 }}>
                              {p.prompt.length > 160 ? p.prompt.slice(0, 160) + "…" : p.prompt}
                            </div>
                          </td>
                          <td style={{ width: 170, textAlign: "right", whiteSpace: "nowrap" }}>
                            <button
                              className="btn ghost sm"
                              onClick={() => {
                                setEditing({ ...p });
                                setIsNew(false);
                              }}
                            >
                              Edit
                            </button>
                            <button className="btn ghost sm" onClick={() => dup(p.id)}>
                              Duplicate
                            </button>
                            <button className="btn ghost sm" onClick={() => remove(p.id)}>
                              Delete
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </Card>
              ))}
            </>
          );
        }}
      </Async>

      {/* Add / edit modal */}
      {editing && (
        <Modal
          title={isNew ? "Add prompt" : "Edit prompt"}
          onClose={() => setEditing(null)}
          footer={
            <>
              <button className="btn" onClick={() => setEditing(null)}>
                Cancel
              </button>
              <button className="btn primary" onClick={save} disabled={busy}>
                {busy ? <Spinner /> : null} Save
              </button>
            </>
          }
        >
          <div className="field">
            <label>Title (optional)</label>
            <input
              type="text"
              value={editing.title ?? ""}
              placeholder="Short name for this task"
              onChange={(e) => setEditing({ ...editing, title: e.target.value })}
            />
          </div>
          <div className="row" style={{ gap: 16 }}>
            <div className="field" style={{ flex: 1 }}>
              <label>Category</label>
              <select
                value={editing.category ?? ""}
                onChange={(e) => setEditing({ ...editing, category: e.target.value })}
              >
                <option value="">Uncategorized</option>
                {CATEGORIES.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.label}
                  </option>
                ))}
              </select>
            </div>
            <div className="field" style={{ flex: 1 }}>
              <label>ID {isNew && <span className="faint">(auto if blank)</span>}</label>
              <input
                type="text"
                value={editing.id}
                placeholder="auto-generated"
                onChange={(e) => setEditing({ ...editing, id: e.target.value })}
              />
            </div>
          </div>
          <div className="field">
            <label>Prompt</label>
            <div className="help">The task or question to send to the tool.</div>
            <textarea
              rows={6}
              value={editing.prompt}
              onChange={(e) => setEditing({ ...editing, prompt: e.target.value })}
            />
          </div>
        </Modal>
      )}

      {/* Generate modal */}
      {genOpen && (
        <Modal
          title="Generate prompts with AI"
          onClose={() => setGenOpen(false)}
          footer={
            <>
              <button className="btn" onClick={() => setGenOpen(false)}>Close</button>
              {genResults ? (
                <button className="btn primary" onClick={addGenerated} disabled={busy || !genResults.length}>
                  {busy ? <Spinner /> : null} Add {genResults.length} prompt(s)
                </button>
              ) : (
                <button className="btn primary" onClick={generate} disabled={generating}>
                  {generating ? <Spinner /> : "✨"} Generate
                </button>
              )}
            </>
          }
        >
          {!genResults ? (
            <>
              <div className="field">
                <label>Topic / focus (optional)</label>
                <div className="help">e.g. “authentication and billing” — leave blank for a broad set.</div>
                <input type="text" value={genTopic} placeholder="What should the tasks be about?" onChange={(e) => setGenTopic(e.target.value)} />
              </div>
              <div className="row" style={{ gap: 16 }}>
                <div className="field" style={{ width: 130 }}>
                  <label>How many</label>
                  <input type="number" min={1} max={20} value={genCount} onChange={(e) => setGenCount(Math.max(1, Math.min(20, +e.target.value)))} />
                </div>
                <div className="field" style={{ flex: 1 }}>
                  <label>Grounding</label>
                  <label className="row" style={{ gap: 8, marginTop: 4 }}>
                    <input type="checkbox" checked={genGround} style={{ width: "auto" }} onChange={(e) => setGenGround(e.target.checked)} />
                    Use my indexed repositories (recommended)
                  </label>
                </div>
              </div>
              {generating && <p className="muted"><Spinner /> Drafting prompts… this makes one model call.</p>}
              {genErr && <Banner kind="err" title="Couldn’t generate">{genErr}</Banner>}
            </>
          ) : (
            <>
              <p className="muted" style={{ marginBottom: 10 }}>
                Review these {genResults.length} drafts. Click <strong>Add</strong> to append them to your prompts.
              </p>
              <div className="stack" style={{ gap: 10 }}>
                {genResults.map((p, i) => (
                  <div key={i} className="card card-pad">
                    <div className="row" style={{ marginBottom: 4 }}>
                      <span className="tag">{categoryLabel(p.category)}</span>
                      {p.title && <span style={{ fontWeight: 600 }}>{p.title}</span>}
                    </div>
                    <div className="muted" style={{ fontSize: 12.5 }}>{p.prompt}</div>
                  </div>
                ))}
              </div>
            </>
          )}
        </Modal>
      )}

      {/* Import modal */}
      {importOpen && (
        <Modal
          title="Import prompts from JSON"
          onClose={() => setImportOpen(false)}
          footer={
            <>
              <label className="row" style={{ marginRight: "auto", gap: 6 }}>
                <input
                  type="checkbox"
                  checked={importReplace}
                  style={{ width: "auto" }}
                  onChange={(e) => setImportReplace(e.target.checked)}
                />
                Replace all existing prompts
              </label>
              <button className="btn" onClick={() => setImportOpen(false)}>
                Cancel
              </button>
              <button className="btn primary" onClick={doImport} disabled={busy}>
                {busy ? <Spinner /> : null} Import
              </button>
            </>
          }
        >
          <div className="field">
            <div className="help">
              Paste a JSON array of <span className="mono">{`{ "id", "prompt", "category?" }`}</span>{" "}
              objects. By default, imported prompts merge with (and update) your current list.
            </div>
            <textarea
              rows={12}
              value={importText}
              placeholder='[ { "id": "q01", "prompt": "How does billing work?", "category": "single-repo" } ]'
              onChange={(e) => setImportText(e.target.value)}
            />
          </div>
        </Modal>
      )}
    </div>
  );
}
