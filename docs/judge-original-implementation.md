# Original Judge Implementation — Reference Snapshot

> Saved before any changes to the judging system. Use this to revert to the original blind judge if needed.

---

## What the judge does

After all arm runs complete, a **blind judge** scores each prompt's three answers (A, B, C) independently using Claude Opus. The answers are shuffled into random order before scoring so the judge doesn't know which arm produced which answer. Scores are stored in the `judgments` table in `results.db`.

---

## Key constants — `harness.py`

```python
# Line 84-86
UNIVERSAL_DIMS   = ["correctness", "completeness", "grounding", "hallucination_resistance", "reasoning"]
CONDITIONAL_DIMS = ["planning_quality", "impact_analysis"]
RUBRIC_DIMS      = UNIVERSAL_DIMS + CONDITIONAL_DIMS

DEFAULT_JUDGE_MODEL = "claude-opus-4-7-20251101"   # search harness.py for DEFAULT_JUDGE_MODEL
DEFAULT_JUDGE_SEED  = 42                            # rng seed for answer shuffle
```

Universal dims are scored 1–5 on every prompt. Conditional dims are scored 1–5 only when the question is a plan/design task (`planning_quality`) or a change/fix task (`impact_analysis`); otherwise `null`.

---

## Judge prompt — `harness.py:1665`

`JUDGE_PROMPT_TEMPLATE` — the full prompt sent to the judge model. Key sections:

- Tells the judge it is evaluating THREE unlabeled answers
- Passes `{canonical_repos}` — a list of indexed repo names from `indexed-repos.txt` (used as a hint, not actual source code)
- Passes `{rubric}` — the dimension labels/descriptions
- Anti-vagueness instructions: penalise hedged/generic answers, reward specific file/symbol citations
- Conditional dimension rules: return `null` when dimension doesn't apply
- Instructs the judge to return strict JSON: `{ answer1: {refusal, scores...}, answer2: ..., answer3: ..., rationale: "..." }`

**Known limitation:** the judge receives only the question + 3 answers + repo names. It does NOT receive actual source code, so it cannot verify whether file paths, function names, or code references cited in answers are real. This means confident-but-wrong answers (Arm A hallucinating cross-repo details) can score as high as correct answers (Arm C grounded via Bito index).

---

## Score extraction — `harness.py:1734`

```python
def extract_judge_json(text: str) -> dict | None:
    # Strips ```json fences, then tries json.loads, then regex-extracts {...}
```

```python
def _coerce_scores(raw: dict | None, refusal: bool) -> dict:
    # Refusal → all universal dims = 0, conditional = None
    # Otherwise: clamps each dim to int, 0 if missing
```

---

## Judge execution — `backend/services/runner.py:554`

`_judge_batch(conn, job)` runs after all arm runs finish (only when `run_judge=True` and arms = A/B/C):

1. Uses the **same tool adapter** that ran the benchmark (Cursor judged by Cursor, Claude by Claude)
2. Uses `mcp-arm-bito.json` as its MCP config (falls back to `mcp-arm-a.json`)
3. For each distinct `prompt_id` in the batch:
   - Loads A/B/C responses from `runs` table
   - Skips if any arm errored
   - Shuffles answer order with `random.Random(seed=42)`
   - Calls `judge_adapter.complete(prompt=judge_prompt, model=judge_model, max_turns=10)`
   - Retries on failure (uses `harness.RETRY_BACKOFFS_SEC`)
   - Writes result to `judgments` table

---

## Database schema — `judgments` table

```sql
CREATE TABLE judgments (
    prompt_id          TEXT PRIMARY KEY,
    scores_a_json      TEXT,   -- JSON: {"correctness": 4, "completeness": 5, ...}
    scores_b_json      TEXT,
    scores_c_json      TEXT,
    refusal_a          INTEGER, -- 0 or 1
    refusal_b          INTEGER,
    refusal_c          INTEGER,
    rationale          TEXT,    -- 2-3 sentence comparison from the judge
    presentation_order TEXT,    -- e.g. "CAB" (shuffle order used)
    judge_cost_usd     REAL,
    judge_duration_ms  INTEGER,
    error              TEXT,
    judged_at          TEXT
);
```

---

## What depends on the judge

| Component | Dependency |
|---|---|
| `backend/services/metrics.py` | Quality score, hallucination rate, refusal detection, rubric averages, per-prompt totals |
| `backend/services/leaderboard.py` | `quality` and `hallucination_rate` leaderboard columns |
| `backend/services/reports.py` | Quality totals, per-dimension table, judge rationale in markdown/CSV |
| `frontend/src/pages/Metrics.tsx` | Overall score bars, leader badge, key metrics (hallucination rate), by-dimension section, rubric table, prompt-level results table |
| `frontend/src/pages/Leaderboard.tsx` | "Best quality" and "Fewest hallucinations" ranking columns |
| `frontend/src/pages/Runner.tsx` | `run_judge` toggle, `judge_model` config |
| `backend/models.py:70-72` | `judge_model`, `run_judge` fields on `BatchConfig` |

---

## Proposed improvement (grounded judge)

Instead of passing only repo names, pass **actual relevant source files** fetched from the Bito index alongside the answers. This lets the judge verify whether file paths, function names, and code references are real — which is the exact scenario where the current judge fails (Arm A hallucinating cross-repo details gets scored the same as Arm C's grounded answer).

Implementation sketch:
1. After collecting A/B/C answers for a prompt, extract code references (repo names, file paths, symbols) mentioned across all three answers
2. Fetch those files from the Bito MCP index
3. Append the fetched source as a `REFERENCE SOURCE CODE` section in the judge prompt
4. Update the rubric instructions to tell the judge to use the source to verify claims

Files to change: `backend/services/runner.py:_judge_batch`, `harness.py:JUDGE_PROMPT_TEMPLATE`
