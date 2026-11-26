"""The main process tree (System Informer's "Processes" tab)."""

import signal

from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Pango

from ..models.process_tree import SUM_KEYS, ProcessItem
from ..providers.processes import NCPU
from ..util.format import (fmt_bytes, fmt_cpu_time, fmt_int, fmt_pct, fmt_pct_label, fmt_rate,
                           fmt_timestamp)
from ..util.linux import SIGNALS, STATE_NAMES
from . import process_actions as pa
from .graph import CPU as CPU_COLOR
from .graph import MEM as MEM_COLOR
from .sparkline import Sparkline
from .table import (context_menu, find_ancestor_with, make_reorderable, popup_at, present_popover,
                    remember_sort, set_row_class)


def _num(v):
    return "" if v is None else str(v)


class ProcColumn:
    def __init__(self, cid, title, key, fmt=_num, numeric=False, width=80, dynamic=False, expand=False,
                 heat=None, na_hint=None, spark=None, aggregate=False, sort_item=None):
        self.id = cid
        self.title = title
        self.get = key if callable(key) else (lambda rec, k=key: rec.get(k))
        self.key = None if callable(key) else key
        # in tree view a collapsed row shows the total of its subtree
        self.summed = self.key in SUM_KEYS
        self.fmt = fmt
        self.numeric = numeric
        self.width = width
        self.dynamic = dynamic  # the value changes from tick to tick (informational)
        self.expand = expand
        self.heat = heat  # callable(value, view) -> 0..5 background intensity
        self.na_hint = na_hint  # tooltip for cells whose value is unreadable (None)
        self.spark = spark  # "cpu" | "mem": draw the item's history instead of text
        # in tree view, order siblings by the busiest process in their subtree
        self.aggregate = aggregate
        self.sort_item = sort_item  # callable(item) -> sort key (overrides get)


_NICE_LEVELS = (("Highest (−20)", -20), ("High (−10)", -10), ("Above Normal (−5)", -5), ("Normal (0)", 0),
                ("Below Normal (5)", 5), ("Low (10)", 10), ("Idle (19)", 19))
_IONICE_LEVELS = (("Realtime", "1:4"), ("High", "2:0"), ("Normal", "2:4"), ("Low", "2:7"), ("Idle", "3:0"))
_OOM_LEVELS = (("Never Kill (−1000)", -1000), ("Spare (−500)", -500), ("Normal (0)", 0), ("Prefer (500)", 500),
               ("Kill First (1000)", 1000))
# already offered as End Process / Kill / Suspend / Resume
_MENU_SIGNALS_SKIP = {signal.SIGTERM, signal.SIGKILL, signal.SIGSTOP, signal.SIGCONT}


# context menu actions that act on one process only: disabled while several are selected
_SINGLE_ACTIONS = ("properties", "open-location")
# actions that change a process: off while every selected process has exited (red rows)
_LIVE_ACTIONS = ("terminate", "terminate-tree", "kill", "kill-tree", "suspend", "resume", "signal",
                 "nice", "nice-custom", "ionice", "affinity", "oom-adj", "oom-adj-custom")


def _hidden_unless_enabled(label, action):
    item = Gio.MenuItem.new(label, action)
    item.set_attribute_value("hidden-when", GLib.Variant.new_string("action-disabled"))
    return item


def _levels(value, steps):
    level = 0
    if value:
        for step in steps:
            if value < step:
                break
            level += 1
    return level


# CPU values are a share of the whole machine; steps are 2/10/30/70/100 % of one core
_CPU_STEPS = tuple(pct / NCPU for pct in (2, 10, 30, 70, 100))
_MEM_STEPS = (0.02, 0.04, 0.08, 0.15, 0.30)  # share of physical memory
_RATE_STEPS = (16 << 10, 256 << 10, 4 << 20, 32 << 20, 128 << 20)  # bytes/s


def heat_cpu(value, _view):
    return _levels(value, _CPU_STEPS)


def heat_mem(value, view):
    total = view.mem_total()
    return _levels(value / total, _MEM_STEPS) if value and total else 0


def heat_rate(value, _view):
    return _levels(value, _RATE_STEPS)


_GPU_STEPS = (2, 10, 30, 70, 100)  # % of the busiest GPU engine


def heat_gpu(value, _view):
    return _levels(value, _GPU_STEPS)


def _fmt_gpu_mem(value):
    return fmt_bytes(value) if value else ""  # most processes don't use the GPU: leave them blank




IO_HINT = ("Not readable: the I/O counters of other users' processes are only "
           "available to administrators")


def _cpu_mean(item):
    hist = item.cpu_hist
    return sum(hist) / len(hist) if hist else 0.0


def _describe_cpu(data):
    mean = sum(data) / len(data)
    return (f"CPU now {fmt_pct_label(data[-1])}, average {fmt_pct_label(mean)}, "
            f"peak {fmt_pct_label(max(data))}\n(last {len(data)} updates)")


def _describe_mem(data):
    return (f"Working set now {fmt_bytes(data[-1])}\nmin {fmt_bytes(min(data))}, max {fmt_bytes(max(data))} "
            f"(last {len(data)} updates)")


def _state(rec):
    return STATE_NAMES.get(rec.get("state"), rec.get("state"))


