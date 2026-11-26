"""System Information window (CPU, memory, I/O, network, GPU history)."""

import platform

from gi.repository import Adw, GLib, Gtk, Pango

from ..providers.netinfo import fmt_bitrate
from ..providers.system import cpu_caches, read_pressure
from ..util.format import (fmt_bit_rate, fmt_bytes, fmt_duration, fmt_int, fmt_rate, fmt_short_bit_rate,
                           fmt_short_rate)
from ..util.lifecycle import destroy_on_close, destroy_tree, is_destroyed
from ..widgets.graph import (BUSY, CACHE, CPU, GPU, KERNEL, MEM, READ, RX, SIGNAL, SWAP, TX, VIDEO, WRITE,
                             Graph, Series)


def _cpu_model():
    try:
        with open("/proc/cpuinfo") as fh:
            for line in fh:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "CPU"


def _cache_size(size):
    """Bytes as a compact power-of-two size: 32768 -> "32 KiB", 1536 KiB -> "1.5 MiB"."""
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:g} {unit}" if size == int(size) else f"{size:.1f} {unit}"
        size /= 1024


def _cache_text(sizes):
    """"256 KiB (8 × 32 KiB)", "16 MiB", or "12 MiB (8 × 1 MiB + 2 × 2 MiB)" on hybrid CPUs."""
    total = _cache_size(sum(sizes))
    if len(sizes) == 1:
        return total
    counts = {}
    for size in sizes:
        counts[size] = counts.get(size, 0) + 1
    return f"{total} ({' + '.join(f'{n} × {_cache_size(size)}' for size, n in counts.items())})"


def _pct(v):
    return f"{v:.1f}%"


PSI_MIN_SCALE = 10  # percent: stalls of a fraction of a percent are noise, not a full-height graph
# the resource's "some" and its "full" share
PSI_SERIES = (
    ("cpu", "CPU", (("cpu_some", CPU, "Some tasks waited"),)),
    ("memory", "Memory", (("memory_some", MEM, "Some tasks waited"), ("memory_full", CACHE, "All tasks waited"))),
    ("io", "I/O", (("io_some", BUSY, "Some tasks waited"), ("io_full", KERNEL, "All tasks waited"))),
)


def _psi_value(snap, resource):
    """"some 1.2% · full 0.3%" for this interval."""
    pressure = snap.get("pressure") or {}
    parts = [f"{label} {pressure[f'{resource}_{kind}']:.1f}%" for kind, label in (("some", "some"), ("full", "all"))
             if pressure.get(f"{resource}_{kind}") is not None]
    return " · ".join(parts) or "—"


def _psi_averages(resource, kind):
    def get(snap, _sampler):
        values = (snap.get("pressure_avg") or {}).get(resource, {}).get(kind)
        if not values:
            return "—"
        return f"{values['avg10']:.1f}% / {values['avg60']:.1f}% / {values['avg300']:.1f}%"
    return get


def _used_total(used, total):
    if used is None or not total:
        return "—"
    return f"{fmt_bytes(used)} / {fmt_bytes(total)} ({100 * used / total:.0f}%)"


def _gpu_power(info):
    if info["power"] is None:
        return "—"
    text = f"{info['power']:.1f} W"
    if info["power_cap"]:
        text += f" / {info['power_cap']:.0f} W"
    if info["power_label"]:  # amdgpu: "PPT"; on APUs that is the whole SoC (CPU + GPU)
        text += f" ({info['power_label']})"
    return text


def _gpu_fan(info):
    if info.get("fan_rpm") is not None:
        return f"{info['fan_rpm']} RPM"
    if info.get("fan_pct") is not None:
        return f"{info['fan_pct']:.0f}%"
    return "—"


_GPU_COLORS = (GPU, READ, RX, MEM)


def _gpu_color(index):
    return _GPU_COLORS[index % len(_GPU_COLORS)]


def _gpu_long(info):
    if info["suspended"]:
        return "Suspended"
    return _pct(info["busy"]) if info["busy"] is not None else "—"


def _gpu_short(info):
    if info["suspended"]:
        return "off"
    return f"{info['busy']:.0f}%" if info["busy"] is not None else "—"


def _used(used, total):
    return _used_total(used, total) if total else (fmt_bytes(used) if used is not None else "—")


class GpuPanel:
    """The widgets of one GPU's page that change on every tick."""

    def __init__(self, dev, page, title):
        self.dev = dev
        self.page = page
        self.title = title
        self.box = None
        self.value = Gtk.Label()
        self.vram_value = Gtk.Label()
        self.vram_graph = self.vram_card = None
        self.gtt_value = self.gtt_graph = None
        self.stats = None

    def update(self, info):
        self.value.set_text(_gpu_long(info))
        if info["suspended"]:
            return
        if self.dev.uses_fdinfo and info["vram_used"] is not None:
            self.vram_card.set_visible(True)
        self.vram_graph.max_value = info["vram_total"]  # None: scaled to the values
        self.vram_value.set_text(_used(info["vram_used"], info["vram_total"]))
        if self.gtt_value is not None:
            self.gtt_graph.max_value = info["gtt_total"]
            self.gtt_value.set_text(_used(info["gtt_used"], info["gtt_total"]))


def _signal(dbm, wifi):
    if dbm is None:
        return "—"
    text = f"{dbm} dBm"
    if wifi["quality"] is not None:
        text += f", {wifi['quality']:.0f}% ({wifi['rating']})"
    return text


def _signal_fmt(v):
    # graph values are signal quality %, i.e. 2 * (dBm + 100) clamped to 0..100
    if 0 < v < 100:
        return f"{v:.0f}% (~{v / 2 - 100:.0f} dBm)"
    return f"{v:.0f}%"


def _wifi_channel(w):
    parts = [w["band"]] if w["band"] else []
    if w["channel"]:
        parts.append(f"channel {w['channel']}")
    if w["width"]:
        parts.append(f"{w['width']} MHz wide")
    if w["freq"] and not w["channel"]:
        parts.append(f"{w['freq']:.0f} MHz")
    return ", ".join(parts)


def _iface_state(i):
    if not i["up"]:
        return "Disabled"
    state = i["operstate"]
    if i["kind"] == "Wi-Fi":
        w = i["wifi"]
        if w and w["connected"]:
            return f"Connected to {w['ssid']}" if w["ssid"] else "Connected"
        return "Not connected"
    if state == "up" or (state == "unknown" and i["carrier"]):
        return "Connected"
    if state == "dormant":
        return "Connecting"
    return "No link" if state in ("down", "lowerlayerdown") else (state or "—").capitalize()


