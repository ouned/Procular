"""Per-process properties window (General, Statistics, Performance, Threads, ...)."""

import grp
import os
import signal
import time
from collections import deque
from datetime import date, datetime

import psutil
from gi.repository import Adw, Gio, GLib, GObject, Gtk, Pango

from ..helper.procular_helper import read_starttime, require_process
from ..models.process_tree import SPARK_SAMPLES
from ..providers.gpu import engine_label, region_label
from ..providers.handles import describe_socket
from ..providers.network import PROTO_FILES, TrafficRates, tcp_traffic
from ..providers.processes import BOOT_TIME, CLK_TCK, IS_ROOT, MY_UID, NCPU, username
from ..util import linux, privileged
from ..util.format import (fmt_bytes, fmt_cpu_time, fmt_int, fmt_pct, fmt_rate, fmt_relative,
                           fmt_short_rate, fmt_span, fmt_timestamp)
from ..util.lifecycle import destroy_on_close, is_destroyed
from ..util.tasks import run_async
from ..widgets import process_actions as pa
from ..widgets.graph import CACHE as WORKING_SET_COLOR
from ..widgets.graph import GPU as GPU_COLOR
from ..widgets.graph import Graph, Series
from ..widgets.mini_graphs import CPU_COLOR, KERNEL_COLOR, MEM_COLOR, READ_COLOR, WRITE_COLOR
from ..widgets.network_view import (COPY_FIELDS, add_copy_actions, add_traffic, copy_submenu, state_column,
                                    state_legend, traffic_columns, traffic_summary)
from ..widgets.sparkline import Sparkline
from ..widgets.table import Column, Table
from ..widgets.wrap_box import WrapBox
from .sysinfo import TabScroller, card

SIDE_GRAPH_MIN = 26  # the sidebar's CPU graph; grows up to TabScroller's maximum in tall windows

NA = "Access denied"

# process / thread state letter -> status-dot class (see style.css)
_STATE_CLASS = {"R": "running", "S": "active", "I": "active", "D": "changing", "T": "changing",
                "t": "changing", "Z": "failed", "X": "failed"}
_FD_ICONS = {"File": "text-x-generic-symbolic", "Socket": "network-wired-symbolic",
             "Pipe": "media-playlist-consecutive-symbolic", "Device": "drive-harddisk-symbolic",
             "Memfd": "media-flash-symbolic", "Namespace": "view-paged-symbolic"}
_FD_ICON_DEFAULT = "emblem-system-symbolic"


def state_class(code):
    return _STATE_CLASS.get(code, "inactive")


# states worth a highlighted pill in the overview; running / sleeping / idle are normal
_NOTABLE_STATES = {"D", "T", "t", "Z", "X"}


def state_text(rec):
    text = linux.STATE_NAMES.get(rec["state"], rec["state"])
    running, total = rec.get("threads_running", 0), rec.get("threads") or 0
    if total > 1 and running:
        text += f" ({running} of {total} threads running)"
    return text


def path_markup(path):
    """Directory part dimmed so the file name stands out."""
    if not path:
        return ""
    esc = GLib.markup_escape_text
    head, sep, tail = str(path).rpartition("/")
    if not sep or not tail or not str(path).startswith("/"):
        return esc(str(path))
    return f'<span alpha="55%">{esc(head + "/")}</span>{esc(tail)}'


def when_text(ts):
    """Clock time, with the date in front when it is not today."""
    dt = datetime.fromtimestamp(ts)
    clock = dt.strftime("%H:%M:%S")
    return clock if dt.date() == date.today() else dt.strftime("%Y-%m-%d ") + clock


def started_text(ts):
    return f"{when_text(ts)} · {fmt_relative(time.time() - ts)}"


def snapshot_note(ts):
    """Marks a value that is an elevated read's snapshot (see ProcReader.elevated_at)."""
    return f"as of {when_text(ts)}"


def oom_text(pid):
    """"Score 334, adjustment −500": how likely the kernel's OOM killer is to pick the process."""
    try:
        score = int(linux.read_text(f"/proc/{pid}/oom_score"))
        adj = int(linux.read_text(f"/proc/{pid}/oom_score_adj"))
    except (OSError, ValueError):
        return "—"
    if adj == -1000:
        return f"Never killed (score {score})"
    return f"Score {score}, adjustment {adj:+d}" if adj else f"Score {score}"


_GROUP_NAMES = {}


def group_name(gid):
    if gid not in _GROUP_NAMES:
        try:
            _GROUP_NAMES[gid] = grp.getgrgid(gid).gr_name
        except (KeyError, OverflowError):
            _GROUP_NAMES[gid] = str(gid)
    return _GROUP_NAMES[gid]


class ProcReader:
    """Reads /proc/<pid> files, falling back to data fetched through the helper.

    What the helper returned is a snapshot: it is kept and returned instead of the (unreadable)
    file until the next elevate() of that name, which needs authentication again. elevated_at
    holds when each one was read, so that pages can say so instead of looking live.
    """

    def __init__(self, pid, starttime):
        self.pid = pid
        self.starttime = starttime
        self.elevated = {}
        self.elevated_at = {}

    def elevated_since(self, names):
        """When the oldest of the elevated snapshots among names was read, or None if none is."""
        times = [self.elevated_at[n] for n in names if n in self.elevated_at]
        return min(times) if times else None

    def is_current(self):
        return read_starttime(self.pid) == self.starttime

    def collect(self, fetch, *args):
        """Discard a batch of reads if the process exited or was replaced during it."""
        require_process(self.pid, self.starttime)
        result = fetch(*args)
        require_process(self.pid, self.starttime)
        return result

    def read(self, name):
        if name in self.elevated:
            return self.elevated[name]
        with open(f"/proc/{self.pid}/{name}", "rb") as fh:
            return fh.read()

    def text(self, name):
        return self.read(name).decode("utf-8", "replace")

    def fds(self):
        if "fds" in self.elevated:
            return self.elevated["fds"]
        result = []
        for fd, target in linux.list_fds(self.pid):
            try:  # e.g. a dma-buf's name is set by the process and need not be UTF-8
                info = linux.read_text(f"/proc/{self.pid}/fdinfo/{fd}")
            except OSError:
                info = ""
            result.append((fd, target, info))
        return result

    def elevate(self, names, callback):
        pending = list(names)
        errors = []
        targets = [(self.pid, self.starttime)]

        def next_one():
            if not pending:
                callback(errors[0] if errors else None)
                return
            name = pending.pop(0)

            def store(result):
                # paths in the helper's JSON keep undecodable bytes as surrogates (clean_text)
                if name == "fds":
                    self.elevated["fds"] = [(fd, linux.clean_text(target), linux.clean_text(info))
                                            for fd, target, info in result]
                elif name == "threads":
                    self.elevated["threads"] = [{k: linux.clean_text(v) for k, v in t.items()} for t in result]
                elif isinstance(result, str):  # exe / cwd / root symlink targets
                    self.elevated[name] = linux.clean_text(result)
                else:
                    self.elevated[name] = privileged.decode_file(result)
                self.elevated_at[name] = time.time()

            def done(result, error):
                if error:
                    errors.append(error)
                    pending.clear()
                else:
                    store(result)
                next_one()

            privileged.run_for_processes("read", targets, [name], done)

        next_one()


# ---------------------------------------------------------------------------


