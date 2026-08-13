#!/usr/bin/env bash
# Double-click this on macOS to set up (first time) and launch the A/B Benchmark.
# It runs scripts/start.sh and keeps the window open on error.
cd "$(dirname "$0")" || exit 1
bash ./scripts/start.sh
status=$?
if [ $status -ne 0 ]; then
  echo
  echo "Setup/launch exited with an error (code $status). Read the messages above."
  echo "Press Return to close this window."
  read -r _
fi
