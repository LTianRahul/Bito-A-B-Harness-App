#!/usr/bin/env bash
# Container entrypoint — runs as root so it can fix ownership on mounted
# volumes, then drops to the non-root "appuser" before starting the app.
#
# Everything the harness treats as per-machine state (configs/ — including the
# Anthropic key and Bito token, runs/, judgments/, reports/, prompt_sets/,
# prompts.json, results.db*) is symlinked into a single mounted /data volume,
# so `docker run -v harness-data:/data ...` is the only data volume a customer
# ever needs — see scripts/package.py's exclusion list, which already treats
# these exact paths as secrets/runtime state, never shipped code.
set -euo pipefail

mkdir -p /data/configs /data/runs /data/judgments /data/reports /data/prompt_sets
touch -a /data/results.db  # sqlite treats a 0-byte file as a valid empty db
# prompts.json is parsed as JSON on every read — a 0-byte file (what plain
# `touch` would leave a brand new one as) fails that parse ("Expecting value:
# line 1 column 1"), breaking Prompts entirely until something else happens to
# rewrite it. Seed it with a valid empty array instead, but only if it's
# missing or already empty — never touch a file that already has real prompts.
[ -s /data/prompts.json ] || echo '[]' > /data/prompts.json

for d in configs runs judgments reports prompt_sets; do
    rm -rf "/app/$d"
    ln -s "/data/$d" "/app/$d"
done
for f in results.db prompts.json; do
    rm -f "/app/$f"
    ln -s "/data/$f" "/app/$f"
done

# claude refuses --dangerously-skip-permissions as root, so the app must run as
# appuser — but a volume created/populated by an OLDER, root-run version of
# this image is still root-owned on disk, and Docker does NOT retroactively
# rechown an already-existing volume's contents when a new image mounts it
# (only a freshly-created empty one gets seeded from the image, ownership
# included). Fixing ownership here, every start, self-heals both cases: a
# pre-existing root-owned volume from before this fix, and a brand new one.
chown -R appuser:appuser /data /home/appuser/.claude /home/appuser/.config

exec setpriv --reuid=appuser --regid=appuser --clear-groups \
    python app.py --host 0.0.0.0 --port 8765 --no-browser
