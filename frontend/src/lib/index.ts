// Shared constants and formatters used across pages.

export const ARMS = ["A", "B", "C"] as const;
export type Arm = (typeof ARMS)[number];

export const ARM_INFO: Record<Arm, { name: string; blurb: string; color: string }> = {
  A: {
    name: "Vanilla tool",
    blurb: "The code-gen tool on its own, without Bito AI Architect.",
    color: "var(--arm-a)",
  },
  B: {
    name: "With Bito MCP + Skill",
    blurb: "Bito AI Architect MCP + the bito-codebase-explorer skill (only that skill).",
    color: "var(--arm-b)",
  },
  C: {
    name: "Bito MCP + all Skills",
    blurb: "Bito AI Architect MCP + free use of the full bito-* skill suite.",
    color: "var(--arm-c)",
  },
};

// The 8 prompt categories (Req #4), with friendly labels + helper text.
export const CATEGORIES = [
  { id: "single-repo", label: "Single-repo task", hint: "A task within one repository." },
  { id: "cross-repo", label: "Cross-repo task", hint: "Spans multiple repositories." },
  { id: "architecture", label: "Architecture planning", hint: "System design / planning." },
  { id: "bug-fix", label: "Bug fix", hint: "Diagnose and fix a defect." },
  { id: "refactor", label: "Refactor", hint: "Restructure without changing behavior." },
  { id: "explanation", label: "Code explanation", hint: "Explain how something works." },
  { id: "test-gen", label: "Test generation", hint: "Write tests for code." },
  { id: "hallucination", label: "Hallucination detection", hint: "Probes for fabricated claims." },
] as const;

export type CategoryId = (typeof CATEGORIES)[number]["id"];

export function categoryLabel(id?: string | null): string {
  if (!id) return "Uncategorized";
  return CATEGORIES.find((c) => c.id === id)?.label ?? id;
}

// Benchmark modes shown in the runner.
export const MODES = [
  { id: "standard", label: "Standard", hint: "Full A/B/C comparison." },
  { id: "quick", label: "Quick", hint: "Fewer turns; faster, cheaper smoke test." },
  { id: "thorough", label: "Thorough", hint: "Higher turn budget for hard tasks." },
] as const;

// ---- formatters ----
export function money(v: number | null | undefined): string {
  if (v == null) return "n/a";
  return v < 1 ? `$${v.toFixed(4)}` : `$${v.toFixed(2)}`;
}

export function secs(ms: number | null | undefined): string {
  if (ms == null) return "n/a";
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
}

export function num(v: number | null | undefined, digits = 0): string {
  if (v == null) return "n/a";
  return v.toLocaleString(undefined, { maximumFractionDigits: digits });
}

export function pct(v: number | null | undefined, digits = 0): string {
  if (v == null) return "n/a";
  return `${(v * 100).toFixed(digits)}%`;
}

export function tokens(v: number | null | undefined): string {
  if (v == null) return "n/a";
  if (v >= 1000) return `${(v / 1000).toFixed(1)}k`;
  return `${v}`;
}

// Render a batch's created_at (ISO "2026-06-19T18:56:57") as a sortable, readable
// timestamp "2026-06-19 18:56:57". Batches come from the API newest-first, so using
// this as the dropdown label keeps lists chronological and easy to scan.
export function fmtTs(iso: string | null | undefined): string {
  if (!iso) return "—";
  return iso.replace("T", " ").slice(0, 19);
}
