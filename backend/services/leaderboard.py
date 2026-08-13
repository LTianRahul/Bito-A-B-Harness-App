"""Leaderboard: compare arms (and tools) across many runs, with filters and a
best-performer pick per dimension.

An *entry* is one (tool, arm) aggregate over the filtered runs. Best-by chips
name the winning entry for cost, speed, token efficiency, Bito usage, and
skills. All metrics are execution-derived — no blind judge required.
"""

from __future__ import annotations

from typing import Any, Optional

from .. import engine
from .metrics import _tool_call_stats

harness = engine.harness


def _avg(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return sum(xs) / len(xs) if xs else None


def filters() -> dict[str, list[str]]:
    """Distinct filter values present in the data."""
    conn = engine.connect()
    try:
        def distinct(col):
            return [
                r[0]
                for r in conn.execute(
                    f"SELECT DISTINCT {col} FROM runs WHERE batch_id IS NOT NULL AND {col} IS NOT NULL"
                ).fetchall()
            ]

        return {
            "tools": sorted(distinct("tool")),
            "repos": sorted(distinct("repo")),
            "categories": sorted(distinct("category")),
        }
    finally:
        conn.close()


def compute(
    tool: Optional[str] = None,
    repo: Optional[str] = None,
    category: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> dict[str, Any]:
    where = ["batch_id IS NOT NULL"]
    args: list[Any] = []
    if tool:
        where.append("tool = ?"); args.append(tool)
    if repo:
        where.append("repo = ?"); args.append(repo)
    if category:
        where.append("category = ?"); args.append(category)
    if date_from:
        where.append("started_at >= ?"); args.append(date_from)
    if date_to:
        where.append("started_at <= ?"); args.append(date_to + "T23:59:59")

    conn = engine.connect()
    try:
        runs = [dict(r) for r in conn.execute(
            f"SELECT * FROM runs WHERE {' AND '.join(where)}", args
        ).fetchall()]
    finally:
        conn.close()

    # Group by (tool, arm).
    groups: dict[tuple, list[dict]] = {}
    for r in runs:
        groups.setdefault((r["tool"] or "claude", r["arm"]), []).append(r)

    # Fairness: compare arms only on prompts where Arm A succeeded.
    a_attempted = {r["prompt_id"] for r in runs if r["arm"] == "A"}
    a_succeeded = {
        r["prompt_id"] for r in runs
        if r["arm"] == "A" and r["exit_code"] == 0 and not r["error"]
    }

    def baseline_qualifies(pid: str) -> bool:
        return (pid not in a_attempted) or (pid in a_succeeded)

    entries = []
    for (tool_id, arm), rows in sorted(groups.items()):
        own_ok = [r for r in rows if r["exit_code"] == 0 and not r["error"]]
        paired = [r for r in rows if baseline_qualifies(r["prompt_id"])]
        success = [r for r in paired if r["exit_code"] == 0 and not r["error"]]

        tc = [_tool_call_stats(r.get("tool_calls_json")) for r in success]
        total_toks = [
            ((r.get("input_tokens") or 0) + (r.get("output_tokens") or 0)
             + (r.get("cache_read_tokens") or 0) + (r.get("cache_creation_tokens") or 0))
            for r in success
        ]
        total_toks = [t for t in total_toks if t > 0]
        costs = [r["total_cost_usd"] for r in success if isinstance(r["total_cost_usd"], (int, float))]
        durs = [r["duration_ms"] for r in success if isinstance(r["duration_ms"], (int, float))]
        out_toks = [r["output_tokens"] for r in success if r.get("output_tokens")]
        avg_total_tok = _avg(total_toks)

        turns = [r["num_turns"] for r in success if isinstance(r.get("num_turns"), (int, float))]
        entries.append({
            "tool": tool_id,
            "arm": arm,
            "n": len(rows),
            "n_compared": len(success),
            "success_rate": (len(own_ok) / len(rows)) if rows else None,
            "total_cost": sum(costs) if costs else None,
            "avg_cost": _avg(costs),
            "total_duration_ms": sum(durs) if durs else None,
            "avg_duration_ms": _avg(durs),
            "total_output_tokens": sum(out_toks) if out_toks else None,
            "avg_output_tokens": _avg(out_toks),
            "avg_total_tokens": avg_total_tok,
            "token_efficiency": (1_000_000 / avg_total_tok) if avg_total_tok else None,
            "avg_bito_calls": _avg([t["bito"] for t in tc]),
            "avg_mcp_calls": _avg([t["mcp"] for t in tc]),
            "avg_num_turns": _avg(turns),
        })

    max_cmp = max((e["n_compared"] for e in entries), default=0)
    min_cmp = max(2, max_cmp // 2)

    def best(metric: str, higher=True):
        cand = [
            e for e in entries
            if isinstance(e.get(metric), (int, float)) and e["n_compared"] >= min_cmp
        ]
        if not cand:
            return None
        ext = (max if higher else min)(e[metric] for e in cand)
        winners = [e for e in cand if abs(e[metric] - ext) <= 1e-9]
        e = winners[0]
        out = {"tool": e["tool"], "arm": e["arm"], "value": e[metric]}
        if len(winners) > 1:
            out["tie"] = True
            out["tied_arms"] = sorted(w["arm"] for w in winners)
        return out

    best_by = {
        "cost": best("avg_cost", False),
        "speed": best("avg_duration_ms", False),
        "turns": best("avg_num_turns", False),
        "token_efficiency": best("token_efficiency", True),
        "bito_calls": best("avg_bito_calls", True),
    }

    return {"entries": entries, "best_by": best_by, "n_runs": len(runs)}
