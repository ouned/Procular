#!/bin/sh
# Build the Procular .deb or .rpm package.
#
# usage: pkg/build.sh deb|rpm [OUTDIR]          (OUTDIR default: dist)
#
# Builds in a rootless Podman container (deb: Ubuntu 24.04, the oldest supported stack;
# rpm: Fedora), so any Linux with Podman can build both. Both packages are architecture
# independent. Set NATIVE=1 to build on this system instead; it then needs the build tools:
#   deb: debhelper dh-python make python3         rpm: rpm-build python3-devel make desktop-file-utils
#
# The version comes from git (procular/_version.py) and is mapped by pkg/pkgversion.py.
set -e
DEB_IMAGE=docker.io/library/ubuntu:24.04
RPM_IMAGE=docker.io/library/fedora:45

# Inside the build environment: $1 = format, $2 = staged source dir, $3 = output dir.
build_deb() {
  cd "$2/procular"
  cp -r pkg/debian debian
  maintainer=$(sed -n 's/^Maintainer: //p' debian/control)
  date=$(LC_ALL=C date -R ${SOURCE_DATE_EPOCH:+-d "@$SOURCE_DATE_EPOCH"})
  printf 'procular (%s) unstable; urgency=medium\n\n  * Upstream version %s.\n\n -- %s  %s\n' \
    "$PKG_VERSION" "$VERSION" "$maintainer" "$date" > debian/changelog
  dpkg-buildpackage -b -us -uc -d  # -d: no build-essential, nothing is compiled
  cp ../procular_"$PKG_VERSION"_all.deb "$3/"
}

build_rpm() {
  top=$2/rpmbuild
  mkdir -p "$top/SOURCES"
  tar -C "$2" -czf "$top/SOURCES/procular.tar.gz" procular
  rpmbuild -bb --define "_topdir $top" --define "pkg_version $PKG_VERSION" "$2/procular/pkg/procular.spec"
  cp "$top"/RPMS/noarch/procular-"$PKG_VERSION"-*.noarch.rpm "$3/"
}

if [ "$1" = --in-container ]; then  # entry point inside the Podman container
  case "$2" in
    deb) export DEBIAN_FRONTEND=noninteractive
         apt-get update -qq
         apt-get install -y -qq --no-install-recommends debhelper dh-python make python3 >/dev/null ;;
    rpm) dnf install -y -q rpm-build python3-devel make desktop-file-utils tar gzip ;;
  esac
  build_"$2" "$2" /work /out
  exit
fi

FORMAT=$1
case "$FORMAT" in
  deb) IMAGE=$DEB_IMAGE ;;
  rpm) IMAGE=$RPM_IMAGE ;;
  *) echo "usage: $0 deb|rpm [OUTDIR]" >&2; exit 2 ;;
esac
SRC=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "${2:-dist}"
OUT=$(cd "${2:-dist}" && pwd)

VERSION=$(python3 "$SRC/procular/_version.py")
PKG_VERSION=$(python3 "$SRC/pkg/pkgversion.py" "$FORMAT" "$VERSION")
export VERSION PKG_VERSION
[ -n "$SOURCE_DATE_EPOCH" ] || SOURCE_DATE_EPOCH=$(git -C "$SRC" log -1 --format=%ct 2>/dev/null || date +%s)
export SOURCE_DATE_EPOCH
echo "Building procular $PKG_VERSION ($FORMAT)"

# Stage only what the package is made of, with the version stamped in (no .git in there).
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
mkdir "$WORK/procular"
(cd "$SRC" && tar -c --exclude=__pycache__ --exclude='*.pyc' procular data pkg Makefile LICENSE README.md) \
  | tar -x -C "$WORK/procular"
echo "$VERSION" > "$WORK/procular/procular/_version.txt"

if [ -n "$NATIVE" ]; then
  build_"$FORMAT" "$FORMAT" "$WORK" "$OUT"
else
  podman run --rm --network=host --security-opt label=disable \
    -e VERSION -e PKG_VERSION -e SOURCE_DATE_EPOCH \
    -v "$WORK:/work" -v "$OUT:/out" "$IMAGE" sh /work/procular/pkg/build.sh --in-container "$FORMAT"
fi
ls -l "$OUT"/procular[_-]"$PKG_VERSION"[_-]*."$FORMAT"
