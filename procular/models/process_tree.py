"""Live process tree: one stable GObject per process, updated in place."""

import time
from collections import deque
from functools import reduce
from operator import add

from gi.repository import Gio, GObject

from ..util.timeline import SampleTime
from . import highlight
from .process_history import make_exit_record

SPARK_SAMPLES = 60  # per-process history kept for the sparkline columns

# usage values that add up: a collapsed row in tree view shows the total of its subtree
SUM_KEYS = ("cpu", "cpu_kernel", "cpu_time", "gpu", "gpu_mem", "private", "rss", "shared", "vms", "swap",
            "io_total", "io_read", "io_write", "disk_total", "disk_read", "disk_write", "threads", "fds",
            "ctx", "majflt")


class ProcessItem(GObject.Object):
    __gtype_name__ = "ProcularProcessItem"
    __gsignals__ = {
        "updated": (GObject.SignalFlags.RUN_FIRST, None, ()),  # rec, hl, icon or children changed
        "history": (GObject.SignalFlags.RUN_FIRST, None, ()),  # cpu_hist / mem_hist look different
    }

    def __init__(self, rec):
        super().__init__()
        self.rec = rec
        self.pid = rec["pid"]
        self.parent = None
        self.children = Gio.ListStore(item_type=ProcessItem)
        self.icon = None
        self.born = 0.0
        self.terminated_at = None
        self.hl = ""
        self.times = deque(maxlen=SPARK_SAMPLES)
        self.cpu_hist = deque(maxlen=SPARK_SAMPLES)
        self.mem_hist = deque(maxlen=SPARK_SAMPLES)
        self.peak_cpu = 0.0
        self.peak_rss = 0
        self.changed = True  # rec differs from the previous tick's
        self.spark_ticks = 0  # ticks until the history graphs stop changing (all samples equal again)
        self.base_hl = None  # category highlight of the current rec, None = not computed yet
        # totals of SUM_KEYS over this process and its live descendants; None without children
        self.sums = None
        self.sum_cpu_hist = None
        self.sum_mem_hist = None
        self.sum_ticks = 0  # like spark_ticks, for the summed histories
        self.sums_changed = False
        self.contrib = None  # SUM_KEYS values this item adds to its parent's sums (see _subtree)
        self._rank = 0.0

    @GObject.Property(type=float)
    def sort_rank(self):
        """Position in the process list's current sort order (set by ProcessView)."""
        return self._rank

    @property
    def name(self):
        return self.rec["name"]

    @property
    def terminated(self):
        return self.terminated_at is not None

    def ancestors(self):
        node = self.parent
        while node is not None:
            yield node
            node = node.parent

    def descendants(self):
        for i in range(self.children.get_n_items()):
            child = self.children.get_item(i)
            yield child
            yield from child.descendants()


