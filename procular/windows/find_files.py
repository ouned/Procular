"""Find Open Files: which processes hold a file, directory, socket, device or library."""

import signal
import time

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from ..providers.handles import MAX_RESULTS, elevated_snapshot, run_search
from ..util import privileged
from ..util.icons import GENERIC_ICON
from ..util.lifecycle import destroy_on_close, is_destroyed
from ..util.tasks import run_async
from ..widgets import process_actions as pa
from ..widgets.table import Column, Table
from .properties import path_markup, when_text

# (kind id, label) of the kind filter, in the order of the drop-down
KIND_CHOICES = (
    ("all", "Everything"),
    ("files", "Files and directories"),
    ("mapped", "Libraries and mapped files"),
    ("sockets", "Sockets"),
    ("pipes", "Pipes"),
    ("devices", "Devices"),
    ("deleted", "Deleted files"),
)
_TYPE_ICONS = {
    "File": "text-x-generic-symbolic", "Deleted file": "user-trash-symbolic",
    "Executable": "application-x-executable-symbolic", "Working directory": "folder-symbolic",
    "Root directory": "folder-symbolic", "Library": "application-x-addon-symbolic",
    "Mapped file": "text-x-generic-symbolic", "Socket": "network-wired-symbolic",
    "Pipe": "media-playlist-consecutive-symbolic", "Device": "drive-harddisk-symbolic",
    "Memfd": "media-flash-symbolic", "Shared memory": "media-flash-symbolic",
}
_TYPE_ICON_DEFAULT = "emblem-system-symbolic"
# types whose name is a path in the file system
_PATH_TYPES = {"File", "Deleted file", "Executable", "Working directory", "Root directory", "Library",
               "Mapped file", "Device"}


def _path(data):
    """The file system path of a result, or None (sockets, pipes, anonymous inodes)."""
    if data is None or data["type"] not in _PATH_TYPES:
        return None
    return data["name"].removesuffix(" (deleted)")


