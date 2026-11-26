#!/bin/sh
# Run a command in an off-screen Wayland session: a headless GNOME compositor (mutter --headless)
# with one virtual monitor. GTK renders as on a desktop, with its regular GPU renderer (or Mesa's
# software one where there is no GPU), and windows can be rendered to PNG (tools/shots.py).
#
# usage: tools/headless-session.sh COMMAND [ARGS...]
#   e.g. tools/headless-session.sh python3 -m pytest
#   PROCULAR_HEADLESS_SIZE=WxH (env) size of the virtual monitor, default 1920x1080
#   PROCULAR_HEADLESS_TIMEOUT=seconds (env) how long to wait for mutter to start, default 60
#
# Needs mutter and dbus-run-session (Debian/Ubuntu: mutter dbus-daemon; Fedora: mutter dbus-daemon).
#
# mutter gets a D-Bus session bus of its own: on the desktop's bus it would announce its display
# to the whole session (activation environment), and apps started later would open on it. The
# command keeps the caller's bus (Services tab: user units), if there is one. mutter gets no
# system bus: it needs none of its services (logind, RealtimeKit, locale) when headless, and
# waiting for them has delayed its start by many seconds on CI runners.
#
# The desktop's settings (font, text scaling, icon theme, dark style) are not used, as on a
# machine without a desktop: GTK's and libadwaita's defaults, plus a settings.ini if there is one.
# That keeps screenshots and tests independent of the machine they run on.
set -e
[ $# -gt 0 ] || { echo "usage: $0 COMMAND [ARGS...]" >&2; exit 2; }
for tool in mutter dbus-run-session; do
  command -v "$tool" >/dev/null || { echo "$0: $tool not found (packages: mutter dbus-daemon)" >&2; exit 2; }
done

# a runtime directory for the Wayland socket (containers and CI jobs may have none)
if [ -z "$XDG_RUNTIME_DIR" ] || [ ! -w "$XDG_RUNTIME_DIR" ]; then
  XDG_RUNTIME_DIR=$(mktemp -d /tmp/procular-runtime.XXXXXX)
  OWN_RUNTIME=$XDG_RUNTIME_DIR
  export XDG_RUNTIME_DIR
fi
LOG=$(mktemp -d /tmp/procular-headless.XXXXXX)
SOCKET=procular-headless-$$  # relative to XDG_RUNTIME_DIR: older mutter takes no absolute path
MUTTER=
RUNNER=
cleanup() {  # keeps the exit status
  status=$?
  set +e  # a mutter ended by the signal makes wait fail
  # also when mutter never came up: it is running all the same, and dbus-run-session waits for it
  if [ -z "$MUTTER" ] && [ -n "$RUNNER" ] && kill -0 "$RUNNER" 2>/dev/null; then
    for _ in 1 2 3 4 5 6 7 8 9 10; do [ -s "$LOG/mutter.pid" ] && break; sleep 0.1; done
    [ -s "$LOG/mutter.pid" ] && MUTTER=$(cat "$LOG/mutter.pid")
  fi
  if [ -n "$MUTTER" ]; then
    kill "$MUTTER" 2>/dev/null  # dbus-run-session then ends its bus and exits
    [ -n "$RUNNER" ] && wait "$RUNNER" 2>/dev/null
  fi
  rm -rf "$LOG" ${OWN_RUNTIME:+"$OWN_RUNTIME"}
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# GIO_USE_VFS=local: no gvfs daemon on mutter's private bus
# shellcheck disable=SC2016  # $0 and $@ belong to the inner shell, which records mutter's PID
env -u DISPLAY -u WAYLAND_DISPLAY DBUS_SYSTEM_BUS_ADDRESS=unix:path=/nonexistent GIO_USE_VFS=local \
  dbus-run-session -- \
  sh -c 'echo $$ >"$0"; exec "$@"' "$LOG/mutter.pid" \
  mutter --headless --wayland --no-x11 --virtual-monitor "${PROCULAR_HEADLESS_SIZE:-1920x1080}" \
  --wayland-display "$SOCKET" >"$LOG/mutter.log" 2>&1 &
RUNNER=$!

TIMEOUT=${PROCULAR_HEADLESS_TIMEOUT:-60}
i=0
until [ -S "$XDG_RUNTIME_DIR/$SOCKET" ]; do
  i=$((i + 1))
  if ! kill -0 "$RUNNER" 2>/dev/null; then
    echo "$0: mutter exited:" >&2
    cat "$LOG/mutter.log" >&2
    exit 1
  elif [ $i -gt $((TIMEOUT * 10)) ]; then
    echo "$0: mutter did not start within $TIMEOUT s:" >&2
    cat "$LOG/mutter.log" >&2
    exit 1
  fi
  sleep 0.1
done
[ $i -gt 50 ] && echo "$0: mutter took $((i / 10)) s to start" >&2
MUTTER=$(cat "$LOG/mutter.pid")

# no session bus at all (containers, CI): a private one, or GTK warns and GApplication has no bus.
# It has no accessibility bus either, which GTK would also warn about.
[ -n "$DBUS_SESSION_BUS_ADDRESS" ] || set -- dbus-run-session -- env GTK_A11Y=none "$@"
set +e
# no-portals and default-settings: neither the settings portal nor GSettings (older GTK reads
# that when there is no portal); ADW_DISABLE_PORTAL, GSETTINGS_BACKEND: the same for libadwaita
# PROCULAR_HEADLESS: tests/test_ui.py runs only here
env -u DISPLAY PROCULAR_HEADLESS=1 WAYLAND_DISPLAY="$SOCKET" GDK_BACKEND=wayland \
  GDK_DEBUG="${GDK_DEBUG:+$GDK_DEBUG,}no-portals,default-settings" ADW_DISABLE_PORTAL=1 GSETTINGS_BACKEND=memory \
  "$@"
