#!/bin/sh
# Render the README screenshots. Procular runs off-screen (tools/headless-session.sh) next to a spiky demo
# load (tools/demo-load.py) until its graphs are full, then renders every shot of
# tools/readme-shots.json. One instance per colour scheme, side by side, so that both see the
# same load and the warm-up is needed once.
#
# usage: tools/readme-shots.sh [OUTDIR] [--net URL]     OUTDIR default: docs/screenshots
#   --net URL   also download URL now and then, for the network graphs (off by default)
#   PROCULAR_SHOT_WARMUP=seconds (env) shorter warm-up for trying out the plan
#   PROCULAR_SHOT_THEMES="light dark" (env, the default) the colour schemes to render
#   PROCULAR_SHOT_ONLY="processes history" (env) just these shots of the plan
# Afterwards oxipng (if installed) recompresses the PNGs losslessly.
set -e
cd "$(dirname "$0")/.."
OUT=$(realpath -m "${1:-docs/screenshots}")
[ $# -gt 0 ] && shift
TMP=$(mktemp -d)
PIDS=
LOAD=
UNIT=
# the load removes its scratch file when it ends: wait for it (systemctl stop waits, too)
trap 'set +e; kill $PIDS 2>/dev/null
  if [ -n "$UNIT" ]; then systemctl --user stop "$UNIT"; elif [ -n "$LOAD" ]; then kill $LOAD; wait $LOAD; fi
  rm -rf "$TMP"' EXIT INT TERM

# As a transient user service the load hangs right below "systemd --user" in the process tree,
# not deep below the terminal that started this script.
if systemd-run --user --quiet --collect --unit="procular-demo-$$" \
     python3 "$PWD/tools/demo-load.py" 0 "$@" 2>/dev/null; then
  UNIT="procular-demo-$$"
else
  python3 tools/demo-load.py 0 "$@" &
  LOAD=$!
fi

python3 - "$TMP/plan.json" <<'PY'
import json, os, sys
plan = json.load(open("tools/readme-shots.json"))
if os.environ.get("PROCULAR_SHOT_WARMUP"):
    plan["warmup"] = float(os.environ["PROCULAR_SHOT_WARMUP"])
if os.environ.get("PROCULAR_SHOT_ONLY"):
    only = os.environ["PROCULAR_SHOT_ONLY"].split()
    plan["shots"] = [shot for shot in plan["shots"] if shot["name"] in only]
json.dump(plan, open(sys.argv[1], "w"))
print(f"warming up for {plan['warmup']:.0f}s, then {len(plan['shots'])} shots per theme")
PY
WARMUP=$(python3 -c 'import json,sys; print(int(json.load(open(sys.argv[1]))["warmup"]))' "$TMP/plan.json")

run() {  # theme: one off-screen session per colour scheme
  mkdir -p "$TMP/$1"
  # a fast refresh fills the graphs sooner; columns that show the heat colours, and a name column
  # wide enough for the deep branch of the demo load
  cat > "$TMP/$1/settings.json" <<'JSON'
{"interval_ms": 500, "background_slowdown": false, "about_music": false, "tree_mode": true, "highlight_seconds": 1,
 "column_widths": {"name": 420},
 "columns": ["name", "pid", "cpu", "gpu", "private", "io_total", "user", "cmdline"],
 "sort": {"processes": {"column": "cpu", "descending": true}}}
JSON
  PROCULAR_CONFIG="$TMP/$1/settings.json" PROCULAR_SCREENSHOT="$OUT" PROCULAR_SHOT_PLAN="$TMP/plan.json" \
    PROCULAR_SHOT_THEME="$1" \
    tools/headless-session.sh timeout $((WARMUP + 240)) python3 tools/shots.py || { echo "$1: failed"; return 1; }
}

# one instance per theme, side by side (PROCULAR_SHOT_THEMES="dark" renders just that one)
for theme in ${PROCULAR_SHOT_THEMES:-light dark}; do
  run "$theme" &
  PIDS="$PIDS $!"
done
STATUS=0
for pid in $PIDS; do
  wait "$pid" || STATUS=1
done

# lossless recompression: every regeneration ends up in the git history
if [ $STATUS -eq 0 ]; then
  if command -v oxipng >/dev/null; then
    oxipng -q -o max --strip safe "$OUT"/*.png
  else
    echo "oxipng not found: screenshots left uncompressed (cargo install oxipng)"
  fi
fi
exit $STATUS
