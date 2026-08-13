"""AI-assisted prompt generation.

Asks the model to draft a set of benchmark tasks — optionally grounded in the
workspace's indexed repositories (so the prompts name real repos/areas) — and
returns them in the UI's prompt shape ({id, title, category, prompt}). The user
reviews and imports them on the Prompts tab.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from .. import engine

harness = engine.harness

CATEGORY_IDS = [
    "single-repo", "cross-repo", "architecture", "bug-fix",
    "refactor", "explanation", "test-gen", "hallucination",
]

_INSTRUCTION = """You are generating realistic engineering investigation tasks for a benchmark
that compares how coding assistants perform on real-world, cross-repo work: tracing incidents,
verifying claims about the system, writing tests against real contracts, and fixing bugs whose
root cause spans repo boundaries.

## What real engineering investigations look like

When an engineer is asked to trace an incident, verify a design doc, or write a test against
a service they don't own, they are almost never handed the exact file, class, or config key in
advance. They start from a symptom — "customers report X", "finance suspects Y" — and have to
figure out ownership, find the exact contract (topic names, table columns, config keys), and
confirm their answer against the real code before it's trustworthy. That discovery process is
the realistic task. Prompts that hand over the file path or class name up front skip the part
of the job that actually matters, and stop being a meaningful test of anything.

## Step 1 — Deep research FIRST (mandatory, before writing a single prompt)

Use BitoAIArchitect MCP to explore the repos extensively. Do NOT write prompts from memory.
Discover and record in your working notes (not in the prompts):
- Exact runtime contract values: Kafka/SQS topic strings, DB table/column names, HTTP header
  names, enum constant names, config key strings, feature flag names — the exact byte-for-byte
  strings as they appear in code
- Cross-repo dependency chains: which service calls which, via what mechanism, and what
  exact field/type is exchanged at the boundary
- Non-obvious ownership: components where the responsible class has a name that does NOT
  match the business domain (e.g. a class named "CoordinatorImpl" that owns fee calculation)
- High-churn / high-risk areas: classes with many authors or changes — contract bugs there are
  realistic and worth investigating

## Your task

Write {count} DISTINCT benchmark tasks{topic_clause}, spread across these categories:
{categories}

{repo_clause}

## Rules for writing a realistic task

### Rule 1 — Describe the task the way a stakeholder would hand it off

Write the way a support ticket, an incident report, or a design-review request actually reads:
in business/symptom language, without pre-naming the internal implementation details that the
investigation itself is supposed to uncover. NEVER include in the prompt text:
- Class names, method names, interface names
- File paths, package names, module names
- Exact Kafka topic strings, DB column names, config key names, enum values, HTTP header names
- Line numbers, annotation strings, endpoint paths

These are the answer, not the question — you discovered them from the index as part of writing
a grounded, verifiable task. They belong in your research notes, not in the prompt.

WRONG — hands over the answer instead of posing the investigation:
  ✗ "The `FeeCalculatorImpl` class in `server` has a rounding bug..."
  ✗ "Find the `bulk_download_pre_generate_event` topic producer..."
  ✗ "The `payment_transactions` table column `settled_amount` is..."

RIGHT — poses the task the way a stakeholder actually would:
  ✓ "In `server`, personal-trip booking fees are occasionally reported as off by a fraction
     of a cent for multi-currency payments. Find the component that calculates these fees,
     identify whether a precision or rounding defect exists, and produce the corrected code."
  ✓ "In `commerce-invoice-lambdas`, the bulk invoice download flow emits a Kafka event when
     a pre-generate job is queued. Find the exact topic name this lambda publishes to, the
     class that publishes it, and every repo that references this topic — whether as a consumer,
     metric emitter, or unrelated subscriber."

### Rule 2 — Every prompt should require at least TWO exact runtime facts

A trustworthy investigation ends with verified specifics, not a paraphrase. The answer should
require finding at least two of:
  - An exact Kafka/SQS topic string (e.g. the real topic name, not a description)
  - An exact DB table or column name
  - An exact HTTP header name or auth token claim key
  - An exact config key or feature flag name
  - An exact enum constant or error code

