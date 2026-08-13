#!/usr/bin/env bash
# Container entrypoint: everything the harness treats as per-machine state
# (configs/ — including the Anthropic key and Bito token, runs/, judgments/,
# reports/, prompt_sets/, prompts.json, results.db*) is symlinked into a single
# mounted /data volume, so `docker run -v harness-data:/data ...` is the only
# volume a customer ever needs — see scripts/package.py's exclusion list, which
# already treats these exact paths as secrets/runtime state, never shipped code.
set -euo pipefail

mkdir -p /data/configs /data/runs /data/judgments /data/reports /data/prompt_sets
touch -a /data/results.db /data/prompts.json

for d in configs runs judgments reports prompt_sets; do
    rm -rf "/app/$d"
    ln -s "/data/$d" "/app/$d"
done
for f in results.db prompts.json; do
    rm -f "/app/$f"
    ln -s "/data/$f" "/app/$f"
done

exec python app.py --host 0.0.0.0 --port 8765 --no-browser
