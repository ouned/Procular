"""The version, taken from git tags (v1.2.3 or 1.2.3).

Resolution order:
  1. `git describe` when running from a git checkout of Procular itself
     ("1.2.3" on a tag, "1.2.3-4-gabc1234" past it, "-dirty" with local changes);
  2. _version.txt next to this file, written by install.sh and by the pip build (setuptools-scm);
  3. the installed package metadata;
  4. "0.0.0" when none of these is available.

Standalone on purpose (no gi import), so install.sh can run it as a script to stamp the version.
"""

import os
import re
import subprocess

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_FILE = os.path.join(_HERE, "_version.txt")
FALLBACK = "0.0.0"


def _from_git(root=_ROOT):
    # Only our own checkout: an install below a git-managed $HOME must not pick up that repo's tags.
    if not os.path.exists(os.path.join(root, ".git")):
        return None
    try:
        out = subprocess.run(
            ["git", "-C", root, "describe", "--tags", "--long", "--dirty", "--always",
             "--match", "v[0-9]*", "--match", "[0-9]*"],
            capture_output=True, text=True, timeout=2, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):  # (a tag that is not UTF-8)
        return None
    return _normalize(out)


def _normalize(described):
    """Turn `git describe --long` output into "1.2.3", "1.2.3-4-gabc1234" or "0.0.0-gabc1234"."""
    if not described:
        return None
    m = re.fullmatch(r"v?(.+)-(\d+)-(g[0-9a-f]+)(-dirty)?", described)
    if not m:  # no tag reachable: --always gave just the abbreviated hash (plus -dirty)
        return f"{FALLBACK}-g{described}"
    tag, distance, commit, dirty = m.groups()
    version = tag if distance == "0" else f"{tag}-{distance}-{commit}"
    return version + (dirty or "")


def _from_file(path=_FILE):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def _from_metadata():
    try:
        from importlib.metadata import PackageNotFoundError, version
        return version("procular")
    except (ImportError, PackageNotFoundError):
        return None


def get_version():
    return _from_git() or _from_file() or _from_metadata() or FALLBACK


if __name__ == "__main__":
    print(get_version())
