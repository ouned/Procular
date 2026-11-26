"""Main window: header bar, tabs (Processes/Services/Network/Disk/History/Devices), status bar."""

import os

from gi.repository import Adw, Gio, GLib, GObject, Gtk

from ..config import APP_ID, APP_NAME
from ..util.format import fmt_bytes, fmt_int
from ..widgets.color_menu import RowColorsButton
from ..widgets.devices_view import DevicesView
from ..widgets.disk_view import DiskView
from ..widgets.history_view import HistoryView
from ..widgets.info_menu import InfoButton
from ..widgets.mini_graphs import MiniGraphs
from ..widgets.network_view import NetworkView
from ..widgets.process_view import ProcessView
from ..widgets.services_view import ServicesView


SEARCH_HINTS = {
    "processes": "Filter by name, PID, user, command line, unit…",
    "services": "Filter by unit, description, status, scope…",
    "network": "Filter by process, address, port, host…",
    "disk": "Filter disk activity by process, PID, user…",
    "history": "Filter by name, PID, user, command line…",
    "devices": "Filter by name, driver, vendor, address, interface, process…",
}


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME)
        self.app = app
        self.config = app.config
        self.sampler = app.sampler
        self.tree = app.tree
        self.my_uid = os.getuid()
        self._properties = {}
        self._device_properties = {}

        geo = self.config.get("window")
        self.set_default_size(geo.get("width", 1280), geo.get("height", 800))
        if geo.get("maximized"):
            self.maximize()

        # ---- pages
        self.stack = Adw.ViewStack()
        self.process_view = ProcessView(self, self.tree, self.sampler, self.config, app.services)
        self.process_view.connect("open-properties", lambda _v, item: self.open_properties(item))
        self.services_view = ServicesView(self)
        self.network_view = NetworkView(self)
        self.disk_view = DiskView(self)
        self.history_view = HistoryView(self)
        self.devices_view = DevicesView(self)
        self.pages = {
            "processes": self.process_view,
            "services": self.services_view,
            "network": self.network_view,
            "disk": self.disk_view,
            "history": self.history_view,
            "devices": self.devices_view,
        }
        for name, title, icon in (
            ("processes", "Processes", "view-list-symbolic"),
            ("services", "Services", "system-run-symbolic"),
            ("network", "Network", "network-workgroup-symbolic"),
            ("disk", "Disk", "drive-harddisk-symbolic"),
            ("history", "History", "document-open-recent-symbolic"),
            ("devices", "Devices", "preferences-system-devices-symbolic"),
        ):
            page = self.stack.add_titled(self.pages[name], name, title)
            page.set_icon_name(icon)
        self.stack.connect("notify::visible-child-name", self._on_page_changed)

        # ---- header bar
        header = Adw.HeaderBar()
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)
        # six wide tabs plus the mini graphs don't fit a narrow window: stack icon over label
        self.set_size_request(640, 400)
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 1360sp"))
        narrow.add_setter(switcher, "policy", Adw.ViewSwitcherPolicy.NARROW)
        self.add_breakpoint(narrow)

        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh (F5)",
                             action_name="app.refresh")
        pause = Gtk.ToggleButton(icon_name="media-playback-pause-symbolic", tooltip_text="Pause updates (F6)",
                                 action_name="app.pause")
        # the logo opens the About dialog; as a button it also keeps the spacing of the buttons next to it,
        # clear of the window corner
        logo = Gtk.Button(child=Gtk.Image(icon_name=APP_ID, pixel_size=24), valign=Gtk.Align.CENTER,
                          tooltip_text=f"About {APP_NAME}", action_name="app.about")
        logo.add_css_class("flat")
        logo.add_css_class("logo-button")
        header.pack_start(logo)
        header.pack_start(refresh)
        header.pack_start(pause)

        # pack_end places right to left: menu, gear, info, search, then the mini graphs
        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=self._build_menu(),
                                     tooltip_text="Main Menu", primary=True)
        header.pack_end(menu_button)
        # row colours of the process and services lists; hidden on the other pages
        self.colors_button = RowColorsButton(app)
        header.pack_end(self.colors_button)
        self.info_button = InfoButton(self)
        header.pack_end(self.info_button)
        self.search_button = Gtk.ToggleButton(icon_name="system-search-symbolic",
                                              tooltip_text="Search (Ctrl+K)")
        header.pack_end(self.search_button)
        self.mini = MiniGraphs(self.sampler)
        self.mini.set_margin_start(8)
        self.mini.set_margin_end(8)
        self.mini.set_action_name("app.sysinfo")
        header.pack_end(self.mini)

        # ---- search bar
        self.search_entry = Gtk.SearchEntry(placeholder_text=SEARCH_HINTS["processes"], hexpand=True)
        self.search_entry.set_max_width_chars(60)
        self.search_entry.connect("search-changed", self._on_search_changed)
        self.search_entry.connect("stop-search", lambda *_: self.clear_search())
        self.search_bar = Gtk.SearchBar()
        clamp = Adw.Clamp(maximum_size=700)
        clamp.set_child(self.search_entry)
        self.search_bar.set_child(clamp)
        self.search_bar.connect_entry(self.search_entry)
        self.search_bar.set_key_capture_widget(self)
        self.search_button.bind_property("active", self.search_bar, "search-mode-enabled",
                                         GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE)

        # ---- status bar
        status = Gtk.Box(spacing=0)
        status.add_css_class("statusbar")
        self.status_cpu = Gtk.Label(xalign=0)
        self.status_mem = Gtk.Label(xalign=0)
        self.status_swap = Gtk.Label(xalign=0)
        self.status_procs = Gtk.Label(xalign=0)
        self.status_threads = Gtk.Label(xalign=0)
        self.status_paused = Gtk.Label(xalign=1, hexpand=True)
        for lbl in (self.status_cpu, self.status_mem, self.status_swap, self.status_procs,
                    self.status_threads, self.status_paused):
            lbl.add_css_class("numeric")
            status.append(lbl)
        # flat list ⇄ parent/child tree; the same action as Main Menu → Tree View
        self.tree_button = Gtk.ToggleButton(icon_name="format-indent-more-symbolic",
                                            tooltip_text="Tree View (Ctrl+T)", action_name="win.tree-mode")
        self.tree_button.add_css_class("flat")
        # one button that alternates between collapsing and expanding every branch of the tree
        self._tree_collapsed = False
        self.expand_button = Gtk.Button(css_classes=["flat"])
        self.expand_button.connect("clicked", self._on_expand_clicked)
        self._show_expand_state()
        status.append(self.expand_button)
        status.append(self.tree_button)

        self.toasts = Adw.ToastOverlay()
        self.toasts.set_child(self.stack)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.add_top_bar(self.search_bar)
        toolbar.set_content(self.toasts)
        toolbar.add_bottom_bar(status)
        self.set_content(toolbar)

        self._install_actions()
        self.config.connect("changed", self._on_tree_mode_changed)
        self.expand_button.set_sensitive(self.config.get("tree_mode"))
        self.sampler.connect("tick", self._on_tick)
        self.sampler.connect("paused-changed", self._on_tick)
        self.connect("close-request", self._on_close)

    # ------------------------------------------------------------------ menu/actions
    def _build_menu(self):
        menu = Gio.Menu()
        view = Gio.Menu()
        view.append("Tree View", "win.tree-mode")
        view.append("Show Kernel Threads", "win.show-kthreads")
        view.append("Show Processes From All Users", "win.show-others")
        menu.append_section(None, view)

        interval = Gio.Menu()
        for label, ms in (("Fast (0.5 s)", 500), ("Normal (1 s)", 1000), ("Below normal (2 s)", 2000),
                          ("Slow (5 s)", 5000), ("Very slow (10 s)", 10000)):
            interval.append(label, f"app.interval({ms})")
        tools = Gio.Menu()
        tools.append("Find Open Files…", "app.find-files")
        tools.append("System Information", "app.sysinfo")
        tools.append_submenu("Update Interval", interval)
        menu.append_section(None, tools)

        app_section = Gio.Menu()
        app_section.append("Preferences", "app.preferences")
        app_section.append("Keyboard Shortcuts", "win.show-help-overlay")
        app_section.append(f"About {APP_NAME}", "app.about")
        menu.append_section(None, app_section)
        return menu

    def _install_actions(self):
        for name, key, handler in (
            ("tree-mode", "tree_mode", lambda v: self.process_view.set_tree_mode(v)),
            ("show-kthreads", "show_kernel_threads", lambda v: self.config.set("show_kernel_threads", v)),
            ("show-others", "show_other_users", lambda v: self.config.set("show_other_users", v)),
        ):
            action = Gio.SimpleAction.new_stateful(name, None, GLib.Variant.new_boolean(self.config.get(key)))

            def on_change(act, value, h=handler):
                act.set_state(value)
                h(value.get_boolean())

            action.connect("change-state", on_change)
            self.add_action(action)

            def sync(_cfg, changed_key, act=action, k=key):
                if changed_key == k and act.get_state().get_boolean() != self.config.get(k):
                    act.set_state(GLib.Variant.new_boolean(self.config.get(k)))

            self.config.connect("changed", sync)

        for name, handler in (
            ("search", lambda *_: self.search_button.set_active(True)),
            ("show-help-overlay", lambda *_: self._show_shortcuts()),
            ("explain", lambda *_: self.info_button.popup()),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)

        page = Gio.SimpleAction.new("show-page", GLib.VariantType.new("s"))
        page.connect("activate", lambda _a, v: self.stack.set_visible_child_name(v.get_string()))
        self.add_action(page)

    def _on_expand_clicked(self, _button):
        self._tree_collapsed = not self._tree_collapsed
        self.process_view.expand_all(not self._tree_collapsed)
        self._show_expand_state()

    def _show_expand_state(self):
        if self._tree_collapsed:
            self.expand_button.set_icon_name("pan-down-symbolic")
            self.expand_button.set_tooltip_text("Expand All")
        else:
            self.expand_button.set_icon_name("pan-up-symbolic")
            self.expand_button.set_tooltip_text("Collapse All")

    def _on_tree_mode_changed(self, _cfg, key):
        if key != "tree_mode":
            return
        tree = self.config.get("tree_mode")
        self.expand_button.set_sensitive(tree and not self.process_view.searching)
        if tree:
            # a new tree model starts with every branch expanded
            self._tree_collapsed = False
            self._show_expand_state()

    def _show_shortcuts(self):
        text = (
            "<b>General</b>\n"
            "F1\tWhat the values mean\nCtrl+K\tSearch / filter\nF5\tRefresh now\nF6\tPause updates\nCtrl+I\tSystem information\n"
            "Ctrl+Shift+F\tFind open files\n"
            "Ctrl+,\tPreferences\nAlt+1…6\tSwitch tab\nCtrl+Q\tQuit\n\n"
            "<b>Processes</b>\n"
            "Ctrl+T\tTree view\n"
            "Enter / double-click\tProperties\nDelete\tTerminate\nShift+Delete\tKill\nCtrl+C\tCopy row\n"
            "Right-click\tContext menu\nRight-click header\tChoose columns"
        )
        dialog = Adw.AlertDialog(heading="Keyboard Shortcuts")
        label = Gtk.Label(use_markup=True, xalign=0)
        label.set_markup(text.replace("&", "&amp;"))
        dialog.set_extra_child(label)
        dialog.add_response("close", "Close")
        dialog.present(self)

    # ------------------------------------------------------------------ helpers used by pages
    def toast(self, message):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(message), timeout=4))

    def clear_search(self):
        self.search_entry.set_text("")
        self.search_bar.set_search_mode(False)
        self._on_search_changed(self.search_entry)

    def current_page(self):
        return self.pages.get(self.stack.get_visible_child_name())

    def refresh_active_page(self):
        page = self.current_page()
        if page in (self.services_view, self.network_view, self.devices_view):
            page.refresh(force=True)

    def goto_process(self, pid):
        if pid is None:
            return
        self.stack.set_visible_child_name("processes")
        self.present()
        GLib.idle_add(lambda: self.process_view.select_pid(pid) and False)

    def open_properties(self, item_or_pid):
        from .properties import PropertiesWindow

        item = item_or_pid if not isinstance(item_or_pid, int) else self.tree.get(item_or_pid)
        if item is None:
            self.toast("Process not found")
            return
        win = self._properties.get(item.pid)
        if win is not None and win.item is item:
            win.present()
            return
        win = PropertiesWindow(self, item)
        self._properties[item.pid] = win
        win.connect("close-request", self._on_properties_closed, item.pid)
        win.present()

    def open_device_properties(self, key):
        """The Properties window of the device `key` (its path below /sys/devices)."""
        from .device_properties import DevicePropertiesWindow, ancestors_of

        win = self._device_properties.get(key)
        if win is not None and not win.removed:
            win.present()
            return
        rows = self.devices_view.table._rows
        row = rows.get(key)
        if row is None:
            self.toast("The device is no longer listed")
            return
        win = DevicePropertiesWindow(self, row.data, ancestors_of(rows, key))
        self._device_properties[key] = win
        win.connect("close-request", self._on_device_properties_closed, key)
        win.present()

    def _on_device_properties_closed(self, win, key):
        if self._device_properties.get(key) is win:
            del self._device_properties[key]
        return False

    def _on_properties_closed(self, win, pid):
        # the PID may have been reused and now belong to a newer window
        if self._properties.get(pid) is win:
            del self._properties[pid]
        return False

    # ------------------------------------------------------------------ events
    def _on_search_changed(self, entry):
        page = self.current_page()
        if page is not None:
            page.set_filter_text(entry.get_text())
        if page is self.process_view:
            self.expand_button.set_sensitive(self.config.get("tree_mode") and not page.searching)

    def _on_page_changed(self, *_):
        name = self.stack.get_visible_child_name()
        self.colors_button.set_visible(name in ("processes", "services"))
        self.tree_button.set_visible(name == "processes")
        self.expand_button.set_visible(name == "processes")
        self.search_entry.set_placeholder_text(SEARCH_HINTS.get(name, "Filter"))
        for pname, page in self.pages.items():
            if hasattr(page, "set_active"):
                page.set_active(pname == name)
        self._on_search_changed(self.search_entry)

    def _on_tick(self, sampler):
        snap = sampler.snapshot
        if snap is None:
            return
        self.status_cpu.set_text(f"CPU usage: {snap['cpu_total']:.1f}%")
        pct = 100.0 * snap["mem_used"] / snap["mem_total"] if snap["mem_total"] else 0
        self.status_mem.set_text(
            f"Physical memory: {fmt_bytes(snap['mem_used'])} / {fmt_bytes(snap['mem_total'])} ({pct:.0f}%)")
        self.status_swap.set_text(f"Swap: {fmt_bytes(snap['swap_used'])} / {fmt_bytes(snap['swap_total'])}")
        self.status_procs.set_text(f"Processes: {len(sampler.records)}")
        self.status_threads.set_text(f"Threads: {fmt_int(sampler.total_threads)}")
        self.status_paused.set_text("Updates paused" if sampler.paused else "")

    def save_state(self):
        self.process_view.save_widths()
        if not self.is_maximized():
            width, height = self.get_default_size()
        else:
            width, height = self.config.get("window").get("width"), self.config.get("window").get("height")
        self.config.set("window", {"width": width, "height": height, "maximized": self.is_maximized()})

    def _on_close(self, *_):
        self.save_state()
        return False
