# =============================================================================
#  A/B Benchmark - one-command setup and launch  (Windows)
#
#  Double-click start.bat at the root, or run:
#    powershell -ExecutionPolicy Bypass -File scripts\start.ps1
#
#  First run installs everything it can (Python deps, the web UI, and the
#  Claude Code CLI via winget), then starts the app at http://127.0.0.1:8765
#  and opens your browser. Every later run just launches it.
# =============================================================================
Set-Location (Join-Path $PSScriptRoot "..")   # project root (this script lives in scripts/)
$AppUrl = "http://127.0.0.1:8765"

function Say($m){ Write-Host "`n> $m" -ForegroundColor Cyan }
function OK($m){ Write-Host "  [ok] $m" -ForegroundColor Green }
function Warn($m){ Write-Host "  [!] $m" -ForegroundColor Yellow }
function Fail($m){ Write-Host "  [x] $m" -ForegroundColor Red }

$HasWinget = [bool](Get-Command winget -ErrorAction SilentlyContinue)
function Refresh-Path {
  $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
              [System.Environment]::GetEnvironmentVariable("Path","User")
}

# ---- 1. Python 3.10+ --------------------------------------------------------
function Find-Python {
  foreach ($c in @("python","python3","py")) {
    if (Get-Command $c -ErrorAction SilentlyContinue) {
      try { $v = & $c -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null } catch { continue }
      if ($v) { $p = "$v".Trim().Split("."); if ($p.Length -ge 2 -and [int]$p[0] -eq 3 -and [int]$p[1] -ge 10) { return $c } }
    }
  }
  return $null
}
Say "Checking for Python 3.10+"
$py = Find-Python
if (-not $py) {
  Warn "Python 3.10+ not found - attempting to install..."
  if ($HasWinget) {
    winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
    Refresh-Path; $py = Find-Python
  }
  if (-not $py) { Fail "Couldn't auto-install. Get Python 3.10+ from https://www.python.org/downloads/ then re-run."; exit 1 }
}
OK "Using Python ($py)"

# ---- 2. Python venv + dependencies (idempotent) -----------------------------
Say "Setting up the Python environment"
$VenvPy = ".\.venv\Scripts\python.exe"
if (-not (Test-Path ".venv")) { & $py -m venv .venv }
if (-not (Test-Path $VenvPy)) { Fail "Couldn't create the Python virtual environment (.venv). Re-run, or reinstall Python."; exit 1 }
OK ".venv ready"
& $VenvPy -m pip install --quiet --upgrade pip
& $VenvPy -m pip install --quiet -r requirements.txt
if ($LASTEXITCODE -ne 0) { Fail "Failed to install Python dependencies."; exit 1 }
OK "Dependencies installed"

# ---- 3. Web UI (use the shipped build, else build it) -----------------------
Say "Preparing the web UI"
if (Test-Path "frontend\dist\index.html") {
  OK "UI is ready (using the included build)"
} else {
  Warn "UI build not found - building it now (needs Node.js)..."
  if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    if ($HasWinget) { winget install -e --id OpenJS.NodeJS.LTS --accept-source-agreements --accept-package-agreements; Refresh-Path }
  }
  if (Get-Command npm -ErrorAction SilentlyContinue) {
    Push-Location frontend
    npm install
    npm run build
    $built = ($LASTEXITCODE -eq 0)
    Pop-Location
    if ($built) { OK "UI built" } else { Fail "UI build failed."; exit 1 }
  } else { Fail "Node.js is needed to build the UI. Install from https://nodejs.org then re-run."; exit 1 }
}

# ---- 4. Claude Code CLI (required for runs; best-effort install) ------------
Say "Checking for the Claude Code CLI"
if (Get-Command claude -ErrorAction SilentlyContinue) {
  OK "claude found"
} else {
  Warn "claude not found - the benchmark needs it. Attempting to install..."
  if (-not (Get-Command node -ErrorAction SilentlyContinue) -and $HasWinget) {
    winget install -e --id OpenJS.NodeJS.LTS --accept-source-agreements --accept-package-agreements; Refresh-Path
  }
  if (Get-Command npm -ErrorAction SilentlyContinue) { npm install -g @anthropic-ai/claude-code; Refresh-Path }
  if (Get-Command claude -ErrorAction SilentlyContinue) {
    OK "Installed claude - finish by running 'claude' once and logging in (/login)"
  } else {
    Warn "Couldn't auto-install the Claude CLI. The app will still start, but RUNS"
    Warn "will fail until you install Node.js (https://nodejs.org), run"
    Warn "'npm install -g @anthropic-ai/claude-code', then 'claude' and /login."
  }
}

# ---- 5. Free a stale port (a previous instance that didn't exit cleanly) ----
$conns = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
if ($conns) {
  Warn "Port 8765 is held by a previous instance - stopping it."
  foreach ($procId in ($conns.OwningProcess | Select-Object -Unique)) {
    Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
  }
  Start-Sleep -Seconds 1
}

# ---- 6. Launch --------------------------------------------------------------
Say "Starting the A/B Benchmark"
OK "Opening $AppUrl  (press Ctrl+C here to stop)"
& $VenvPy app.py
