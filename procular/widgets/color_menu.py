"""Header bar button with the row colour settings of the process list."""

from gi.repository import Gdk, Gtk, Pango

from ..models import highlight

MODES = [("user", "User"), ("category", "Category"), ("none", "None")]
MODE_HINTS = {
    "user": "Processes and services get the colour of the user they run as.",
    "category": "Processes are coloured by what they are: service, container, sandboxed app, … "
                "Categories and their colours are set in the preferences. Services are not coloured.",
    "none": "Only new, terminated, suspended and zombie processes are coloured.",
}


def _hex(rgba):
    return f"#{round(rgba.red * 255):02x}{round(rgba.green * 255):02x}{round(rgba.blue * 255):02x}"


class _Choice(Gtk.Box):
    """A row of linked buttons, one of them active."""

    def __init__(self, options, on_changed):
        super().__init__(css_classes=["linked"], homogeneous=True, hexpand=True)
        self._buttons = {}
        self._on_changed = on_changed
        first = None
        for key, label in options:
            button = Gtk.ToggleButton(label=label)
            if first is None:
                first = button
            else:
                button.set_group(first)
            button.connect("toggled", self._on_toggled, key)
            self._buttons[key] = button
            self.append(button)

    def select(self, key):
        button = self._buttons.get(key)
        if button is not None:
            button.set_active(True)

    def _on_toggled(self, button, key):
        if button.get_active():
            self._on_changed(key)


class RowColorsButton(Gtk.MenuButton):
    def __init__(self, app):
        super().__init__(icon_name="emblem-system-symbolic", tooltip_text="Row Colours")
        self.app = app
        self.config = app.config
        self.tree = app.tree
        self._syncing = False
        self._user_buttons = {}  # uid -> colour button of the users listed in the popover
        self._color_dialog = Gtk.ColorDialog(with_alpha=False, title="Row Colour")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, css_classes=["color-menu"],
                      width_request=300)
        box.append(Gtk.Label(label="Colour rows by", xalign=0, css_classes=["heading"]))
        self.mode = _Choice(MODES, lambda key: self._set("highlight_mode", key))
        box.append(self.mode)
        self.hint = Gtk.Label(xalign=0, wrap=True, max_width_chars=36, css_classes=["dim", "caption"])
        box.append(self.hint)

        # ---- one colour per user
        self.users_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.users = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                                      max_content_height=264)
        scroller.set_child(self.users)
        self.users_box.append(scroller)
        self.reset = Gtk.Button(label="Reset Colours", halign=Gtk.Align.START, css_classes=["flat"])
        self.reset.connect("clicked", lambda _b: self.config.reset_user_colors())
        self.users_box.append(self.reset)
        box.append(self.users_box)

        box.append(Gtk.Separator())
        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        grid.attach(Gtk.Label(label="Intensity", xalign=0), 0, 0, 1, 1)
        self.alpha = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 5, 100, 5)
        self.alpha.set_hexpand(True)
        self.alpha.set_draw_value(False)
        self.alpha.connect("value-changed", self._on_alpha)
        grid.attach(self.alpha, 1, 0, 1, 1)
        box.append(grid)

        box.append(Gtk.Separator())
        prefs = Gtk.Button(label="Highlighting Preferences…", css_classes=["flat"])
        prefs.get_child().set_xalign(0)
        prefs.connect("clicked", self._on_preferences)
        box.append(prefs)

        self.popover = Gtk.Popover()
        self.popover.set_child(box)
        self.popover.connect("show", lambda _p: self._sync())
        self.set_popover(self.popover)
        self.config.connect("changed", self._on_config_changed)

    # ------------------------------------------------------------------
    def _set(self, key, value):
        if not self._syncing:
            self.config.set(key, value)

    def _on_alpha(self, scale):
        if not self._syncing:
            self.config.set("highlight_alpha", round(scale.get_value()) / 100.0)

    def _on_preferences(self, _button):
        self.popover.popdown()
        self.app.show_preferences("highlighting")

    def _on_config_changed(self, _config, key):
        if key == "highlight_mode" and self.popover.get_visible():
            self._sync()
        elif key == "user_colors":
            self._show_user_colors()

    def _sync(self):
        """Show the current settings and the users that have processes right now."""
        cfg = self.config
        mode = cfg.get("highlight_mode")
        self._syncing = True
        self.mode.select(mode)
        self.alpha.set_value(round(cfg.get("highlight_alpha") * 100))
        self._syncing = False
        self.hint.set_text(MODE_HINTS.get(mode, ""))
        self.hint.set_visible(mode in MODE_HINTS)
        self.users_box.set_visible(mode == "user")
        if mode == "user":
            self._fill_users()

    def _fill_users(self):
        child = self.users.get_first_child()
        while child is not None:
            self.users.remove(child)
            child = self.users.get_first_child()
        counts = {}
        names = {}
        for item in self.tree.items.values():
            uid = item.rec.get("uid")
            if uid is None or item.terminated:
                continue
            counts[uid] = counts.get(uid, 0) + 1
            names[uid] = item.rec.get("user") or str(uid)
        my_uid = highlight.MY_UID
        self._user_buttons = {}
        # the current user and root first, then everybody else by name
        for uid in sorted(counts, key=lambda u: (u != my_uid, u != 0, names[u].casefold(), u)):
            row = Gtk.Box(spacing=10)
            button = Gtk.ColorDialogButton(dialog=self._color_dialog, valign=Gtk.Align.CENTER,
                                           tooltip_text=f"Colour of {names[uid]}'s processes")
            button.connect("notify::rgba", self._on_user_color, uid)
            self._user_buttons[uid] = button
            row.append(button)
            name = Gtk.Label(label=names[uid], xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
            row.append(name)
            if uid == my_uid:
                row.append(Gtk.Label(label="you", css_classes=["dim", "caption"]))
            row.append(Gtk.Label(label=str(counts[uid]), xalign=1, width_chars=4, css_classes=["dim", "numeric"],
                                 tooltip_text="Processes"))
            self.users.append(row)
        self._show_user_colors()

    def _show_user_colors(self):
        overrides = self.config.get("user_colors")
        self._syncing = True
        for uid, button in self._user_buttons.items():
            rgba = Gdk.RGBA()
            rgba.parse(self.app.user_colors.color(uid, overrides))
            button.set_rgba(rgba)
        self._syncing = False
        self.reset.set_sensitive(bool(overrides))

    def _on_user_color(self, button, _pspec, uid):
        if not self._syncing:
            self.config.set_user_color(uid, _hex(button.get_rgba()))
