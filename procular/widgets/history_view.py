"""History tab: processes that exited while Procular was running."""

import time

from gi.repository import Adw, Gio, Gtk

from ..models.process_history import exit_failed
from ..util.format import fmt_bytes, fmt_clock, fmt_cpu_time, fmt_duration, fmt_pct, fmt_timestamp
from ..util.icons import GENERIC_ICON
from . import process_actions as pa
from .process_view import heat_cpu, heat_mem
from .table import Column, Table

STATUS_HINT = ("The exit status is only known when Procular saw the process as a zombie, "
               "and only for your own processes")


def _status(data):
    return data["status"] or "—"


def _status_tooltip(data):
    return None if data["status"] else STATUS_HINT


def _hl(data):
    return "svc-failed" if exit_failed(data["exit_code"]) else ""


class HistoryView(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.tree = win.tree
        self._active = False
        self._dirty = True
        self._since = time.time()

        bar = Gtk.Box(spacing=6, margin_top=6, margin_bottom=6, margin_start=8, margin_end=6)
        self.summary = Gtk.Label(xalign=0, hexpand=True, css_classes=["dim"])
        bar.append(self.summary)
        clear = Gtk.Button(label="Clear", tooltip_text="Forget the recorded processes")
        clear.connect("clicked", lambda *_: self.clear())
        bar.append(clear)
        self.append(bar)

        columns = [
            Column("name", "Name", "name", width=220, icon=lambda d: d["icon"] or GENERIC_ICON,
                   tooltip=lambda d: d["exe"] or None),
            Column("pid", "PID", "pid", numeric=True, width=70),
            Column("exited", "Exited", "exit_time", fmt=lambda v: fmt_clock(v) if v else "", width=80),
            Column("runtime", "Ran for", "runtime", fmt=fmt_duration, numeric=True, width=90),
            Column("status", "Exit status", _status, width=140, tooltip=_status_tooltip,
                   sort=lambda d: d["exit_code"] if d["exit_code"] is not None else -1),
            Column("peak_cpu", "Peak CPU", "peak_cpu", fmt=fmt_pct, numeric=True, width=80,
                   heat=lambda v: heat_cpu(v, None) if win.config.get("heat_cells") else 0),
            Column("peak_rss", "Peak working set", "peak_rss", fmt=fmt_bytes, numeric=True, width=120,
                   heat=lambda v: heat_mem(v, win.process_view) if win.config.get("heat_cells") else 0),
            Column("cpu_time", "CPU time", "cpu_time", fmt=fmt_cpu_time, numeric=True, width=95),
            Column("parent", "Parent", "parent", width=170),
            Column("user", "User", "user", width=100),
            Column("started", "Started", "create_time", fmt=fmt_timestamp, width=150),
            Column("unit", "systemd unit", "unit", width=180),
            Column("cmdline", "Command line", "cmdline", width=300, expand=True),
        ]
        self.table = Table(columns, key="seq", filter_keys=("name", "pid", "user", "cmdline", "unit", "parent"),
                           menu_model=self._menu())
        self.table.hl_func = _hl
        self.table.remember_sort(win.config, "history", "exited", descending=True)

        empty = Adw.StatusPage(icon_name="document-open-recent-symbolic", title="No Exited Processes Yet",
                               description="Processes that exit while Procular is running are listed here. "
                                           "Processes that start and exit between two updates are not seen.")
        self.stack = Gtk.Stack(vexpand=True, transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self.table, "table")
        self.stack.add_named(empty, "empty")
        self.append(self.stack)

        group = Gio.SimpleActionGroup()
        for name, handler in (
            ("goto-parent", lambda d: self.win.goto_process(d["ppid"])),
            ("copy", lambda _d: self.table.copy_selected()),
            ("copy-cmdline", lambda d: pa.copy_text(d["cmdline"])),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_a, h=handler: self._with(h))
            group.add_action(action)
        self.insert_action_group("hist", group)

        self.tree.connect("exited", self._on_exited)
        self._update()

    def _menu(self):
        menu = Gio.Menu()
        menu.append("Go to Parent Process", "hist.goto-parent")
        copy = Gio.Menu()
        copy.append("Copy Row", "hist.copy")
        copy.append("Copy Command Line", "hist.copy-cmdline")
        menu.append_section(None, copy)
        return menu

    def _with(self, func):
        data = self.table.selected()
        if data is not None:
            func(data)

    def set_active(self, active):
        self._active = active
        if active and self._dirty:
            self._update()

    def set_filter_text(self, text):
        self.table.set_filter_text(text)

    def clear(self):
        self.tree.clear_exit_history()
        self._since = time.time()
        self.table.clear()
        self._update()

    def _on_exited(self, _tree, _records):
        if self._active:
            self._update()
        else:
            self._dirty = True

    def _update(self):
        self._dirty = False
        rows = list(self.tree.exit_history)
        self.table.set_rows(rows)
        self.stack.set_visible_child_name("table" if rows else "empty")
        failed = sum(1 for r in rows if exit_failed(r["exit_code"]))
        text = f"{len(rows)} processes exited since {fmt_clock(self._since)}"
        if failed:
            text += f", {failed} with an error"
        self.summary.set_text(text)
