#!/usr/bin/env bash
# Reset the working copy to a fresh-install state for re-testing (macOS / Linux).
# Deletes everything regenerable — build artifacts (node_modules, package-lock,
# dist, venv, caches) AND runtime state (Bito config/token, results, prompts).
# The launcher (./start.sh) recreates all of it.
#
#   ./scripts/clean.sh      then      ./start.sh
set -e
cd "$(dirname "$0")/.."   # project root (this script lives in scripts/)

rm -rf configs dist-package .venv \
       frontend/dist frontend/node_modules frontend/package-lock.json \
       results.db results.db-wal results.db-shm \
       prompts.json prompt_sets runs judgments reports indexed-repos.txt
find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
find . -name '*.pyc' -delete 2>/dev/null || true
find . -name .DS_Store -exec rm -f {} + 2>/dev/null || true

echo "Cleaned. Run ./start.sh to set up and launch fresh."