class ProcessTree(GObject.Object):
    __gsignals__ = {
        "applied": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),  # structure changed?
        "exited": (GObject.SignalFlags.RUN_FIRST, None, (object,)),  # list of new exit records
        "users-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),  # a row is coloured for a new uid
    }

    def __init__(self, config, icon_resolver=None):
        super().__init__()
        self.config = config
        self.icons = icon_resolver
        self.items = {}
        self.root = Gio.ListStore(item_type=ProcessItem)
        self.flat = Gio.ListStore(item_type=ProcessItem)
        self._first = True
        self._enabled = None  # (colour mode, enabled highlights) at the last apply()
        self._sample_interval = None
        self.users = set()  # uids that rows are coloured for (colour mode "user")
        self._users_changed = False
        self.exit_history = deque(maxlen=config.get("exit_history_size"))
        self._exit_seq = 0
        self._new_exits = []
        # items whose subtree totals have to be added up again in this apply(): a changed record,
        # a child added, removed or terminated somewhere below them
        self._dirty = set()

    # ------------------------------------------------------------------
    def _store_for(self, parent):
        return parent.children if parent is not None else self.root

    def _mark(self, item):
        """item's subtree totals, and so those of all its ancestors, are out of date.

        An item already marked has its ancestors marked as well (_attach marks the new ones of
        an item that moves), so the walk ends there. That also ends it in a cycle of parents,
        which a /proc read while pids are reused could in theory produce.
        """
        dirty = self._dirty
        while item is not None and item not in dirty:
            dirty.add(item)
            item = item.parent

    @staticmethod
    def _acyclic_parent(item, parent):
        """Return parent unless attaching item under it would form a cycle (e.g. mid-scan PID reuse)."""
        node = parent
        while node is not None:
            if node is item:
                return None
            node = node.parent
        return parent

    def _attach(self, item):
        ppid = item.rec["ppid"]
        parent = self.items.get(ppid) if ppid and ppid != item.pid else None
        if parent is not None and parent.terminated:
            parent = None
        parent = self._acyclic_parent(item, parent)
        item.parent = parent
        self._store_for(parent).append(item)
        if parent is not None:
            self._mark(parent)
            parent.emit("updated")

    def _detach(self, item):
        store = self._store_for(item.parent)
        found, pos = store.find(item)
        if found:
            store.remove(pos)
        old_parent = item.parent
        item.parent = None
        if old_parent is not None:
            self._mark(old_parent)
            old_parent.emit("updated")

    def _remove(self, item):
        children = [item.children.get_item(i) for i in range(item.children.get_n_items())]
        for child in children:
            item.children.remove(0)
            child.parent = None
            self.root.append(child)
        self._detach(item)
        found, pos = self.flat.find(item)
        if found:
            self.flat.remove(pos)
        self.items.pop(item.pid, None)

    def _record_exit(self, item):
        if item.rec.get("kthread") and not self.config.get("show_kernel_threads"):
            return  # kworkers come and go constantly
        self._exit_seq += 1
        record = make_exit_record(item, time.time(), self._exit_seq)
        self.exit_history.append(record)
        self._new_exits.append(record)

    def clear_exit_history(self):
        self.exit_history.clear()

    @staticmethod
    def _push(cpu_hist, mem_hist, cpu, rss, ticks):
        """Append a sample to the histories; returns the ticks until the graphs are flat again."""
        if not cpu_hist or cpu_hist[-1] != cpu or mem_hist[-1] != rss:
            ticks = SPARK_SAMPLES  # the new sample has to scroll through the graphs
        elif ticks:
            ticks -= 1
        cpu_hist.append(cpu)
        mem_hist.append(rss)
        return ticks

    def _sample(self, item, stamp):
        rec = item.rec
        cpu = rec.get("cpu") or 0.0
        rss = rec.get("rss") or 0
        item.times.append(stamp)
        item.spark_ticks = self._push(item.cpu_hist, item.mem_hist, cpu, rss, item.spark_ticks)
        if cpu > item.peak_cpu:
            item.peak_cpu = cpu
        if rss > item.peak_rss:
            item.peak_rss = rss

    def _subtree(self, item, dirty):
        """Totals of SUM_KEYS over item and its live descendants (stored on items with children).

        Returns them as a tuple in SUM_KEYS order, which is also kept as item.contrib: what the
        item adds to its parent's totals. Only the items in `dirty` (all of them when None) are
        added up again; the others' contrib still holds. Most processes are idle, so that is a
        small part of the tree, but a parent like init has hundreds of children: their values
        are added column by column in C, in the same order as one by one (the same float sums).
        """
        rec = item.rec
        own = tuple([rec.get(k) for k in SUM_KEYS])
        children = item.children
        n = children.get_n_items()
        if not n:
            if item.sums is not None:
                item.sums = item.sum_cpu_hist = item.sum_mem_hist = None
                item.sum_ticks = 0
                item.sums_changed = True
                item.spark_ticks = max(item.spark_ticks, 1)  # graphs switch back to the own history
            item.contrib = own
            return own
        rows = [own]
        for i in range(n):
            child = children.get_item(i)
            if dirty is None or child.contrib is None or child in dirty:
                sub = self._subtree(child, dirty)
            else:
                sub = child.contrib
            if child.terminated_at is None:  # an exited child's last values are stale
                rows.append(sub)
        totals = []
        for column in zip(*rows):
            values = [v for v in column if v is not None]
            totals.append(reduce(add, values) if values else None)
        contrib = item.contrib = tuple(totals)
        sums = dict(zip(SUM_KEYS, contrib))
        if sums != item.sums:
            item.sums = sums
            item.sums_changed = True
        return contrib

    def _highlight_setup(self):
        mode = self.config.get("highlight_mode")
        enabled = frozenset(k for k, v in self.config.get("highlights").items() if v.get("enabled"))
        return mode, enabled

    def _base_hl(self, rec, mode, enabled):
        """Steady highlight class of a record: its state, else its category or user."""
        if mode == "category":
            return highlight.pick(highlight.categories(rec), enabled)
        hl = highlight.state(rec, enabled)
        if hl or mode != "user":
            return hl
        uid = rec.get("uid")
        if uid is None:
            return ""
        if uid not in self.users:
            self.users.add(uid)
            self._users_changed = True
        return highlight.user_class(uid)

    # ------------------------------------------------------------------
    def apply(self, records, stamp=None):
        now = time.monotonic()
        stamp = stamp or SampleTime.now()
        timing_changed = stamp.gap or stamp.interval != self._sample_interval
        self._sample_interval = stamp.interval
        duration = self.config.get("highlight_seconds")
        mode, enabled = self._highlight_setup()
        rehighlight = (mode, enabled) != self._enabled  # cached steady highlights are stale
        self._enabled = (mode, enabled)
        structure_changed = False

        added = []
        moved = []
        for pid in sorted(records):
            rec = records[pid]
            item = self.items.get(pid)
            if item is not None and (item.rec["starttime"] != rec["starttime"] or item.terminated):
                if not item.terminated and not self._first:
                    self._record_exit(item)  # PID reused within one interval
                self._remove(item)
                item = None
            if item is None:
                item = ProcessItem(rec)
                if not self._first:
                    item.born = now
                if self.icons is not None:
                    item.icon = self.icons.resolve(rec)
                self.items[pid] = item
                added.append(item)
            else:
                old = item.rec
                item.rec = rec
                # most processes are idle: their record is identical to the last one
                item.changed = rec != old
                if item.changed:
                    self._mark(item)
                    if old["ppid"] != rec["ppid"]:
                        moved.append(item)
                    if (old["name"], old.get("app_id"), old.get("exe")) != (rec["name"], rec.get("app_id"),
                                                                           rec.get("exe")) \
                            and self.icons is not None:
                        item.icon = self.icons.resolve(rec)
            self._sample(item, stamp)

        if added:
            structure_changed = True
            if self._first:
                # bulk insert: build children lists first, then splice
                roots = []
                kids = {}
                for item in added:
                    ppid = item.rec["ppid"]
                    parent = self.items.get(ppid) if ppid and ppid != item.pid else None
                    parent = self._acyclic_parent(item, parent)
                    item.parent = parent
                    if parent is None:
                        roots.append(item)
                    else:
                        kids.setdefault(parent.pid, []).append(item)
                for ppid, children in kids.items():
                    self.items[ppid].children.splice(0, 0, children)
                self.root.splice(self.root.get_n_items(), 0, roots)
                self.flat.splice(self.flat.get_n_items(), 0, added)
            else:
                for item in added:
                    self._attach(item)
                    self.flat.append(item)
        for item in moved:
            structure_changed = True
            self._detach(item)
            self._attach(item)

        # terminated processes stay visible (red) for the highlight duration
        term_enabled = "term" in enabled and duration > 0
        for pid, item in list(self.items.items()):
            if pid in records:
                continue
            if item.terminated_at is None:
                self._record_exit(item)
            if item.terminated_at is None and term_enabled:
                item.terminated_at = now
                self._mark(item)  # its parent no longer counts it
                structure_changed = True
            elif item.terminated_at is None or now - item.terminated_at >= duration:
                self._remove(item)
                structure_changed = True

        dirty = None if self._first else self._dirty  # the first tree is added up in full
        for i in range(self.root.get_n_items()):
            item = self.root.get_item(i)
            if dirty is None or item in dirty:
                self._subtree(item, dirty)
        self._dirty = set()

        new_enabled = "new" in enabled
        for item in self.items.values():
            if item.sums is not None and not item.terminated:
                # the summed histories get a sample on every tick, changed or not
                if item.sum_cpu_hist is None:
                    item.sum_cpu_hist = deque(maxlen=SPARK_SAMPLES)
                    item.sum_mem_hist = deque(maxlen=SPARK_SAMPLES)
                item.sum_ticks = self._push(item.sum_cpu_hist, item.sum_mem_hist, item.sums["cpu"] or 0.0,
                                            item.sums["rss"] or 0, item.sum_ticks)
            changed = item.changed
            if changed or rehighlight or item.base_hl is None:
                item.base_hl = self._base_hl(item.rec, mode, enabled)
            if item.terminated:
                hl = "term"
            elif new_enabled and item.born and now - item.born < duration:
                hl = "new"
            else:
                hl = item.base_hl
            # only rows that show something new are refreshed: every bound cell runs Python code
            if changed or hl != item.hl or item.sums_changed:
                item.hl = hl
                item.changed = False
                item.sums_changed = False
                item.emit("updated")
            if timing_changed and not item.terminated:
                # Even a flat graph changes when a gap or a new cadence scrolls across it.
                item.spark_ticks = SPARK_SAMPLES
            if item.spark_ticks or item.sum_ticks:
                item.emit("history")

        self._first = False
        if self._users_changed:
            self._users_changed = False
            self.emit("users-changed")  # before the rows are drawn: their colour has to exist
        self.emit("applied", structure_changed)
        if self._new_exits:
            exits, self._new_exits = self._new_exits, []
            self.emit("exited", exits)

    def get(self, pid):
        return self.items.get(pid)

    def refresh_highlights(self):
        """The colour mode or the enabled highlights changed. The caller rebuilds the CSS."""
        mode, enabled = self._highlight_setup()
        self._enabled = (mode, enabled)
        for item in self.items.values():
            item.base_hl = self._base_hl(item.rec, mode, enabled)
            if item.hl in ("new", "term") or item.hl == item.base_hl:
                continue
            item.hl = item.base_hl
            item.emit("updated")
        self._users_changed = False