def _link_speed(i):
    if not i["speed"]:
        return None
    speed = f"{i['speed'] / 1000:g} Gbit/s" if i["speed"] >= 1000 else f"{i['speed']} Mbit/s"
    return f"{speed}, {i['duplex']} duplex" if i["duplex"] else speed


def _pair(a, b, fmt=fmt_int):
    if a is None and b is None:
        return None
    return f"{fmt(a)} / {fmt(b)}"


def _capacity(size):
    """Binary size plus the decimal one printed on the box: "1.8 TiB (2.00 TB)"."""
    if not size:
        return None
    for unit, div in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6)):
        if size >= div:
            return f"{fmt_bytes(size)} ({size / div:.2f} {unit})"
    return fmt_bytes(size)


def _sectors(info):
    lo, ph = info["logical_sector"], info["physical_sector"]
    if not lo:
        return None
    return f"{fmt_bytes(lo)}" if not ph or ph == lo else f"{fmt_bytes(lo)} logical, {fmt_bytes(ph)} physical"


def _temps(info):
    temps = info["temps"]
    if not temps:
        return None
    labels = list(temps)
    text = f"{temps[labels[0]]:.0f} °C"
    if len(labels) > 1:
        text += " (" + ", ".join(f"{k} {temps[k]:.0f} °C" for k in labels[1:]) + ")"
    if info["temp_crit"]:
        text += f"\ncritical at {info['temp_crit']:.0f} °C"
    return text


def _latency(ms):
    if ms is None:
        return "—"
    return f"{ms * 1000:.0f} µs" if ms < 1 else f"{ms:.1f} ms" if ms < 100 else f"{ms:.0f} ms"


def _ops(rate):
    return f"{rate:.0f}" if rate >= 10 or rate == 0 else f"{rate:.1f}"


_FS_NAMES = {"crypto_LUKS": "LUKS", "LVM2_member": "LVM PV", "linux_raid_member": "RAID member",
             "swap": "swap", "BitLocker": "BitLocker", "zfs_member": "ZFS member", "bcache": "bcache"}


def _fs_text(row):
    fs = row["fstype"]
    if not fs:
        return "—"
    text = _FS_NAMES.get(fs, fs)
    if fs == "crypto_LUKS" and row["fsversion"]:
        text += row["fsversion"]
    elif fs == "vfat" and row["fsversion"] and row["fsversion"].startswith("FAT"):
        text = row["fsversion"]
    return text


class StatsGrid(Gtk.Grid):
    """Label/value grid refreshed on every tick.

    Every value starts right after its label, however wide the longest value of the column is.
    An entry may be None: that cell stays empty, to keep related values in one row."""

    def __init__(self, entries, columns=2):
        super().__init__(column_spacing=12, row_spacing=4, css_classes=["card", "stats-card"])
        self.entries = []
        for idx, entry in enumerate(entries):
            if entry is None:
                continue
            title, getter = entry
            r, c = divmod(idx, columns)
            t = Gtk.Label(label=title, xalign=0, yalign=0, valign=Gtk.Align.START, css_classes=["dim"])
            # the gap to the next label/value pair is wider than the one inside a pair
            v = Gtk.Label(xalign=0, yalign=0, valign=Gtk.Align.START, css_classes=["numeric"], selectable=False,
                          margin_end=24 if c < columns - 1 else 0)
            self.attach(t, c * 2, r, 1, 1)
            self.attach(v, c * 2 + 1, r, 1, 1)
            self.entries.append((v, getter))

    def update(self, snap, sampler):
        for label, getter in self.entries:
            try:
                label.set_text(getter(snap, sampler) or "—")
            except (KeyError, TypeError, ZeroDivisionError):
                label.set_text("—")


def card(title, graph, value_label, height=None):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    head = Gtk.Box(spacing=6)
    head.append(Gtk.Label(label=title, xalign=0, css_classes=["heading"], hexpand=True))
    value_label.set_xalign(1)
    value_label.add_css_class("numeric")
    head.append(value_label)
    box.append(head)
    if height:
        graph.set_content_height(height)
    graph.set_vexpand(True)
    box.append(graph)
    return box


TAB_GRAPH_MIN = 30
TAB_GRAPH_MAX = 90


