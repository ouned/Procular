"""Print the library stack Procular runs on (used by tools/compat-test.sh and CI)."""

import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
import psutil  # noqa: E402
from gi.repository import Adw, Gtk  # noqa: E402


def version(module):
    return ".".join(str(f()) for f in (module.get_major_version, module.get_minor_version, module.get_micro_version))


print(f"GTK {version(Gtk)}, libadwaita {version(Adw)}, Python {sys.version.split()[0]}, "
      f"PyGObject {gi.__version__}, psutil {psutil.__version__}")
