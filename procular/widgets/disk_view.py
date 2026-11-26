"""Disk tab: per-process disk I/O and per-device statistics."""

import os
import time

import psutil
from gi.repository import Adw, Gio, Gtk

from ..providers.system import backing_disks
from ..util.format import fmt_bytes, fmt_rate
from ..util.icons import GENERIC_ICON
from ..util.linux import clean_text
from .table import Column, Table

DISK_ICON = Gio.ThemedIcon.new("drive-harddisk-symbolic")
RECENT_SECONDS = 30


def _device_model(name):
    for path in (f"/sys/block/{name}/device/model", f"/sys/block/{name}/device/name"):
        try:
            with open(path) as fh:
                return fh.read().strip()
        except OSError:
            continue
    return ""


class DiskView(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self._active = False
        self._recent = {}  # (pid, starttime) -> last active monotonic time
        self._models = {}
        self._mounts = {}
        self._mounts_time = 0

        proc_cols = [
            Column("name", "Process", "name", width=220, icon=self._icon),
            Column("pid", "PID", "pid", numeric=True, width=70),
            Column("read", "Read rate", "read", fmt=fmt_rate, numeric=True, width=100),
            Column("write", "Write rate", "write", fmt=fmt_rate, numeric=True, width=100),
            Column("total", "Total rate", "total", fmt=fmt_rate, numeric=True, width=100),
            Column("read_bytes", "Total read", "read_bytes", fmt=fmt_bytes, numeric=True, width=100),
            Column("write_bytes", "Total written", "write_bytes", fmt=fmt_bytes, numeric=True, width=110),
            Column("user", "User", "user", width=100),
            Column("cmdline", "Command line", "cmdline", width=300, expand=True),
        ]
        self.procs = Table(proc_cols, key="pid", menu_model=self._proc_menu())
        self.procs.connect("activated", lambda _t, d: self.win.open_properties(d["pid"]))
        self.procs.remember_sort(win.config, "disk_processes", "total", descending=True)

        dev_cols = [
            Column("name", "Device", "name", width=140, icon=lambda _d: DISK_ICON),
            Column("model", "Model", "model", width=220),
            Column("mounts", "Mount points", "mounts", width=220, expand=True),
            Column("read_rate", "Read rate", "read_rate", fmt=fmt_rate, numeric=True, width=100),
            Column("write_rate", "Write rate", "write_rate", fmt=fmt_rate, numeric=True, width=100),
            Column("busy", "Active time", "busy", fmt=lambda v: f"{v:.1f}%" if v is not None else "",
                   numeric=True, width=130, bar=lambda d: (d.get("busy") or 0) / 100),
            Column("read_total", "Total read", "read_total", fmt=fmt_bytes, numeric=True, width=100),
            Column("write_total", "Total written", "write_total", fmt=fmt_bytes, numeric=True, width=110),
        ]
        self.devices = Table(dev_cols, key="name")
        self.devices.remember_sort(win.config, "disk_devices", "name")
        # as tall as its rows (machines have few disks); the process list gets the rest of the page
        self.devices.set_vexpand(False)
        self.devices.view.set_vexpand(False)
        dev_scroller = self.devices.view.get_parent()
        dev_scroller.set_vexpand(False)
        dev_scroller.set_propagate_natural_height(True)
        dev_scroller.set_max_content_height(260)

        empty = Adw.StatusPage(icon_name="drive-harddisk-symbolic", title="No Recent Disk Activity",
                               description=f"Your processes that read or write the disk show up here "
                                           f"for {RECENT_SECONDS} seconds.")
        empty.add_css_class("compact")
        self.procs_stack = Gtk.Stack(vexpand=True, transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.procs_stack.add_named(self.procs, "table")
        self.procs_stack.add_named(empty, "empty")
        self.procs_stack.set_visible_child_name("empty")
        self.append(self._caption("Processes with recent disk activity"))
        self.append(self.procs_stack)
        self.append(Gtk.Separator())
        self.append(self._caption("Block devices"))
        self.append(self.devices)

        group = Gio.SimpleActionGroup()
        for name, handler in (
            ("goto", lambda: self._with(lambda d: self.win.goto_process(d["pid"]))),
            ("properties", lambda: self._with(lambda d: self.win.open_properties(d["pid"]))),
            ("copy", self.procs.copy_selected),
        ):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda *_a, h=handler: h())
            group.add_action(a)
        self.insert_action_group("disk", group)
        win.sampler.connect("tick", self._on_tick)

    def _caption(self, text):
        label = Gtk.Label(label=text, xalign=0, margin_start=8, margin_top=6, margin_bottom=4)
        label.add_css_class("heading")
        return label

    def _proc_menu(self):
        menu = Gio.Menu()
        menu.append("Go to Process", "disk.goto")
        menu.append("Properties", "disk.properties")
        menu.append("Copy", "disk.copy")
        return menu

    def _with(self, func):
        data = self.procs.selected()
        if data:
            func(data)

    def _icon(self, data):
        item = self.win.tree.get(data["pid"])
        return item.icon if item is not None and item.icon is not None else GENERIC_ICON

    def set_active(self, active):
        self._active = active
        if active:
            self._on_tick(self.win.sampler)

    def set_filter_text(self, text):
        self.procs.set_filter_text(text)

    def _mount_map(self):
        now = time.monotonic()
        if now - self._mounts_time > 30:
            self._mounts_time = now
            mounts = {}
            try:
                for part in psutil.disk_partitions(all=False):
                    dev = os.path.basename(os.path.realpath(part.device))
                    for disk in backing_disks(dev):
                        mounts.setdefault(disk, []).append(clean_text(part.mountpoint))
            except OSError:
                pass
            self._mounts = mounts
        return self._mounts

    def _on_tick(self, sampler):
        if not self._active or sampler.snapshot is None:
            return
        now = time.monotonic()
        rows = []
        for item in self.win.tree.items.values():
            rec = item.rec
            key = (rec["pid"], rec["starttime"])
            total = rec.get("disk_total")
            if total:
                self._recent[key] = now
            last = self._recent.get(key)
            if last is None or now - last > RECENT_SECONDS or item.terminated:
                continue
            rows.append({
                "pid": rec["pid"], "name": rec["name"], "read": rec.get("disk_read"),
                "write": rec.get("disk_write"), "total": total, "read_bytes": rec.get("read_bytes"),
                "write_bytes": rec.get("write_bytes"), "user": rec.get("user"), "cmdline": rec.get("cmdline"),
            })
        for key in [k for k, t in self._recent.items() if now - t > RECENT_SECONDS]:
            del self._recent[key]
        self.procs.set_rows(rows, highlight=self.win.config.get("highlight_seconds") or None, now=now)
        # rows that just stopped linger (red) in the store, so look at the store, not at rows
        self.procs_stack.set_visible_child_name("table" if self.procs.store.get_n_items() else "empty")

        mounts = self._mount_map()
        disks = sampler.snapshot["disks"]
        for name in [n for n in self._models if n not in disks]:
            del self._models[name]
        devs = []
        for name, d in disks.items():
            if name not in self._models:
                self._models[name] = _device_model(name)
            row = dict(d)
            row["name"] = name
            row["model"] = self._models[name]
            row["mounts"] = ", ".join(sorted(mounts.get(name, [])))
            devs.append(row)
        self.devices.set_rows(devs)
