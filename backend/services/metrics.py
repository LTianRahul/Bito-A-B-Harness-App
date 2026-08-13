"""Metrics & evaluation aggregation.

Turns the raw ``runs`` rows for a batch (or across all UI batches) into the
full metric set the Metrics, Leaderboard, and Reports pages need. All metrics
are derived purely from execution data (cost, tokens, time, tool/MCP/Bito
usage, skills, errors) — no blind judge.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any, Optional

from .. import engine

harness = engine.harness
ARMS = ("A", "B", "C")


def _avg(xs: list) -> Optional[float]:
    xs = [x for x in xs if isinstance(x, (int, float))]
    return sum(xs) / len(xs) if xs else None


def _tool_call_stats(tool_calls_json: Optional[str]) -> dict[str, Any]:
    """Counts derived from a run's tool_calls list."""
    try:
        calls = json.loads(tool_calls_json or "[]")
    except json.JSONDecodeError:
        calls = []
    mcp = bito = total = 0
    skills: list[str] = []
    for c in calls:
        name = str(c.get("name", ""))
        cnt = int(c.get("count", 0) or 0)
        total += cnt
        if name.startswith("mcp__"):
            mcp += cnt
        if name.startswith("mcp__BitoAIArchitect__"):
            bito += cnt
        if name == "Skill" and isinstance(c.get("skills"), list):
            skills.extend(c["skills"])
    return {"mcp": mcp, "bito": bito, "total": total, "skills": skills}


def _load(conn, batch_id: Optional[str]):
    """Return runs list scoped to a batch if given, else all UI batches."""
    if batch_id:
        runs = conn.execute("SELECT * FROM runs WHERE batch_id=?", (batch_id,)).fetchall()
    else:
        runs = conn.execute("SELECT * FROM runs WHERE batch_id IS NOT NULL").fetchall()
    return [dict(r) for r in runs]


def compute(batch_id: Optional[str] = None) -> dict[str, Any]:
    conn = engine.connect()
    try:
        runs = _load(conn, batch_id)
    finally:
        conn.close()

    per_arm: dict[str, list[dict]] = {a: [] for a in ARMS}
    for r in runs:
        if r["arm"] in per_arm:
            per_arm[r["arm"]].append(r)

    def arm_metrics(arm: str) -> dict[str, Any]:
        rows = per_arm[arm]
        success = [r for r in rows if r["exit_code"] == 0 and not r["error"]]
        errors = [r for r in rows if r["exit_code"] != 0 or r["error"]]

        tc = [_tool_call_stats(r.get("tool_calls_json")) for r in success]
        all_skills = sorted({s for t in tc for s in t["skills"]})

        total_toks = [
            ((r.get("input_tokens") or 0) + (r.get("output_tokens") or 0)
             + (r.get("cache_read_tokens") or 0) + (r.get("cache_creation_tokens") or 0))
            for r in success
        ]
        avg_total_tok = _avg([t for t in total_toks if t > 0])

        costs = [r["total_cost_usd"] for r in success if isinstance(r["total_cost_usd"], (int, float))]

        return {
            "n": len(rows),
            "n_success": len(success),
            "n_completed": len(success),
            "n_errors": len(errors),
            "n_violations": sum(1 for r in rows if r.get("bito_violation")),
            "success_rate": (len(success) / len(rows)) if rows else None,
            "avg_cost": _avg(costs),
            "total_cost": sum(costs) if costs else None,
            "avg_duration_ms": _avg([r["duration_ms"] for r in success]),
            "avg_input_tokens": _avg([r["input_tokens"] for r in success]),
            "avg_output_tokens": _avg([r["output_tokens"] for r in success]),
            "avg_total_tokens": avg_total_tok,
            "avg_tool_calls": _avg([t["total"] for t in tc]),
            "avg_num_turns": _avg([r["num_turns"] for r in success]),
            "avg_mcp_calls": _avg([t["mcp"] for t in tc]),
            "avg_bito_calls": _avg([t["bito"] for t in tc]),
            "skills_used": all_skills,
        }

    arms = {a: arm_metrics(a) for a in ARMS}

    return {
        "batch_id": batch_id,
        "arms": arms,
        "n_runs": len(runs),
    }


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------
_CSV_METRICS = [
    ("success_rate", "Task success rate"),
    ("n_completed", "Completed"),
    ("n_errors", "Errors"),
    ("n_violations", "Bito violations"),
    ("total_cost", "Total cost (USD)"),
    ("avg_cost", "Avg cost/answer (USD)"),
    ("avg_duration_ms", "Avg time/answer"),
    ("avg_input_tokens", "Avg input tokens"),
    ("avg_output_tokens", "Avg output tokens"),
    ("avg_tool_calls", "Avg tool calls"),
    ("avg_num_turns", "Avg turns"),
    ("avg_mcp_calls", "Avg MCP calls"),
    ("avg_bito_calls", "Avg Bito MCP calls"),
]

_HTML_METRICS = [m for m in _CSV_METRICS if m[0] not in ("avg_input_tokens", "avg_output_tokens", "avg_mcp_calls")]