class LazyPage(Gtk.Stack):
    """A page that loads data in a worker thread and supports elevation.

    It loads when it is opened, every time, and while it is shown again every `refresh_ticks`
    updates of the window: what a process has open or mapped changes while one looks at it.
    Once its files were read as administrator it shows that snapshot, says when it was taken
    and offers to read it again, but does not refresh by itself (that would ask for the password).
    """

    files = ()  # files needing ptrace access (for elevation)
    refresh_ticks = 0  # 0: only when the page is opened

    def __init__(self, win):
        super().__init__(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.win = win
        self.reader = win.reader
        self.loaded = False
        self._loading = False
        self._reload = False  # load again when the running load is done: elevated data arrived
        self._ticks = 0
        size = dict(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, width_request=32, height_request=32)
        if hasattr(Adw, "Spinner"):  # libadwaita >= 1.6
            spinner = Adw.Spinner(**size)
        else:
            spinner = Gtk.Spinner(spinning=True, **size)
        self.add_named(spinner, "loading")
        denied = Adw.StatusPage(icon_name="system-lock-screen-symbolic", title="Access Denied",
                                description="This information belongs to another user. "
                                            "Reading it requires administrator rights.")
        btn = Gtk.Button(label="Read as Administrator", halign=Gtk.Align.CENTER)
        btn.add_css_class("pill")
        btn.add_css_class("suggested-action")
        btn.connect("clicked", self._elevate)
        denied.set_child(btn)
        self.add_named(denied, "denied")
        self.error_page = Adw.StatusPage(icon_name="dialog-error-symbolic", title="Unavailable")
        self.add_named(self.error_page, "error")
        self.content = self.build()
        self.content.set_vexpand(True)
        self.snapshot_banner = Adw.Banner()
        self.snapshot_banner.connect("button-clicked", self._elevate)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.snapshot_banner)
        box.append(self.content)
        self.add_named(box, "content")
        self.set_visible_child_name("loading")

    def build(self):
        raise NotImplementedError

    def fetch(self):
        raise NotImplementedError

    def populate(self, data):
        raise NotImplementedError

    def badge(self):
        """Short text shown next to the page in the sidebar (e.g. a count), or None."""
        return None

    def snapshot_time(self):
        """When the elevated data this page shows was read, or None if it reads /proc itself."""
        return self.reader.elevated_since(self.files)

    def sync_snapshot(self):
        ts = self.snapshot_time()
        if ts is not None:
            self.snapshot_banner.set_title(f"Read as administrator at {when_text(ts)}. "
                                           "Not updated automatically.")
            # nothing to read again once the process is gone
            self.snapshot_banner.set_button_label(None if self.win.exited else "Refresh")
        self.snapshot_banner.set_revealed(ts is not None)

    def load(self, force=False):
        """force: also when a load is running, after it (what it reads is out of date)."""
        if self._loading:
            self._reload = self._reload or force
            return
        if self.win.exited:
            # nothing to read any more, and the pid may belong to another process by now
            if not self.loaded:
                self.loaded = True
                self.error_page.set_description("The process has exited.")
                self.set_visible_child_name("error")
            return
        self._loading = True
        self._ticks = 0
        run_async(self.reader.collect, self._on_fetched, self.fetch)

    def _on_fetched(self, data, error):
        if is_destroyed(self):
            return  # the window was closed while the worker was running
        self._loading = False
        if self._reload:
            self._reload = False
            self.load()
            return
        shown = self.loaded and self.get_visible_child_name() == "content"
        self.loaded = True
        if shown and (error is not None or self.win.exited):
            return  # a refresh that failed (the process is exiting): keep what was read last
        if isinstance(error, PermissionError):
            self.set_visible_child_name("denied")
            return
        if error is not None:
            self.error_page.set_description(GLib.markup_escape_text(str(error)))
            self.set_visible_child_name("error")
            return
        self.populate(data)
        badge = self.badge()
        if badge is not None:
            self.win.set_badge(self, badge)
        self.sync_snapshot()
        self.set_visible_child_name("content")

    def _elevate(self, *_):
        if self.win.exited:
            return

        def done(error):
            if is_destroyed(self):
                return
            if error:
                self.win.toast(f"Failed: {error}")
            else:
                self.load(force=True)

        self.reader.elevate(self.files, done)

    def flash(self):
        """Arguments for Table.set_rows() that mark what a refresh added or removed."""
        return {"highlight": self.win.main.config.get("highlight_seconds") or None, "now": time.monotonic()}

    def tick(self, rec):
        """Called on every update of the window while the page is the visible one."""
        if not self.refresh_ticks or not self.get_mapped():
            return
        if self.snapshot_time() is not None:
            return  # reloading would only show the same snapshot again, as if it were live
        self._ticks += 1
        if self._ticks >= self.refresh_ticks:
            self.load()


# ---------------------------------------------------------------------------
# key / value rows


class ValueRow(Adw.ActionRow):
    """One-line row: title on the left, a (selectable) value on the right. For short values."""

    def __init__(self, title, mono=False):
        super().__init__(title=title, css_classes=["compact"])
        # natural width = the unwrapped text, so short values never wrap; long ones wrap only
        # when the row is too narrow
        self.value = Gtk.Label(xalign=1, justify=Gtk.Justification.RIGHT, wrap=True,
                               wrap_mode=Pango.WrapMode.WORD_CHAR, natural_wrap_mode=Gtk.NaturalWrapMode.NONE,
                               selectable=True, focusable=False, max_width_chars=40,
                               css_classes=["numeric", "value"])
        if mono:
            self.value.add_css_class("monospace-text")
        self.add_suffix(self.value)

    def set_value(self, value):
        text = "—" if value in (None, "") else str(value)
        if self.value.get_text() != text:
            self.value.set_text(text)
        if text == NA:
            self.value.add_css_class("na")
        else:
            self.value.remove_css_class("na")


class BarRow(ValueRow):
    """Value row with a level bar in front of the value (usage against a limit)."""

    def __init__(self, title, warn=0.8):
        super().__init__(title)
        self.bar = Gtk.LevelBar(min_value=0, max_value=1, valign=Gtk.Align.CENTER, width_request=90,
                                visible=False)
        for offset in ("low", "high", "full"):
            self.bar.remove_offset_value(offset)
        self.bar.add_offset_value("disk-ok", warn)
        self.bar.add_offset_value("disk-full", 1.0)
        self.add_suffix(self.bar)
        # the value label must stay last
        self.remove(self.value)
        self.add_suffix(self.value)

    def set_fraction(self, fraction):
        self.bar.set_visible(fraction is not None)
        if fraction is not None:
            self.bar.set_value(max(0.0, min(1.0, fraction)))


class WideRow(Adw.ActionRow):
    """Two-line "property" row (small title above the value). For long values like paths."""

    def __init__(self, title, mono=False):
        super().__init__(title=title, subtitle_selectable=True)
        self.add_css_class("property")
        if mono:
            self.add_css_class("mono")

    def set_value(self, value):
        text = "—" if value in (None, "") else str(value)
        markup = GLib.markup_escape_text(text)
        if self.get_subtitle() != markup:
            self.set_subtitle(markup)
        if text == NA:
            self.add_css_class("na")
        else:
            self.remove_css_class("na")


class ChipsRow(Gtk.ListBoxRow):
    """A title above a wrapping set of small "chips" (capabilities, namespaces, groups)."""

    def __init__(self, title, mono=False):
        super().__init__(activatable=False, selectable=False, css_classes=["chips-row"])
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(Gtk.Label(label=title, xalign=0, css_classes=["chips-title"]))
        self.wrap = WrapBox(child_spacing=4, line_spacing=4)
        box.append(self.wrap)
        self.set_child(box)
        self.mono = mono
        self._items = None

    def set_value(self, items, empty="None"):
        if isinstance(items, str):
            items = [items] if items else []
        items = list(items)
        if items == self._items:
            return
        self._items = items
        while (child := self.wrap.get_first_child()) is not None:
            self.wrap.remove(child)
        if not items:
            self.wrap.append(Gtk.Label(label=empty, css_classes=["dim"]))
        for text in items:
            chip = Gtk.Label(label=text, css_classes=["chip"])
            if text == NA:
                chip.add_css_class("na")
            if self.mono:
                chip.add_css_class("monospace-text")
            self.wrap.append(chip)


def _flat_button(icon, tooltip, handler):
    btn = Gtk.Button(icon_name=icon, tooltip_text=tooltip, valign=Gtk.Align.CENTER, css_classes=["flat"])
    btn.connect("clicked", lambda *_: handler())
    return btn


class InfoPage(Gtk.ScrolledWindow):
    """Scrollable page of groups; `columns()` gives a two-column area that stacks on narrow windows."""

    def __init__(self, win):
        super().__init__(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.win = win
        self.rows = {}
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=18,
                            margin_bottom=24, margin_start=18, margin_end=18)
        self.set_child(Adw.Clamp(maximum_size=1100, tightening_threshold=900, child=self.body))

    def columns(self):
        box = Gtk.Box(spacing=18, homogeneous=True)
        cols = []
        for _ in range(2):
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, valign=Gtk.Align.START)
            box.append(col)
            cols.append(col)
        self.body.append(box)
        self.win.responsive(box)
        return cols

    def group(self, title, parent=None):
        group = Adw.PreferencesGroup(title=title)
        (parent or self.body).append(group)
        return group

    def add(self, group, key, row):
        self.rows[key] = row
        group.add(row)
        return row

    def value(self, group, key, title, mono=False):
        return self.add(group, key, ValueRow(title, mono))

    def wide(self, group, key, title, mono=False):
        return self.add(group, key, WideRow(title, mono))

    def chips(self, group, key, title, mono=False):
        return self.add(group, key, ChipsRow(title, mono))

    def set(self, key, value):
        self.rows[key].set_value(value)


# ---------------------------------------------------------------------------


def _axis_pct(v):
    """Scale label of the CPU tile: "10%", "2.5%"."""
    return f"{v:g}%"


class StatTile(Gtk.Box):
    """Small live tile: caption, big value, detail line and an optional sparkline."""

    def __init__(self, title, spark=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2, css_classes=["stat-tile"])
        self.append(Gtk.Label(label=title, xalign=0, css_classes=["caption", "dim"]))
        self.value = Gtk.Label(xalign=0, css_classes=["stat-value", "numeric"], ellipsize=Pango.EllipsizeMode.END)
        self.detail = Gtk.Label(xalign=0, css_classes=["caption", "dim", "numeric"],
                                ellipsize=Pango.EllipsizeMode.END)
        self.append(self.value)
        self.append(self.detail)
        self.spark = spark
        if spark is not None:
            spark.set_content_height(44)
            spark.set_margin_top(6)
            spark.set_valign(Gtk.Align.END)
            spark.set_vexpand(True)
            self.append(spark)

    def update(self, value, detail=""):
        self.value.set_text(value)
        self.detail.set_text(detail)
        if self.spark is not None:
            self.spark.queue_draw()