COLUMNS = [
    ProcColumn("name", "Name", "name", str, width=260),
    ProcColumn("pid", "PID", "pid", numeric=True, width=70),
    ProcColumn("ppid", "Parent PID", "ppid", numeric=True, width=80),
    ProcColumn("cpu", "CPU", "cpu", fmt_pct, numeric=True, width=60, dynamic=True, heat=heat_cpu,
               aggregate=True),
    ProcColumn("cpu_graph", "CPU history", "cpu", fmt_pct, width=90, dynamic=True, spark="cpu",
               aggregate=True, sort_item=_cpu_mean),
    ProcColumn("cpu_kernel", "Kernel CPU", "cpu_kernel", fmt_pct, numeric=True, width=80, dynamic=True,
               heat=heat_cpu, aggregate=True),
    ProcColumn("cpu_time", "CPU time", "cpu_time", fmt_cpu_time, numeric=True, width=95, dynamic=True,
               aggregate=True),
    ProcColumn("gpu", "GPU", "gpu", fmt_pct, numeric=True, width=60, dynamic=True, heat=heat_gpu,
               aggregate=True),
    ProcColumn("gpu_mem", "GPU memory", "gpu_mem", _fmt_gpu_mem, numeric=True, width=100, dynamic=True,
               aggregate=True),
    ProcColumn("private", "Private bytes", "private", fmt_bytes, numeric=True, width=100, dynamic=True,
               heat=heat_mem, aggregate=True),
    ProcColumn("rss", "Working set", "rss", fmt_bytes, numeric=True, width=100, dynamic=True, heat=heat_mem,
               aggregate=True),
    ProcColumn("mem_graph", "Memory history", "rss", fmt_bytes, width=112, dynamic=True, spark="mem",
               aggregate=True),
    ProcColumn("shared", "Shared", "shared", fmt_bytes, numeric=True, width=90, dynamic=True, aggregate=True),
    ProcColumn("vms", "Virtual size", "vms", fmt_bytes, numeric=True, width=100, dynamic=True, aggregate=True),
    ProcColumn("swap", "Swap", "swap", fmt_bytes, numeric=True, width=80, dynamic=True, aggregate=True),
    ProcColumn("io_total", "I/O total rate", "io_total", fmt_rate, numeric=True, width=105, dynamic=True,
               heat=heat_rate, na_hint=IO_HINT, aggregate=True),
    ProcColumn("io_read", "I/O read rate", "io_read", fmt_rate, numeric=True, width=100, dynamic=True,
               heat=heat_rate, na_hint=IO_HINT, aggregate=True),
    ProcColumn("io_write", "I/O write rate", "io_write", fmt_rate, numeric=True, width=100, dynamic=True,
               heat=heat_rate, na_hint=IO_HINT, aggregate=True),
    ProcColumn("disk_total", "Disk rate", "disk_total", fmt_rate, numeric=True, width=90, dynamic=True,
               heat=heat_rate, na_hint=IO_HINT, aggregate=True),
    ProcColumn("disk_read", "Disk read rate", "disk_read", fmt_rate, numeric=True, width=100, dynamic=True,
               heat=heat_rate, na_hint=IO_HINT, aggregate=True),
    ProcColumn("disk_write", "Disk write rate", "disk_write", fmt_rate, numeric=True, width=100, dynamic=True,
               heat=heat_rate, na_hint=IO_HINT, aggregate=True),
    ProcColumn("threads", "Threads", "threads", numeric=True, width=65, dynamic=True, aggregate=True),
    ProcColumn("fds", "Open files", "fds", numeric=True, width=80, dynamic=True, aggregate=True),
    ProcColumn("user", "User name", "user", str, width=100),
    ProcColumn("nice", "Nice", "nice", numeric=True, width=50, dynamic=True),
    ProcColumn("priority", "Priority", "priority", numeric=True, width=60, dynamic=True),
    ProcColumn("state", "State", _state, str, width=80, dynamic=True),
    ProcColumn("processor", "Last CPU", "processor", numeric=True, width=70, dynamic=True),
    ProcColumn("oom_score", "OOM score", "oom_score", numeric=True, width=85, dynamic=True),
    ProcColumn("oom_adj", "OOM adjustment", "oom_adj", numeric=True, width=115, dynamic=True),
    ProcColumn("ctx", "Context switches", "ctx", fmt_int, numeric=True, width=110, dynamic=True,
               aggregate=True),
    ProcColumn("majflt", "Page faults (major)", "majflt", fmt_int, numeric=True, width=120, dynamic=True,
               aggregate=True),
    ProcColumn("create_time", "Start time", "create_time", fmt_timestamp, width=140),
    ProcColumn("unit", "systemd unit", "unit", str, width=180),
    ProcColumn("container", "Container", "container", str, width=120),
    ProcColumn("app_id", "Application ID", "app_id", str, width=160),
    ProcColumn("session", "Session", "session", str, width=60),
    ProcColumn("tty", "TTY", "tty", str, width=60),
    ProcColumn("exe", "File name", "exe", str, width=220),
    ProcColumn("cmdline", "Command line", "cmdline", str, width=300, expand=True),
]
COLUMNS_BY_ID = {c.id: c for c in COLUMNS}


_NONE_KEY = (0, 0)  # unreadable values sort before everything else


def _sort_key(value):
    if value is None:
        return _NONE_KEY
    return (1, value.casefold() if isinstance(value, str) else value)


def _set_heat(label, level):
    """Tint the cell label (heat-1 .. heat-5)."""
    old = getattr(label, "_heat", 0)
    if old == level:
        return
    if old:
        label.remove_css_class(f"heat-{old}")
    if level:
        label.add_css_class(f"heat-{level}")
    label._heat = level