class TabScroller(Gtk.ScrolledWindow):
    """Sidebar scroller whose mini graphs share the spare height, between min_height and max_height each.

    A ListBox gives its rows their minimum height and leaves the rest empty, so the graph
    heights are adjusted to the allocated height instead."""

    def __init__(self, listbox, graphs, min_height=TAB_GRAPH_MIN, max_height=TAB_GRAPH_MAX):
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER, child=listbox)
        self.listbox = listbox
        self.tab_graphs = graphs
        self.min_height = min_height
        self.max_height = max_height

    def do_size_allocate(self, width, height, baseline):
        graphs = [gr for gr in self.tab_graphs if gr.get_visible()]
        if graphs:
            needed = self.listbox.measure(Gtk.Orientation.VERTICAL, width)[0]
            current = graphs[0].get_content_height()
            target = max(self.min_height, min(self.max_height, current + (height - needed) // len(graphs)))
            if target != current:
                # queues another layout pass; it settles as soon as the heights fit
                for gr in graphs:
                    gr.set_content_height(target)
        Gtk.ScrolledWindow.do_size_allocate(self, width, height, baseline)


class SystemInfoWindow(Adw.Window):
    def __init__(self, app):
        super().__init__(title="System Information", transient_for=None)
        self.app = app
        self.sampler = app.sampler
        h = self.sampler.history
        self.set_default_size(1000, 700)
        self.graphs = []
        self.updaters = []

        # history entries: (name, pid, value) or, for CPU, (name, pid, value, count) where
        # count > 1 means a group of same-named processes caused the load
        def top(deque_, unit_fmt):
            def func(idx):
                n = len(deque_)
                if idx < n and deque_[n - 1 - idx]:
                    name, pid, value, *rest = deque_[n - 1 - idx]
                    if rest and rest[0] > 1:
                        return f"{name} ×{rest[0]}: {unit_fmt(value)}"
                    return f"{name} ({pid}): {unit_fmt(value)}"
                return ""
            return func

        def top_name(deque_):
            def func(idx):
                n = len(deque_)
                if idx < n and deque_[n - 1 - idx]:
                    name, _pid, _value, *rest = deque_[n - 1 - idx]
                    return f"{name} ×{rest[0]}" if rest and rest[0] > 1 else name
                return ""
            return func

        def g(series, **kw):
            kw.setdefault("show_avg", True)
            graph = Graph(series, times=h.times, **kw)
            self.graphs.append(graph)
            return graph

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, hexpand=True)

        # ---------------- summary
        summary = Gtk.Grid(column_spacing=12, row_spacing=12, margin_top=12, margin_bottom=12,
                           margin_start=12, margin_end=12, column_homogeneous=True, row_homogeneous=True)
        self.sum_cpu = Gtk.Label()
        self.sum_mem = Gtk.Label()
        self.sum_io = Gtk.Label()
        self.sum_net = Gtk.Label()
        # 6-column grid: the GPU (when present) shares the top row instead of taking a row of its own
        gpus = self.sampler.system.gpu.devices
        multi_gpu = len(gpus) > 1
        has_gpu = bool(gpus)
        top_w = 2 if has_gpu else 3
        summary.attach(card("CPU", g([Series(h.cpu_total, CPU, "Total"), Series(h.cpu_kernel, KERNEL, "Kernel")],
                                     max_value=100, fmt=_pct, tooltip=top(h.top_cpu, _pct),
                                     peak_label=top_name(h.top_cpu)), self.sum_cpu),
                       0, 0, top_w, 1)
        self.mem_graph_small = g([Series(h.mem_used, MEM, "Used")], max_value=1, fmt=fmt_bytes,
                                 tooltip=top(h.top_mem, fmt_bytes))
        summary.attach(card("Memory", self.mem_graph_small, self.sum_mem), top_w, 0, top_w, 1)
        if has_gpu:
            self.sum_gpu = Gtk.Label()
            series = [Series(h.gpus[d.key]["busy"], _gpu_color(d.index), f"GPU {d.index}" if multi_gpu
                             else "Utilization") for d in gpus]
            summary.attach(card("GPU", g(series, max_value=100, fmt=_pct, tooltip=top(h.top_gpu, _pct)),
                                self.sum_gpu), 4, 0, 2, 1)
        else:
            self.sum_gpu = None
        summary.attach(card("Disk I/O", g([Series(h.disk_read, READ, "Read"), Series(h.disk_write, WRITE, "Write")],
                                          fmt=fmt_rate, tooltip=top(h.top_io, fmt_rate)), self.sum_io), 0, 1, 3, 1)
        summary.attach(card("Network", g([Series(h.net_rx, RX, "Receive"), Series(h.net_tx, TX, "Send")],
                                         fmt=fmt_bit_rate, scale_unit=8), self.sum_net), 3, 1, 3, 1)
        self.stack.add_titled(summary, "summary", "Summary")

        # ---------------- CPU
        cpu_box = self._page()
        self.cpu_value = Gtk.Label()
        head = Gtk.Box(spacing=12)
        head.append(Gtk.Label(label=_cpu_model(), xalign=0, css_classes=["title-3"], hexpand=True))
        self.per_core = Gtk.CheckButton(label="One graph per CPU")
        self.per_core.connect("toggled", self._toggle_cores)
        head.append(self.per_core)
        cpu_box.append(head)
        self.cpu_stack = Gtk.Stack(vexpand=True)
        self.cpu_stack.add_named(card("Utilization", g(
            [Series(h.cpu_total, CPU, "Total"), Series(h.cpu_kernel, KERNEL, "Kernel")],
            max_value=100, fmt=_pct, tooltip=top(h.top_cpu, _pct),
            peak_label=top_name(h.top_cpu)), self.cpu_value), "total")
        ncores = len(h.cpu_cores)
        max_cols = 2 if ncores <= 4 else 4 if ncores <= 16 else 6 if ncores <= 24 else 8
        cores = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                            min_children_per_line=2, max_children_per_line=max_cols, row_spacing=6, column_spacing=6)
        self.core_graphs = []
        for i, dq in enumerate(h.cpu_cores):
            graph = g([Series(dq, CPU, f"CPU {i}")], max_value=100, fmt=_pct, step=2, min_height=70,
                      show_avg=False)
            graph.overlay_text = f"CPU {i}"
            cores.append(graph)
            self.core_graphs.append((graph, dq))
        cs = Gtk.ScrolledWindow(vexpand=True)
        cs.set_child(cores)
        self.cpu_stack.add_named(cs, "cores")
        self.per_core.set_active(app.config.get("sysinfo_per_core"))
        cpu_box.append(self.cpu_stack)
        self.cpu_stats = StatsGrid([
            ("Utilization", lambda s, _: _pct(s["cpu_total"])),
            ("Kernel time", lambda s, _: _pct(s["cpu_kernel"])),
            ("Speed", lambda s, _: f"{s['freq'] / 1000:.2f} GHz" if s["freq"] else "—"),
            ("Governor", lambda s, _: s["governor"] or "—"),
            ("Logical processors", lambda s, sm: str(sm.ncpu)),
            ("Load average", lambda s, _: " / ".join(f"{x:.2f}" for x in s["load"])),
            ("Up time", lambda s, _: fmt_duration(s["uptime"])),
            ("Processes", lambda _s, sm: fmt_int(len(sm.records))),
            ("Threads", lambda _s, sm: fmt_int(sm.total_threads)),
            ("Context switches/s", lambda s, _: fmt_int(int(s["ctx_rate"]))),
            ("Interrupts/s", lambda s, _: fmt_int(int(s["intr_rate"]))),
        ], columns=2)
        stats_row = Gtk.Box(spacing=12)
        self.cpu_stats.set_hexpand(True)
        stats_row.append(self.cpu_stats)
        caches = cpu_caches()
        if caches:
            cache_grid = StatsGrid([(f"{name} cache", lambda _s, _sm, t=_cache_text(sizes): t)
                                    for name, sizes in caches], columns=1)
            cache_grid.update(None, None)
            stats_row.append(cache_grid)
        cpu_box.append(stats_row)
        self.updaters.append(self.cpu_stats)
        self.stack.add_titled(cpu_box, "cpu", "CPU")

        # ---------------- Memory
        mem_box = self._page()
        self.mem_value = Gtk.Label()
        self.swap_value = Gtk.Label()
        self.mem_graph = g([Series(h.mem_cache, CACHE, "Used + cache", fill=False),
                            Series(h.mem_used, MEM, "Used")], max_value=1, fmt=fmt_bytes,
                           tooltip=top(h.top_mem, fmt_bytes), avg_series=1)
        self.swap_graph = g([Series(h.swap_used, SWAP, "Swap used")], max_value=1, fmt=fmt_bytes)
        mem_box.append(card("Physical memory", self.mem_graph, self.mem_value))
        mem_box.append(card("Swap", self.swap_graph, self.swap_value, height=90))

        def mi(key):
            return lambda s, _: fmt_bytes(s["meminfo"].get(key))

        self.mem_stats = StatsGrid([
            ("Total", lambda s, _: fmt_bytes(s["mem_total"])),
            ("Used", lambda s, _: fmt_bytes(s["mem_used"])),
            ("Available", lambda s, _: fmt_bytes(s["mem_avail"])),
            ("Cache", lambda s, _: fmt_bytes(s["mem_cache"])),
            ("Buffers", mi("Buffers")),
            ("Shared (shmem)", mi("Shmem")),
            ("Kernel slab", mi("Slab")),
            ("Page tables", mi("PageTables")),
            ("Huge pages", lambda s, _: str(s["meminfo"].get("HugePages_Total", 0))),
            ("Dirty", mi("Dirty")),
            ("Writeback", mi("Writeback")),
            None,
            ("Committed", mi("Committed_AS")),
            ("Commit limit", mi("CommitLimit")),
            None,
            ("Swap total", lambda s, _: fmt_bytes(s["swap_total"])),
            ("Swap used", lambda s, _: fmt_bytes(s["swap_used"])),
            ("Swap cached", mi("SwapCached")),
        ], columns=3)
        mem_box.append(self.mem_stats)
        self.updaters.append(self.mem_stats)
        self.stack.add_titled(mem_box, "memory", "Memory")

        # ---------------- Disk
        self.io_box = self._page()
        self.io_value = Gtk.Label()
        self.io_box.append(card("Total disk I/O", g([Series(h.disk_read, READ, "Read"),
                                                     Series(h.disk_write, WRITE, "Write")],
                                                    fmt=fmt_rate, tooltip=top(h.top_io, fmt_rate)), self.io_value,
                                height=120))
        self.disk_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.io_box.append(self.disk_list)
        self.disk_panels = {}  # name -> DiskPanel, sorted by name in disk_list
        io_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=self.io_box)
        self.stack.add_titled(io_scroll, "disk", "Disk")

        # ---------------- Network
        self.net_box = self._page()
        self.net_value = Gtk.Label()
        self.net_box.append(card("Total network", g([Series(h.net_rx, RX, "Receive"), Series(h.net_tx, TX, "Send")],
                                                    fmt=fmt_bit_rate, scale_unit=8), self.net_value, height=120))

        def ni(fn):
            return lambda s, _: fn(s["net_info"])

        self.net_stats = StatsGrid([
            ("Host name", ni(lambda n: n["hostname"])),
            ("Default route via", ni(lambda n: n["default"])),
            ("DNS servers", ni(lambda n: "\n".join(n["dns"]))),
            ("Search domains", ni(lambda n: " ".join(n["search"]))),
        ], columns=2)
        self.net_box.append(self.net_stats)
        self.updaters.append(self.net_stats)
        self.nic_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.net_box.append(self.nic_list)
        self.nic_panels = {}  # name -> NicPanel, kept in display order in nic_list
        net_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=self.net_box)
        self.stack.add_titled(net_scroll, "network", "Network")

        # ---------------- GPUs
        self.gpu_panels = [self._build_gpu_page(dev, multi_gpu, g, top(h.top_gpu, _pct)) for dev in gpus]
        for panel in self.gpu_panels:
            self.stack.add_titled(panel.box, panel.page, panel.title)

        # ---------------- Pressure
        snap = self.sampler.snapshot
        self.has_psi = bool(snap["pressure_avg"] if snap is not None else read_pressure())
        self.stack.add_titled(self._build_pressure_page(g, top), "pressure", "Pressure")

        sidebar = self._build_tabs()
        paned = Gtk.Box()
        paned.append(sidebar)
        paned.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        paned.append(self.stack)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)
        toolbar.set_content(paned)
        self.set_content(toolbar)

        self._hid = self.sampler.connect("tick", self._on_tick)
        self.connect("close-request", self._on_close)
        destroy_on_close(self)
        if self.sampler.snapshot is not None:
            self._on_tick(self.sampler)
        # the details shown here are only collected while this window is open; get them right away
        self.sampler.system.set_detailed(True)
        self.sampler.refresh()

    def _build_pressure_page(self, g, top):
        if not self.has_psi:
            return Adw.StatusPage(
                icon_name="dialog-information-symbolic", title="No Pressure Information",
                description="This kernel does not report pressure stall information (PSI). It was "
                            "built without it or started with psi=0; the kernel option psi=1 turns it on.")
        h = self.sampler.history
        box = self._page()
        box.append(Gtk.Label(
            label="How much of the time tasks had to wait for the processor, for memory or for storage. "
                  "“Some” means at least one task was waiting; “all” means every task that had work was "
                  "waiting at once, so the time was lost. Memory pressure counts the time spent reclaiming "
                  "memory and reading back what was swapped out or dropped from the cache.",
            xalign=0, wrap=True, css_classes=["dim"]))
        tops = {"cpu": top(h.top_cpu, _pct), "memory": top(h.top_mem, fmt_bytes), "io": top(h.top_io, fmt_rate)}
        self.psi_values = {}
        for resource, title, series in PSI_SERIES:
            value = Gtk.Label()
            self.psi_values[resource] = value
            graph = g([Series(h.pressure[key], color, label, fill=(i == 0)) for i, (key, color, label)
                       in enumerate(series)], fmt=_pct, min_scale=PSI_MIN_SCALE, tooltip=tops[resource])
            box.append(card(title, graph, value, height=100))
        box.append(Gtk.Label(label="The kernel's averages over 10 seconds, 1 minute and 5 minutes",
                             xalign=0, css_classes=["heading"]))
        self.psi_stats = StatsGrid([
            ("CPU, some", _psi_averages("cpu", "some")),
            None,
            ("Memory, some", _psi_averages("memory", "some")),
            ("Memory, all", _psi_averages("memory", "full")),
            ("I/O, some", _psi_averages("io", "some")),
            ("I/O, all", _psi_averages("io", "full")),
        ], columns=2)
        box.append(self.psi_stats)
        self.updaters.append(self.psi_stats)
        return Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=box)

    def _build_gpu_page(self, dev, multi, g, top_gpu):
        hist = self.sampler.history.gpus[dev.key]
        i = dev.index
        p = GpuPanel(dev, f"gpu{i}", f"GPU {i}" if multi else "GPU")
        box = p.box = self._page()
        box.append(Gtk.Label(label=dev.name, xalign=0, css_classes=["title-3"]))
        util_series = [Series(hist["busy"], GPU, "Graphics/compute")]
        if dev.has_video or dev.uses_fdinfo:
            util_series.append(Series(hist["video"], VIDEO, "Video codec", fill=False))
        # the top process is machine wide: only meaningful with a single GPU
        util = g(util_series, max_value=100, fmt=_pct, tooltip=None if multi else top_gpu)
        util_card = card("Utilization", util, p.value)
        if dev.uses_fdinfo:
            util_card.set_tooltip_text("Busiest engine of the processes that use this GPU. As a normal user, "
                                       "the processes of other users are not included.")
        box.append(util_card)

        mem_row = Gtk.Box(spacing=12, homogeneous=True)
        p.vram_graph = g([Series(hist["vram"], MEM, "VRAM")], max_value=1, fmt=fmt_bytes)
        p.vram_card = card("Dedicated memory (VRAM)", p.vram_graph, p.vram_value, height=110)
        p.vram_card.set_tooltip_text(
            "GPU buffers in the GPU's own memory, of the processes whose GPU use is visible."
            if dev.uses_fdinfo else
            "Memory reserved for the GPU. On integrated GPUs this is a small "
            "carve-out of system RAM set in the firmware.")
        mem_row.append(p.vram_card)
        # fdinfo GPUs: only shown once a process has buffers in dedicated memory
        p.vram_card.set_visible(not dev.uses_fdinfo)
        if dev.has_gtt:
            p.gtt_value = Gtk.Label()
            p.gtt_graph = g([Series(hist["gtt"], CACHE, "GTT")], max_value=1, fmt=fmt_bytes)
            gtt_card = card("Shared memory (GTT)" if not dev.uses_fdinfo else "Shared memory", p.gtt_graph,
                            p.gtt_value, height=110)
            gtt_card.set_tooltip_text(
                "GPU buffers in system RAM, of the processes whose GPU use is visible. They are already part "
                "of the system memory usage." if dev.uses_fdinfo else
                "System RAM mapped for the GPU. Integrated GPUs keep most of "
                "their data here; it is already part of the system memory usage.")
            mem_row.append(gtt_card)
        box.append(mem_row)

        def gi(fn):
            return lambda s, _: fn(s["gpus"][i])

        def opt(fmt):
            return lambda v: fmt(v) if v is not None else "—"

        static = dev.static
        p.stats = StatsGrid([
            ("Utilization", gi(lambda x: opt(_pct)(x["busy"]))),
            ("Video codec", gi(lambda x: opt(_pct)(x["video"]))),
            ("GPU clock", gi(lambda x: opt(lambda v: f"{v / 1e6:.0f} MHz")(x["sclk"]))),
            ("Memory clock", gi(lambda x: opt(lambda v: f"{v / 1e6:.0f} MHz")(x["mclk"]))),
            ("Temperature", gi(lambda x: ", ".join(f"{k} {v:.0f} °C" for k, v in x["temps"].items()) or "—")),
            ("Power", gi(_gpu_power)),
            ("GPU voltage", gi(lambda x: opt(lambda v: f"{v:.3f} V")(x["voltage"]))),
            ("Fan", gi(_gpu_fan)),
            ("VRAM", gi(lambda x: _used(x["vram_used"], x["vram_total"]))),
            ("Shared memory", gi(lambda x: _used(x["gtt_used"], x["gtt_total"]))),
            ("PCIe link", gi(lambda x: x["link"] or "—")),
            ("Power profile", gi(lambda x: x["perf_level"] or "—")),
            ("Driver", lambda *_: static.get("driver") or "—"),
            ("VBIOS", lambda *_: static.get("vbios") or "—"),
            ("PCI slot", lambda *_: static.get("pci") or "—"),
            ("PCI ID", lambda *_: static.get("pci_id") or static.get("model") or "—"),
        ], columns=2)
        box.append(p.stats)
        self.updaters.append(p.stats)
        return p

    def _build_tabs(self):
        """Sidebar listing the pages, each with its current value and a small live graph."""
        h = self.sampler.history

        def mini(series, max_value=None, min_scale=0):
            return Graph(series, max_value=max_value, times=h.times, compact=True, min_height=TAB_GRAPH_MIN, step=1,
                         min_scale=min_scale)

        self.tab_mem_graph = mini([Series(h.mem_used, MEM)], max_value=1)
        tabs = [
            ("summary", "Summary", None),
            ("cpu", "CPU", mini([Series(h.cpu_total, CPU), Series(h.cpu_kernel, KERNEL)], 100)),
            ("memory", "Memory", self.tab_mem_graph),
            ("disk", "Disk", mini([Series(h.disk_read, READ), Series(h.disk_write, WRITE)])),
            ("network", "Network", mini([Series(h.net_rx, RX), Series(h.net_tx, TX)])),
        ]
        for p in self.gpu_panels:
            tabs.append((p.page, p.title, mini([Series(h.gpus[p.dev.key]["busy"], GPU)], 100)))
        if self.has_psi:
            tabs.append(("pressure", "Pressure", mini([Series(h.pressure["memory_some"], MEM),
                                                        Series(h.pressure["io_some"], BUSY),
                                                        Series(h.pressure["cpu_some"], CPU)],
                                                       min_scale=PSI_MIN_SCALE)))
        else:
            tabs.append(("pressure", "Pressure", None))

        self.tab_values = {}
        self.tab_rows = {}
        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.BROWSE, css_classes=["navigation-sidebar"])
        for name, title, graph in tabs:
            # centered: the graph-less Summary row is shorter than the row's minimum height
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=4, margin_bottom=4,
                          valign=Gtk.Align.CENTER)
            head = Gtk.Box(spacing=6)
            head.append(Gtk.Label(label=title, xalign=0, hexpand=True))
            if graph is not None:
                value = Gtk.Label(xalign=1, css_classes=["dim", "numeric", "caption"])
                head.append(value)
                self.tab_values[name] = value
            box.append(head)
            if graph is not None:
                graph.add_css_class("mini-graph")
                box.append(graph)
                self.graphs.append(graph)
            row = Gtk.ListBoxRow(child=box)
            row.page_name = name
            listbox.append(row)
            self.tab_rows[name] = row

        listbox.connect("row-selected", self._on_tab_selected)
        self.stack.connect("notify::visible-child-name", self._on_page_changed)
        self._on_page_changed(self.stack)

        scroller = TabScroller(listbox, [graph for _name, _title, graph in tabs if graph is not None])
        scroller.set_size_request(200, -1)
        return scroller

    def _on_tab_selected(self, _listbox, row):
        if not is_destroyed(self) and row is not None and (name := getattr(row, "page_name", None)):
            self.stack.set_visible_child_name(name)

    def _on_page_changed(self, stack, *_):
        row = self.tab_rows.get(stack.get_visible_child_name())
        if row is not None and not row.is_selected():
            row.get_parent().select_row(row)

    def _update_tabs(self, snap):
        values = self.tab_values
        rate = fmt_short_rate
        values["cpu"].set_text(f"{snap['cpu_total']:.0f}%")
        values["memory"].set_text(f"{100 * snap['mem_used'] / (snap['mem_total'] or 1):.0f}%")
        values["disk"].set_text(f"R {rate(snap['disk_read'] or 0)}  W {rate(snap['disk_write'] or 0)}")
        bits = fmt_short_bit_rate
        values["network"].set_text(f"↓ {bits(snap['net_rx'] or 0)}  ↑ {bits(snap['net_tx'] or 0)}")
        for p, info in zip(self.gpu_panels, snap["gpus"]):
            values[p.page].set_text(_gpu_short(info))
        pressure = snap.get("pressure")
        if pressure and "pressure" in values:
            # the resource that waited the most
            key = max(("memory_some", "io_some", "cpu_some"), key=lambda k: pressure.get(k) or 0)
            worst = pressure.get(key) or 0
            name = {"memory_some": "Mem", "io_some": "I/O", "cpu_some": "CPU"}[key]
            values["pressure"].set_text(f"{name} {worst:.1f}%" if worst >= 0.05 else "0%")

    def _page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=12,
                      margin_start=12, margin_end=12)
        return box

    def _toggle_cores(self, btn):
        self.cpu_stack.set_visible_child_name("cores" if btn.get_active() else "total")
        self.app.config.set("sysinfo_per_core", btn.get_active())

    def _on_tick(self, sampler):
        snap = sampler.snapshot
        h = sampler.history
        total = snap["mem_total"] or 1
        for graph in (self.mem_graph, self.mem_graph_small, self.tab_mem_graph):
            graph.max_value = total
        self.swap_graph.max_value = snap["swap_total"] or 1
        self.sum_cpu.set_text(_pct(snap["cpu_total"]))
        self.cpu_value.set_text(f"{_pct(snap['cpu_total'])} (kernel {_pct(snap['cpu_kernel'])})")
        for i, (graph, dq) in enumerate(self.core_graphs):
            graph.overlay_text = f"CPU {i}  {dq[-1]:.0f}%" if dq and dq[-1] is not None else f"CPU {i}"
        mem_text = f"{fmt_bytes(snap['mem_used'])} / {fmt_bytes(total)} ({100 * snap['mem_used'] / total:.0f}%)"
        self.sum_mem.set_text(mem_text)
        self.mem_value.set_text(mem_text)
        self.swap_value.set_text(f"{fmt_bytes(snap['swap_used'])} / {fmt_bytes(snap['swap_total'])}")
        io_text = f"R {fmt_rate(snap['disk_read']) or '0 B/s'}  W {fmt_rate(snap['disk_write']) or '0 B/s'}"
        self.sum_io.set_text(io_text)
        self.io_value.set_text(io_text)
        net_text = f"↓ {fmt_bit_rate(snap['net_rx']) or '0 bit/s'}  ↑ {fmt_bit_rate(snap['net_tx']) or '0 bit/s'}"
        self.sum_net.set_text(net_text)
        self.net_value.set_text(net_text)
        infos = snap["gpus"]
        for p, info in zip(self.gpu_panels, infos):
            p.update(info)
        if self.sum_gpu is not None and infos:
            if len(infos) == 1:
                self.sum_gpu.set_text(_gpu_long(infos[0]))
            else:
                self.sum_gpu.set_text("  ".join(f"{i}: {_gpu_short(info)}" for i, info in enumerate(infos)))

        if self.has_psi:
            for resource, value in self.psi_values.items():
                value.set_text(_psi_value(snap, resource))
        self._update_disks(snap, h)
        self._update_nics(snap, h)

        self._update_tabs(snap)
        for upd in self.updaters:
            upd.update(snap, sampler)
        for graph in self.graphs:
            if graph.get_mapped():
                graph.queue_draw()

    def _drop_panels(self, panels, box, history):
        """Remove the panels of devices the history has let go of (see History): their graphs
        show queues that get no more samples, and a device coming back starts new ones."""
        for name in [n for n in panels if n not in history]:
            panel = panels.pop(name)
            self.graphs = [graph for graph in self.graphs if graph not in panel.graphs]
            box.remove(panel)
            destroy_tree(panel)

    def _update_disks(self, snap, h):
        self._drop_panels(self.disk_panels, self.disk_list, h.per_disk)
        infos = snap.get("disk_info") or {}
        for name in sorted(snap["disks"]):
            if name not in self.disk_panels and name in h.per_disk and name in infos:
                panel = DiskPanel(name, infos[name], h)
                self.graphs.extend(panel.graphs)
                before = max((n for n in self.disk_panels if n < name), default=None)
                if before is None:
                    self.disk_list.prepend(panel)
                else:
                    self.disk_list.insert_child_after(panel, self.disk_panels[before])
                self.disk_panels[name] = panel
        for name, panel in self.disk_panels.items():
            present = name in snap["disks"] and name in infos
            panel.set_visible(present)
            if present:
                panel.update(snap, self.sampler)

    def _update_nics(self, snap, h):
        self._drop_panels(self.nic_panels, self.nic_list, h.per_nic)
        net = snap.get("net_info")
        ifaces = net["ifaces"] if net else {}
        for name in snap["nics"]:
            if name not in self.nic_panels and name in h.per_nic and name in ifaces:
                self._add_nic_panel(name, ifaces[name], net["default"], h)
        for name, panel in self.nic_panels.items():
            panel.set_visible(name in ifaces)
            if name in ifaces:
                panel.update(snap, self.sampler)

    def _add_nic_panel(self, name, info, default, h):
        # default route first, then hardware adapters, then virtual interfaces; collapse the latter
        physical = info["adapter"] is not None or info["driver"] is not None
        key = (name != default, not physical, name)
        panel = NicPanel(name, info, h, expanded=physical or name == default)
        self.graphs.extend(panel.graphs)
        before = None
        for other in self.nic_panels.values():
            if other.sort_key < key and (before is None or other.sort_key > before.sort_key):
                before = other
        panel.sort_key = key
        if before is None:
            self.nic_list.prepend(panel)
        else:
            self.nic_list.insert_child_after(panel, before)
        self.nic_panels[name] = panel

    def _on_close(self, *_):
        if self._hid:
            self.sampler.disconnect(self._hid)
            self._hid = 0
        self.sampler.system.set_detailed(False)
        return False



