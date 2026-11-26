"""Preferences dialog: update interval, behaviour, highlighting colours."""

from gi.repository import Adw, Gdk, Gtk

from ..config import HIGHLIGHTS
from ..models.highlight import STATES
from ..util.lifecycle import destroy_on_close
from ..widgets.color_menu import MODES

INTERVALS = [(500, "0.5 seconds"), (1000, "1 second"), (2000, "2 seconds"), (5000, "5 seconds"),
             (10000, "10 seconds")]
COLOR_SCHEMES = [("system", "Automatic"), ("light", "Light"), ("dark", "Dark")]


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, app, page=None):
        super().__init__(title="Preferences")
        destroy_on_close(self)
        self.app = app
        self.config = cfg = app.config

        general = Adw.PreferencesPage(title="General", icon_name="preferences-system-symbolic")
        self.add(general)

        appearance = Adw.PreferencesGroup(title="Appearance")
        general.add(appearance)
        scheme = Adw.ComboRow(title="Style", subtitle="Automatic follows the desktop setting",
                              model=Gtk.StringList.new([label for _, label in COLOR_SCHEMES]))
        scheme.set_selected(next((i for i, (k, _) in enumerate(COLOR_SCHEMES) if k == cfg.get("color_scheme")), 0))
        scheme.connect("notify::selected",
                       lambda row, _p: cfg.set("color_scheme", COLOR_SCHEMES[row.get_selected()][0]))
        appearance.add(scheme)

        updates = Adw.PreferencesGroup(title="Updates")
        general.add(updates)
        interval = Adw.ComboRow(title="Update interval",
                                model=Gtk.StringList.new([label for _, label in INTERVALS]))
        current = cfg.get("interval_ms")
        interval.set_selected(next((i for i, (ms, _) in enumerate(INTERVALS) if ms == current), 1))
        interval.connect("notify::selected",
                         lambda row, _p: cfg.set("interval_ms", INTERVALS[row.get_selected()][0]))
        updates.add(interval)
        slow = Adw.SwitchRow(title="Slow down in the background", active=cfg.get("background_slowdown"),
                             subtitle="Update every 5 seconds while no window is visible. Uses less CPU; "
                                      "history graphs get coarser and short-lived processes may be missed.")
        slow.connect("notify::active", lambda row, _p: cfg.set("background_slowdown", row.get_active()))
        updates.add(slow)
        hl = Adw.SpinRow.new_with_range(0, 10, 1)
        hl.set_title("Highlighting duration")
        hl.set_subtitle("Seconds new and terminated items stay highlighted")
        hl.set_value(cfg.get("highlight_seconds"))
        hl.connect("notify::value", lambda row, _p: cfg.set("highlight_seconds", int(row.get_value())))
        updates.add(hl)

        behaviour = Adw.PreferencesGroup(title="Processes")
        general.add(behaviour)
        for key, title, subtitle in (
            ("tree_mode", "Tree view", "Show processes nested under their parents"),
            ("show_kernel_threads", "Show kernel threads", "Threads the kernel runs itself"),
            ("show_other_users", "Show processes from all users", None),
            ("confirm_kill", "Confirm before terminating", "Ask before sending SIGTERM or SIGKILL. Several selected processes always ask."),
            ("heat_cells", "Heat colouring", "Tint CPU, memory and I/O cells by how high the value is"),
            ("binary_names", "Show program file names",
             "Name processes after the file they run (firefox, python3) rather than the name "
             "they give themselves (Web Content, procular)"),
        ):
            row = Adw.SwitchRow(title=title, active=cfg.get(key))
            if subtitle:
                row.set_subtitle(subtitle)
            if key == "tree_mode":
                row.connect("notify::active",
                            lambda r, _p: app.window.process_view.set_tree_mode(r.get_active()))
            else:
                row.connect("notify::active", lambda r, _p, k=key: cfg.set(k, r.get_active()))
            behaviour.add(row)

        colors = Adw.PreferencesPage(title="Highlighting", icon_name="applications-graphics-symbolic")
        self.add(colors)
        group = Adw.PreferencesGroup(
            title="Row Colours",
            description="Also available from the gear button in the header bar, where each user's "
                        "colour can be changed.")
        colors.add(group)
        mode = Adw.ComboRow(title="Colour rows by", model=Gtk.StringList.new([label for _, label in MODES]))
        mode.set_subtitle("User: one colour per user, in the process and services lists. "
                          "Category: what a process is (service, container, …).")
        mode.set_selected(next((i for i, (k, _) in enumerate(MODES) if k == cfg.get("highlight_mode")), 0))
        mode.connect("notify::selected", lambda r, _p: cfg.set("highlight_mode", MODES[r.get_selected()][0]))
        group.add(mode)
        alpha = Adw.SpinRow.new_with_range(5, 100, 5)
        alpha.set_title("Intensity")
        alpha.set_subtitle("Opacity of row colours, in percent. Users and categories show as a "
                           "coloured bar at the row start with a fainter tint; new, terminated, "
                           "suspended and zombie processes colour the whole row.")
        alpha.set_value(round(cfg.get("highlight_alpha") * 100))
        alpha.connect("notify::value", lambda r, _p: cfg.set("highlight_alpha", r.get_value() / 100.0))
        group.add(alpha)

        states = Adw.PreferencesGroup(title="States", description="Always shown, whatever rows are coloured by.")
        colors.add(states)
        categories = Adw.PreferencesGroup(
            title="Categories",
            description="When colouring by category, a process gets the colour of the first enabled "
                        "category that matches, in this order.")
        colors.add(categories)
        categories.set_sensitive(cfg.get("highlight_mode") == "category")
        mode.connect("notify::selected",
                     lambda r, _p: categories.set_sensitive(MODES[r.get_selected()][0] == "category"))

        dialog = Gtk.ColorDialog(with_alpha=False)
        for key, label, desc, _default, _enabled in HIGHLIGHTS:
            entry = cfg.get("highlights")[key]
            row = Adw.ActionRow(title=label, subtitle=desc)
            button = Gtk.ColorDialogButton(dialog=dialog, valign=Gtk.Align.CENTER)
            rgba = Gdk.RGBA()
            rgba.parse(entry["color"])
            button.set_rgba(rgba)
            button.connect("notify::rgba", self._on_color, key)
            switch = Gtk.Switch(active=entry["enabled"], valign=Gtk.Align.CENTER)
            switch.connect("notify::active", lambda s, _p, k=key: cfg.set_highlight(k, "enabled", s.get_active()))
            row.add_suffix(button)
            row.add_suffix(switch)
            row.set_activatable_widget(switch)
            (states if key in STATES else categories).add(row)

        if page == "highlighting":
            self.set_visible_page(colors)

    def _on_color(self, button, _pspec, key):
        rgba = button.get_rgba()
        color = f"#{round(rgba.red * 255):02x}{round(rgba.green * 255):02x}{round(rgba.blue * 255):02x}"
        self.config.set_highlight(key, "color", color)
