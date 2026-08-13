@echo off
REM Double-click this on Windows to set up (first time) and launch the A/B Benchmark.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1"
echo.
echo If a window error appeared above, read the messages, then close this window.
pause