class NicPanel(Gtk.Expander):
    """One network interface: throughput (and Wi-Fi signal) history plus a details grid."""

    def __init__(self, name, info, history, expanded=True):
        super().__init__(expanded=expanded)
        self.name = name
        self.graphs = []
        self.wifi = info["kind"] == "Wi-Fi"

        head = Gtk.Box(spacing=8)
        head.append(Gtk.Label(label=name, css_classes=["heading"]))
        self.subtitle = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END, css_classes=["dim"])
        head.append(self.subtitle)
        self.state = Gtk.Label(xalign=1, css_classes=["dim", "numeric"])
        head.append(self.state)
        self.set_label_widget(head)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=6, margin_bottom=12,
                       margin_start=18)
        row = Gtk.Box(spacing=12, homogeneous=True)
        rx, tx = history.per_nic[name]
        graph = Graph([Series(rx, RX, "Receive"), Series(tx, TX, "Send")], fmt=fmt_bit_rate, scale_unit=8,
                      times=history.times, min_height=100)
        self.graphs.append(graph)
        self.rate_value = Gtk.Label()
        row.append(card("Throughput", graph, self.rate_value))
        self.signal_value = None
        if self.wifi and name in history.wifi_signal:
            sig = Graph([Series(history.wifi_signal[name], SIGNAL, "Signal")], max_value=100, fmt=_signal_fmt,
                        times=history.times, min_height=100, show_avg=True)
            self.graphs.append(sig)
            self.signal_value = Gtk.Label()
            sig_card = card("Signal strength", sig, self.signal_value)
            sig_card.set_tooltip_text("Signal quality: -50 dBm or better is 100 %, -100 dBm is 0 %.")
            row.append(sig_card)
        body.append(row)

        def i(fn):
            return lambda s, _: fn(s["net_info"]["ifaces"][name])

        def w(fn):
            return i(lambda x: fn(x["wifi"]) if x["wifi"] and x["wifi"]["connected"] else None)

        def n(fn):
            return lambda s, _: fn(s["nics"][name])

        entries = []
        if self.wifi:
            entries += [
                ("Network (SSID)", w(lambda x: x["ssid"])),
                ("Access point", w(lambda x: x["bssid"])),
                ("Signal", w(lambda x: _signal(x["signal"], x))),
                ("Signal average", w(lambda x: f"{x['signal_avg']} dBm" if x["signal_avg"] is not None else None)),
                ("Standard", w(lambda x: x["standard"])),
                ("Band", w(_wifi_channel)),
                ("Receive rate", w(lambda x: fmt_bitrate(x["rx_bitrate"]))),
                ("Send rate", w(lambda x: fmt_bitrate(x["tx_bitrate"]))),
                ("Expected throughput", w(lambda x: f"{x['expected_throughput']:.0f} Mbit/s"
                                          if x["expected_throughput"] else None)),
                ("Transmit power", w(lambda x: f"{x['txpower']:g} dBm" if x["txpower"] is not None else None)),
                ("Retries / failed", w(lambda x: _pair(x["tx_retries"], x["tx_failed"]))),
                ("Beacon loss", w(lambda x: fmt_int(x["beacon_loss"]) if x["beacon_loss"] is not None else None)),
                ("Connected for", w(lambda x: fmt_duration(x["connected_time"])
                                    if x["connected_time"] is not None else None)),
                ("Driver", i(lambda x: x["driver"])),
            ]
        else:
            entries += [("Link speed", i(_link_speed)), ("Driver", i(lambda x: x["driver"]))]
        # two columns: entries pair up per row (address | gateway, received | sent, ...)
        entries += [
            ("IPv4 address", i(lambda x: "\n".join(x["ipv4"]))),
            ("IPv4 gateway", i(lambda x: x["gateway4"])),
            ("IPv6 addresses", i(lambda x: "\n".join(x["ipv6"]))),
            ("IPv6 gateway", i(lambda x: x["gateway6"])),
            ("MAC address", i(lambda x: x["mac"])),
            ("MTU", i(lambda x: str(x["mtu"]) if x["mtu"] else None)),
            ("Received", n(lambda x: fmt_bytes(x["rx_total"]))),
            ("Sent", n(lambda x: fmt_bytes(x["tx_total"]))),
            ("Packets in / out", i(lambda x: _pair(x.get("rx_packets"), x.get("tx_packets")))),
            ("Errors in / out", i(lambda x: _pair(x.get("rx_errors"), x.get("tx_errors")))),
            ("Dropped in / out", i(lambda x: _pair(x.get("rx_dropped"), x.get("tx_dropped")))),
        ]
        self.stats = StatsGrid(entries, columns=2)
        body.append(self.stats)
        self.set_child(body)
        self.sort_key = None

    def update(self, snap, sampler):
        info = snap["net_info"]["ifaces"][self.name]
        self.subtitle.set_text(" · ".join(x for x in (info["kind"], info["adapter"]) if x))
        state = _iface_state(info)
        wifi = info["wifi"]
        if wifi and wifi["connected"] and wifi["signal"] is not None:
            state += f" · {wifi['signal']} dBm"
        self.state.set_text(state)
        nic = snap["nics"].get(self.name)
        if nic:
            rx, tx = fmt_bit_rate(nic["rx_rate"]) or "0", fmt_bit_rate(nic["tx_rate"]) or "0"
            self.rate_value.set_text(f"↓ {rx}  ↑ {tx}")
        if self.signal_value is not None:
            self.signal_value.set_text(_signal(wifi["signal"], wifi) if wifi and wifi["connected"] else "—")
        if self.get_expanded():
            self.stats.update(snap, sampler)