class Badge(Gtk.Box):
    """Rounded pill with an optional status dot."""

    def __init__(self, dot=False, tooltip=None):
        super().__init__(spacing=6, css_classes=["badge-pill"], tooltip_text=tooltip)
        self.dot = None
        if dot:
            self.dot = Gtk.Box(css_classes=["status-dot"], valign=Gtk.Align.CENTER)
            self.append(self.dot)
        self.label = Gtk.Label()
        self.append(self.label)

    def update(self, text, state=None):
        self.set_visible(bool(text))
        self.label.set_text(text or "")
        if self.dot is not None:
            self.dot.set_css_classes(["status-dot", f"status-{state}"] if state else ["status-dot"])


def gpu_summary(win, rec):
    """(value, detail) of a process's GPU use for the overview tile."""
    state = win.gpu_state(rec)
    if state != "ok":
        return "—", state
    pct, mem = rec.get("gpu"), rec.get("gpu_mem")
    return f"{pct or 0:.1f}%", f"memory {fmt_bytes(mem or 0)}"


class Overview(Gtk.Box):
    """Header card of the General page: identity, state and live key figures."""

    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=16, css_classes=["card", "overview"])
        self.win = win
        item = win.item
        top = Gtk.Box(spacing=16)
        icon = Gtk.Image(pixel_size=56, valign=Gtk.Align.CENTER, css_classes=["icon-dropshadow"])
        if item.icon is not None:
            icon.set_from_gicon(item.icon)
        top.append(icon)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER, hexpand=True)
        self.name = Gtk.Label(label=item.name, xalign=0, css_classes=["title-2"], ellipsize=Pango.EllipsizeMode.END,
                              selectable=True, focusable=False)
        self.cmdline = Gtk.Label(xalign=0, css_classes=["dim", "monospace-text", "caption"],
                                 ellipsize=Pango.EllipsizeMode.MIDDLE, single_line_mode=True)
        badges = Gtk.Box(spacing=6, margin_top=4)
        self.state = Badge(dot=True, tooltip="Process state")
        self.user = Badge(tooltip="User")
        self.kind = Badge(tooltip="Type / container")
        self.uptime = Badge(tooltip="Running time")
        for b in (self.state, self.user, self.kind, self.uptime):
            badges.append(b)
        text.append(self.name)
        text.append(self.cmdline)
        text.append(badges)
        top.append(text)
        self.append(top)

        self.io_hist = deque(maxlen=SPARK_SAMPLES)
        self.cpu = StatTile("CPU", Sparkline(CPU_COLOR, mode="zero", floor=100.0 / NCPU, axis_fmt=_axis_pct,
                                              value_fmt=lambda v: f"{v:.1f}%"))
        self.cpu.spark.set_data(item.cpu_hist, item.times)
        self.mem = StatTile("Working set", Sparkline(MEM_COLOR, mode="range", axis_fmt=fmt_short_rate,
                                                      value_fmt=fmt_bytes, base=1024))
        self.mem.spark.set_data(item.mem_hist, item.times)
        # all I/O (read / write calls), like the I/O graph on the Performance page: not only disks
        self.io = StatTile("I/O", Sparkline(READ_COLOR, mode="zero", floor=64 * 1024,
                                            axis_fmt=lambda v: fmt_short_rate(v) + "/s",
                                            value_fmt=lambda v: fmt_bytes(v) + "/s", base=1024))
        self.io.spark.set_data(self.io_hist, item.times)
        self.gpu = None
        self.gpu_hist = deque(maxlen=SPARK_SAMPLES)
        if win.gpu.available:
            self.gpu = StatTile("GPU", Sparkline(GPU_COLOR, mode="zero", floor=10.0, axis_fmt=_axis_pct,
                                                  value_fmt=lambda v: f"{v:.1f}%"))
            self.gpu.spark.set_data(self.gpu_hist, item.times)
        # all sparkline tiles in one balanced row, wrapping into rows of two on narrow windows
        shown = [t for t in (self.cpu, self.mem, self.io, self.gpu) if t is not None]
        tiles = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, min_children_per_line=2,
                            max_children_per_line=len(shown), column_spacing=8, row_spacing=8,
                            css_classes=["stat-tiles"])
        for t in shown:
            t.set_size_request(140 if self.gpu is not None else 160, -1)
            tiles.append(t)
            t.get_parent().set_focusable(False)
        self.append(tiles)

    def record(self, rec):
        """Called on every tick, shown or not: the I/O sparkline needs every sample."""
        rd = rec.get("io_read")
        self.io_hist.append(rd + (rec.get("io_write") or 0) if rd is not None else None)
        if self.gpu is not None:
            self.gpu_hist.append(rec.get("gpu") or 0)

    def tick(self, rec, exited=False):
        self.cmdline.set_text(rec.get("cmdline") or f"[{rec['name']}]")
        self.cmdline.set_tooltip_text(rec.get("cmdline") or None)
        if exited:
            self.state.update("Exited", "inactive")
        elif rec["state"] in _NOTABLE_STATES:
            self.state.update(linux.STATE_NAMES.get(rec["state"], rec["state"]), state_class(rec["state"]))
        else:
            self.state.update("")  # hidden: see the State row below
        self.user.update(rec.get("user") or "")
        kind = "Kernel thread" if rec.get("kthread") else (rec.get("container") or rec.get("sandboxed") or "")
        self.kind.update(kind)
        self.cpu.update(f"{rec['cpu']:.1f}%", f"kernel {rec['cpu_kernel']:.1f}%")
        self.mem.update(fmt_bytes(rec["rss"]), f"private {fmt_bytes(rec['private'])}")
        rd, wr = rec.get("io_read"), rec.get("io_write")
        if rd is None:
            self.io.update("—", NA)
        else:
            self.io.update(fmt_rate((rd or 0) + (wr or 0)) or "0 B/s",
                           f"R {fmt_short_rate(rd or 0)}  W {fmt_short_rate(wr or 0)}")
        if self.gpu is not None:
            self.gpu.update(*gpu_summary(self.win, rec))
        created = rec["create_time"]
        started = datetime.fromtimestamp(created).strftime("%H:%M:%S")
        if exited:
            self.uptime.update(f"Started {started}")
        else:
            self.uptime.update(f"Up {fmt_span(time.time() - created)} · since {started}")


