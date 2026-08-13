"""Engine integration layer.

Imports the existing ``harness.py`` as a module and exposes thin helpers the API
routers call. This is the ONLY place that knows how the harness stores things, so
the rest of the backend stays decoupled from harness internals.

Design rules:
- Reuse harness primitives; never shell out to the CLI.
- Never call harness functions that read stdin or call ``sys.exit`` (e.g.
  ``cmd_run``/``cmd_setup``); call the lower-level building blocks instead.
- ``results.db`` is the source of truth. We migrate it idempotently on startup.
"""

from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Locate the project root (the dir that holds harness.py) and import harness.
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

harness = importlib.import_module("harness")

# Re-export the canonical paths the harness already defines, so the backend and
# the CLI always agree on where state lives.
DB_PATH: Path = harness.DB_PATH
CONFIGS: Path = harness.CONFIGS
RUNS: Path = harness.RUNS
JUDGMENTS: Path = harness.JUDGMENTS
REPORTS: Path = harness.REPORTS
ROOT: Path = harness.ROOT

# UI-owned state that the harness doesn't manage.
PROMPT_SETS: Path = ROOT / "prompt_sets"

ARMS = ("A", "B", "C")


# ---------------------------------------------------------------------------
# Database: connection + idempotent migrations for the UI's extra columns.
# ---------------------------------------------------------------------------
def connect() -> sqlite3.Connection:
    """Open results.db with the harness schema guaranteed to exist, then apply
    the UI migrations. Rows come back as dict-like ``sqlite3.Row``."""
    conn = harness.db()  # creates runs + judgments tables if missing
    conn.row_factory = sqlite3.Row
    _migrate(conn)
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate(conn: sqlite3.Connection) -> None:
    """Add the UI's columns/tables without disturbing the CLI's schema.

    All changes are additive and nullable so the existing CLI keeps working and
    old ``results.db`` files upgrade in place.
    """
    run_cols = _columns(conn, "runs")
    additions = {
        "tool": "TEXT DEFAULT 'claude'",   # which code-gen tool produced the run
        "repo": "TEXT",                     # target repo/folder label
        "category": "TEXT",                 # prompt category at run time
        "batch_id": "TEXT",                 # benchmark session this run belongs to
        "base_prompt_id": "TEXT",           # the human prompt id (UI rows namespace prompt_id as "<batch>:<pid>")
    }
    for col, decl in additions.items():
        if col not in run_cols:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {col} {decl}")

    if "extended_json" not in _columns(conn, "judgments"):
        conn.execute("ALTER TABLE judgments ADD COLUMN extended_json TEXT")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS batches (
          batch_id     TEXT PRIMARY KEY,
          label        TEXT,
          tool         TEXT,
          repo         TEXT,
          prompt_set   TEXT,
          mode         TEXT,
          arms         TEXT,           -- JSON list e.g. ["A","B","C"]
          n_runs       INTEGER,
          model        TEXT,
          judge_model  TEXT,
          status       TEXT,           -- queued|running|stopped|done|error
          progress     INTEGER,        -- completed (arm,prompt) units
          total        INTEGER,        -- planned (arm,prompt) units
          error        TEXT,
          created_at   TEXT,
          updated_at   TEXT
        )
        """
    )
    conn.commit()


def db_health() -> dict[str, Any]:
    """Cheap liveness probe used by /api/health."""
    try:
        conn = connect()
        n_runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        n_batches = conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0]
        conn.close()
        return {"ok": True, "runs": n_runs, "batches": n_batches, "db": str(DB_PATH)}
    except Exception as e:  # pragma: no cover - defensive
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
