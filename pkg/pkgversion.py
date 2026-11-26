"""Turn Procular's version (procular/_version.py) into a .deb or .rpm package version.

    1.2.3                    -> 1.2.3
    1.2.3rc1, 1.2.3-beta.1   -> 1.2.3~rc1, 1.2.3~beta.1     (sorts before 1.2.3)
    1.2.3-4-gabc1234         -> 1.2.3+git4.gabc1234 (deb)   (sorts after 1.2.3)
                                1.2.3^git4.gabc1234 (rpm)
    0.0.0-gabc1234           -> 0.0.0+gabc1234, 0.0.0^gabc1234  (no tag yet)
    ...-dirty                -> ....dirty

usage: python3 pkg/pkgversion.py deb|rpm [VERSION]   (default: the version of this checkout)
"""

import os
import re
import sys

_PATTERN = re.compile(
    r"(?P<release>\d+(?:\.\d+)*)"
    r"(?:[-._]?(?P<pre>(?:a|b|c|rc|alpha|beta|pre|preview)(?:[._]?\d+)*))?"
    r"(?:-(?P<distance>\d+)-(?P<commit>g[0-9a-f]+)|-(?P<bare>g[0-9a-f]+))?"
    r"(?P<dirty>-dirty)?")


def package_version(version, fmt):
    if fmt not in ("deb", "rpm"):
        raise ValueError(f"unknown package format {fmt!r}")
    m = _PATTERN.fullmatch(version.strip().removeprefix("v"))
    if not m:
        raise ValueError(f"cannot turn version {version!r} into a {fmt} version")
    out = m["release"]
    if m["pre"]:
        out += "~" + m["pre"].replace("_", ".")
    post = []
    if m["distance"]:
        post.append(f"git{m['distance']}.{m['commit']}")
    elif m["bare"]:
        post.append(m["bare"])
    if m["dirty"]:
        post.append("dirty")
    if post:
        out += ("+" if fmt == "deb" else "^") + ".".join(post)
    return out


def main(argv):
    if len(argv) not in (2, 3):
        sys.exit(__doc__.rsplit("usage: ", 1)[1].strip())
    if len(argv) == 3:
        version = argv[2]
    else:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "procular"))
        from _version import get_version
        version = get_version()
    try:
        print(package_version(version, argv[1]))
    except ValueError as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main(sys.argv)