class ProcessView(Gtk.Box):
    __gsignals__ = {
        "open-properties": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, win, tree, sampler, config, services):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.tree = tree
        self.sampler = sampler
        self.config = config
        self.services = services
        self._filter_text = ""
        self._visible = None
        self._columns = {}
        self._order = None  # (column id, tree mode, [(pid, starts a new rank), ...]) of the last sort
        self._heat_on = config.get("heat_cells")

        self.view = Gtk.ColumnView()
        self.view.add_css_class("procs")
        self.view.add_css_class("data-table")
        make_reorderable(self.view)
        self.view.set_tab_behavior(Gtk.ListTabBehavior.ITEM)
        self._build_columns()

        self._browse_model = self._make_tree_model()
        self.tree_model = self._browse_model
        # Sorting never calls into Python: _update_order() gives every process a rank for the
        # column picked in the header, and GTK sorts on that number. (The view's own sorter
        # would run a Python comparator thousands of times per update; it only tracks which
        # header was clicked.)
        self.rank_sorter = Gtk.NumericSorter.new(Gtk.PropertyExpression.new(ProcessItem, None, "sort-rank"))
        self.row_sorter = Gtk.TreeListRowSorter.new(self.rank_sorter)
        self.sort_model = Gtk.SortListModel.new(self.tree_model, self.row_sorter)
        self.view.get_sorter().connect("changed", lambda *_: self._resort())
        self.filter = Gtk.CustomFilter.new(self._filter_func)
        self.filter_model = Gtk.FilterListModel.new(self.sort_model, self.filter)
        # Ctrl/Shift+click select several processes; the context menu acts on all of them
        self.selection = Gtk.MultiSelection.new(self.filter_model)
        self.selection.connect("selection-changed", self._on_selection_changed)
        self.view.set_model(self.selection)
        self.view.connect("activate", self._on_activate)

        scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        scroller.set_child(self.view)
        self.append(scroller)
        self._vadj = scroller.get_vadjustment()
        remember_sort(self.view, config, "processes")

        self._build_actions()
        self._build_menu()
        self._build_shortcuts()
        tree.connect("applied", self._on_applied)
        config.connect("changed", self._on_config_changed)
        self._update_scanner_wants()

    # ------------------------------------------------------------------ model
    def _make_tree_model(self):
        if self.config.get("tree_mode"):
            return Gtk.TreeListModel.new(self.tree.root, False, True, self._create_children)
        return Gtk.TreeListModel.new(self.tree.flat, False, False, lambda _item: None)

    @staticmethod
    def _create_children(item):
        return item.children

    def set_tree_mode(self, enabled):
        if enabled == self.config.get("tree_mode"):
            return
        self.config.set("tree_mode", enabled)
        self._browse_model = self._make_tree_model()
        self._use_tree_model(self._make_tree_model() if enabled and self.searching else self._browse_model)

    def _use_tree_model(self, model):
        selected = self.selected_items()
        self.tree_model = model
        self.sort_model.set_model(model)
        self.refilter()  # a tree keeps ancestors; a flat list must not
        self._resort()
        self._keep_selection(selected)

    # ------------------------------------------------------------------ columns
    def _build_columns(self):
        visible = list(self.config.get("columns"))
        if "name" not in visible:
            visible.insert(0, "name")
        widths = self.config.get("column_widths")
        order = [c for c in visible if c in COLUMNS_BY_ID] + [c.id for c in COLUMNS if c.id not in visible]
        self._header_menu = Gio.Menu()
        self._col_actions = Gio.SimpleActionGroup()
        for cid in order:
            col = COLUMNS_BY_ID[cid]
            factory = Gtk.SignalListItemFactory()
            if cid == "name":
                factory.connect("setup", self._setup_name)
                factory.connect("bind", self._bind_name)
            elif col.spark:
                factory.connect("setup", self._setup_spark, col)
                factory.connect("bind", self._bind_spark, col)
            else:
                factory.connect("setup", self._setup_cell, col)
                factory.connect("bind", self._bind_cell, col)
            factory.connect("unbind", self._unbind)
            column = Gtk.ColumnViewColumn(title=col.title, factory=factory)
            column.set_id(cid)
            column.set_resizable(True)
            column.set_fixed_width(widths.get(cid, col.width))
            column.set_expand(col.expand)
            column.set_visible(cid in visible)
            column.set_sorter(Gtk.NumericSorter.new(None))  # makes the header clickable; see rank_sorter
            column.set_header_menu(self._header_menu)
            self._columns[cid] = column
            self.view.append_column(column)

        section = Gio.Menu()
        for col in COLUMNS:
            if col.id == "name":
                continue
            action = Gio.SimpleAction.new_stateful(
                "col-" + col.id, None, GLib.Variant.new_boolean(col.id in visible))
            action.connect("change-state", self._on_toggle_column, col.id)
            self._col_actions.add_action(action)
            section.append(col.title, "cols.col-" + col.id)
        self._header_menu.append_section("Columns", section)
        self.view.insert_action_group("cols", self._col_actions)
        self.view.get_columns().connect("items-changed", lambda *_: self._save_columns())

    def _on_toggle_column(self, action, value, cid):
        action.set_state(value)
        self._columns[cid].set_visible(value.get_boolean())
        self._save_columns()
        self._update_scanner_wants()

    def _update_scanner_wants(self):
        """Data the scanner only collects while a column shows it."""
        scanner = self.sampler.scanner
        scanner.want_fds = self._columns["fds"].get_visible()
        scanner.want_status = self._columns["swap"].get_visible() or self._columns["ctx"].get_visible()
        scanner.want_oom = self._columns["oom_score"].get_visible() or self._columns["oom_adj"].get_visible()
        self.sampler.system.gpu.want_processes = (self._columns["gpu"].get_visible()
                                                  or self._columns["gpu_mem"].get_visible())

    def _save_columns(self):
        cols = self.view.get_columns()
        visible = []
        for i in range(cols.get_n_items()):
            column = cols.get_item(i)
            if column.get_visible():
                visible.append(column.get_id())
        self.config.set("columns", visible)

    def save_widths(self):
        widths = {cid: col.get_fixed_width() for cid, col in self._columns.items()}
        self.config.set("column_widths", widths)

    # ------------------------------------------------------------------ cells
    def _setup_name(self, _factory, list_item):
        expander = Gtk.TreeExpander()
        expander.set_indent_for_icon(True)
        box = Gtk.Box(spacing=6)
        image = Gtk.Image(pixel_size=16)
        label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, single_line_mode=True)
        box.append(image)
        box.append(label)
        expander.set_child(box)
        expander._image = image
        expander._label = label
        list_item.set_child(expander)

    def _bind_name(self, _factory, list_item):
        expander = list_item.get_child()
        row = list_item.get_item()
        item = row.get_item()
        expander.set_list_row(row)
        expander._item = item

        def refresh(*_):
            expander._label.set_text(item.rec["name"])
            # only when it changed: setting an icon makes GTK look it up again, and unless it is
            # still in the icon theme's small cache, load and decode the image file on the next draw
            icon = item.icon
            if icon is not getattr(expander, "_icon", None):
                expander._icon = icon
                if icon is not None:
                    expander._image.set_from_gicon(icon)
                else:
                    expander._image.clear()
            expander.set_hide_expander(item.children.get_n_items() == 0)
            set_row_class(expander, item.hl)

        refresh()
        expander._hid = item.connect("updated", refresh)
        expander._hid_item = item

    def _setup_cell(self, _factory, list_item, col):
        label = Gtk.Label(ellipsize=Pango.EllipsizeMode.END, single_line_mode=True)
        label.set_xalign(1.0 if col.numeric else 0.0)
        if col.numeric:
            label.add_css_class("numeric")
        list_item.set_child(label)

    def _bind_cell(self, _factory, list_item, col):
        label = list_item.get_child()
        row = list_item.get_item()
        item = row.get_item()
        label._item = item
        get, fmt, heat, na_hint, key = col.get, col.fmt, col.heat, col.na_hint, col.key
        # a collapsed row stands for its whole subtree: it shows the totals of the usage columns
        # and leaves blank what only describes the one process (PID, command line, state, ...)
        tree_mode = self.config.get("tree_mode")
        summed = col.summed
        last = [None]

        def refresh(*_):
            sums = item.sums
            if tree_mode and sums is not None and not row.get_expanded():
                if not summed:
                    value, text = None, ""
                else:
                    value = sums[key]
                    text = "n/a" if value is None and na_hint else fmt(value)
            else:
                value = get(item.rec)
                text = "n/a" if value is None and na_hint else fmt(value)
            if text != last[0]:
                was_na = last[0] == "n/a"
                last[0] = text
                label.set_text(text)
                if na_hint and (text == "n/a") != was_na:
                    if text == "n/a":
                        label.add_css_class("na")
                        label.set_tooltip_text(na_hint)
                    else:
                        label.remove_css_class("na")
                        label.set_tooltip_text(None)
            if heat is not None:
                _set_heat(label, heat(value, self) if self._heat_on and value is not None else 0)

        refresh()
        label._hid = item.connect("updated", refresh)
        label._hid_item = item
        if tree_mode:
            label._hid_row = row.connect("notify::expanded", refresh)
            label._hid_row_obj = row

    def _setup_spark(self, _factory, list_item, col):
        if col.spark == "cpu":
            spark = Sparkline(CPU_COLOR, mode="zero", floor=100.0 / NCPU, describe=_describe_cpu)
        else:
            spark = Sparkline(MEM_COLOR, mode="range", describe=_describe_mem)
        list_item.set_child(spark)

    def _bind_spark(self, _factory, list_item, col):
        spark = list_item.get_child()
        row = list_item.get_item()
        item = row.get_item()
        spark._item = item
        cpu = col.spark == "cpu"
        summed = self.config.get("tree_mode")

        def redraw(*_):
            if summed and item.sums is not None and not row.get_expanded():
                data = item.sum_cpu_hist if cpu else item.sum_mem_hist  # collapsed: the subtree's total
            else:
                data = item.cpu_hist if cpu else item.mem_hist
            if data is not spark.data:
                spark.set_data(data, item.times)
            else:
                spark.queue_draw()

        redraw()
        spark._hid = item.connect("history", redraw)
        spark._hid_item = item
        if summed:
            spark._hid_row = row.connect("notify::expanded", redraw)
            spark._hid_row_obj = row

    def mem_total(self):
        snap = self.sampler.snapshot
        return snap["mem_total"] if snap else 0

    def _unbind(self, _factory, list_item):
        child = list_item.get_child()
        hid = getattr(child, "_hid", 0)
        item = getattr(child, "_hid_item", None)
        if hid and item is not None:
            item.disconnect(hid)
        child._hid = 0
        child._hid_item = None
        row = getattr(child, "_hid_row_obj", None)
        if row is not None:
            row.disconnect(child._hid_row)
            child._hid_row = 0
            child._hid_row_obj = None
        child._item = None
        if getattr(child, "_heat", 0):
            _set_heat(child, 0)
        if child.has_css_class("na"):  # the next binding starts without knowing it (refresh's `last`)
            child.remove_css_class("na")
            child.set_tooltip_text(None)
        if isinstance(child, Sparkline):
            child.set_data(None)
        if isinstance(child, Gtk.TreeExpander):
            set_row_class(child, "")
            child.set_list_row(None)

    def _sort_keys(self, col, tree_mode):
        """pid -> sort key of every process for column col."""
        items = self.tree.items
        get, sort_item = col.get, col.sort_item
        if sort_item is not None:
            own = {pid: _sort_key(sort_item(item)) for pid, item in items.items()}
        else:
            own = {pid: _sort_key(get(item.rec)) for pid, item in items.items()}
        if not (col.aggregate and tree_mode):
            return own
        # siblings are ordered by the busiest process in their subtree, so in tree view the
        # top consumer is reached by following the first rows down
        best = {}

        def subtree(item):
            key = best.get(item.pid)
            if key is None:
                key = own[item.pid]
                children = item.children
                for i in range(children.get_n_items()):
                    child = subtree(children.get_item(i))
                    if child > key:
                        key = child
                best[item.pid] = key
            return key

        return {pid: (subtree(item), own[pid]) for pid, item in items.items()}

    def _update_order(self, force=False):
        """Rank the processes for the sorted column; re-sort the list if their order changed.

        Returns True if rows changed places (not when new ones were merely sorted in)."""
        sorter = self.view.get_sorter()
        column = sorter.get_primary_sort_column()
        if column is None:
            if self._order is None:
                return False
            self._order = None
            for item in self.tree.items.values():
                item._rank = 0.0
            self.rank_sorter.changed(Gtk.SorterChange.DIFFERENT)
            return True
        col = COLUMNS_BY_ID[column.get_id()]
        tree_mode = self.config.get("tree_mode")
        keys = self._sort_keys(col, tree_mode)
        items = self.tree.items
        try:
            ordered = sorted(items, key=keys.__getitem__)
        except TypeError:  # a column mixing numbers and text
            keys = {pid: (key[0], str(key[1])) for pid, key in keys.items()}
            ordered = sorted(items, key=keys.__getitem__)
        # equal values share a rank, so GTK keeps such rows in their natural order
        ranks = []
        rank, last = -1, None
        for pid in ordered:
            key = keys[pid]
            if key != last:
                rank += 1
                last = key
            ranks.append(rank)
        order = (col.id, tree_mode, ordered, [b - a for a, b in zip([-1] + ranks, ranks)])
        self.rank_sorter.set_sort_order(sorter.get_primary_sort_order())
        if not force and order == self._order:
            return False  # same order as on screen: nothing for GTK to do
        moved = self._order is None or self._order[:2] != order[:2]
        if not moved and self._order[2] != ordered:
            # compare the order of the processes that are in both the old and the new list
            before = [pid for pid in self._order[2] if pid in items]
            known = set(before)
            moved = before != [pid for pid in ordered if pid in known]
        self._order = order
        for pid, rank in zip(ordered, ranks):
            items[pid]._rank = rank
        selected = self.selected_items()
        self.rank_sorter.changed(Gtk.SorterChange.DIFFERENT)
        self._keep_selection(selected)
        return moved

    def _resort(self):
        scroll = self._vadj.get_value()
        self._update_order(force=True)
        self._keep_scroll(scroll)

    def _keep_scroll(self, value):
        """Stay at the scroll offset `value` after the rows changed their order.

        GTK anchors a list to a row, not to an offset: when the rows are re-sorted the view
        follows the row that was at its top edge to wherever that row went. With a column that
        changes all the time (CPU) the list wanders off on every update and cannot be kept at
        the top. The anchor can only be set through the scroll position, and only once the new
        order is laid out, so: lay it out now (before the next frame is drawn, nothing flickers)
        and scroll back.
        """
        view = self.view
        width, height = view.get_width(), view.get_height()
        if not view.get_mapped() or width <= 0 or height <= 0:
            return
        view.measure(Gtk.Orientation.VERTICAL, -1)  # GTK wants a measure before every allocate
        view.allocate(width, height, -1, None)  # moves the adjustment to where the anchor row went
        if self._vadj.get_value() != value:
            self._vadj.set_value(value)

    # ------------------------------------------------------------------ filtering
    def _filter_func(self, row):
        visible = self._visible
        if visible is None:
            return True
        return row.get_item().pid in visible

    def _compute_visible(self):
        text = self._filter_text
        show_k = self.config.get("show_kernel_threads")
        show_o = self.config.get("show_other_users")
        if not text and show_k and show_o:
            return None
        my_uid = self.win.my_uid
        matched = set()
        for pid, item in self.tree.items.items():
            rec = item.rec
            if not show_k and rec["kthread"]:
                continue
            if not show_o and rec["uid"] != my_uid:
                continue
            if text:
                if not (text in rec["name"].casefold() or text == str(pid) or
                        text in (rec.get("cmdline") or "").casefold() or
                        text in (rec.get("user") or "").casefold() or
                        text in (rec.get("unit") or "").casefold() or
                        text in (rec.get("container") or "").casefold()):
                    continue
            matched.add(pid)
        # keep ancestors so the tree context stays visible
        result = set(matched)
        if self.config.get("tree_mode"):
            for pid in matched:
                for anc in self.tree.items[pid].ancestors():
                    if anc.pid in result:
                        break
                    result.add(anc.pid)
        return frozenset(result)

    def refilter(self):
        visible = self._compute_visible()
        if visible != self._visible:
            selected = self.selected_items()
            self._visible = visible
            self.filter.changed(Gtk.FilterChange.DIFFERENT)
            self._keep_selection(selected)
            self._on_selection_changed()  # selected rows that were filtered out are no longer selected

    @property
    def searching(self):
        return bool(self._filter_text)

    def set_filter_text(self, text):
        text = (text or "").casefold().strip()
        if text == self._filter_text:
            return
        was_searching = self.searching
        self._filter_text = text
        if self.config.get("tree_mode"):
            if was_searching != self.searching:
                # GTK only puts expanded branches in the flat model that the filter sees.
                # Search a fully expanded model without changing the user's normal expansion.
                self._use_tree_model(self._make_tree_model() if self.searching else self._browse_model)
                return
            if self.searching:
                self.expand_all(True)
        self.refilter()

    def _on_applied(self, _tree, structure_changed):
        if not getattr(self, "_scrolled_top", False) and self.filter_model.get_n_items():
            self._scrolled_top = True
            GLib.idle_add(lambda: self.view.scroll_to(0, None, Gtk.ListScrollFlags.NONE, None) and False)
        # the scroll position of the last frame: GTK only updates it with the next layout
        scroll = self._vadj.get_value()
        if structure_changed or self._visible is not None:
            self.refilter()
        if structure_changed:  # a selected process may just have exited
            self._disable_dead_actions(self.selected_items())
        # processes that appeared since the last sort have no rank yet
        moved = self._update_order(force=structure_changed)
        # rows that only appear or vanish leave the view on the rows it shows (GTK's anchor),
        # except at the top: stay there, so a row added above is not hidden
        if moved or (structure_changed and scroll == 0):
            self._keep_scroll(scroll)

    def _on_config_changed(self, _config, key):
        if key in ("show_kernel_threads", "show_other_users"):
            self.refilter()
        elif key == "heat_cells":
            self._heat_on = self.config.get("heat_cells")
            for item in self.tree.items.values():
                item.emit("updated")

    # ------------------------------------------------------------------ selection
    def selected_items(self):
        """The selected processes, in list order."""
        selection = self.selection.get_selection()
        model = self.filter_model
        items = []
        for i in range(selection.get_size()):
            row = model.get_item(selection.get_nth(i))
            if row is not None:
                items.append(row.get_item())
        return items

    def selected_item(self):
        """The first selected process, or None."""
        selection = self.selection.get_selection()
        if selection.is_empty():
            return None
        row = self.filter_model.get_item(selection.get_minimum())
        return row.get_item() if row is not None else None

    def _select_item(self, item, scroll=True):
        """Select just this process."""
        model = self.filter_model
        for i in range(model.get_n_items()):
            if model.get_item(i).get_item() is item:
                self.selection.select_item(i, True)
                if scroll:
                    self.view.scroll_to(i, None, Gtk.ListScrollFlags.FOCUS | Gtk.ListScrollFlags.SELECT, None)
                return True
        return False

    def _select_items(self, items):
        """Select exactly those of items that are shown. Returns how many were."""
        if len(items) == 1:
            if self._select_item(items[0], scroll=False):
                return 1
            self.selection.unselect_all()
            return 0
        wanted = set(items)
        model = self.filter_model
        n = model.get_n_items()
        positions = Gtk.Bitset.new_empty()
        for i in range(n):
            if model.get_item(i).get_item() in wanted:
                positions.add(i)
        self.selection.set_selection(positions, Gtk.Bitset.new_range(0, n))
        return positions.get_size()

    def _keep_selection(self, before):
        """Re-select the processes `before` if re-sorting or filtering the rows lost some of them."""
        if before and set(self.selected_items()) != set(before):
            self._select_items(before)

    def select_pid(self, pid):
        item = self.tree.get(pid)
        if item is None:
            self.win.toast(f"Process {pid} not found")
            return False
        if self._visible is not None and pid not in self._visible:
            self.win.clear_search()
            if not self.config.get("show_kernel_threads") and item.rec["kthread"]:
                self.config.set("show_kernel_threads", True)
            if not self.config.get("show_other_users"):
                self.config.set("show_other_users", True)
            self.refilter()
        # expand ancestors
        if self.config.get("tree_mode"):
            chain = list(item.ancestors())[::-1]
            for anc in chain:
                for i in range(self.tree_model.get_n_items()):
                    row = self.tree_model.get_row(i)
                    if row is not None and row.get_item() is anc:
                        row.set_expanded(True)
                        break
        return self._select_item(item)

    def _on_activate(self, _view, position):
        row = self.filter_model.get_item(position)
        if row is not None:
            self.emit("open-properties", row.get_item())

    def _on_selection_changed(self, *_):
        items = self.selected_items()
        # Refreshes re-sort and refilter the rows, which emits selection-changed. While the
        # context menu is open its items were set up by _sync_menu_state; recomputing them here
        # would show/hide entries (Suspend and Resume, Go to Parent, …) under the pointer. Only
        # a process that exited is still taken into account. The "closed" handler resyncs.
        popover = getattr(self, "popover", None)  # selection-changed can fire before _build_menu
        if popover is not None and popover.get_visible():
            self._disable_dead_actions(items)
            return
        has = bool(items)
        for name in self._item_actions:
            self.actions.lookup_action(name).set_enabled(has)
        # these only make sense for one process
        for name in _SINGLE_ACTIONS:
            self.actions.lookup_action(name).set_enabled(len(items) == 1)
        any_service = any(self._unit_target(i.rec) for i in items)
        for name in ("unit-stop", "unit-restart"):
            self.actions.lookup_action(name).set_enabled(any_service)
        self.actions.lookup_action("goto-parent").set_enabled(len(items) == 1 and items[0].parent is not None)
        self._disable_dead_actions(items)

    def _disable_dead_actions(self, items):
        """A process that exited can't be acted on; its pid may already belong to another one."""
        if items and all(i.terminated for i in items):
            for name in _LIVE_ACTIONS:
                self.actions.lookup_action(name).set_enabled(False)

    # ------------------------------------------------------------------ actions
    def _build_actions(self):
        self.actions = Gio.SimpleActionGroup()
        simple = {
            "terminate": lambda: self._signal(signal.SIGTERM, "Terminate"),
            "terminate-tree": lambda: self._signal(signal.SIGTERM, "Terminate", tree=True),
            "kill": lambda: self._signal(signal.SIGKILL, "Kill"),
            "kill-tree": lambda: self._signal(signal.SIGKILL, "Kill", tree=True),
            "suspend": lambda: self._signal_now(signal.SIGSTOP),
            "resume": lambda: self._signal_now(signal.SIGCONT),
            "properties": lambda: self._with_item(lambda i: self.emit("open-properties", i)),
            "affinity": lambda: self._with_items(
                lambda items: pa.AffinityDialog(self.win, pa.targets_of(items), items[0].name).present(self.win)),
            "nice-custom": lambda: self._with_items(
                lambda items: pa.NiceDialog(self.win, pa.targets_of(items), items[0].name,
                                            items[0].rec["nice"]).present(self.win)),
            "oom-adj-custom": lambda: self._with_items(
                lambda items: pa.OomDialog(self.win, pa.targets_of(items), items[0].name,
                                           pa.get_oom_adj(items[0].pid)).present(self.win)),
            "open-location": lambda: self._with_item(lambda i: pa.open_location(self.win, i.rec.get("exe"))),
            "copy-name": lambda: self._copy_lines(lambda i: i.name),
            "copy-pid": lambda: self._copy_lines(lambda i: str(i.pid)),
            "copy-cmdline": lambda: self._copy_lines(lambda i: i.rec.get("cmdline") or ""),
            "copy-row": lambda: self._copy_lines(self._row_text),
            "unit-stop": lambda: self._unit_action("StopUnit"),
            "unit-restart": lambda: self._unit_action("RestartUnit"),
            "goto-parent": lambda: self._with_item(
                lambda i: i.parent and self.select_pid(i.parent.pid)),
        }
        self._item_actions = list(simple.keys())
        for name, func in simple.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, f=func: f())
            action.set_enabled(False)
            self.actions.add_action(action)

        # nice/ionice carry the selected process's current value as state, so the
        # context menu renders them as radio lists with the active level checked
        for name, vtype, handler, state in (
            ("signal", "i", lambda v: self._signal_now(v.get_int32()), None),
            ("nice", "i", lambda v: self._with_items(
                lambda items: pa.renice(self.win, pa.targets_of(items), v.get_int32())),
             GLib.Variant.new_int32(0)),
            ("ionice", "s", self._on_ionice, GLib.Variant.new_string("")),
            ("oom-adj", "i", lambda v: self._with_items(
                lambda items: pa.set_oom_adj(self.win, pa.targets_of(items), v.get_int32())),
             GLib.Variant.new_int32(0)),
        ):
            if state is None:
                action = Gio.SimpleAction.new(name, GLib.VariantType.new(vtype))
            else:
                action = Gio.SimpleAction.new_stateful(name, GLib.VariantType.new(vtype), state)
            action.connect("activate", lambda _a, p, h=handler: h(p))
            action.set_enabled(False)
            self.actions.add_action(action)
            self._item_actions.append(name)
        self.insert_action_group("proc", self.actions)

    def _with_item(self, func):
        """func(item) for an action on a single process (disabled while several are selected)."""
        items = self.selected_items()
        if len(items) == 1:
            func(items[0])

    def _live_items(self):
        """The selected processes that are still running (not the red rows of exited ones)."""
        return [i for i in self.selected_items() if not i.terminated]

    def _with_items(self, func):
        items = self._live_items()
        if items:
            func(items)

    def _signal(self, sig, verb, tree=False):
        roots = self._live_items()
        if not roots:
            return
        if not tree:
            pa.confirm_signal(self.win, roots, sig, verb, roots=roots)
            return
        # a selected process inside another selected one's tree gets the signal with that tree
        picked = set(roots)
        roots = [r for r in roots if not any(a in picked for a in r.ancestors())]
        items = []
        for root in roots:
            items.append(root)
            items += [d for d in reversed(list(root.descendants())) if not d.terminated]
        pa.confirm_signal(self.win, items, sig, verb + " tree", roots=roots)

    def _signal_now(self, sig):
        items = self._live_items()
        if items:
            pa.send_signal(self.win, items, sig)
            self.sampler.refresh()

    def _on_ionice(self, value):
        klass, level = (int(x) for x in value.get_string().split(":"))
        self._with_items(lambda items: pa.set_ionice(self.win, pa.targets_of(items), klass, level))

    def _unit_target(self, rec):
        """(scope, unit name, name to show) of the service a process belongs to, or None.

        None also for a service of another user's manager: we only talk to the system manager
        and to our own, where a unit of the same name is a different unit."""
        if rec.get("unit_kind") != "service" or not rec.get("unit_id"):
            return None
        if rec.get("unit_scope") == "user":
            if rec.get("unit_uid") != self.win.my_uid:
                return None
            return "user", rec["unit_id"], rec["unit"]
        return "system", rec["unit_id"], rec["unit"]

    def _unit_action(self, action):
        units = []
        for item in self.selected_items():
            unit = self._unit_target(item.rec)
            if unit is not None and unit not in units:
                units.append(unit)
        verb = "Stopped" if action == "StopUnit" else "Restarted"
        for scope, unit, label in units:
            self.services.unit_action(
                scope, unit, action,
                lambda ok, err, label=label: self.win.toast(f"{verb} {label}" if ok else f"Failed: {err}"))

    def _copy_lines(self, text):
        """Copy text(item) of every selected process, one per line."""
        items = self.selected_items()
        if items:
            pa.copy_text("\n".join(text(i) for i in items))

    def _row_text(self, item):
        parts = []
        cols = self.view.get_columns()
        for i in range(cols.get_n_items()):
            column = cols.get_item(i)
            if column.get_visible():
                col = COLUMNS_BY_ID[column.get_id()]
                parts.append(col.fmt(col.get(item.rec)))
        return "\t".join(parts)

    def _build_menu(self):
        # Built once: swapping the model of a GtkPopoverMenu later leaves its popup
        # surface sized too small. Per-process differences are made with hidden-when
        # (Suspend/Resume, Go to Parent, the service items) and action state
        # (the priority radio lists); see _sync_menu_state.
        menu = Gio.Menu()

        header = Gio.MenuItem.new(None, None)
        header.set_attribute_value("custom", GLib.Variant.new_string("header"))
        top = Gio.Menu()
        top.append_item(header)
        menu.append_section(None, top)

        info = Gio.Menu()
        info.append("Properties", "proc.properties")
        info.append_item(_hidden_unless_enabled("Go to Parent", "proc.goto-parent"))
        info.append("Open File Location", "proc.open-location")
        menu.append_section(None, info)

        ctl = Gio.Menu()
        ctl.append("End Process", "proc.terminate")
        ctl.append("End Process Tree", "proc.terminate-tree")
        ctl.append_item(_hidden_unless_enabled("Suspend", "proc.suspend"))
        ctl.append_item(_hidden_unless_enabled("Resume", "proc.resume"))
        force = Gio.Menu()
        force.append("Kill", "proc.kill")
        force.append("Kill Tree", "proc.kill-tree")
        signals = Gio.Menu()
        for name, num in SIGNALS:
            if num not in _MENU_SIGNALS_SKIP:
                signals.append(f"{name} ({num})", f"proc.signal({num})")
        force_more = Gio.Menu()
        force_more.append_submenu("Send Signal", signals)
        force.append_section(None, force_more)
        ctl.append_submenu("Force", force)
        menu.append_section(None, ctl)

        tune = Gio.Menu()
        prio = Gio.Menu()
        for label, value in _NICE_LEVELS:
            prio.append(label, f"proc.nice({value})")
        prio_more = Gio.Menu()
        prio_more.append("Custom…", "proc.nice-custom")
        prio.append_section(None, prio_more)
        tune.append_submenu("Priority", prio)
        io = Gio.Menu()
        for label, value in _IONICE_LEVELS:
            io.append(label, f"proc.ionice('{value}')")
        tune.append_submenu("I/O Priority", io)
        tune.append("CPU Affinity…", "proc.affinity")
        oom = Gio.Menu()
        for label, value in _OOM_LEVELS:
            oom.append(label, f"proc.oom-adj({value})")
        oom_more = Gio.Menu()
        oom_more.append("Custom…", "proc.oom-adj-custom")
        oom.append_section(None, oom_more)
        tune.append_submenu("OOM Killer", oom)
        tune.append_item(_hidden_unless_enabled("Stop Service", "proc.unit-stop"))
        tune.append_item(_hidden_unless_enabled("Restart Service", "proc.unit-restart"))
        menu.append_section(None, tune)

        misc = Gio.Menu()
        copy = Gio.Menu()
        copy.append("Name", "proc.copy-name")
        copy.append("PID", "proc.copy-pid")
        copy.append("Command Line", "proc.copy-cmdline")
        copy.append("Visible Columns", "proc.copy-row")
        misc.append_submenu("Copy", copy)
        menu.append_section(None, misc)

        self.popover = context_menu(menu, self)
        self.popover.add_child(self._build_menu_header(), "header")
        # Suspend/Resume are narrowed to one only while the menu is open
        self.popover.connect("closed", self._on_selection_changed)
        gesture = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        gesture.connect("pressed", self._on_right_click)
        self.view.add_controller(gesture)

    def _build_menu_header(self):
        grid = Gtk.Grid(column_spacing=12)
        grid.add_css_class("proc-menu-header")
        self._menu_title = Gtk.Label(xalign=0, hexpand=True, max_width_chars=32,
                                     ellipsize=Pango.EllipsizeMode.END)
        self._menu_title.add_css_class("proc-menu-title")
        self._menu_pid = Gtk.Label(xalign=1)
        self._menu_pid.add_css_class("dim-label")
        self._menu_pid.add_css_class("numeric")
        self._menu_unit = Gtk.Label(xalign=0, max_width_chars=40, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self._menu_unit.add_css_class("dim-label")
        grid.attach(self._menu_title, 0, 0, 1, 1)
        grid.attach(self._menu_pid, 1, 0, 1, 1)
        grid.attach(self._menu_unit, 0, 1, 2, 1)
        return grid

    def _sync_menu_state(self, items):
        item = items[0]
        if len(items) == 1:
            rec = item.rec
            self._menu_title.set_label(item.name)
            self._menu_pid.set_label(f"PID {item.pid}")
            unit = rec.get("unit") if rec.get("unit_kind") == "service" else None
            self._menu_unit.set_label(unit or "")
            self._menu_unit.set_visible(bool(unit))
        else:
            self._menu_title.set_label(f"{len(items)} processes")
            self._menu_pid.set_label("")
            self._menu_unit.set_label(pa.names_summary(items, 3))
            self._menu_unit.set_visible(True)
        stopped = [i.rec.get("state") == "T" for i in items]
        self.actions.lookup_action("suspend").set_enabled(not all(stopped))
        self.actions.lookup_action("resume").set_enabled(any(stopped))
        # the priority lists show a level as checked only when all selected processes have it
        nices = {i.rec.get("nice") for i in items}
        nice = nices.pop() if len(nices) == 1 else None
        self.actions.lookup_action("nice").set_state(GLib.Variant.new_int32(nice if nice is not None else 99))
        ionice = pa.get_ionice(item.pid, item.rec.get("nice"))
        for other in items[1:]:
            if pa.get_ionice(other.pid, other.rec.get("nice")) != ionice:
                ionice = ""
                break
        self.actions.lookup_action("ionice").set_state(GLib.Variant.new_string(ionice))
        oom_adj = pa.get_oom_adj(item.pid)
        for other in items[1:]:
            if pa.get_oom_adj(other.pid) != oom_adj:
                oom_adj = None
                break
        self.actions.lookup_action("oom-adj").set_state(GLib.Variant.new_int32(9999 if oom_adj is None else oom_adj))
        self._disable_dead_actions(items)

    def _on_right_click(self, gesture, _n, x, y):
        widget = self.view.pick(x, y, Gtk.PickFlags.DEFAULT)
        holder = find_ancestor_with(widget, "_item")
        if holder is None or holder._item is None:
            return
        self.popup_menu(holder._item, x, y)
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def popup_menu(self, item, x, y):
        """Open the context menu at x, y of the view for item, or for all selected processes if it is one of them."""
        items = self.selected_items()
        if item not in items:
            self._select_item(item, scroll=False)
            items = [item]
        self._sync_menu_state(items)
        popup_at(self.popover, self.view, x, y)

    def do_size_allocate(self, width, height, baseline):
        Gtk.Box.do_size_allocate(self, width, height, baseline)
        present_popover(self.popover)

    def _build_shortcuts(self):
        ctrl = Gtk.ShortcutController()
        ctrl.set_scope(Gtk.ShortcutScope.LOCAL)
        for trigger, action in (
            ("Delete", "proc.terminate"),
            ("<Shift>Delete", "proc.kill"),
            ("<Control>c", "proc.copy-row"),
            ("<Alt>Return", "proc.properties"),
        ):
            ctrl.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string(trigger),
                                               Gtk.NamedAction.new(action)))
        self.view.add_controller(ctrl)

    def expand_all(self, expanded):
        i = 0
        while i < self.tree_model.get_n_items():
            row = self.tree_model.get_row(i)
            if row is not None and row.is_expandable():
                row.set_expanded(expanded)
            i += 1
