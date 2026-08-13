"""Benchmark report builder.

Produces a structured report from execution metrics (cost, speed, tokens,
Bito usage, skills). All judge-derived metrics have been removed.
"""

from __future__ import annotations

import html
from typing import Any

from .. import engine
from . import metrics as metrics_svc
from . import runner as runner_svc

ARMS = ("A", "B", "C")
ARM_NAMES = {"A": "Vanilla tool", "B": "With Bito", "C": "Bito + Skills"}


def _fmt_money(v):
    if v is None:
        return "n/a"
    return f"${v:.4f}" if v < 1 else f"${v:.2f}"


def _fmt_secs(v):
    return "n/a" if v is None else f"{v/1000:.1f}s"


def _fmt_pct(v, d=0):
    return "n/a" if v is None else f"{v*100:.{d}f}%"


def _fmt(v, d=1):
    return "n/a" if v is None else (f"{v:.{d}f}" if isinstance(v, float) else str(v))


def build(batch_id: str) -> dict[str, Any]:
    batch = runner_svc.get_batch(batch_id)
    if not batch:
        raise ValueError("Batch not found.")
    m = metrics_svc.compute(batch_id)
    arms = m["arms"]

    summary = {
        "batch": batch,
        "n_runs": m["n_runs"],
    }

    rec = _recommend(arms)

    return {
        "batch": batch,
        "summary": summary,
        "arms": arms,
        "arm_names": ARM_NAMES,
        "recommendation": rec,
    }


def _recommend(arms: dict) -> dict:
    cost_a = arms["A"].get("avg_cost")
    cost_b = arms["B"].get("avg_cost")
    cost_c = arms["C"].get("avg_cost")
    time_a = arms["A"].get("avg_duration_ms")
    time_b = arms["B"].get("avg_duration_ms")
    time_c = arms["C"].get("avg_duration_ms")

    bits = []
    best_arm = "A"

    if cost_b is not None and cost_a is not None and cost_b < cost_a:
        bits.append(f"Bito (Arm B) reduced cost from {_fmt_money(cost_a)} to {_fmt_money(cost_b)}/answer")
        best_arm = "B"
    if cost_c is not None and cost_a is not None and cost_c < cost_a:
        bits.append(f"Bito + Skills (Arm C) achieved {_fmt_money(cost_c)}/answer")
        best_arm = "C"
    if time_b is not None and time_a is not None and time_b < time_a:
        bits.append(f"Arm B was faster ({_fmt_secs(time_b)} vs {_fmt_secs(time_a)})")

    if not bits:
        return {"verdict": "neutral", "text": "No clear cost or speed advantage detected on this set — try prompts that need deeper cross-repo context."}

    verdict = "adopt" if best_arm in ("B", "C") else "neutral"
    text = " · ".join(bits) + f". Arm {best_arm} ({ARM_NAMES[best_arm]}) is recommended."
    return {"verdict": verdict, "best_arm": best_arm, "text": text}


# ---------------------------------------------------------------------------
# HTML export
# ---------------------------------------------------------------------------
def render_html(batch_id: str) -> str:
    r = build(batch_id)
    e = html.escape
    b = r["batch"]
    arms = r["arms"]

    def cells(fmt, key):
        out = ""
        for a in ARMS:
            val = arms[a].get(key)
            out += f"<td class='num'>{e(fmt(val))}</td>"
        return out

    metric_rows = "".join(
        f"<tr><td>{e(label)}</td>{cells(fmt, key)}</tr>"
        for label, key, fmt in [
            ("Task success rate", "success_rate", _fmt_pct),
            ("Avg cost / answer", "avg_cost", _fmt_money),
            ("Total cost", "total_cost", _fmt_money),
            ("Avg time / answer", "avg_duration_ms", _fmt_secs),
            ("Avg output tokens", "avg_output_tokens", lambda v: _fmt(v, 0)),
            ("Avg tool calls", "avg_tool_calls", lambda v: _fmt(v, 1)),
            ("Avg MCP calls", "avg_mcp_calls", lambda v: _fmt(v, 1)),
            ("Bito MCP calls", "avg_bito_calls", lambda v: _fmt(v, 1)),
            ("Avg turns", "avg_num_turns", lambda v: _fmt(v, 1)),
            ("Errors / failures", "n_errors", lambda v: _fmt(v, 0)),
            ("Violations", "n_violations", lambda v: _fmt(v, 0)),
        ]
    )

    skills_rows = "".join(
        f"<tr><td class='pill {a.lower()}'>Arm {a}</td><td>{e(', '.join(arms[a].get('skills_used') or []) or 'none')}</td></tr>"
        for a in ARMS
    )

    arm_cards = "".join(
        f"<div class='card'><div class='pill {a.lower()}'>Arm {a}</div>"
        f"<div class='aname'>{e(ARM_NAMES[a])}</div>"
        f"<div class='small'>{_fmt_pct(arms[a]['success_rate'])} success · {_fmt_money(arms[a]['avg_cost'])}/answer · {_fmt_secs(arms[a]['avg_duration_ms'])}</div></div>"
        for a in ARMS
    )

    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>A/B Testing report — {e(batch_id)}</title>