These are the details that make it obvious whether an answer is right or wrong — a paraphrase
like "the payment success topic" isn't verifiable the way "commerce.payment.settled.v2" is.

### Rule 3 — Require a concrete, falsifiable output

An investigation isn't done until it produces something checkable, not a narrative. Every
prompt should end with a concrete output requirement appropriate to its category:
  - cross-repo / architecture: "State the exact topic string, the exact class name, and the
    exact downstream consumer — byte-for-byte as they appear in the code."
  - bug-fix: "Produce the corrected method in full — pseudocode is not acceptable."
  - refactor: "Produce a before/after diff showing the exact method signatures."
  - test-gen: "Produce a runnable test file using the real constructor signature, real field
    names, and real enum values from the current codebase — placeholder types are not acceptable."
  - hallucination: "State TRUE or FALSE for each sub-claim and cite the exact evidence
    (class name, topic string, or column name) that confirms or refutes it."

### Rule 4 — Cross-repo chains of 3+ hops

Real cross-service incidents rarely stop at one repo. Every cross-repo prompt should require
tracing a full chain: producer → broker → consumer → downstream side-effect, across at least
3 repos — the way an actual on-call engineer would have to.

### Rule 5 — Hallucination prompts should test a claim someone plausibly got wrong

For hallucination tasks: take a real pattern from the index and change one detail slightly
(wrong topic suffix, wrong repo name, wrong field name) to mirror the kind of claim that shows
up in a stale wiki page or an confidently-wrong Slack message. Ask the assistant to verify it
against the actual code rather than take it at face value.

## Prompt length and structure

- 120–200 words per prompt. Longer wastes tokens; shorter loses falsifiability.
- Open with the business scenario (2–3 sentences in plain language).
- State the specific question(s) requiring exact runtime values.
- Close with the concrete output requirement.
- Never mention a class name, file path, topic string, or column name — those are findings,
  not inputs.

## What makes a weak prompt (reject these)

- It's answerable purely by pattern-matching on business domain keywords, with no need to
  verify anything against the actual code.
- The answer is derivable from public documentation or general knowledge.
- It involves only one repo when the underlying scenario is naturally cross-repo.
- It asks "explain how X works" without requiring exact, verifiable evidence.
- The output requirement is vague ("describe", "explain", "summarise").
- The prompt states the technical identifier that the investigation is supposed to discover.

## Output

