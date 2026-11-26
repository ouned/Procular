#!/bin/sh
# Render Procular off-screen (tools/headless-session.sh) to a PNG.
# usage: tools/headless-shot.sh OUT.png [ACTIONS] [DELAY]
#   ACTIONS: see tools/shots.py, e.g. "page:services" or "sysinfo,sysinfopage:cpu"
set -e
OUT=$(realpath -m "$1"); ACTIONS=${2:-}; DELAY=${3:-6}
cd "$(dirname "$0")/.."
CFG=$(mktemp -d)
trap 'rm -rf "$CFG"' EXIT
PROCULAR_CONFIG="$CFG/settings.json" PROCULAR_SCREENSHOT="$OUT" PROCULAR_SHOT_ACTION="$ACTIONS" \
  PROCULAR_SHOT_DELAY="$DELAY" \
  tools/headless-session.sh timeout $((${DELAY%.*} + 20)) python3 tools/shots.py
