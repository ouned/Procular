#!/bin/sh
# Run the tests (unit and UI) and a set of screenshots against an older
# GTK / libadwaita stack in a rootless Podman container, and compare the screenshots
# with the same views rendered on this machine.
#
# usage: tools/compat-test.sh [TARGET] [OUTDIR]
#   TARGET  a tools/compat/<TARGET>.Containerfile (default: ubuntu-24.04)
#   OUTDIR  default: ./compat-out/<TARGET>
#             <view>.png          rendered in the container
#             host/<view>.png     rendered on this machine with the same font and DPI
#             compare/<view>.png  side by side: this machine left, container right
#   MIRROR=http://de.archive.ubuntu.com/ubuntu  (env) package mirror for building the image
#
# The container shares the host's process list, network and D-Bus, so Procular inside
# it monitors this machine; only the libraries come from the container image. Rootless
# Podman cannot map other users' IDs, so their processes show as owned by "nobody".
set -e
cd "$(dirname "$0")/.."
TARGET=${1:-ubuntu-24.04}
OUT=$(realpath -m "${2:-compat-out/$TARGET}")
IMAGE="localhost/procular-compat:$TARGET"
mkdir -p "$OUT/host" "$OUT/compare"

echo "== building $IMAGE"
podman build -q --network=host ${MIRROR:+--build-arg MIRROR="$MIRROR"} -t "$IMAGE" \
  -f "tools/compat/$TARGET.Containerfile" tools/compat >/dev/null

# a process with plenty of capability chips for the Properties screenshots
PROPS_PID=$(pgrep -x systemd-logind | head -n1 || true)
PROPS_PID=${PROPS_PID:-1}
SHOTS="processes -
services page:services
sysinfo sysinfo
preferences prefs
properties props:$PROPS_PID
properties-chips props:$PROPS_PID,propsscroll"

# the accessibility bus, whose address GTK gets from the session bus (else a Gtk-CRITICAL)
ATSPI=
[ -d "$XDG_RUNTIME_DIR/at-spi" ] && ATSPI="$XDG_RUNTIME_DIR/at-spi:/run/user/$(id -u)/at-spi"
run() {
  podman run --rm --pid=host --network=host --userns=keep-id \
    -v "$PWD:/src:ro" -v "$OUT:/out" -w /src \
    -v /run/dbus/system_bus_socket:/run/dbus/system_bus_socket \
    -v "$XDG_RUNTIME_DIR/bus:/run/user/host-bus" ${ATSPI:+-v "$ATSPI"} \
    -e DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/host-bus -e HOME=/tmp \
    "$IMAGE" "$@" </dev/null
}

echo "== $TARGET"
run python3 tools/print-versions.py

echo "== tests (unit and UI, off-screen)"
if ! run env PROCULAR_DEBUG_LIFECYCLE=1 tools/headless-session.sh python3 -m pytest -q -p no:cacheprovider \
    >"$OUT/pytest.log" 2>&1; then
  tail -n 40 "$OUT/pytest.log"; echo "tests FAILED (log: $OUT/pytest.log)"; exit 1
fi
tail -n 1 "$OUT/pytest.log"

echo "== screenshots -> $OUT"
HOSTCFG=$(mktemp -d)
trap 'rm -rf "$HOSTCFG"' EXIT
mkdir -p "$HOSTCFG/gtk-4.0" && cp tools/compat/gtk-settings.ini "$HOSTCFG/gtk-4.0/settings.ini"
echo "$SHOTS" | while read -r name actions; do
  [ "$actions" = - ] && actions=
  run tools/headless-shot.sh "/out/$name.png" "$actions" 6 2>&1 | grep -E "Error|Traceback" || true
  XDG_CONFIG_HOME="$HOSTCFG" \
    tools/headless-shot.sh "$OUT/host/$name.png" "$actions" 6 </dev/null 2>&1 | grep -E "Error|Traceback" || true
  ls "$OUT/$name.png" "$OUT/host/$name.png" >/dev/null 2>&1 && echo "  $name" || echo "  $name: screenshot missing"
done

python3 - "$OUT" <<'EOF'
import os, sys
import gi
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf

out = sys.argv[1]
for name in sorted(f for f in os.listdir(f"{out}/host") if f.endswith(".png")):
    try:
        a = GdkPixbuf.Pixbuf.new_from_file(f"{out}/host/{name}")
        b = GdkPixbuf.Pixbuf.new_from_file(f"{out}/{name}")
    except Exception:  # noqa: BLE001 - one side missing
        continue
    pair = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, a.get_width() + b.get_width() + 16,
                                max(a.get_height(), b.get_height()))
    pair.fill(0x808080ff)
    a.copy_area(0, 0, a.get_width(), a.get_height(), pair, 0, 0)
    b.copy_area(0, 0, b.get_width(), b.get_height(), pair, a.get_width() + 16, 0)
    pair.savev(f"{out}/compare/{name}", "png", [], [])
print(f"== side by side (this machine left, container right): {out}/compare/")
EOF