Return ONLY a raw JSON array — no markdown fences, no prose, no explanation:
[
  {{
    "title": "<short title, ≤10 words>",
    "category": "<exact category id from the list above>",
    "prompt": "<full prompt text>"
  }},
  ...
]
"""


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:24] or "prompt"


def _scrub_hints(text: str, repo_names: list[str] | None = None) -> str:
    """Strip file paths, line numbers, class names, config keys, branch names, and
    endpoints that would hand over implementation details the task is meant to uncover.
    Applied to every generated prompt before it reaches the UI regardless of what the
    model produced.
    repo_names: indexed repo names that must NOT be scrubbed (naming the repo is fine)."""

    # Temporarily replace repo names with placeholders so they survive scrubbing
    _repos = repo_names or []
    _markers: dict[str, str] = {}
    for i, repo in enumerate(_repos):
        marker = f"__REPO_{i}__"
        _markers[marker] = repo
        text = text.replace(f"`{repo}`", f"`{marker}`")
        text = text.replace(repo, marker)

    # --- File paths (backtick-wrapped or bare, with or without extension) ---
    # Backtick-wrapped paths with ellipsis: `business/foo-bar/...`
    text = re.sub(r'`[a-zA-Z0-9._-]+(?:/[a-zA-Z0-9._/-]+|\.\.\.)+`',
                  '[find the relevant file]', text)
    # Backtick-wrapped paths with extension: `src/main/java/Foo.java`
    text = re.sub(r'`[a-zA-Z0-9._-]+(?:/[a-zA-Z0-9._-]+)+\.[a-zA-Z]{2,5}`',
                  '[find the relevant file]', text)
    # Bare paths: any token containing 2+ slashes (with or without extension)
    text = re.sub(r'[a-zA-Z0-9._-]+(?:/[a-zA-Z0-9._-]+){2,}(?:\.[a-zA-Z]{2,5})?',
                  '[find the relevant file]', text)

    # --- Fully-qualified Java/Kotlin class names: com.foo.bar.ClassName ---
    text = re.sub(r'com\.[a-z][a-zA-Z0-9.]+', '[find the relevant class]', text)

    # --- Git branch names that reveal ticket/file info: SRE-32149-remove-nr-log-shipping ---
    text = re.sub(r'`?[A-Z]+-\d+-[a-z][a-zA-Z0-9-]+`?', '[see relevant branch]', text)

    # --- Backtick-wrapped PascalCase class names: `BookingVirtualCardCreateRequest` ---
    text = re.sub(r'`[A-Z][a-zA-Z0-9]{4,}`', '[find the relevant class]', text)

    # --- Backtick-wrapped camelCase/snake_case identifiers: `createSession`, `winston-nr` ---
    text = re.sub(r'`[a-z][a-zA-Z0-9_./-]{5,}`', '[find the relevant identifier]', text)

    # --- Annotation blocks: `@KafkaListener(topics = "foo", groupId = "bar")` ---
    text = re.sub(r'`@[A-Za-z]+\([^`]+\)`', '[find the relevant annotation]', text)

    # --- Internal REST endpoints: /session/{sessionID}/message, /api/v2/foo/bar ---
    text = re.sub(r'`?/[a-zA-Z0-9_{}-]+(?:/[a-zA-Z0-9_{}-]+){2,}`?',
                  '[find the relevant endpoint]', text)

    # --- Bare filenames with known extensions: BookingVirtualCardCreateRequest.java ---
    text = re.sub(
        r'\b[A-Za-z][a-zA-Z0-9_]{3,}\.(java|kt|ts|js|py|yaml|yml|xml|json|swift|go|md)\b',
        '[find the relevant file]', text
    )

    # --- Line number references: "at line 27", "around line 445", "line 83" ---
    text = re.sub(
        r'\b(?:around|at|see|near|inside)?\s*\b(?:line|lines?)\s+\d+(?:\s*[-–]\s*\d+)?\b',
        '', text, flags=re.IGNORECASE
    )

    # --- Parenthetical location hints: "(in `...`)", "(see `...`)" ---
    text = re.sub(r'\((?:in|see|at|inside)\s+`[^`]+`\)', '', text, flags=re.IGNORECASE)

    # --- Cleanup ---
    text = re.sub(r'\(\s*\)', '', text)
    text = re.sub(r'\s{2,}', ' ', text)
    text = re.sub(r' ([,.])', r'\1', text)

    # Restore repo names
    for marker, repo in _markers.items():
        text = text.replace(f"`{marker}`", f"`{repo}`")
        text = text.replace(marker, repo)

    return text.strip()


def generate(
    topic: Optional[str] = None,
    count: int = 6,
    categories: Optional[list[str]] = None,
    ground: bool = True,
    model: Optional[str] = None,
) -> dict[str, Any]:
    count = max(1, min(int(count or 6), 20))
    cats = [c for c in (categories or CATEGORY_IDS) if c in CATEGORY_IDS] or CATEGORY_IDS
    model = model or harness.DEFAULT_MODEL

    # Ground in indexed repos when available + requested → prompts name real repos.
    repos = harness.load_indexed_repos() if ground else []
    if repos:
        names = ", ".join(repos[:40]) + ("…" if len(repos) > 40 else "")
        repo_clause = (
            f"Indexed repositories available via BitoAIArchitect MCP: {names}.\n\n"
            "RESEARCH PHASE — before writing any prompt, use BitoAIArchitect MCP to query at "
            "least 6 of these repos. For each prompt you plan to write, find and record in your "
            "working notes (NOT in the prompt text): the exact Kafka/SQS topic strings, the exact "
            "DB table/column names, the exact config key names, the exact class that owns the "
            "behaviour, and the full cross-repo dependency chain. This is your research — the "
            "prompt must describe the SYMPTOM only, never the findings.\n\n"
            "WRITING PHASE — use only repo names (from the list above) and business-language "
            "symptom descriptions. Every technical identifier you discovered stays in your notes. "
            "The benchmark assistant must find them through their own exploration."
        )
    else:
        repo_clause = (
            "No repository index is available. Write realistic coding tasks that would apply "
            "to any mid-sized backend codebase. Do NOT invent specific repo names or URLs."
        )

    # Use Bito MCP config so the model can query BitoAIArchitect to discover
    # real cross-repo relationships and generate well-grounded complex prompts.
    # Fall back to arm-a config if bito config is not available.
    mcp_config = engine.CONFIGS / "mcp-arm-bito.json"
    instruction = _INSTRUCTION
    max_turns = 16

    if not mcp_config.exists():
        for alt in ("mcp-arm-a.json",):
            if (engine.CONFIGS / alt).exists():
                mcp_config = engine.CONFIGS / alt
                break

    if not mcp_config.exists():
        raise ValueError("No MCP config found. Connect Bito / run Setup first.")

    prompt = instruction.format(
        count=count,
        topic_clause=f" about: {topic.strip()}" if topic and topic.strip() else "",
        categories="\n".join(f"- {c}" for c in cats),
        repo_clause=repo_clause,
    )

    rc, _wall, obj, stderr = harness.run_claude_json(
        prompt=prompt, mcp_config=mcp_config, model=model, max_turns=max_turns,
    )
    if rc != 0 or not obj:
        detail = (stderr or "")[:300].strip()
        raise ValueError(f"Generation failed (exit {rc}). {detail}" if detail else f"Generation failed (exit {rc}).")

    parsed = _extract_array(obj.get("result") or "")
    if not parsed:
        raise ValueError("The model did not return a usable prompt list. Try again or adjust the topic.")

    out: list[dict] = []
    seen: set[str] = set()
    for i, item in enumerate(parsed):
        if not isinstance(item, dict):
            continue
        text = _scrub_hints(str(item.get("prompt") or "").strip(), repo_names=repos)
        if not text:
            continue
        title = str(item.get("title") or "").strip()
        cat = item.get("category")
        cat = cat if cat in CATEGORY_IDS else None
        pid = _slug(title or text)
        base, n = pid, 2
        while pid in seen:
            pid = f"{base}-{n}"; n += 1
        seen.add(pid)
        out.append({"id": pid, "title": title or None, "category": cat, "prompt": text})

    return {"prompts": out, "grounded": bool(repos), "n": len(out)}


def _extract_array(text: str) -> list:
    """Extract a JSON array from model output, handling prose wrappers and code fences."""
    if not text:
        return []
    # Strip markdown code fences (```json ... ``` or ``` ... ```)
    text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.MULTILINE)
    text = re.sub(r"\s*```$", "", text.strip(), flags=re.MULTILINE)
    text = text.strip()

    # Try direct parse first (model returned pure JSON as requested).
    try:
        v = json.loads(text)
        if isinstance(v, list):
            return v
        if isinstance(v, dict) and isinstance(v.get("prompts"), list):
            return v["prompts"]
    except json.JSONDecodeError:
        pass

    # Walk the text to find a balanced JSON array, handling strings correctly
    # so brackets inside string values don't confuse the depth counter.
    start = text.find("[")
    if start == -1:
        return []
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\" and in_str:
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                candidate = text[start : i + 1]
                try:
                    v = json.loads(candidate)
                    if isinstance(v, list):
                        return v
                except json.JSONDecodeError:
                    pass
                # Array boundary found but parse failed — stop trying.
                break
    return []
