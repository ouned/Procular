import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

@pytest.fixture
def gtk():
    """For tests that create real GTK widgets: skips them where GTK has no display.

    Without one, GTK 4.24 still reports a successful init but segfaults on the first widget
    (CI runs pytest in tools/headless-session.sh so these tests run there).
    """
    import procular  # noqa: F401  (pins the Gtk 4 typelib)
    from gi.repository import Gdk, Gtk

    if not Gtk.init_check() or Gdk.Display.get_default() is None:
        pytest.skip("needs a display")