class GeneralPage(InfoPage):
    def __init__(self, win):
        super().__init__(win)
        item = win.item
        self.overview = Overview(win)
        self.body.append(self.overview)

        g = self.group("File")
        self.elevate_btns = {key: _flat_button("system-lock-screen-symbolic", "Read as Administrator",
                                               lambda k=key: self._elevate(k))
                             for key in ("exe", "cwd")}
        for btn in self.elevate_btns.values():
            btn.set_visible(False)
        exe = self.wide(g, "exe", "Executable", mono=True)
        exe.add_suffix(self.elevate_btns["exe"])
        self.open_btn = _flat_button("folder-open-symbolic", "Open file location",
                                     lambda: pa.open_location(win.main, self._exe()))
        exe.add_suffix(self.open_btn)
        self.copy_btn = _flat_button("edit-copy-symbolic", "Copy path", lambda: self._copy(self._exe()))
        exe.add_suffix(self.copy_btn)
        cmd = self.wide(g, "cmdline", "Command line", mono=True)
        cmd.add_suffix(_flat_button("edit-copy-symbolic", "Copy command line",
                                    lambda: self._copy(self.win.item.rec.get("cmdline"))))
        cwd = self.wide(g, "cwd", "Current directory", mono=True)
        cwd.add_suffix(self.elevate_btns["cwd"])

        left, right = self.columns()
        g = self.group("Process", left)
        self.value(g, "pid", "Process ID")
        self.value(g, "state", "State")
        parent = self.value(g, "parent", "Parent")
        self.parent_btn = _flat_button("go-jump-symbolic", "Show parent in the process list",
                                       lambda: win.main.goto_process(item.rec["ppid"]))
        parent.add_suffix(self.parent_btn)
        self.value(g, "started", "Started")
        self.value(g, "user", "User")
        self.value(g, "tty", "Terminal")
        self.value(g, "session", "Session")

        g = self.group("Scheduling", left)
        self.value(g, "priority", "Nice")
        self.value(g, "kprio", "Kernel priority")
        self.value(g, "ionice", "I/O priority")
        self.value(g, "affinity", "CPU affinity")
        self.value(g, "oom", "OOM killer")

        g = self.group("Control Group", right)
        self.wide(g, "unit", "systemd unit", mono=True)
        self.value(g, "unit_scope", "Manager")
        self.wide(g, "app_id", "Application")
        self.value(g, "container", "Container")
        self.wide(g, "cgroup", "Control group", mono=True)

        g = self.group("Security", right)
        self.value(g, "nnp", "No new privileges")
        self.value(g, "seccomp", "Seccomp")
        self.value(g, "lsm", "Security label")

        g = self.group("Privileges &amp; Isolation")
        self.chips(g, "groups", "Supplementary groups")
        self.chips(g, "caps", "Effective capabilities", mono=True)
        self.chips(g, "capbnd", "Bounding set", mono=True)
        self.chips(g, "ns", "Namespaces", mono=True)
        self.refresh()

    def _exe(self):
        return self.win.item.rec.get("exe") or self.win.reader.elevated.get("exe")

    def _copy(self, text):
        if text:
            pa.copy_text(text)
            self.win.toast("Copied to clipboard")

    def _elevate(self, key):
        if self.win.exited:
            return

        def done(error):
            if is_destroyed(self):
                return
            if error:
                self.win.toast(f"Failed: {error}")
            self.refresh()

        self.win.reader.elevate([key], done)

    def _set_denied(self, key, value):
        """Show value, or "Access denied" with an inline elevation button."""
        self.elevate_btns[key].set_visible(not value and not self.win.exited)
        self.set(key, value or NA)

    def tick(self, rec, exited=False):
        """Cheap, frequently changing values."""
        self.overview.tick(rec, exited)
        self.set("state", "Exited" if exited else state_text(rec))
        self.set("started", started_text(rec["create_time"]))

    def refresh(self):
        self.tick(self.win._with_thread_state(self.win.item.rec), exited=self.win.exited)
        self.refresh_details()

    def refresh_details(self):
        """What tick() leaves out: values that change rarely (priority, affinity, directory,
        user, ...), read here. Called now and then while the page is shown."""
        item = self.win.item
        rec = item.rec
        pid = item.pid
        reader = self.win.reader
        live = not self.win.exited and reader.is_current()
        if rec.get("kthread"):
            self.set("exe", "Kernel thread")
            self.open_btn.set_visible(False)
            self.copy_btn.set_visible(False)
        else:
            exe = self._exe()
            self._set_denied("exe", exe)
            self.open_btn.set_visible(bool(exe))
            self.copy_btn.set_visible(bool(exe))
        self.set("cmdline", rec.get("cmdline"))
        cwd = reader.elevated.get("cwd")
        cwd_at = reader.elevated_at.get("cwd")
        if live:
            try:
                cwd = linux.readlink_text(f"/proc/{pid}/cwd")
                cwd_at = None
            except OSError:
                pass
        self._set_denied("cwd", cwd)
        # the directory can change; an elevated read of it does not follow: say when it was read
        # and keep the button to read it again
        snapshot = bool(cwd and cwd_at)
        btn = self.elevate_btns["cwd"]
        self.rows["cwd"].set_tooltip_text(f"Read as administrator, {snapshot_note(cwd_at)}" if snapshot else None)
        btn.set_tooltip_text("Read Again as Administrator" if snapshot else "Read as Administrator")
        if snapshot and live:
            btn.set_visible(True)
        parent = self.win.main.tree.get(rec["ppid"])
        self.parent_btn.set_visible(parent is not None)
        self.set("parent", f"{parent.name} ({rec['ppid']})" if parent else
                 (f"Non-existent ({rec['ppid']})" if rec["ppid"] else "None"))
        self.set("pid", pid)
        try:
            status = linux.parse_status(reader.text("status")) if live else {}
        except OSError:
            status = {}
        uids = status.get("Uid", "").split()
        gids = status.get("Gid", "").split()
        if uids:
            u = f"{username(int(uids[0]))} ({uids[0]}"
            if len(uids) > 1 and uids[1] != uids[0]:
                u += f", effective {username(int(uids[1]))} {uids[1]}"
            if gids:
                u += f", group {group_name(int(gids[0]))}"
            self.set("user", u + ")")
        groups = [group_name(int(g)) for g in (status.get("Groups") or "").split() if g.isdigit()]
        self.set("groups", groups)
        self.set("priority", rec["nice"])
        self.set("kprio", rec["priority"])
        ionice = NA
        if live:
            try:
                io = psutil.Process(pid).ionice()
                klass = {0: "None (from nice)", 1: "Realtime", 2: "Best effort", 3: "Idle"}.get(int(io.ioclass),
                                                                                             io.ioclass)
                ionice = f"{klass}, level {io.value}"
            except (psutil.Error, OSError):
                pass
        self.set("ionice", ionice)
        aff = pa.get_affinity(pid) if live else None
        self.set("affinity", "All CPUs" if aff and len(aff) == NCPU else
                 (", ".join(str(c) for c in sorted(aff)) if aff else "—"))
        self.set("oom", oom_text(pid) if live and not rec.get("kthread") else "—")
        self.set("tty", rec.get("tty") or "None")
        self.set("session", rec.get("session"))
        self.set("unit", rec.get("unit"))
        self.set("unit_scope", {"system": "System", "user": "User session"}.get(rec.get("unit_scope"),
                                                                              rec.get("unit_scope")))
        self.set("app_id", rec.get("app_id") + (f" via {rec['launcher']}" if rec.get("launcher") else "")
                 if rec.get("app_id") else None)
        self.set("container", rec.get("container") or rec.get("sandboxed") or "None")
        self.set("cgroup", rec.get("cgroup"))
        if status:
            caps = linux.decode_caps(status.get("CapEff"))
            self.set("caps", caps)
            bnd = linux.decode_caps(status.get("CapBnd"))
            self.set("capbnd", ["All capabilities"] if len(bnd) >= len(linux.CAP_NAMES) else bnd)
            self.set("nnp", "Yes" if status.get("NoNewPrivs") == "1" else "No")
        else:
            for key in ("caps", "capbnd"):
                self.set(key, [NA])
            self.set("nnp", NA)
        self.set("seccomp", linux.SECCOMP_MODES.get(status.get("Seccomp"), status.get("Seccomp")))
        try:
            label = reader.text("attr/current").strip("\0\n ") if live else ""
        except OSError:
            label = ""
        self.set("lsm", label or "Unconfined")
        links = {}
        try:
            for name in sorted(os.listdir(f"/proc/{pid}/ns")) if live else ():
                try:
                    links[name] = os.readlink(f"/proc/{pid}/ns/{name}").partition(":")[2].strip("[]")
                except OSError:
                    pass
        except OSError:
            pass
        # pid_for_children / time_for_children normally equal the process's own namespace
        ns = [f"{name} {ident}" for name, ident in links.items()
              if not (name.endswith("_for_children") and links.get(name.removesuffix("_for_children")) == ident)]
        self.set("ns", ns or [NA])


