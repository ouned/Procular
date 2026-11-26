"""Application object: shared state, actions, CSS."""

import os
import signal

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from .config import APP_ID, APP_NAME, HIGHLIGHTS, Config
from .models import highlight
from .models.process_tree import ProcessTree
from .providers.sampler import Sampler
from .providers.services import ServicesProvider
from .util.icons import IconResolver

HERE = os.path.dirname(os.path.abspath(__file__))
STRONG = {"new", "term"}
TRANSIENT = {"stopped", "zombie"}  # states, not categories: always tint the whole row


def _fix_unset_dpi():
    """Work around libadwaita < 1.6 turning an unset DPI into 0 px "sp" sizes.

    GTK reports an unknown DPI as gtk-xft-dpi = -1 (no XSETTINGS daemon, or no
    org.gnome.desktop.interface schema on Wayland). libadwaita 1.5 only falls back to 96 DPI
    for 0, so every "sp" length becomes 0 px: Adw.Clamp squeezes pages to their minimum
    width and "max-width: …sp" breakpoints never apply. Newer versions handle -1.
    """
    settings = Gtk.Settings.get_default()
    if (Adw.get_major_version(), Adw.get_minor_version()) < (1, 6) and settings is not None \
            and settings.props.gtk_xft_dpi <= 0:
        settings.props.gtk_xft_dpi = 96 * 1024


