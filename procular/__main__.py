import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from .util import process_name  # noqa: E402

MIN_GTK = (4, 12)
MIN_ADW = (1, 5)


def _check_versions():
    """Error message if the installed GTK or libadwaita is too old, else None."""
    gtk = (Gtk.get_major_version(), Gtk.get_minor_version())
    adw = (Adw.get_major_version(), Adw.get_minor_version())
    problems = [f"{name} {'.'.join(map(str, need))} or newer is required (found {'.'.join(map(str, have))})"
                for name, have, need in (("GTK", gtk, MIN_GTK), ("libadwaita", adw, MIN_ADW)) if have < need]
    return "Procular cannot start: " + "; ".join(problems) if problems else None


def main():
    process_name.set_name("procular")
    error = _check_versions()
    if error:
        print(error, file=sys.stderr)
        return 1
    from .app import ProcularApp

    app = ProcularApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
