#!/bin/sh
# Install a built package in a fresh container, check that it works, and remove it again.
#
# usage: pkg/smoke-test.sh PACKAGE IMAGE
#   e.g. pkg/smoke-test.sh dist/procular_1.2.3_all.deb docker.io/library/debian:13
set -e

if [ "$1" = --in-container ]; then
  pkg=/pkg/$(basename "$2")
  case "$pkg" in
    *.deb) export DEBIAN_FRONTEND=noninteractive
           apt-get update -qq
           apt-get install -y -qq "$pkg" >/dev/null ;;
    *.rpm) dnf install -y -q "$pkg" ;;
  esac
  set -x
  # starts as far as GApplication's own option parsing (no display in here)
  procular --help | grep -q -- --help
  python3 -sPB - <<'EOF'  # flags as in /usr/bin/procular
import sys
sys.path.insert(0, "/usr/share/procular")
import procular.windows.main_window  # every module of the UI imports
from procular import _version
from procular.util import privileged
assert privileged.helper_command() == ["pkexec", "/usr/libexec/procular-helper"], privileged.helper_command()
assert _version.get_version() != _version.FALLBACK
print("version", _version.get_version())
EOF
  grep -q '>/usr/libexec/procular-helper<' /usr/share/polkit-1/actions/io.github.ouned.Procular.policy
  test "$(/usr/libexec/procular-helper bogus || true)" = '{"ok": false, "error": "unknown operation"}'
  test -f /usr/share/metainfo/io.github.ouned.Procular.metainfo.xml
  test -f /usr/share/icons/hicolor/scalable/apps/io.github.ouned.Procular.svg
  case "$pkg" in
    *.deb) apt-get remove -y -qq procular >/dev/null ;;
    *.rpm) dnf remove -y -q procular ;;
  esac
  test ! -e /usr/share/procular && test ! -e /usr/bin/procular
  exit
fi

[ $# = 2 ] || { echo "usage: $0 PACKAGE IMAGE" >&2; exit 2; }
PKG=$(readlink -f "$1")
HERE=$(cd "$(dirname "$0")" && pwd)
podman run --rm --network=host --security-opt label=disable \
  -v "$(dirname "$PKG"):/pkg:ro" -v "$HERE/smoke-test.sh:/smoke-test.sh:ro" \
  "$2" sh /smoke-test.sh --in-container "$PKG"
echo "OK: $(basename "$PKG") on $2"