class StatisticsPage(InfoPage):
    def __init__(self, win):
        super().__init__(win)
        left, right = self.columns()
        g = self.group("CPU", left)
        for key, title in (("cpu", "Usage"), ("cpu_kernel", "Kernel usage"), ("cpu_user", "User time"),
                           ("cpu_system", "Kernel time"), ("cpu_time", "Total time"),
                           ("ctx_vol", "Voluntary context switches"),
                           ("ctx_invol", "Involuntary context switches"), ("cpu_last", "Last CPU")):
            self.value(g, key, title)
        g = self.group("I/O", left)
        for key, title in (("io_read", "Read rate"), ("io_write", "Write rate"),
                           ("rchar", "Read (all I/O)"), ("wchar", "Written (all I/O)"),
                           ("read_bytes", "Read from storage"), ("write_bytes", "Written to storage"),
                           ("syscr", "Read operations"), ("syscw", "Write operations")):
            self.value(g, key, title)

        g = self.group("Memory", right)
        self.rss_row = self.add(g, "rss", BarRow("Working set (RSS)", warn=0.5))
        self.rss_row.set_tooltip_text("Bar: share of the installed memory")
        for key, title in (("peak", "Peak working set"), ("private", "Private (anonymous)"),
                           ("shared", "Shared"), ("pss", "Proportional set (PSS)"), ("uss", "Unique set (USS)"),
                           ("vms", "Virtual size"), ("vmpeak", "Peak virtual size"), ("swap", "Swapped"),
                           ("minflt", "Minor page faults"), ("majflt", "Major page faults")):
            self.value(g, key, title)
        g = self.group("Resources", right)
        self.value(g, "threads", "Threads")
        self.fds_row = self.add(g, "fds", BarRow("Open files"))
        self.fds_row.set_tooltip_text("Bar: share of the open file limit")
        self.value(g, "fdlimit", "Open file limit")
        self._fd_limit = None
        self.gpu_devices = win.gpu.devices
        if self.gpu_devices:
            g = self.group("GPU", right)
            self.value(g, "gpu", "Usage").set_tooltip_text("The busiest GPU engine the process uses")
            self.value(g, "gpu_mem", "GPU memory")
            multi = len(self.gpu_devices) > 1
            for dev in self.gpu_devices:
                prefix = f"GPU {dev.index} " if multi else ""
                self.value(g, f"gpu_eng{dev.index}", prefix + ("engines" if multi else "Engines")).set_tooltip_text(
                    dev.name)
                self.value(g, f"gpu_reg{dev.index}", prefix + ("memory" if multi else "Memory by region"))

    @staticmethod
    def fetch(reader, heavy):
        """Read what the sampler's record does not have. Runs in a worker thread.

        heavy: also smaps_rollup (the kernel walks all page tables of the process for it) and
        the limits; asked for only every few ticks.
        """
        data = {"status": None, "io": None}
        try:
            data["status"] = linux.parse_kv_kb(reader.text("status"))
        except OSError:
            pass
        try:
            data["io"] = linux.parse_kv_kb(reader.text("io"))
        except OSError:
            pass
        if heavy:
            try:
                data["rollup"] = linux.parse_kv_kb(reader.text("smaps_rollup"))
            except OSError:
                data["rollup"] = None
            try:
                data["limits"] = linux.parse_limits(reader.text("limits"))
                data["limits_at"] = reader.elevated_at.get("limits")
            except OSError:
                pass
        return data

    def apply_gpu(self, rec):
        if not self.gpu_devices:
            return
        keys = ["gpu", "gpu_mem"] + [f"gpu_{k}{d.index}" for d in self.gpu_devices for k in ("eng", "reg")]
        state = self.win.gpu_state(rec)
        if state != "ok":
            for key in keys:
                # "Access denied" on every row, like the other unreadable values; anything else once
                self.set(key, state if key == "gpu" or state == NA else "—")
            return
        snap = self.win.main.sampler.snapshot
        detail = {d["key"]: d for d in (snap.get("gpu_detail") or {}).get(rec["pid"], ())}
        self.set("gpu", f"{rec.get('gpu') or 0:.2f}%")
        self.set("gpu_mem", fmt_bytes(rec.get("gpu_mem") or 0))
        for dev in self.gpu_devices:
            d = detail.get(dev.key)
            if d is None:
                self.set(f"gpu_eng{dev.index}", "Not used")
                self.set(f"gpu_reg{dev.index}", "—")
                continue
            engines = sorted(d["engines"].items(), key=lambda kv: -kv[1])
            self.set(f"gpu_eng{dev.index}", ", ".join(f"{engine_label(e)} {pct:.1f}%" for e, pct in engines) or "—")
            regions = sorted(((r, b) for r, b in d["memory"].items() if b), key=lambda kv: -kv[1])
            self.set(f"gpu_reg{dev.index}", ", ".join(f"{region_label(r)} {fmt_bytes(b)}" for r, b in regions) or "—")

    def apply(self, rec, data, live):
        """Show rec plus the result of fetch(); live is the window's fetch_live() result."""
        self.apply_gpu(rec)
        self.set("cpu", f"{rec['cpu']:.2f}%")
        self.set("cpu_kernel", f"{rec['cpu_kernel']:.2f}%")
        self.set("cpu_user", fmt_cpu_time(rec["cpu_user"]))
        self.set("cpu_system", fmt_cpu_time(rec["cpu_system"]))
        self.set("cpu_time", fmt_cpu_time(rec["cpu_time"]))
        self.set("cpu_last", rec["processor"])
        self.set("private", fmt_bytes(rec["private"]))
        self.set("rss", fmt_bytes(rec["rss"]))
        snap = self.win.main.sampler.snapshot
        total = snap.get("mem_total") if snap else None
        self.rss_row.set_fraction(rec["rss"] / total if total else None)
        self.set("shared", fmt_bytes(rec["shared"]))
        self.set("vms", fmt_bytes(rec["vms"]))
        self.set("minflt", fmt_int(rec["minflt"]))
        self.set("majflt", fmt_int(rec["majflt"]))
        self.set("threads", rec["threads"])
        # swap and context switches: the sampler only refreshes these now and then
        status = data["status"]
        if status is not None:
            self.set("peak", fmt_bytes(status.get("VmHWM")))
            self.set("vmpeak", fmt_bytes(status.get("VmPeak")))
            self.set("swap", fmt_bytes(status.get("VmSwap")))
            self.set("ctx_vol", fmt_int(status.get("voluntary_ctxt_switches")))
            self.set("ctx_invol", fmt_int(status.get("nonvoluntary_ctxt_switches")))
        else:
            self.set("swap", fmt_bytes(rec["swap"]))
            self.set("ctx_vol", fmt_int(rec["ctx_vol"]))
            self.set("ctx_invol", fmt_int(rec["ctx_invol"]))
        if "rollup" in data:
            roll = data["rollup"]
            if roll is not None:
                self.set("pss", fmt_bytes(roll.get("Pss")))
                self.set("uss", fmt_bytes(roll.get("Private_Clean", 0) + roll.get("Private_Dirty", 0)))
            else:
                self.set("pss", NA)
                self.set("uss", NA)
        for row in data.get("limits") or ():
            if row[0] == "Max open files":
                note = f", {snapshot_note(data['limits_at'])}" if data.get("limits_at") else ""
                self.set("fdlimit", f"{row[1]} (hard {row[2]}{note})")
                self._fd_limit = int(row[1]) if str(row[1]).isdigit() else None
        if "fds" in live:
            fds = live["fds"]
            if fds is not None:
                fds_at = live.get("fds_at")
                self.set("fds", f"{fmt_int(fds)} ({snapshot_note(fds_at)})" if fds_at else fmt_int(fds))
                self.fds_row.set_fraction(fds / self._fd_limit if self._fd_limit else None)
            else:
                self.set("fds", NA)
                self.fds_row.set_fraction(None)
        io = data["io"]
        if io is not None:
            for key in ("rchar", "wchar", "read_bytes", "write_bytes"):
                self.set(key, fmt_bytes(io.get(key)))
            self.set("syscr", fmt_int(io.get("syscr")))
            self.set("syscw", fmt_int(io.get("syscw")))
            self.set("io_read", fmt_rate(rec.get("io_read")) or "0 B/s")
            self.set("io_write", fmt_rate(rec.get("io_write")) or "0 B/s")
        else:
            for key in ("rchar", "wchar", "read_bytes", "write_bytes", "syscr", "syscw", "io_read", "io_write"):
                self.set(key, NA)


class PerformancePage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=12,
                         margin_start=12, margin_end=12)
        size = 600
        self.times = deque(maxlen=size)
        self.cpu = deque(maxlen=size)
        self.kernel = deque(maxlen=size)
        self.private = deque(maxlen=size)
        self.rss = deque(maxlen=size)
        self.io_r = deque(maxlen=size)
        self.io_w = deque(maxlen=size)
        self.gpu = deque(maxlen=size)
        self.win = win
        self._seed(win)
        pct = lambda v: f"{v:.2f}%"  # noqa: E731
        self.g_cpu = Graph([Series(self.cpu, CPU_COLOR, "Total"), Series(self.kernel, KERNEL_COLOR, "Kernel")],
                           fmt=pct, times=self.times, show_avg=True)
        self.g_mem = Graph([Series(self.rss, WORKING_SET_COLOR, "Working set", fill=False),
                            Series(self.private, MEM_COLOR, "Private bytes")],
                           fmt=fmt_bytes, times=self.times, show_avg=True)
        self.g_io = Graph([Series(self.io_r, READ_COLOR, "Read"), Series(self.io_w, WRITE_COLOR, "Write")],
                          fmt=fmt_rate, times=self.times, show_avg=True)
        graphs = [("CPU", self.g_cpu), ("Memory", self.g_mem), ("I/O", self.g_io)]
        self.g_gpu = None
        if win.gpu.available:
            self.g_gpu = Graph([Series(self.gpu, GPU_COLOR, "Busiest engine")], fmt=pct, times=self.times,
                               show_avg=True)
            graphs.append(("GPU", self.g_gpu))
        self.labels = []
        for title, graph in graphs:
            value = Gtk.Label()
            self.labels.append(value)
            self.append(card(title, graph, value))

    def _seed(self, win):
        """Start with the history the process list already recorded, so the graphs are not empty."""
        # The current record and its timestamp are added by record().
        self.times.extend(list(win.item.times)[:-1])
        self.cpu.extend(list(win.item.cpu_hist)[:-1])
        self.rss.extend(list(win.item.mem_hist)[:-1])

    def record(self, rec, stamp):
        """Called on every tick, shown or not: the graphs need every sample."""
        self.times.append(stamp)
        self.cpu.append(rec["cpu"])
        self.kernel.append(rec["cpu_kernel"])
        self.private.append(rec["private"])
        self.rss.append(rec["rss"])
        self.io_r.append(rec.get("io_read"))
        self.io_w.append(rec.get("io_write"))
        self.gpu.append(rec.get("gpu") or 0)

    def refresh(self, rec):
        self.labels[0].set_text(f"{rec['cpu']:.2f}%  (kernel {rec['cpu_kernel']:.2f}%)")
        self.labels[1].set_text(f"Working set {fmt_bytes(rec['rss'])}  Private {fmt_bytes(rec['private'])}")
        if rec.get("io_read") is None:
            self.labels[2].set_text(NA)
        else:
            self.labels[2].set_text(f"R {fmt_rate(rec['io_read']) or '0 B/s'}  W {fmt_rate(rec['io_write']) or '0 B/s'}")
        if self.g_gpu is not None:
            state = self.win.gpu_state(rec)
            self.labels[3].set_text(f"{rec.get('gpu') or 0:.2f}%  memory {fmt_bytes(rec.get('gpu_mem') or 0)}"
                                    if state == "ok" else state)
        if self.get_mapped():
            for g in (self.g_cpu, self.g_mem, self.g_io, self.g_gpu):
                if g is not None:
                    g.queue_draw()