def to_html(batch_id: Optional[str] = None) -> str:
    """Self-contained HTML report of arm metrics — opens in any browser."""
    m = compute(batch_id)
    arms = m["arms"]
    session_label = batch_id or "All sessions"

    def fmt(v: Any, key: str = "") -> str:
        if v is None:
            return "—"
        if key in ("avg_duration_ms", "total_duration_ms") and isinstance(v, (int, float)):
            s = v / 1000
            if s < 60:
                return f"{s:.1f}s"
            return f"{int(s // 60)}m {int(s % 60)}s"
        if isinstance(v, float):
            return f"{v:.4f}" if v < 1 else f"{v:.2f}"
        return str(v)

    def pct(v: Any) -> str:
        if v is None:
            return "—"
        return f"{float(v)*100:.0f}%"

    # Badge metrics: (key, tooltip label) — lower is better
    _BADGE_METRICS = {
        "total_cost":      ("↓", "lower cost"),
        "avg_cost":        ("↓", "lower cost"),
        "avg_duration_ms": ("⚡", "faster"),
        "avg_num_turns":   ("↓", "fewer turns"),
        "avg_tool_calls":  ("↓", "fewer tool calls"),
    }

    def improvement_badge(arm: str, key: str, val: Any) -> str:
        if arm == "A" or key not in _BADGE_METRICS or not isinstance(val, (int, float)):
            return ""
        a_val = arms["A"].get(key)
        b_val = arms["B"].get(key)
        c_val = arms["C"].get(key)
        if not isinstance(a_val, (int, float)) or not isinstance(b_val, (int, float)) or not isinstance(c_val, (int, float)):
            return ""
        if a_val <= b_val and a_val <= c_val:
            return ""  # Arm A already best
        if val >= a_val:
            return ""  # this arm not better than baseline
        pct_val = round(((a_val - val) / a_val) * 100)
        icon, label = _BADGE_METRICS[key]
        return f' <span title="{label}" style="font-size:0.78rem;font-weight:700;color:#16a34a;cursor:default">{icon} {pct_val}%</span>'

    rows_html = ""
    for key, label in _HTML_METRICS:
        vals = [arms[a].get(key) for a in ARMS]
        is_pct = "rate" in key
        cells = "".join(
            f"<td>{pct(v) if is_pct else fmt(v, key)}{improvement_badge(a, key, v)}</td>"
            for a, v in zip(ARMS, vals)
        )
        rows_html += f"<tr><td>{label}</td>{cells}</tr>\n"

    skills_cells = "".join(
        f"<td>{', '.join(arms[a].get('skills_used') or []) or '—'}</td>" for a in ARMS
    )
    rows_html += f"<tr><td>Skills used</td>{skills_cells}</tr>\n"

    arm_names = {"A": "Vanilla tool", "B": "With Bito MCP + Skill", "C": "Bito MCP + all Skills"}
    headers = "".join(f"<th>Arm {a}<br><small>{arm_names[a]}</small></th>" for a in ARMS)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>A/B Benchmark — {session_label}</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 860px; margin: 40px auto; padding: 0 20px; color: #1a1a1a; }}
  h1 {{ font-size: 1.4rem; margin-bottom: 4px; }}
  .meta {{ color: #666; font-size: 0.85rem; margin-bottom: 28px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
  th, td {{ padding: 9px 14px; text-align: left; border-bottom: 1px solid #e5e7eb; }}
  th {{ background: #f9fafb; font-weight: 600; }}
  th:not(:first-child), td:not(:first-child) {{ text-align: right; }}
  tr:hover td {{ background: #f9fafb; }}
  small {{ font-weight: 400; color: #666; }}
  .pill {{ display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 0.75rem; font-weight: 600; }}
  span[title] {{ cursor: default; }}
  .a {{ background: #e0f2fe; color: #0369a1; }}
  .b {{ background: #dcfce7; color: #15803d; }}
  .c {{ background: #f3e8ff; color: #7e22ce; }}
</style>
</head>
<body>
<h1>A/B Benchmark Results</h1>
<div class="meta">Session: {session_label}</div>
<table>
  <thead>
    <tr>
      <th>Metric</th>
      {"".join(f'<th><span class="pill {a.lower()}">Arm {a}</span><br><small>{arm_names[a]}</small></th>' for a in ARMS)}
    </tr>
  </thead>
  <tbody>
    {rows_html}
  </tbody>
</table>
</body>
</html>"""


def to_csv(batch_id: Optional[str] = None) -> str:
    """Flat CSV: per-arm metric table."""
    m = compute(batch_id)
    arms = m["arms"]
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["A/B Testing metrics", f"batch={batch_id or 'all sessions'}"])
    w.writerow([])
    w.writerow(["Metric", "Arm A", "Arm B", "Arm C"])
    for key, label in _CSV_METRICS:
        w.writerow([label] + [arms[a].get(key) for a in ARMS])
    w.writerow(["skills used"] + ["; ".join(arms[a].get("skills_used") or []) for a in ARMS])
    return out.getvalue()