class FindFilesWindow(Adw.Window):
    def __init__(self, app):
        super().__init__(title="Find Open Files", default_width=1000, default_height=640)
        destroy_on_close(self)
        self.app = app
        self.config = app.config
        self.tree = app.tree
        self._index = None  # the HandleIndex of the last search, reused while it is fresh
        self._elevated = None  # what the helper read: {(pid, starttime): ...}; replaced, never changed
        self._elevated_at = None
        self._loading = False
        self._again = None  # (force) of a search asked for while one was running
        self._type_icons = {}

        self.entry = Gtk.SearchEntry(placeholder_text="File name, path, port, library or device", hexpand=True,
                                     search_delay=300)
        self.entry.connect("search-changed", lambda *_: self.search())
        self.entry.connect("activate", lambda *_: self.search())
        self.kind = Gtk.DropDown.new_from_strings([label for _id, label in KIND_CHOICES])
        self.kind.set_tooltip_text("What to search")
        self.kind.connect("notify::selected", lambda *_: self.search())
        self.count = Gtk.Label(css_classes=["dim", "numeric"])
        bar = Gtk.Box(spacing=6, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        bar.append(self.entry)
        bar.append(self.kind)
        bar.append(self.count)

        self.banner = Adw.Banner(button_label="Search as Administrator")
        self.banner.connect("button-clicked", self._elevate)

        columns = [
            Column("process", "Process", "process", width=180, icon=self._process_icon),
            Column("pid", "PID", "pid", numeric=True, width=70),
            Column("user", "User", "user", width=90),
            Column("type", "Type", "type", width=150, icon=self._type_icon),
            Column("name", "Name", "name", width=360, expand=True, markup=path_markup,
                   ellipsize=Pango.EllipsizeMode.MIDDLE, tooltip=lambda d: d["name"]),
            Column("fd", "FD", "fd", numeric=True, width=55,
                   tooltip=lambda d: None if d["fd"] is not None else "Not a file descriptor"),
        ]
        self.table = Table(columns, key="key", menu_model=self._build_menu())
        self.table.remember_sort(self.config, "find_files", "process")
        self.table.connect("activated", lambda *_: self._properties())
        self._install_actions()

        self.stack = Gtk.Stack()
        self.stack.add_named(Adw.StatusPage(
            icon_name="system-search-symbolic", title="Find Open Files",
            description="Find the processes that hold a file or directory open, use a library, listen on a "
                        "port or keep a device busy. Type part of a name, or choose a kind such as "
                        "“Deleted files” to list them all."), "start")
        self.stack.add_named(Adw.StatusPage(icon_name="edit-find-symbolic", title="Nothing Found"), "empty")
        self.stack.add_named(self.table, "results")

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(self.banner)
        content.append(bar)
        content.append(Gtk.Separator())
        content.append(self.stack)
        self.toasts = Adw.ToastOverlay(child=content)

        header = Adw.HeaderBar()
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Search Again (F5)")
        refresh.connect("clicked", lambda *_: self.search(force=True))
        header.pack_start(refresh)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(self.toasts)
        self.set_content(toolbar)

        shortcuts = Gtk.ShortcutController(scope=Gtk.ShortcutScope.MANAGED)
        for trigger, handler in (("F5", lambda *_: self.search(force=True) or True),
                                 ("<Control>f", lambda *_: self.entry.grab_focus() or True),
                                 ("<Control>k", lambda *_: self.entry.grab_focus() or True)):
            shortcuts.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string(trigger),
                                                    Gtk.CallbackAction.new(handler)))
        self.add_controller(shortcuts)
        self.entry.grab_focus()

    def toast(self, message):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(message), timeout=4))

    def find(self, text, kind=None):
        """Start a search for text (and kind id), as if typed."""
        if kind is not None:
            ids = [kid for kid, _label in KIND_CHOICES]
            self.kind.set_selected(ids.index(kind))
        self.entry.set_text(text)
        self.search()

    # ------------------------------------------------------------------ searching
    def _query(self):
        return self.entry.get_text().strip(), KIND_CHOICES[self.kind.get_selected()][0]

    def search(self, force=False):
        query, kind = self._query()
        if not query and kind == "all":
            self.stack.set_visible_child_name("start")
            self.count.set_text("")
            self.banner.set_revealed(False)
            return
        if self._loading:
            self._again = bool(self._again) or force
            return
        self._loading = True
        index = None if force else self._index
        run_async(run_search, lambda result, error: self._on_result(result, error, query, kind),
                  index, self._elevated, query, kind, force)

    def _on_result(self, result, error, query, kind):
        if is_destroyed(self):
            return
        self._loading = False
        if self._again is not None:
            force, self._again = self._again, None
            self.search(force=force)
            return
        if (query, kind) != self._query():
            self.search()  # changed while this one ran
            return
        if error is not None or result is None:
            self.toast(f"Search failed: {error}")
            return
        index, found, total = result
        self._index = index
        rows = []
        for entry in found:
            pid, starttime, fd, type_, name, _deleted = entry
            item = self.tree.get(pid)
            if item is not None and item.rec["starttime"] != starttime:
                item = None  # the pid belongs to another process by now
            rows.append({
                "key": entry[:5], "pid": pid, "starttime": starttime, "fd": fd, "type": type_, "name": name,
                "process": item.name if item is not None else index.names.get((pid, starttime), str(pid)),
                "user": item.rec.get("user") if item is not None else "",
                "item": item,
            })
        self.table.set_rows(rows)
        self.stack.set_visible_child_name("results" if rows else "empty")
        text = f"{total} found" if total <= MAX_RESULTS else f"first {MAX_RESULTS} of {total} shown"
        self.count.set_text(text)
        self._update_banner(index)

    def _update_banner(self, index):
        denied = len(index.denied)
        if denied and self._elevated is not None:
            self.banner.set_title(f"{denied} processes started since the search as administrator were not searched")
            self.banner.set_button_label("Search Again as Administrator")
        elif denied:
            self.banner.set_title(f"{denied} processes of other users were not searched")
            self.banner.set_button_label("Search as Administrator")
        elif self._elevated is not None:
            self.banner.set_title(f"Other users' processes as they were at {when_text(self._elevated_at)}")
            self.banner.set_button_label("Search Again as Administrator")
        self.banner.set_revealed(bool(denied) or self._elevated is not None)

    def _elevate(self, *_):
        def done(result, error):
            if is_destroyed(self):
                return
            if error:
                self.toast(f"Failed: {error}")
                return
            self._elevated = elevated_snapshot(result)
            self._elevated_at = time.time()
            self.search(force=True)

        privileged.run(["handles"], done)

    # ------------------------------------------------------------------ cells
    def _process_icon(self, data):
        item = data.get("item")
        return item.icon if item is not None and item.icon is not None else GENERIC_ICON

    def _type_icon(self, data):
        name = _TYPE_ICONS.get(data["type"], _TYPE_ICON_DEFAULT)
        icon = self._type_icons.get(name)
        if icon is None:
            icon = self._type_icons[name] = Gio.ThemedIcon.new(name)
        return icon

    # ------------------------------------------------------------------ actions
    def _build_menu(self):
        menu = Gio.Menu()
        proc = Gio.Menu()
        proc.append("Go to Process", "find.goto")
        proc.append("Process Properties", "find.properties")
        menu.append_section(None, proc)
        end = Gio.Menu()
        end.append("End Process", "find.terminate")
        end.append("Kill Process", "find.kill")
        menu.append_section(None, end)
        misc = Gio.Menu()
        misc.append("Open File Location", "find.open-location")
        misc.append("Copy Name", "find.copy-name")
        misc.append("Copy Row", "find.copy")
        menu.append_section(None, misc)
        return menu

    def _install_actions(self):
        group = Gio.SimpleActionGroup()
        self._actions = {}
        for name, handler in (
            ("goto", self._goto),
            ("properties", self._properties),
            ("terminate", lambda: self._signal(signal.SIGTERM, "End")),
            ("kill", lambda: self._signal(signal.SIGKILL, "Kill")),
            ("open-location", self._open_location),
            ("copy-name", self._copy_name),
            ("copy", self.table.copy_selected),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_a, h=handler: h())
            group.add_action(action)
            self._actions[name] = action
        self.insert_action_group("find", group)
        self.table.connect("selection-changed", lambda *_: self._sync_actions())
        if self.table.popover is not None:
            self.table.popover.connect("show", lambda *_: self._sync_actions())
        self._sync_actions()

    def _live_item(self):
        """The selected result's process, if it still runs (and is still that process)."""
        data = self.table.selected()
        if data is None:
            return None
        item = self.tree.get(data["pid"])
        if item is None or item.terminated or item.rec["starttime"] != data["starttime"]:
            return None
        return item

    def _sync_actions(self):
        data = self.table.selected()
        live = self._live_item() is not None
        for name in ("goto", "properties", "terminate", "kill"):
            self._actions[name].set_enabled(live)
        self._actions["open-location"].set_enabled(_path(data) is not None)
        self._actions["copy-name"].set_enabled(data is not None)
        self._actions["copy"].set_enabled(data is not None)

    def _main(self):
        return self.app.window

    def _goto(self):
        item = self._live_item()
        if item is not None and self._main() is not None:
            self._main().goto_process(item.pid)

    def _properties(self):
        item = self._live_item()
        if item is None:
            self.toast("The process has exited")
        elif self._main() is not None:
            self._main().open_properties(item)

    def _signal(self, sig, verb):
        item = self._live_item()
        if item is None:
            self.toast("The process has exited")
            return
        pa.confirm_signal(self, [item], sig, verb)

    def _open_location(self):
        path = _path(self.table.selected())
        if path is not None:
            pa.open_location(self, path)

    def _copy_name(self):
        data = self.table.selected()
        if data is not None:
            pa.copy_text(_path(data) or data["name"])
            self.toast("Copied to clipboard")
