import type { StatusKind } from "../components/ui";

// MCP state → badge appearance + friendly label + next-step hint.
export const MCP_STATE: Record<string, { kind: StatusKind; label: string }> = {
  // "configured" = a Bito server entry exists in the config. That is NOT proof the
  // token authenticates, so we label it "Configured" (not "Connected"). Live
  // connection state is shown separately from the /health + /validate probes.
  configured: { kind: "ok", label: "Configured" },
  missing: { kind: "neutral", label: "Not set up" },
  invalid: { kind: "err", label: "Needs fixing" },
  "needs-auth": { kind: "warn", label: "Needs sign-in" },
};

export function mcpBadge(state: string) {
  return MCP_STATE[state] ?? { kind: "neutral" as StatusKind, label: state };
}

export interface McpStatus {
  state: string;
  server_key: string | null;
  url: string | null;
  workspace_id: string | null;
  via_one_command: boolean;
  auth_kind: string | null;
  detail: string;
  other_servers: string[];
}

export interface ToolInfo {
  id: string;
  name: string;
  kind: string;
  installed: boolean;
  version: string | null;
  detect_detail: string;
  supports_headless: boolean;
  headless_note: string;
  config_path: string | null;
  mcp: McpStatus;
}
