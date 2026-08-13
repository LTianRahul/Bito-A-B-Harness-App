# Reset the working copy to a fresh-install state for re-testing (Windows).
# Deletes everything regenerable — build artifacts (node_modules, package-lock,
# dist, venv, caches) AND runtime state (Bito config/token, results, prompts).
# The launcher (start.bat) recreates all of it.
#
#   powershell -ExecutionPolicy Bypass -File scripts\clean.ps1      then      start.bat
Set-Location (Join-Path $PSScriptRoot "..")

foreach ($p in @("configs","dist-package",".venv","frontend\dist","frontend\node_modules",
                 "frontend\package-lock.json","results.db","results.db-wal","results.db-shm",
                 "prompts.json","prompt_sets","runs","judgments","reports","indexed-repos.txt")) {
  if (Test-Path $p) { Remove-Item -Recurse -Force $p }
}
Get-ChildItem -Recurse -Directory -Filter __pycache__ -ErrorAction SilentlyContinue |
  Remove-Item -Recurse -Force
Get-ChildItem -Recurse -Filter *.pyc -ErrorAction SilentlyContinue |
  Remove-Item -Force
Get-ChildItem -Recurse -Filter .DS_Store -ErrorAction SilentlyContinue |
  Remove-Item -Force

Write-Host "Cleaned. Run start.bat (or start.ps1) to set up and launch fresh."