class ThreadsPage(LazyPage):
    files = ("threads",)

    def build(self):
        self._prev = {}
        self._prev_time = None
        cols = [
            Column("tid", "TID", "tid", numeric=True, width=80),
            Column("name", "Name", "name", width=200),
            Column("cpu", "CPU", "cpu", fmt=fmt_pct, numeric=True, width=60),
            Column("cpu_time", "CPU time", "cpu_time", fmt=fmt_cpu_time, numeric=True, width=100),
            Column("state", "State", "state", width=110, indicator=lambda d: state_class(d["code"])),
            Column("priority", "Priority", "priority", numeric=True, width=70),
            Column("nice", "Nice", "nice", numeric=True, width=50),
            Column("processor", "Last CPU", "processor", numeric=True, width=70),
            Column("started", "Started", "started", fmt=fmt_timestamp, width=140),
            Column("wchan", "Wait channel", "wchan", width=200, expand=True, css=("monospace-text",)),
            Column("ctx", "Context switches", "ctx", fmt=fmt_int, numeric=True, width=110),
        ]
        self.table = Table(cols, key="tid", menu_model=None)
        self.table.remember_sort(self.win.main.config, "threads", "cpu", descending=True)
        self.count = 0
        return self.table

    @staticmethod
    def _parse_thread(tid, stat, wchan, status_text):
        if not stat:
            return None
        if isinstance(stat, str):
            stat = stat.encode("utf-8", "replace")
        lpar, rpar = stat.find(b"("), stat.rfind(b")")
        if lpar < 0 or rpar <= lpar:
            return None
        name = stat[lpar + 1:rpar].decode("utf-8", "replace")
        f = stat[rpar + 2:].split()
        if len(f) < 20:
            return None
        code = f[0].decode("ascii", "replace")
        ctx = None
        if status_text:
            try:
                st = linux.parse_status(status_text)
                ctx = int(st.get("voluntary_ctxt_switches", 0)) + int(st.get("nonvoluntary_ctxt_switches", 0))
            except ValueError:
                pass
        wchan = wchan.strip()
        return {
            "tid": int(tid), "name": name, "state": linux.STATE_NAMES.get(code, code), "code": code,
            "cpu_time": (int(f[11]) + int(f[12])) / CLK_TCK, "priority": int(f[15]), "nice": int(f[16]),
            "started": BOOT_TIME + int(f[19]) / CLK_TCK, "processor": int(f[36]) if len(f) > 36 else None,
            "wchan": "" if wchan == "0" else wchan, "ctx": ctx,
        }

    def fetch(self):
        if "threads" in self.reader.elevated:
            tasks = []
            for entry in self.reader.elevated["threads"]:
                parsed = self._parse_thread(entry.get("tid", 0), entry.get("stat", ""),
                                            entry.get("wchan", ""), entry.get("status", ""))
                if parsed is not None:
                    tasks.append(parsed)
            return tasks
        pid = self.reader.pid
        tasks = []
        for tid in os.listdir(f"/proc/{pid}/task"):
            base = f"/proc/{pid}/task/{tid}"
            try:
                with open(base + "/stat", "rb") as fh:
                    stat = fh.read()
            except OSError:
                continue
            try:
                wchan = linux.read_text(base + "/wchan")
            except OSError:
                wchan = ""
            try:  # the thread name is raw bytes, possibly cut in the middle of a character
                status_text = linux.read_text(base + "/status")
            except OSError:
                status_text = ""
            parsed = self._parse_thread(tid, stat, wchan, status_text)
            if parsed is not None:
                tasks.append(parsed)
        return tasks

    def populate(self, tasks):
        now = time.monotonic()
        elapsed = now - self._prev_time if self._prev_time else None
        for t in tasks:
            prev = self._prev.get(t["tid"])
            t["cpu"] = max(0.0, (t["cpu_time"] - prev) / elapsed / NCPU * 100) if prev is not None and elapsed else 0.0
        self._prev = {t["tid"]: t["cpu_time"] for t in tasks}
        self._prev_time = now
        self.count = len(tasks)
        self.table.set_rows(tasks, **dict(self.flash(), now=now))

    def badge(self):
        return str(self.count)

    def tick(self, rec):
        # an elevated snapshot loaded again would give every thread 0% CPU (no time has passed in it)
        if self.get_mapped() and self.snapshot_time() is None:
            self.load()


class ModulesPage(LazyPage):
    files = ("maps",)
    refresh_ticks = 5

    def build(self):
        cols = [
            Column("name", "Name", "name", width=170),
            Column("base", "Base address", "base", fmt=lambda v: f"0x{v:x}", width=160, numeric=True,
                   css=("monospace-text",)),
            Column("size", "Size", "size", fmt=fmt_bytes, numeric=True, width=90),
            Column("mapped", "Mapped", "mapped", fmt=fmt_bytes, numeric=True, width=90),
            Column("path", "File name", "path", width=260, expand=True, markup=path_markup,
                   ellipsize=Pango.EllipsizeMode.MIDDLE, tooltip=lambda d: d.get("path")),
        ]
        self.table = Table(cols, key="path")
        self.table.remember_sort(self.win.main.config, "modules")
        exe = self.win.item.rec.get("exe")
        self.table.hl_func = lambda d: "main" if exe and d.get("path") == exe else ""
        return self._with_filter(self.table)

    def _with_filter(self, table):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        bar = Gtk.Box(spacing=12, css_classes=["filter-bar"])
        entry = Gtk.SearchEntry(placeholder_text="Filter", hexpand=True)
        entry.connect("search-changed", lambda e: table.set_filter_text(e.get_text()))
        self.count = Gtk.Label(css_classes=["dim", "caption", "numeric"])
        bar.append(entry)
        bar.append(self.count)
        box.append(bar)
        box.append(Gtk.Separator())
        box.append(table)
        return box

    def fetch(self):
        regions = linux.parse_maps(self.reader.text("maps"))
        return linux.modules_from_maps(regions, self.win.item.rec.get("exe"))

    def populate(self, modules):
        self.table.set_rows(modules, **self.flash())
        self.count.set_text(f"{len(modules)} modules")

    def badge(self):
        return str(len(self.table.all_rows()))


class MemoryPage(ModulesPage):
    files = ("smaps",)
    refresh_ticks = 10  # smaps makes the kernel walk the page tables of the process

    def build(self):
        cols = [
            Column("start", "Base address", "start", fmt=lambda v: f"0x{v:x}", width=160, numeric=True,
                   css=("monospace-text",)),
            Column("size", "Size", "size", fmt=fmt_bytes, numeric=True, width=90),
            Column("perms", "Protection", "perms", width=80, css=("monospace-text",)),
            Column("kind", "Type", "kind", width=100),
            Column("Rss", "Resident", "Rss", fmt=fmt_bytes, numeric=True, width=90),
            Column("Pss", "PSS", "Pss", fmt=fmt_bytes, numeric=True, width=90),
            Column("private", "Private", "private", fmt=fmt_bytes, numeric=True, width=90),
            Column("Swap", "Swap", "Swap", fmt=fmt_bytes, numeric=True, width=80),
            Column("path", "Use", "path", width=320, expand=True, markup=path_markup,
                   ellipsize=Pango.EllipsizeMode.MIDDLE, tooltip=lambda d: d.get("path") or None),
        ]
        self.table = Table(cols, key="start")
        self.table.remember_sort(self.win.main.config, "memory")
        return self._with_filter(self.table)

    def fetch(self):
        regions = linux.parse_smaps(self.reader.text("smaps"))
        for r in regions:
            path = r["path"]
            r["private"] = r.get("Private_Clean", 0) + r.get("Private_Dirty", 0)
            if path.startswith("/"):
                r["kind"] = "Image" if "x" in r["perms"] else "Mapped file"
            elif path == "[heap]":
                r["kind"] = "Heap"
            elif path.startswith("[stack"):
                r["kind"] = "Stack"
            elif path.startswith("["):
                r["kind"] = path.strip("[]")
            else:
                r["kind"] = "Private"
        return regions

    def populate(self, regions):
        self.table.set_rows(regions)
        total = sum(r.get("Rss", 0) for r in regions)
        self.count.set_text(f"{len(regions)} regions, {fmt_bytes(total)} resident")

    def badge(self):
        return None