class DiskPanel(Gtk.Expander):
    """One physical disk: throughput and active time history, hardware details and partitions."""

    def __init__(self, name, info, history, expanded=True):
        super().__init__(expanded=expanded)
        self.name = name
        self.graphs = []

        head = Gtk.Box(spacing=8)
        head.append(Gtk.Label(label=name, css_classes=["heading"]))
        self.subtitle = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END, css_classes=["dim"])
        head.append(self.subtitle)
        self.state = Gtk.Label(xalign=1, css_classes=["dim", "numeric"])
        head.append(self.state)
        self.set_label_widget(head)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=6, margin_bottom=12,
                       margin_start=18)
        row = Gtk.Box(spacing=12, homogeneous=True)
        rd, wr = history.per_disk[name]
        graph = Graph([Series(rd, READ, "Read"), Series(wr, WRITE, "Write")], fmt=fmt_rate, times=history.times,
                      min_height=100)
        self.rate_value = Gtk.Label()
        row.append(card("Throughput", graph, self.rate_value))
        busy = Graph([Series(history.per_disk_busy[name], BUSY, "Active time")], max_value=100, fmt=_pct,
                     times=history.times, min_height=100, show_avg=True)
        self.busy_value = Gtk.Label()
        busy_card = card("Active time", busy, self.busy_value)
        busy_card.set_tooltip_text("Share of time the disk had at least one request in progress. "
                                   "SSDs and NVMe drives serve many requests in parallel, so 100 % "
                                   "does not mean they are saturated.")
        row.append(busy_card)
        self.graphs += [graph, busy]
        body.append(row)

        def i(fn):
            return lambda s, _: fn(s["disk_info"][name])

        def d(fn):
            return lambda s, _: fn(s["disks"][name])

        entries = [
            ("Model", i(lambda x: " ".join(v for v in (x["vendor"], x["model"]) if v))),
            ("Serial number", i(lambda x: x["serial"])),
            ("Type", i(lambda x: x["kind"])),
            ("Interface", i(lambda x: x["link"] or x["bus"])),
            ("Capacity", i(lambda x: _capacity(x["size"]))),
            ("Firmware", i(lambda x: x["firmware"])),
            ("Partition table", i(lambda x: x["ptable"] or ("none" if x["volumes"] else None))),
            ("Sector size", i(_sectors)),
            ("Temperature", i(_temps)),
        ]
        if info["rotational"]:
            entries.append(("Rotation speed", i(lambda x: f"{x['rpm']} RPM" if x["rpm"] else None)))
        else:
            entries.append(("TRIM / discard", i(lambda x: None if x["discard"] is None
                                                 else "supported" if x["discard"] else "not supported")))
        entries += [
            ("I/O scheduler", i(lambda x: x["scheduler"])),
            ("Write cache", i(lambda x: x["write_cache"])),
            ("Read rate", d(lambda x: fmt_rate(x["read_rate"]) or "0 B/s")),
            ("Write rate", d(lambda x: fmt_rate(x["write_rate"]) or "0 B/s")),
            ("Reads/s", d(lambda x: _ops(x["read_iops"]))),
            ("Writes/s", d(lambda x: _ops(x["write_iops"]))),
            ("Read latency", d(lambda x: _latency(x["read_lat"]))),
            ("Write latency", d(lambda x: _latency(x["write_lat"]))),
            ("Read since boot", d(lambda x: f"{fmt_bytes(x['read_total'])} ({fmt_int(x['reads'])} requests)")),
            ("Written since boot", d(lambda x: f"{fmt_bytes(x['write_total'])} ({fmt_int(x['writes'])} requests)")),
            ("Active time", d(lambda x: _pct(x["busy"]))),
            ("In flight (read / write)", i(lambda x: _pair(*x["inflight"]) if x["inflight"] else None)),
        ]
        self.stats = StatsGrid(entries, columns=2)
        body.append(self.stats)

        body.append(Gtk.Label(label="Partitions and file systems", xalign=0, css_classes=["heading"],
                              margin_top=6))
        self.volumes = VolumeList()
        body.append(self.volumes)
        self.set_child(body)

    def update(self, snap, sampler):
        info = snap["disk_info"][self.name]
        d = snap["disks"][self.name]
        model = " ".join(v for v in (info["vendor"], info["model"]) if v)
        parts = [info["kind"], model, fmt_bytes(info["size"]) if info["size"] else None,
                 "read-only" if info["read_only"] else None]
        self.subtitle.set_text(" · ".join(p for p in parts if p))
        state = f"R {fmt_short_rate(d['read_rate'])}  W {fmt_short_rate(d['write_rate'])} · {d['busy']:.0f}%"
        if info["temps"]:
            state += f" · {next(iter(info['temps'].values())):.0f} °C"
        self.state.set_text(state)
        self.rate_value.set_text(f"R {fmt_rate(d['read_rate']) or '0'}  W {fmt_rate(d['write_rate']) or '0'}")
        self.busy_value.set_text(_pct(d["busy"]))
        if self.get_expanded():
            self.stats.update(snap, sampler)
            self.volumes.update(info["volumes"])