<style>
  body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#1a2233;max-width:900px;margin:30px auto;padding:0 20px;line-height:1.5}}
  h1{{font-size:24px;margin-bottom:4px}} h2{{font-size:17px;margin:28px 0 10px;border-bottom:2px solid #eee;padding-bottom:6px}}
  .muted{{color:#64708a}} .small{{font-size:12px;color:#64708a}}
  table{{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}}
  th,td{{text-align:left;padding:8px 10px;border-bottom:1px solid #eee}} .num{{text-align:right;font-variant-numeric:tabular-nums}}
  .cards{{display:flex;gap:12px;margin-top:10px}} .card{{flex:1;border:1px solid #e4e8f0;border-radius:10px;padding:14px}}
  .pill{{display:inline-block;padding:2px 9px;border-radius:6px;font-size:12px;font-weight:700}}
  .a{{background:#eef0f3;color:#8a93a6}} .b{{background:#e3f4f1;color:#2a9d8f}} .c{{background:#ececff;color:#5b5bf0}}
  .aname{{font-weight:600;margin-top:6px}}
  .rec{{background:#ececff;border-radius:10px;padding:16px;font-size:15px}}
</style></head><body>
<h1>A/B Testing Benchmark Report</h1>
<div class="muted">{e(b.get('label') or batch_id)} · tool: {e(b.get('tool') or '')} · repo: {e(b.get('repo') or '—')} · {e(b.get('created_at') or '')}</div>

<h2>Summary</h2>
<div class="cards">{arm_cards}</div>

<h2>Arm A / B / C comparison</h2>
<table><thead><tr><th>Metric</th><th class="num">Arm A</th><th class="num">Arm B</th><th class="num">Arm C</th></tr></thead>
<tbody>{metric_rows}</tbody></table>

<h2>Skills used</h2>
<table><tbody>{skills_rows}</tbody></table>

<h2>Recommendation</h2>
<div class="rec">{e(r['recommendation']['text'])}</div>

<p class="small" style="margin-top:30px">Generated by the A/B Testing Benchmark Harness. Metrics are execution-derived (no blind judge).</p>
</body></html>"""


# ---------------------------------------------------------------------------
# Markdown export
# ---------------------------------------------------------------------------
def render_markdown(batch_id: str) -> str:
    r = build(batch_id)
    b = r["batch"]
    arms = r["arms"]

    def row(label, key, fmt):
        return "| " + label + " | " + " | ".join(fmt(arms[a].get(key)) for a in ARMS) + " |"

    metric_rows = "\n".join(
        row(label, key, fmt)
        for label, key, fmt in [
            ("Task success rate", "success_rate", _fmt_pct),
            ("Avg cost / answer", "avg_cost", _fmt_money),
            ("Total cost", "total_cost", _fmt_money),
            ("Avg time / answer", "avg_duration_ms", _fmt_secs),
            ("Avg output tokens", "avg_output_tokens", lambda v: _fmt(v, 0)),
            ("Avg tool calls", "avg_tool_calls", lambda v: _fmt(v, 1)),
            ("Avg MCP calls", "avg_mcp_calls", lambda v: _fmt(v, 1)),
            ("Bito MCP calls", "avg_bito_calls", lambda v: _fmt(v, 1)),
            ("Avg turns", "avg_num_turns", lambda v: _fmt(v, 1)),
            ("Errors", "n_errors", lambda v: _fmt(v, 0)),
            ("Violations", "n_violations", lambda v: _fmt(v, 0)),
        ]
    )

    arm_summary = "\n".join(
        f"- **Arm {a} — {ARM_NAMES[a]}**: {_fmt_pct(arms[a]['success_rate'])} success · "
        f"{_fmt_money(arms[a]['avg_cost'])}/answer · {_fmt_secs(arms[a]['avg_duration_ms'])}/answer · "
        f"skills: {', '.join(arms[a].get('skills_used') or []) or 'none'}"
        for a in ARMS
    )

    return f"""# A/B Testing Benchmark Report

**{b.get('label') or batch_id}** · tool: {b.get('tool') or ''} · repo: {b.get('repo') or '—'} · {b.get('created_at') or ''}

## Summary

{arm_summary}

## Arm A / B / C comparison

| Metric | Arm A | Arm B | Arm C |
| --- | ---: | ---: | ---: |
{metric_rows}

## Recommendation

{r['recommendation']['text']}

---
_Generated by the A/B Testing Benchmark Harness. Metrics are execution-derived (no blind judge)._
"""