class OpenFilesPage(ModulesPage):
    files = ("fds",)
    refresh_ticks = 2

    def build(self):
        icons = {}

        def icon(d):
            name = _FD_ICONS.get(d["type"], _FD_ICON_DEFAULT)
            if name not in icons:
                icons[name] = Gio.ThemedIcon.new(name)
            return icons[name]

        cols = [
            Column("fd", "FD", "fd", numeric=True, width=60),
            Column("type", "Type", "type", width=130, icon=icon),
            Column("target", "Name", "target", width=320, expand=True, markup=path_markup,
                   ellipsize=Pango.EllipsizeMode.MIDDLE, tooltip=lambda d: d.get("target")),
            Column("pos", "Position", "pos", numeric=True, width=90),
            Column("flags", "Flags", "flags", width=200),
        ]
        menu = Gio.Menu()
        menu.append("Open File Location", "files.open-location")
        menu.append("Copy Path", "files.copy-path")
        menu.append("Copy", "files.copy")
        self.table = Table(cols, key="fd", menu_model=menu)
        self.table.remember_sort(self.win.main.config, "files")
        self.table.connect("activated", lambda _t, d: self._open_location(d))

        group = Gio.SimpleActionGroup()
        self._actions = {}
        for name, handler in (
            ("open-location", lambda: self._open_location(self.table.selected())),
            ("copy-path", lambda: self._copy_path(self.table.selected())),
            ("copy", self.table.copy_selected),
        ):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda *_a, h=handler: h())
            group.add_action(a)
            self._actions[name] = a
        self.insert_action_group("files", group)
        self.table.connect("selection-changed", lambda _t: self._sync_actions())
        self._sync_actions()
        return self._with_filter(self.table)

    @staticmethod
    def _path(d):
        """Filesystem path of an fd row, or None for sockets, pipes, anon inodes and memfds."""
        if d is None or d.get("type") not in ("File", "Device"):
            return None
        return d["target"]

    def _sync_actions(self):
        has_path = self._path(self.table.selected()) is not None
        self._actions["open-location"].set_enabled(has_path)
        self._actions["copy-path"].set_enabled(has_path)

    def _open_location(self, d):
        path = self._path(d)
        if path is not None:
            pa.open_location(self.win, path)

    def _copy_path(self, d):
        path = self._path(d)
        if path is not None:
            pa.copy_text(path.removesuffix(" (deleted)"))
            self.win.toast("Copied to clipboard")

    def fetch(self):
        rows = []
        sockets = _socket_table(self.reader.pid)
        for fd, target, info in self.reader.fds():
            parsed = linux.parse_fdinfo(info)
            kind = linux.fd_type(target)
            inode = linux.socket_inode(target)
            if inode is not None and inode in sockets:
                target = describe_socket(sockets[inode])
            rows.append({"fd": fd, "type": kind, "target": target, "pos": parsed.get("pos"),
                         "flags": linux.describe_open_flags(parsed.get("flags"))})
        return rows

    def populate(self, rows):
        self.table.set_rows(rows, **self.flash())
        self.count.set_text(f"{len(rows)} open files")

    def badge(self):
        return None  # kept live by the Statistics page


def _socket_table(pid=None):
    """inode -> socket, from the network namespace of process pid (a container's or a service's
    with PrivateNetwork= has sockets of its own), else from Procular's."""
    result = {}
    bases = ([f"/proc/{pid}/net"] if pid is not None else []) + ["/proc/net"]
    for fname, v6 in PROTO_FILES:
        for base in bases:
            try:
                text = linux.read_text(f"{base}/{fname}")
                break
            except OSError:
                continue
        else:
            continue
        for s in linux.parse_proc_net(text, fname.rstrip("6"), v6):
            result[s["inode"]] = s
    return result


class NetworkPage(ModulesPage):
    files = ("fds",)
    refresh_ticks = 2

    def build(self):
        # Sized to fit the window's default width; the addresses take what is left over, and a long
        # IPv6 address keeps both ends and shows in full on hover.
        cols = [
            state_column(),
            Column("proto", "Protocol", "proto", width=70, fmt=lambda v: (v or "").upper()),
            *traffic_columns(width=95),  # before the wide address columns: visible without scrolling
            Column("laddr", "Local address", "laddr", width=150, expand=True, css=("monospace-text",),
                   ellipsize=Pango.EllipsizeMode.MIDDLE, tooltip=lambda d: d.get("laddr") or None),
            Column("lport", "Local port", "lport", numeric=True, width=80),
            Column("raddr", "Remote address", "raddr", width=150, expand=True, css=("monospace-text",),
                   ellipsize=Pango.EllipsizeMode.MIDDLE, tooltip=lambda d: d.get("raddr") or None),
            Column("rport", "Remote port", "rport", numeric=True, width=95),
        ]
        self._traffic = TrafficRates()
        self.connect("unmap", lambda *_: self._traffic.reset())  # only refreshed while shown
        menu = Gio.Menu()
        menu.append_submenu("Copy", copy_submenu("sockets", COPY_FIELDS))
        self.table = Table(cols, key="inode", menu_model=menu)
        self.table.remember_sort(self.win.main.config, "sockets")
        group = Gio.SimpleActionGroup()
        add_copy_actions(group, self.table, COPY_FIELDS)
        self.insert_action_group("sockets", group)
        box = self._with_filter(self.table)
        box.append(Gtk.Separator())
        box.append(state_legend())
        return box

    def fetch(self):
        inodes = {linux.socket_inode(t) for _fd, t, _i in self.reader.fds()}
        # (the transfer rates come from Procular's own network namespace only)
        return [s for inode, s in _socket_table(self.reader.pid).items() if inode in inodes], tcp_traffic()

    def populate(self, data):
        rows, traffic = data
        # only this process's sockets: rates of the others would be computed for nothing
        rates = self._traffic.update({s["inode"]: traffic[s["inode"]] for s in rows if s["inode"] in traffic})
        for row in rows:
            add_traffic(row, traffic, rates)
        self.table.set_rows(rows, **self.flash())
        self.count.set_text(f"{len(rows)} sockets (TCP/UDP){traffic_summary(rows)}")


class EnvironmentPage(ModulesPage):
    files = ("environ",)
    refresh_ticks = 0  # /proc shows the environment the process was started with

    def build(self):
        cols = [
            Column("key", "Variable", "key", width=260, css=("monospace-text", "env-key")),
            Column("value", "Value", "value", width=600, expand=True, css=("monospace-text",),
                   tooltip=lambda d: d.get("value") or None),
        ]
        self.table = Table(cols, key="key")
        self.table.remember_sort(self.win.main.config, "environment", "key")
        return self._with_filter(self.table)

    def fetch(self):
        return [{"key": k, "value": v} for k, v in linux.parse_environ(self.reader.read("environ"))]

    def populate(self, rows):
        self.table.set_rows(rows)
        self.count.set_text(f"{len(rows)} variables")


class LimitsPage(ModulesPage):
    files = ("limits",)
    refresh_ticks = 10

    def build(self):
        cols = [
            Column("name", "Limit", "name", width=260),
            Column("soft", "Soft", "soft", width=140, numeric=True),
            Column("hard", "Hard", "hard", width=140, numeric=True),
            Column("units", "Units", "units", width=120, expand=True),
        ]
        self.table = Table(cols, key="name")
        self.table.remember_sort(self.win.main.config, "limits")
        return self._with_filter(self.table)

    def fetch(self):
        return [dict(zip(("name", "soft", "hard", "units"), r)) for r in linux.parse_limits(self.reader.text("limits"))]

    def populate(self, rows):
        self.table.set_rows(rows)
        self.count.set_text("")

    def badge(self):
        return None


# ---------------------------------------------------------------------------

def fetch_live(reader, pid, stats, heavy):
    """What a properties window shows live that the sampler's record lacks. Runs in a worker thread.

    stats: also the Statistics page's data; heavy: also the less urgent, more expensive parts.
    """
    live = {"threads": linux.thread_states(pid), "heavy": heavy}
    if heavy:
        try:
            if "fds" in reader.elevated:
                live["fds"] = len(reader.elevated["fds"])
                live["fds_at"] = reader.elevated_at.get("fds")
            else:
                live["fds"] = len(os.listdir(f"/proc/{pid}/fd"))
        except OSError:
            live["fds"] = None
    if stats:
        live["stats"] = StatisticsPage.fetch(reader, heavy)
    return live


# sidebar: (section, [(page name, icon)])
SECTIONS = [
    ("Overview", [("general", "dialog-information-symbolic"), ("statistics", "view-list-bullet-symbolic"),
                  ("performance", "power-profile-performance-symbolic")]),
    ("Inspect", [("threads", "view-continuous-symbolic"), ("modules", "application-x-addon-symbolic"),
                 ("memory", "media-flash-symbolic"), ("files", "folder-documents-symbolic"),
                 ("network", "network-transmit-receive-symbolic")]),
    ("Configuration", [("environment", "utilities-terminal-symbolic"), ("limits", "security-medium-symbolic")]),
]


