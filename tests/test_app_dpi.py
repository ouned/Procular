from types import SimpleNamespace
from unittest import mock

from procular import app


def run_fix(adw_minor, dpi):
    settings = SimpleNamespace(props=SimpleNamespace(gtk_xft_dpi=dpi))
    with mock.patch.object(app.Gtk.Settings, "get_default", return_value=settings), \
         mock.patch.object(app.Adw, "get_major_version", return_value=1), \
         mock.patch.object(app.Adw, "get_minor_version", return_value=adw_minor):
        app._fix_unset_dpi()
    return settings.props.gtk_xft_dpi


def test_unset_dpi_gets_96_on_libadwaita_1_5():
    assert run_fix(5, -1) == 96 * 1024
    assert run_fix(5, 0) == 96 * 1024


def test_known_dpi_is_kept():
    assert run_fix(5, 107151) == 107151


def test_newer_libadwaita_is_left_alone():
    assert run_fix(6, -1) == -1


def test_no_settings_object():
    with mock.patch.object(app.Gtk.Settings, "get_default", return_value=None):
        app._fix_unset_dpi()  # must not raise