class VolumeList(Gtk.Grid):
    """Partitions of a disk and the volumes stacked on them (LUKS, LVM, RAID), with file system usage."""

    HEADERS = ("Volume", "Size", "File system", "Mounted at", "Usage", "")

    def __init__(self):
        super().__init__(column_spacing=18, row_spacing=4, margin_top=2)
        self._key = None
        self._rows = []  # (level bar, usage label) per volume row

    def update(self, volumes):
        key = tuple((v["dev"], v["depth"], v["fstype"], v["label"], tuple(v["mounts"]), v["swap"]) for v in volumes)
        if key != self._key:
            self._key = key
            self._rebuild(volumes)
        for (bar, label), v in zip(self._rows, volumes):
            if v["total"]:
                bar.set_value(min(1.0, (v["used"] or 0) / v["total"]))
                label.set_text(_used_total(v["used"], v["total"]))
            bar.set_visible(bool(v["total"]))
            label.set_visible(bool(v["total"]))

    def _rebuild(self, volumes):
        while (child := self.get_first_child()) is not None:
            self.remove(child)
        self._rows = []
        if not volumes:
            self.attach(Gtk.Label(label="No partitions", xalign=0, css_classes=["dim"]), 0, 0, 1, 1)
            return
        for col, title in enumerate(self.HEADERS):
            self.attach(Gtk.Label(label=title, xalign=0, css_classes=["dim", "caption-heading"]), col, 0, 1, 1)
        for r, v in enumerate(volumes, start=1):
            name = GLib.markup_escape_text(("↳ " if v["depth"] else "") + v["name"])
            extra = v["label"] or v["role"]
            if extra:
                name += f"  <span alpha='60%'>{GLib.markup_escape_text(extra)}</span>"
            name_label = Gtk.Label(xalign=0, use_markup=True, label=name, margin_start=14 * max(0, v["depth"] - 1),
                                   ellipsize=Pango.EllipsizeMode.END, max_width_chars=40)
            name_label.set_tooltip_text(f"/dev/{v['dev']}")
            mounts = v["mounts"] or (["[swap]"] if v["swap"] else [])
            if len(mounts) > 3:
                mounts = mounts[:3] + [f"+{len(v['mounts']) - 3} more"]
            mount_label = Gtk.Label(label="\n".join(mounts) or "—", xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE,
                                    max_width_chars=32, hexpand=True)
            if len(v["mounts"]) > 3:
                mount_label.set_tooltip_text("\n".join(v["mounts"]))
            bar = Gtk.LevelBar(min_value=0, max_value=1, valign=Gtk.Align.CENTER, width_request=110)
            for offset in ("low", "high", "full"):
                bar.remove_offset_value(offset)
            bar.add_offset_value("disk-ok", 0.9)
            bar.add_offset_value("disk-full", 1.0)
            usage = Gtk.Label(xalign=1, css_classes=["numeric"])
            cells = [
                name_label,
                Gtk.Label(label=fmt_bytes(v["size"]) if v["size"] else "—", xalign=1, css_classes=["numeric"]),
                Gtk.Label(label=_fs_text(v), xalign=0),
                mount_label, bar, usage,
            ]
            for col, widget in enumerate(cells):
                self.attach(widget, col, r, 1, 1)
            self._rows.append((bar, usage))
