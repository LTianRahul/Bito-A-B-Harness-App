// Small reusable presentational components shared across pages.
import React from "react";

export type StatusKind = "ok" | "warn" | "err" | "info" | "neutral";

export function Badge({ kind = "neutral", children }: { kind?: StatusKind; children: React.ReactNode }) {
  return (
    <span className={`badge ${kind}`}>
      <span className="dot" />
      {children}
    </span>
  );
}

export function Card({
  title,
  sub,
  right,
  children,
  pad = true,
}: {
  title?: string;
  sub?: string;
  right?: React.ReactNode;
  children: React.ReactNode;
  pad?: boolean;
}) {
  return (
    <div className="card">
      {title && (
        <div className="card-head">
          <div>
            <h3>{title}</h3>
            {sub && <div className="sub">{sub}</div>}
          </div>
          {right && <div style={{ marginLeft: "auto" }}>{right}</div>}
        </div>
      )}
      <div className={pad ? "card-pad" : undefined}>{children}</div>
    </div>
  );
}

export function Banner({
  kind = "info",
  title,
  children,
}: {
  kind?: "info" | "warn" | "err" | "ok";
  title?: string;
  children?: React.ReactNode;
}) {
  const icon = { info: "ℹ", warn: "⚠", err: "✕", ok: "✓" }[kind];
  return (
    <div className={`banner ${kind}`}>
      <span aria-hidden>{icon}</span>
      <div>
        {title && <div className="b-title">{title}</div>}
        {children}
      </div>
    </div>
  );
}

export function Empty({ icon = "○", title, children }: { icon?: string; title: string; children?: React.ReactNode }) {
  return (
    <div className="empty">
      <div className="ico">{icon}</div>
      <div style={{ fontWeight: 650, color: "var(--text-muted)" }}>{title}</div>
      {children && <div style={{ marginTop: 6 }}>{children}</div>}
    </div>
  );
}

export function Spinner() {
  return <span className="spinner" />;
}

export function Progress({ value, total }: { value: number; total: number }) {
  const p = total > 0 ? Math.min(100, (value / total) * 100) : 0;
  return (
    <div className="progress-track">
      <div className="progress-fill" style={{ width: `${p}%` }} />
    </div>
  );
}

export function Modal({
  title,
  onClose,
  children,
  footer,
}: {
  title: string;
  onClose: () => void;
  children: React.ReactNode;
  footer?: React.ReactNode;
}) {
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h3>{title}</h3>
          <button className="btn ghost sm" style={{ marginLeft: "auto" }} onClick={onClose}>
            ✕
          </button>
        </div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-foot">{footer}</div>}
      </div>
    </div>
  );
}

// Loading / error wrapper for async page sections.
export function Async<T>({
  state,
  children,
}: {
  state: { loading: boolean; error?: string | null; data?: T };
  children: (data: T) => React.ReactNode;
}) {
  if (state.loading) return <div className="row" style={{ padding: 30, justifyContent: "center" }}><Spinner /></div>;
  if (state.error) return <Banner kind="err" title="Something went wrong">{state.error}</Banner>;
  if (state.data === undefined) return null;
  return <>{children(state.data)}</>;
}