class ProcularApp(Adw.Application):
    def __init__(self):
        # test runs (tests/test_ui.py, tools/shots.py) add NON_UNIQUE before starting it
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        GLib.set_application_name(APP_NAME)
        self.window = None
        self.sysinfo_window = None
        self.find_window = None

    # ------------------------------------------------------------------
    def do_startup(self):
        Adw.Application.do_startup(self)
        _fix_unset_dpi()
        # run from the source tree: the app icon is not installed in the icon theme, take it from data/
        source_data = os.path.join(os.path.dirname(HERE), "data")
        if os.path.isfile(os.path.join(source_data, APP_ID + ".svg")):
            Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(source_data)
        Gtk.Window.set_default_icon_name(APP_ID)
        self.config = Config(os.environ.get("PROCULAR_CONFIG") or None)
        self.icons = IconResolver()
        self.sampler = Sampler(self.config)
        self.tree = ProcessTree(self.config, self.icons)
        self.user_colors = highlight.UserColors()
        self.services = ServicesProvider()
        self.sampler.connect("tick", lambda s: self.tree.apply(s.records, s.history.times[-1]))
        self.tree.connect("users-changed", lambda _t: self.update_highlight_css())
        # slow the sampler down while none of our windows is visible
        self._toplevels = Gtk.Window.get_toplevels()
        self._toplevels.connect("items-changed", self._on_toplevels_changed)

        self.apply_color_scheme()
        display = Gdk.Display.get_default()
        static_css = Gtk.CssProvider()
        static_css.load_from_path(os.path.join(HERE, "style.css"))
        Gtk.StyleContext.add_provider_for_display(display, static_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        # light-style overrides: a provider switched with the style, as GTK < 4.20 has no
        # prefers-color-scheme media query
        self.light_css = Gtk.CssProvider()
        self.light_css.load_from_path(os.path.join(HERE, "style-light.css"))
        self._light_css_on = False
        Adw.StyleManager.get_default().connect("notify::dark", lambda *_: self._update_light_css())
        self._update_light_css()
        self.highlight_css = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(display, self.highlight_css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        self.update_highlight_css()
        self.config.connect("changed", self._on_config_changed)

        for name, accels, handler in (
            ("quit", ["<Control>q"], lambda *_: self.quit()),
            ("preferences", ["<Control>comma"], self._on_preferences),
            ("sysinfo", ["<Control>i"], self._on_sysinfo),
            ("find-files", ["<Control><Shift>f"], lambda *_: self.show_find_files()),
            ("refresh", ["F5"], self._on_refresh),
            ("about", [], self._on_about),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)
            if accels:
                self.set_accels_for_action("app." + name, accels)

        pause = Gio.SimpleAction.new_stateful("pause", None, GLib.Variant.new_boolean(False))
        pause.connect("change-state", self._on_pause)
        self.add_action(pause)
        self.set_accels_for_action("app.pause", ["F6"])
        self.sampler.connect("paused-changed",
                             lambda s: pause.set_state(GLib.Variant.new_boolean(s.paused)))

        interval = Gio.SimpleAction.new_stateful(
            "interval", GLib.VariantType.new("i"), GLib.Variant.new_int32(self.config.get("interval_ms")))
        interval.connect("change-state", self._on_interval)
        self.add_action(interval)

        # leave the normal way when killed or when the terminal closes, so that what is only saved
        # on exit (window size, column widths) is not lost
        for sig in (signal.SIGTERM, signal.SIGHUP):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, self._on_quit_signal)

        self.set_accels_for_action("win.search", ["<Control>k"])
        self.set_accels_for_action("win.explain", ["F1"])
        self.set_accels_for_action("win.tree-mode", ["<Control>t"])
        self.set_accels_for_action("win.show-page('processes')", ["<Alt>1"])
        self.set_accels_for_action("win.show-page('services')", ["<Alt>2"])
        self.set_accels_for_action("win.show-page('network')", ["<Alt>3"])
        self.set_accels_for_action("win.show-page('disk')", ["<Alt>4"])
        self.set_accels_for_action("win.show-page('history')", ["<Alt>5"])
        self.set_accels_for_action("win.show-page('devices')", ["<Alt>6"])

    def do_activate(self):
        if self.window is None:
            from .windows.main_window import MainWindow

            self.window = MainWindow(self)
            self.sampler.start()
        self.window.present()

    def _on_toplevels_changed(self, toplevels, position, _removed, added):
        for i in range(position, position + added):
            window = toplevels.get_item(i)
            window.connect("notify::suspended", self._update_background)
            window.connect("notify::visible", self._update_background)
        self._update_background()

    def _update_background(self, *_):
        """Background = every open window is "suspended": minimised, on another workspace,
        or the screen is off. (What exactly counts is up to the compositor.)"""
        windows = [w for w in Gtk.Window.list_toplevels() if w.get_visible()]
        self.sampler.set_background(bool(windows) and all(w.is_suspended() for w in windows))

    def _on_quit_signal(self):
        self.quit()
        return GLib.SOURCE_REMOVE

    def do_shutdown(self):
        from .util import audio

        audio.stop_all()  # the About dialog's music
        if self.window is not None:
            self.window.save_state()
        self.config.save_now()
        Adw.Application.do_shutdown(self)

    # ------------------------------------------------------------------
    def update_highlight_css(self):
        hl = self.config.get("highlights")
        alpha = self.config.get("highlight_alpha")
        mode = self.config.get("highlight_mode")
        rules = []

        def rule(cls, color, a):
            sel = f"columnview.data-table > listview > row.hl-{cls}"
            rules.append(f"{sel}:not(:selected) {{ background-color: alpha({color}, {a:.3f}); }}\n")
            # the stripe also keeps the colour visible on the selected row, whose background is the selection
            rules.append(f"{sel} {{ box-shadow: inset 4px 0 alpha({color}, 0.9); }}\n")

        # steady colours (categories, users): a coloured stripe at the row start and only a faint
        # tint, so the colour stays meaningful when most rows share it
        steady_alpha = alpha * 0.4
        for key, *_ in HIGHLIGHTS:
            entry = hl.get(key, {})
            if not entry.get("enabled"):
                continue
            if key in STRONG:
                rule(key, entry["color"], min(0.45, alpha * 1.1))
            elif key in TRANSIENT:
                rule(key, entry["color"], alpha)
            elif mode == "category":
                rule(key, entry["color"], steady_alpha)
        if mode == "user":
            overrides = self.config.get("user_colors")
            for uid in sorted(self.tree.users):
                rule(highlight.user_class(uid), self.user_colors.color(uid, overrides), steady_alpha)
        self.highlight_css.load_from_string("".join(rules))

    def _update_light_css(self):
        light = not Adw.StyleManager.get_default().get_dark()
        if light == self._light_css_on:
            return
        display = Gdk.Display.get_default()
        if light:
            Gtk.StyleContext.add_provider_for_display(display, self.light_css,
                                                      Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        else:
            Gtk.StyleContext.remove_provider_for_display(display, self.light_css)
        self._light_css_on = light

    def apply_color_scheme(self):
        scheme = {"light": Adw.ColorScheme.FORCE_LIGHT, "dark": Adw.ColorScheme.FORCE_DARK}.get(
            self.config.get("color_scheme"), Adw.ColorScheme.DEFAULT)
        Adw.StyleManager.get_default().set_color_scheme(scheme)

    def _on_config_changed(self, _config, key):
        if key == "color_scheme":
            self.apply_color_scheme()
        elif key in ("highlights", "highlight_mode"):
            self.tree.refresh_highlights()  # first: it finds the users the CSS has to cover
            self.update_highlight_css()
        elif key in ("highlight_alpha", "user_colors"):
            self.update_highlight_css()
        elif key == "interval_ms":  # changed in the preferences: the main menu shows it too
            self.lookup_action("interval").set_state(GLib.Variant.new_int32(self.config.get("interval_ms")))

    def _on_pause(self, action, value):
        action.set_state(value)
        self.sampler.set_paused(value.get_boolean())

    def _on_refresh(self, *_):
        self.sampler.refresh()
        if self.window is not None:
            self.window.refresh_active_page()

    def _on_interval(self, action, value):
        action.set_state(value)
        self.config.set("interval_ms", value.get_int32())

    def _on_preferences(self, *_):
        self.show_preferences()

    def show_preferences(self, page=None):
        from .windows.preferences import PreferencesDialog

        PreferencesDialog(self, page).present(self.window)

    def _on_sysinfo(self, *_):
        from .windows.sysinfo import SystemInfoWindow

        if self.sysinfo_window is None:
            self.sysinfo_window = SystemInfoWindow(self)
            self.sysinfo_window.connect("close-request", self._on_sysinfo_closed)
        self.sysinfo_window.present()

    def _on_sysinfo_closed(self, *_):
        self.sysinfo_window = None
        return False

    def show_find_files(self, text=None, kind=None):
        """Open the Find Open Files window (one for the application), optionally searching."""
        from .windows.find_files import FindFilesWindow

        if self.find_window is None:
            self.find_window = FindFilesWindow(self)
            self.find_window.connect("close-request", self._on_find_closed)
        self.find_window.present()
        if text is not None or kind is not None:
            self.find_window.find(text or "", kind)

    def _on_find_closed(self, *_):
        self.find_window = None
        return False

    def _on_about(self, *_):
        from .windows.about import AboutDialog

        AboutDialog(self).present(self.window)