class PropertiesWindow(Adw.Window):
    def __init__(self, main, item):
        super().__init__(title=f"{item.name} ({item.pid}) Properties")
        self.main = main
        self.item = item
        self.reader = ProcReader(item.pid, item.rec["starttime"])
        self.exited = item.terminated or not self.reader.is_current()
        # GPU use per process is otherwise only collected while the process list shows it
        self.gpu = main.sampler.system.gpu
        self._watching = not self.exited
        if self._watching:
            self.gpu.watch(item.pid)
        self.set_default_size(1000, 740)
        self.set_size_request(560, 400)
        self._bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 900sp"))
        self.add_breakpoint(self._bp)
        self.badges = {}

        self.toasts = Adw.ToastOverlay()
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, hexpand=True)
        self.general = GeneralPage(self)
        self.statistics = StatisticsPage(self)
        self.performance = PerformancePage(self)
        self.pages = {
            "general": ("General", self.general),
            "statistics": ("Statistics", self.statistics),
            "performance": ("Performance", self.performance),
            "threads": ("Threads", ThreadsPage(self)),
            "modules": ("Modules", ModulesPage(self)),
            "memory": ("Memory", MemoryPage(self)),
            "files": ("Open Files", OpenFilesPage(self)),
            "network": ("Network", NetworkPage(self)),
            "environment": ("Environment", EnvironmentPage(self)),
            "limits": ("Limits", LimitsPage(self)),
        }
        for name, (title, page) in self.pages.items():
            page.page_name = name
            self.stack.add_titled(page, name, title)
        self.stack.connect("notify::visible-child", self._on_page)

        body = Gtk.Box()
        body.append(self._build_sidebar())
        body.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        body.append(self.stack)
        self.toasts.set_child(body)

        header = Adw.HeaderBar()
        self.window_title = Adw.WindowTitle(title=item.name)
        header.set_title_widget(self.window_title)
        self.term_btn = Gtk.Button(label="Terminate")
        self.term_btn.add_css_class("destructive-action")
        self.term_btn.connect("clicked", lambda *_: pa.confirm_signal(self, [item], signal.SIGTERM, "Terminate"))
        header.pack_end(self.term_btn)

        self.banner = Adw.Banner(title="This process has exited. The values shown are the last ones recorded.")
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.add_top_bar(self.banner)
        toolbar.set_content(self.toasts)
        self.set_content(toolbar)
        self.config = main.config

        self._live_busy = False  # a fetch_live() worker is running
        self._live_again = False  # ... and another full one was asked for meanwhile
        self._live_count = 0
        self._hid = main.sampler.connect("tick", self._on_tick)
        self.connect("close-request", self._on_close)
        destroy_on_close(self)  # without it every closed window stays in memory (see util.lifecycle)
        rec = self._with_thread_state(item.rec)
        self._update_header(rec)
        self.general.overview.record(item.rec)
        self.performance.record(item.rec, item.times[-1])
        self.performance.refresh(item.rec)
        self._update_sidebar(rec, {})
        if self.exited:
            self._mark_exited()
        else:
            self._refresh_live(full=True, stats=True)  # fill the Statistics page once, before it is shown

    def _with_thread_state(self, rec):
        """rec with "state" describing the whole process instead of only its main thread."""
        if self.exited:
            return rec
        try:
            threads = self.reader.collect(linux.thread_states, rec["pid"])
        except ProcessLookupError:
            return rec
        return dict(rec, state=linux.effective_state(rec["state"], threads),
                    threads_running=threads.count("R"))

    # ----- layout helpers used by the pages
    def responsive(self, box):
        """Two side-by-side columns that stack vertically on narrow windows."""
        self._bp.add_setter(box, "orientation", GObject.Value(Gtk.Orientation, Gtk.Orientation.VERTICAL))
        self._bp.add_setter(box, "homogeneous", GObject.Value(bool, False))

    def set_badge(self, page, text):
        name = page if isinstance(page, str) else getattr(page, "page_name", None)
        label = self.badges.get(name)
        if label is not None:
            label.set_text(text or "")
            label.set_visible(bool(text))

    def _build_sidebar(self):
        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.BROWSE, css_classes=["navigation-sidebar"])
        self._rows = {}
        first_of_section = {}
        for section, pages in SECTIONS:
            first_of_section[pages[0][0]] = section
            for name, icon in pages:
                box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER)
                head = Gtk.Box(spacing=10)
                head.append(Gtk.Image(icon_name=icon))
                head.append(Gtk.Label(label=self.pages[name][0], xalign=0, hexpand=True))
                badge = Gtk.Label(css_classes=["dim", "caption", "numeric"], visible=False)
                head.append(badge)
                self.badges[name] = badge
                box.append(head)
                if name == "performance":
                    # taller than the row's minimum height, where nothing would center it: own margins
                    box.set_margin_top(6)
                    box.set_margin_bottom(6)
                    self.side_graph = Graph([Series(self.performance.cpu, CPU_COLOR),
                                             Series(self.performance.kernel, KERNEL_COLOR)],
                                            max_value=100, times=self.performance.times, compact=True,
                                            min_height=SIDE_GRAPH_MIN, step=1)
                    self.side_graph.add_css_class("mini-graph")
                    box.append(self.side_graph)
                row = Gtk.ListBoxRow(child=box)
                row.page_name = name
                listbox.append(row)
                self._rows[name] = row

        def header(row, _before):
            # also runs while the window is being torn down, when the rows have lost their attributes
            section = first_of_section.get(getattr(row, "page_name", None))
            if section and row.get_header() is None:
                row.set_header(Gtk.Label(label=section, xalign=0, css_classes=["sidebar-heading"]))

        listbox.set_header_func(header)
        listbox.connect("row-selected", self._on_side_selected)
        listbox.select_row(self._rows["general"])
        self._listbox = listbox
        scroller = TabScroller(listbox, [self.side_graph], min_height=SIDE_GRAPH_MIN)
        scroller.set_size_request(190, -1)
        return scroller

    def _on_side_selected(self, _listbox, row):
        if not is_destroyed(self) and row is not None and (name := getattr(row, "page_name", None)):
            self.stack.set_visible_child_name(name)

    def _update_sidebar(self, rec, live):
        self.set_badge("performance", f"{rec['cpu']:.1f}%")
        self.set_badge("threads", str(rec["threads"]))
        if live.get("fds") is not None:
            self.set_badge("files", str(live["fds"]))
        self.side_graph.queue_draw()

    def _update_header(self, rec):
        parts = [f"PID {self.item.pid}", rec.get("user") or ""]
        self.window_title.set_subtitle(" · ".join(p for p in parts if p))

    # used by process_actions
    def toast(self, message):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(message), timeout=4))

    def _on_page(self, *_):
        page = self.stack.get_visible_child()
        name = self.stack.get_visible_child_name()
        row = self._rows.get(name)
        if row is not None and not row.is_selected():
            self._listbox.select_row(row)
        if isinstance(page, LazyPage):
            page.load()  # also when it was shown before: what it lists may have changed since
        # pages are only kept current while they are shown: bring the new one up to date
        if getattr(self, "_hid", 0):
            self._refresh_live(full=True)

    def _on_tick(self, sampler):
        rec = sampler.records.get(self.item.pid)
        if rec is None or rec["starttime"] != self.item.rec["starttime"]:
            self._mark_exited()
            return
        self.general.overview.record(rec)
        self.performance.record(rec, self.item.times[-1])
        self._refresh_live()

    def _refresh_live(self, full=False, stats=False):
        """Update the header, the sidebar and the visible page.

        What the sampler's record lacks is read from /proc first, in a worker: some of it is
        slow for big processes (one file per thread, smaps_rollup) and would stall the UI.
        """
        if self.exited:
            return
        if self._live_busy:
            self._live_again = self._live_again or full
            return
        self._live_busy = True
        self._live_count += 1
        stats = stats or self.stack.get_visible_child() is self.statistics
        heavy = full or self._live_count % 3 == 1
        run_async(self.reader.collect, self._on_live, fetch_live, self.reader, self.item.pid, stats, heavy)

    def _on_live(self, live, error):
        if is_destroyed(self):
            return
        self._live_busy = False
        if self.exited:
            return
        if isinstance(error, ProcessLookupError):
            self._mark_exited()
            return
        live = live or {}
        threads = live.get("threads", "")
        base = self.item.rec
        rec = dict(base, state=linux.effective_state(base["state"], threads), threads_running=threads.count("R"))
        self._update_header(rec)
        page = self.stack.get_visible_child()
        if "stats" in live:  # only read while that page is shown
            self.statistics.apply(rec, live["stats"], live)
        if page is self.general:
            self.general.tick(rec)
            if live.get("heavy"):
                self.general.refresh_details()
        elif page is self.performance:
            self.performance.refresh(rec)
        elif isinstance(page, LazyPage):
            page.tick(rec)
        self._update_sidebar(rec, live)
        if self._live_again:
            self._live_again = False
            self._refresh_live(full=True)

    def _mark_exited(self):
        self.exited = True
        self._disconnect()
        rec = self.item.rec
        self.set_title(f"{self.item.name} ({self.item.pid}) Properties — exited")
        self._update_header(rec)
        self.general.tick(rec, exited=True)
        self._update_sidebar(rec, {})
        self.banner.set_revealed(True)
        self.stack.add_css_class("exited")
        self.term_btn.set_sensitive(False)
        for button in self.general.elevate_btns.values():
            button.set_visible(False)
        for _title, page in self.pages.values():
            if isinstance(page, LazyPage):
                page.sync_snapshot()  # no Refresh button any more

    def gpu_state(self, rec):
        """"ok" when the process's GPU figures can be shown, else the text to show instead."""
        if rec.get("kthread"):
            return "No GPU use"
        if rec.get("uid") != MY_UID and not IS_ROOT:
            return NA  # its descriptors are not readable
        snap = self.main.sampler.snapshot
        if snap is None or rec["pid"] not in (snap.get("gpu_detail") or {}):
            return "Measuring…"  # the first sample since the window opened is still to come
        if rec.get("gpu") is None and not rec.get("gpu_mem"):
            return "Not using a GPU"
        return "ok"

    def _disconnect(self):
        if self._watching:
            self._watching = False
            self.gpu.watch(self.item.pid, False)
        if self._hid:
            self.main.sampler.disconnect(self._hid)
            self._hid = 0

    def _on_close(self, *_):
        self._disconnect()
        return False
