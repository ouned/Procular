#!/bin/sh
# Run Procular from this source tree on your desktop, but with the libraries of an older
# distribution (rootless Podman container, see tools/compat/).
#
# usage: tools/compat-run.sh [TARGET]      TARGET: tools/compat/<TARGET>.Containerfile
#                                          (default: ubuntu-24.04)
#   MIRROR=http://de.archive.ubuntu.com/ubuntu  (env) package mirror for building the image
#
# Works on Wayland (and on X11 through DISPLAY). Limits of the container: other users'
# processes show as owned by "nobody", "Run as Administrator" actions are unavailable (no
# pkexec inside), and settings start fresh on every run.
set -e
cd "$(dirname "$0")/.."
TARGET=${1:-ubuntu-24.04}
IMAGE="localhost/procular-compat:$TARGET"
if ! podman image exists "$IMAGE"; then  # tools/compat-test.sh rebuilds it when the Containerfile changes
  podman build -q --network=host ${MIRROR:+--build-arg MIRROR="$MIRROR"} -t "$IMAGE" \
    -f "tools/compat/$TARGET.Containerfile" tools/compat >/dev/null
fi

RUNDIR=/run/user/$(id -u)
set -- --rm --pid=host --network=host --userns=keep-id --group-add keep-groups \
  --security-opt label=disable \
  -v "$PWD:/src:ro" -w /src -e HOME=/tmp -e PROCULAR_CONFIG=/tmp/procular.json \
  -v /run/dbus/system_bus_socket:/run/dbus/system_bus_socket \
  -v "$XDG_RUNTIME_DIR/bus:$RUNDIR/bus" -e DBUS_SESSION_BUS_ADDRESS="unix:path=$RUNDIR/bus" \
  -e XDG_RUNTIME_DIR="$RUNDIR"
[ -e /dev/dri ] && set -- "$@" --device /dev/dri
# your desktop settings (font, text scaling, dark style), read-only
DCONF_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/dconf
[ -d "$DCONF_DIR" ] && set -- "$@" -v "$DCONF_DIR:/tmp/.config/dconf:ro"
[ -d "$XDG_RUNTIME_DIR/dconf" ] && set -- "$@" -v "$XDG_RUNTIME_DIR/dconf:$RUNDIR/dconf"
# Ubuntu's defaults (font etc.) only apply to the "ubuntu" desktop
[ -n "$XDG_CURRENT_DESKTOP" ] && set -- "$@" -e XDG_CURRENT_DESKTOP
[ -d "$XDG_RUNTIME_DIR/at-spi" ] && set -- "$@" -v "$XDG_RUNTIME_DIR/at-spi:$RUNDIR/at-spi"
if [ -n "$WAYLAND_DISPLAY" ]; then
  set -- "$@" -v "$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY:$RUNDIR/$WAYLAND_DISPLAY" \
    -e WAYLAND_DISPLAY -e GDK_BACKEND=wayland
elif [ -n "$DISPLAY" ]; then
  set -- "$@" -v /tmp/.X11-unix:/tmp/.X11-unix -e DISPLAY -e GDK_BACKEND=x11
  [ -n "$XAUTHORITY" ] && set -- "$@" -v "$XAUTHORITY:/tmp/.Xauthority:ro" -e XAUTHORITY=/tmp/.Xauthority
fi
# the image's settings.ini (font, DPI) is for headless screenshots; a desktop session has its own
exec podman run "$@" -v "$PWD/tools/compat/gtk-settings-desktop.ini:/etc/gtk-4.0/settings.ini:ro" \
  "$IMAGE" python3 -m procular
