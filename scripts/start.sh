#!/usr/bin/env bash
# =============================================================================
#  A/B Benchmark — one-command setup & launch  (macOS / Linux)
#
#  Just run:  ./scripts/start.sh   (or double-click start.command at the root)
#
#  First run installs everything it can (Python deps, the web UI, and the
#  Claude Code CLI), then starts the app at http://127.0.0.1:8765 and opens
#  your browser. Every later run just launches it. Safe to re-run any time.
# =============================================================================
cd "$(dirname "$0")/.." || exit 1   # project root (this script lives in scripts/)
APP_URL="http://127.0.0.1:8765"

say()  { printf "\n\033[1;36m▶ %s\033[0m\n" "$*"; }
ok()   { printf "  \033[1;32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[1;33m!\033[0m %s\n" "$*"; }
err()  { printf "  \033[1;31m✗\033[0m %s\n" "$*"; }

# ---- detect OS + package manager (for any auto-installs) ---------------------
OS="$(uname -s)"
PKG=""
if [ "$OS" = "Darwin" ]; then
  command -v brew    >/dev/null 2>&1 && PKG="brew"
elif [ "$OS" = "Linux" ]; then
  command -v apt-get >/dev/null 2>&1 && PKG="apt"
  [ -z "$PKG" ] && command -v dnf >/dev/null 2>&1 && PKG="dnf"
fi

# ---- 1. Python 3.10+ --------------------------------------------------------
find_python() {
  for c in python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "$c" >/dev/null 2>&1; then
      ver=$("$c" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null) || continue
      maj=${ver%%.*}; min=${ver##*.}
      if [ "$maj" -eq 3 ] && [ "$min" -ge 10 ]; then echo "$c"; return 0; fi
    fi
  done
  return 1
}
say "Checking for Python 3.10+"
PY="$(find_python || true)"
if [ -z "$PY" ]; then
  warn "Python 3.10+ not found — attempting to install…"
  case "$PKG" in
    brew) brew install python ;;
    apt)  sudo apt-get update -y && sudo apt-get install -y python3 python3-venv python3-pip ;;
    dnf)  sudo dnf install -y python3 python3-pip ;;
    *)    err "Couldn't auto-install. Get Python 3.10+ from https://www.python.org/downloads/ then re-run."; exit 1 ;;
  esac
  PY="$(find_python || true)"
  [ -z "$PY" ] && { err "Python still not found. Install it manually, then re-run."; exit 1; }
fi
ok "Using $($PY --version 2>&1)"

# ---- 2. Python venv + dependencies (idempotent) -----------------------------
say "Setting up the Python environment"
if [ ! -d .venv ]; then
  "$PY" -m venv .venv || { err "Couldn't create venv. On Debian/Ubuntu: sudo apt-get install python3-venv"; exit 1; }
  ok "Created .venv"
fi
VENV_PY=".venv/bin/python"
"$VENV_PY" -m pip install --quiet --upgrade pip >/dev/null 2>&1
if "$VENV_PY" -m pip install --quiet -r requirements.txt; then
  ok "Dependencies installed"
else
  err "Failed to install Python dependencies."; exit 1
fi

# ---- 3. Web UI (use the shipped build, else build it) -----------------------
say "Preparing the web UI"
if [ -f frontend/dist/index.html ]; then
  ok "UI is ready (using the included build)"
else
  warn "UI build not found — building it now (needs Node.js)…"
  if ! command -v npm >/dev/null 2>&1; then
    case "$PKG" in
      brew) brew install node ;;
      apt)  sudo apt-get install -y nodejs npm ;;
      dnf)  sudo dnf install -y nodejs npm ;;
      *)    err "Node.js is needed to build the UI. Install it from https://nodejs.org then re-run."; exit 1 ;;
    esac
  fi
  if ( cd frontend && npm install && npm run build ); then
    ok "UI built"
  else
    err "UI build failed. Install Node.js 18+ and re-run."; exit 1
  fi
fi

# ---- 4. Claude Code CLI (required for runs; best-effort install) ------------
say "Checking for the Claude Code CLI"
if command -v claude >/dev/null 2>&1; then
  ok "claude found ($(claude --version 2>&1 | head -1))"
else
  warn "claude not found — the benchmark needs it. Attempting to install…"
  if ! command -v npm >/dev/null 2>&1; then
    case "$PKG" in
      brew) brew install node ;;
      apt)  sudo apt-get install -y nodejs npm ;;
      dnf)  sudo dnf install -y nodejs npm ;;
    esac
  fi
  if command -v npm >/dev/null 2>&1; then
    npm install -g @anthropic-ai/claude-code 2>/dev/null \
      || sudo npm install -g @anthropic-ai/claude-code 2>/dev/null || true
  fi
  if command -v claude >/dev/null 2>&1; then
    ok "Installed claude — finish by running 'claude' once and logging in (/login)"
  else
    warn "Couldn't auto-install the Claude CLI. The app will still start, but"
    warn "benchmark RUNS will fail until you: install Node.js (https://nodejs.org),"
    warn "run 'npm install -g @anthropic-ai/claude-code', then 'claude' and /login."
  fi
fi

# ---- 5. Free a stale port (a previous instance that didn't exit cleanly) ----
if command -v lsof >/dev/null 2>&1; then
  STALE="$(lsof -ti tcp:8765 -sTCP:LISTEN 2>/dev/null || true)"
  if [ -n "$STALE" ]; then
    warn "Port 8765 is held by a previous instance (PID $STALE) — stopping it."
    kill $STALE 2>/dev/null; sleep 1
    STILL="$(lsof -ti tcp:8765 -sTCP:LISTEN 2>/dev/null || true)"
    [ -n "$STILL" ] && kill -9 $STILL 2>/dev/null && sleep 1
  fi
fi

# ---- 6. Launch --------------------------------------------------------------
say "Starting the A/B Benchmark"
ok "Opening $APP_URL  (press Ctrl+C here to stop)"
exec "$VENV_PY" app.py
