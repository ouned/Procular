"""Properties of one hardware device: what it is and which driver runs it, how it is connected,
its power state and resources, what the driver made of it, the processes using it, and every
attribute the kernel shows for it."""

from collections import deque

from gi.repository import Adw, Gio, GLib, GObject, Gtk, Pango

from ..providers.devices import (CLASS_TYPES, ROOT_KEY, CounterRates, describe_region, device_details,
                                 fmt_pcie_link, natural_key)
from ..providers.processes import username
from ..util.format import fmt_bytes, fmt_duration, fmt_int
from ..util.icons import GENERIC_ICON
from ..util.lifecycle import destroy_on_close, is_destroyed
from ..util.tasks import run_async
from ..util.timeline import SampleTime
from ..widgets import process_actions as pa
from ..widgets.graph import KERNEL as POWER_COLOR
from ..widgets.graph import Graph, Series
from ..widgets.table import Column, Table
from .properties import Badge, InfoPage, _flat_button
from .sysinfo import card

REFRESH_SECONDS = 2
POWER_SAMPLES = 450  # 15 minutes of power readings
STATUS_DOT = {"Active": "running", "Suspended": "active", "No driver": "changing"}

PAGES = [
    ("general", "General", "dialog-information-symbolic"),
    ("connection", "Connection", "format-indent-more-symbolic"),
    ("power", "Power", "system-shutdown-symbolic"),
    ("resources", "Resources", "application-x-firmware-symbolic"),
    ("interfaces", "Interfaces", "application-x-addon-symbolic"),
    ("processes", "Processes", "system-run-symbolic"),
    ("attributes", "Attributes", "view-list-bullet-symbolic"),
]


def _hex_id(value):
    """"0x1002" -> "1002"."""
    return value[2:] if value and value.startswith("0x") else (value or "")


def _ms(value):
    try:
        return fmt_duration(int(value) / 1000)
    except (TypeError, ValueError):
        return None


def _usb_version(value):
    """" 2.00" -> "2.0", "3.10" -> "3.1"."""
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return None


def missing_driver_text(candidates):
    """Why a device has no driver, from the modules that declare they can drive it."""
    if not candidates:
        return ("No kernel module declares that it supports this device. A program can still use it "
                "directly, as fingerprint readers are used through libusb.")
    loaded = [name for name, is_loaded in candidates if is_loaded]
    if not loaded:
        names = ", ".join(name for name, _ in candidates)
        return f"The module {names} supports it but is not loaded: it may be blacklisted, or missing from the initramfs."
    return (f"The module {', '.join(loaded)} is loaded but did not take the device: its start failed "
            "(see the kernel log, journalctl -k) or it was unbound by hand.")


def region_text(start, end, flags):
    kind, notes = describe_region(start, end, flags)
    text = f"0x{start:x}–0x{end:x} ({fmt_bytes(end - start + 1)})"
    return kind, f"{text}, {notes}" if notes else text


class _Page(InfoPage):
    """An InfoPage whose rows are shown only where they have a value."""

    def show(self, key, value):
        row = self.rows[key]
        row.set_visible(value not in (None, "", ()))
        if row.get_visible():
            row.set_value(value)


class GeneralPage(_Page):
    def __init__(self, win):
        super().__init__(win)
        row = win.row
        card = Gtk.Box(spacing=16, css_classes=["card", "overview"])
        icon = Gtk.Image(icon_name=row["icon"], pixel_size=56, valign=Gtk.Align.CENTER)
        card.append(icon)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER, hexpand=True)
        text.append(Gtk.Label(label=row["name"], xalign=0, css_classes=["title-2"], selectable=True, focusable=False,
                              ellipsize=Pango.EllipsizeMode.END))
        text.append(Gtk.Label(label=" · ".join(filter(None, (row["type"], row["vendor"]))), xalign=0,
                              css_classes=["dim"], ellipsize=Pango.EllipsizeMode.END))
        badges = Gtk.Box(spacing=6, margin_top=4)
        self.status = Badge(dot=True, tooltip="Status")
        self.bus = Badge(tooltip="Bus")
        self.address = Badge(tooltip="Address on the bus")
        for badge in (self.status, self.bus, self.address):
            badges.append(badge)
        self.bus.update(row["bus"].upper() if row["bus"] in ("pci", "usb", "hid", "i2c", "spi") else row["bus"])
        self.address.update(row["address"])
        text.append(badges)
        card.append(text)
        self.body.append(card)

        self.missing = Gtk.Box(spacing=12, css_classes=["card", "missing-driver"], visible=False)
        self.missing.append(Gtk.Image(icon_name="dialog-warning-symbolic", valign=Gtk.Align.START))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        box.append(Gtk.Label(label="No Driver", xalign=0, css_classes=["heading"]))
        self.missing_text = Gtk.Label(xalign=0, wrap=True, selectable=True, focusable=False)
        box.append(self.missing_text)
        self.missing.append(box)
        self.body.append(self.missing)

        left, right = self.columns()
        g = self.group("Device", left)
        self.value(g, "type", "Type")
        self.value(g, "vendor", "Vendor")
        self.value(g, "ids", "Vendor and device ID", mono=True)
        self.value(g, "subsystem", "Subsystem ID", mono=True)
        self.value(g, "revision", "Revision", mono=True)
        self.value(g, "class", "Class code", mono=True)
        self.value(g, "serial", "Serial number", mono=True)
        self.value(g, "boot_vga", "Boot display")

        g = self.group("Driver", right)
        self.value(g, "driver", "Driver")
        self.value(g, "module", "Kernel module")
        self.value(g, "srcversion", "Module source version", mono=True)
        self.value(g, "taint", "Module taint flags", mono=True)
        self.chips(g, "iface_drivers", "Drivers of the interfaces", mono=True)
        self.chips(g, "candidates", "Modules that support it", mono=True)

        g = self.group("Location")
        path = self.wide(g, "syspath", "Device path", mono=True)
        path.add_suffix(_flat_button("edit-copy-symbolic", "Copy path", lambda: win.copy(win.row["syspath"])))
        alias = self.wide(g, "modalias", "Modalias", mono=True)
        alias.add_suffix(_flat_button("edit-copy-symbolic", "Copy modalias", lambda: win.copy(win.row["modalias"])))
        self.chips(g, "devnodes", "Device files", mono=True)
        self.show("syspath", row["syspath"])
        self.show("modalias", row["modalias"])
        self.show("devnodes", row["devnodes"])

    def update(self, details):
        row = self.win.row
        live = details["live"][row["key"]]
        attrs = details["attrs"]
        status = live["status"]
        self.status.update(status or "", STATUS_DOT.get(status))
        self.show("type", row["type"])
        self.show("vendor", row["vendor"])
        ids = subsystem = None
        if row["bus"] == "pci":
            ids = f"{_hex_id(attrs.get('vendor'))}:{_hex_id(attrs.get('device'))}"
            if attrs.get("subsystem_vendor"):
                subsystem = f"{_hex_id(attrs['subsystem_vendor'])}:{_hex_id(attrs.get('subsystem_device'))}"
        elif attrs.get("idVendor"):
            ids = f"{attrs['idVendor']}:{attrs.get('idProduct', '')}"
        elif attrs.get("vendor_id"):  # an HDA codec
            ids = attrs["vendor_id"]
        self.show("ids", ids)
        self.show("subsystem", subsystem or attrs.get("subsystem_id"))
        self.show("revision", attrs.get("revision") or attrs.get("bcdDevice") or attrs.get("revision_id"))
        self.show("class", attrs.get("class"))
        self.show("serial", attrs.get("serial") or attrs.get("serial_number") or attrs.get("unique_id"))
        self.show("boot_vga", "Yes, the firmware showed its screen on it" if attrs.get("boot_vga") == "1" else None)
        driver = details["driver"]
        interfaces = details["interfaces"]
        self.show("driver", driver if not interfaces else None)
        module = details["module"] if not interfaces else None
        self.show("module", "Built into the kernel" if module == "built-in" else module)
        info = details["modules"].get(details["module"]) or {}
        self.show("srcversion", (info.get("version") or info.get("srcversion")) if not interfaces else None)
        self.show("taint", info.get("taint") if not interfaces else None)
        self.show("iface_drivers", tuple(f"{i['name']}: {i['driver'] or 'no driver'}" for i in interfaces))
        self.show("candidates", tuple(name if loaded else f"{name} (not loaded)"
                                      for name, loaded in details["candidates"]))
        if status == "No driver":
            self.missing_text.set_text(missing_driver_text(details["candidates"]))
        self.missing.set_visible(status == "No driver")


class ConnectionPage(_Page):
    def __init__(self, win):
        super().__init__(win)
        self.path = self.group("Path from the computer")
        self.path.set_description("Each device is connected to the one above it. Open one to see its properties.")
        self.hops = {}
        for hop in [*win.ancestors, win.row]:
            mine = hop["key"] == win.row["key"]
            openable = not mine and hop["key"] != ROOT_KEY
            row = Adw.ActionRow(title=GLib.markup_escape_text(hop["name"]),
                                subtitle=GLib.markup_escape_text(" · ".join(filter(None, (hop["type"],
                                                                                          hop["address"])))),
                                activatable=openable)
            if mine:
                row.add_css_class("current-device")
            row.add_prefix(Gtk.Image(icon_name=hop["icon"]))
            link = Gtk.Label(css_classes=["dim", "numeric"], valign=Gtk.Align.CENTER)
            row.add_suffix(link)
            if openable:
                row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
                row.connect("activated", lambda _r, key=hop["key"]: win.main.open_device_properties(key))
            self.path.add(row)
            self.hops[hop["key"]] = (link, hop)

        g = self.group("Link")
        self.link_group = g
        self.value(g, "current", "Current link")
        self.value(g, "maximum", "Maximum link")
        self.value(g, "usb_speed", "Speed")
        self.value(g, "usb_version", "USB version")
        self.value(g, "ports", "Ports")
        self.value(g, "removable", "Connection")
        self.value(g, "authorized", "Authorized")

    def update(self, details):
        live = details["live"]
        for key, (label, hop) in self.hops.items():
            values = live.get(key)
            if values is not None and values["link"] is not None:
                label.set_text(values["link"])
            elif not label.get_text():
                label.set_text(hop.get("link") or "")
        link = details["link"]
        attrs = details["attrs"]
        current = maximum = None
        if link is not None:
            current, _ = fmt_pcie_link(link["current_link_speed"], link["current_link_width"],
                                       link["current_link_speed"], link["current_link_width"])
            maximum, _ = fmt_pcie_link(link["max_link_speed"], link["max_link_width"],
                                       link["max_link_speed"], link["max_link_width"])
        elif self.win.row["pcie"]:
            current = "Not read while the device is suspended (it would wake it)"
        self.show("current", current)
        self.show("maximum", maximum)
        usb = self.win.row["bus"] == "usb"
        self.show("usb_speed", self.win.row["link"] if usb else None)
        self.show("usb_version", _usb_version(attrs.get("version")) if usb else None)
        self.show("ports", attrs.get("maxchild") if usb and attrs.get("maxchild", "0") != "0" else None)
        removable = {"removable": "Removable (a port you can plug into)", "fixed": "Built in",
                     "unknown": None}.get(attrs.get("removable"))
        self.show("removable", removable)
        self.show("authorized", {"1": "Yes", "0": "No: blocked until authorized"}.get(attrs.get("authorized")))
        self.link_group.set_visible(any(r.get_visible() for k, r in self.rows.items()))


class PowerPage(_Page):
    def __init__(self, win):
        super().__init__(win)
        row = win.row
        self.measured = bool(row["power_sensor"] or row["battery"])
        self.times = deque(maxlen=POWER_SAMPLES)
        self.watts = deque(maxlen=POWER_SAMPLES)
        self.last = None
        if self.measured:
            self.power_value = Gtk.Label()
            self.graph = Graph([Series(self.watts, POWER_COLOR, "Power")], fmt=lambda v: f"{v:.1f} W",
                               times=self.times, show_avg=True, min_scale=1, min_height=140)
            title = "Battery Power" if row["battery"] else "Power Draw"
            self.body.append(card(title, self.graph, self.power_value, height=160))
            self.power_note = Gtk.Label(xalign=0, wrap=True, css_classes=["dim", "caption"])
            self.body.append(self.power_note)
            self.power_note.set_text(_power_note(row))
        left, right = self.columns()
        g = self.group("Runtime Power Management", left)
        g.set_description("The kernel suspends an idle device and wakes it when it is needed.")
        self.value(g, "runtime", "State")
        self.value(g, "control", "Mode")
        self.value(g, "active", "Time active")
        self.value(g, "suspended", "Time suspended")
        self.value(g, "autosuspend", "Suspends after")
        self.value(g, "share", "Share of time suspended")
        g = self.group("Device", right)
        self.value(g, "d_state", "PCI power state")
        self.value(g, "d3cold", "May power off completely")
        self.value(g, "max_power", "Maximum power draw")
        self.value(g, "wakeup", "Can wake the computer")
        self.value(g, "wakeup_count", "Wakeups so far")

    def update(self, details):
        power = details["power"]
        attrs = details["attrs"]
        self.show("runtime", {"active": "Active", "suspended": "Suspended", "suspending": "Suspending",
                              "resuming": "Resuming", "unsupported": "Not supported"}.get(power.get("runtime_status")))
        self.show("control", {"auto": "Automatic: suspended when idle",
                              "on": "Always on: never suspended"}.get(power.get("control")))
        self.show("active", _ms(power.get("runtime_active_time")))
        self.show("suspended", _ms(power.get("runtime_suspended_time")))
        live = details["live"][self.win.row["key"]]
        share = live.get("suspended_share")
        self.show("share", f"{share * 100:.1f} %" if share is not None and power.get("control") == "auto" else None)
        if self.measured:
            self._record(live)
        delay = power.get("autosuspend_delay_ms")
        self.show("autosuspend", f"{int(delay) / 1000:g} s idle" if delay and delay.lstrip("-").isdigit()
                  and int(delay) >= 0 else None)
        if power.get("runtime_status") == "unsupported":
            for key in ("control", "active", "suspended", "autosuspend"):
                self.rows[key].set_visible(False)  # counters and a mode that mean nothing here
        self.show("d_state", attrs.get("power_state"))
        self.show("d3cold", {"1": "Yes", "0": "No"}.get(attrs.get("d3cold_allowed")))
        self.show("max_power", (attrs.get("bMaxPower") or "").replace("mA", " mA") or None)
        self.show("wakeup", {"enabled": "Yes", "disabled": "No"}.get(power.get("wakeup")))
        self.show("wakeup_count", power.get("wakeup_count"))


    def _record(self, live):
        watts = live.get("power")
        if live.get("power_read", True):
            self.last = watts
        elif live.get("runtime_pm") != "suspended":
            watts = self.last  # its sensor was left alone this time, so that it can suspend
        stamp = SampleTime.now()
        self.times.append(stamp._replace(interval=REFRESH_SECONDS))
        self.watts.append(watts)
        if watts is None:
            state = live.get("runtime_pm")
            self.power_value.set_text("Suspended: not read" if state == "suspended" else "—")
        else:
            text = f"{watts:.1f} W"
            if self.win.row["battery"]:
                status = live.get("battery_status") or ""
                text += {"Discharging": " delivered", "Charging": " charging"}.get(status, f" ({status.lower()})")
            self.power_value.set_text(text)
        if self.graph.get_mapped():
            self.graph.queue_draw()


def _power_note(row):
    if row["battery"]:
        return ("While the computer runs on battery, this is the power of the whole computer. While "
                "charging, it is what the battery takes in.")
    label = row["power_sensor"][1]
    if label == "PPT":
        return ("Package power tracking (PPT), measured by the driver. On a processor with integrated "
                "graphics this is the whole chip: processor cores and graphics.")
    return f"Measured by the driver{f' ({label})' if label else ''}."


class ResourcesPage(_Page):
    def __init__(self, win):
        super().__init__(win)
        self.irq_group = self.group("Interrupts")
        self.irq_group.set_description("Requests for a processor's attention, per interrupt line.")
        self.irq_rows = {}
        self.region_group = self.group("Memory and I/O Regions")
        self.region_group.set_description("Address ranges where the processor reaches the device's registers "
                                          "and memory (BARs).")
        self.region_rows = []
        g = self.placement = self.group("Placement")
        self.value(g, "iommu", "IOMMU group")
        self.chips(g, "iommu_members", "Shares the group with", mono=True)
        self.value(g, "numa", "NUMA node")
        self.value(g, "cpus", "Local processors")
        self.value(g, "dma", "DMA address width")
        self.empty = Adw.StatusPage(icon_name="application-x-firmware-symbolic", title="No Resources Listed",
                                    description="The kernel lists interrupts and address ranges for PCI devices.")
        self.empty.set_visible(False)
        self.body.append(self.empty)
        self.rates = CounterRates()

    def update(self, details):
        rates = self.rates.update({irq["irq"]: irq["count"] for irq in details["irqs"]})
        for irq in details["irqs"]:
            row = self.irq_rows.get(irq["irq"])
            if row is None:
                row = Adw.ActionRow(title=f"IRQ {irq['irq']}", css_classes=["compact"])
                row.value = Gtk.Label(css_classes=["numeric", "value"], valign=Gtk.Align.CENTER)
                row.add_suffix(row.value)
                self.irq_group.add(row)
                self.irq_rows[irq["irq"]] = row
            subtitle = " · ".join(filter(None, (irq["handlers"], irq["chip"],
                                                f"processor {irq['affinity']}" if irq["affinity"] else "")))
            row.set_subtitle(GLib.markup_escape_text(subtitle))
            rate = rates.get(irq["irq"])
            total = fmt_int(irq["count"]) if irq["count"] is not None else "—"
            row.value.set_text(f"{rate:.0f}/s · {total}" if rate is not None else total)
        self.irq_group.set_visible(bool(details["irqs"]))
        if not self.region_rows:
            for index, start, end, flags in details["regions"]:
                kind, text = region_text(start, end, flags)
                row = Adw.ActionRow(title=f"Region {index}: {kind}", subtitle=text, subtitle_selectable=True,
                                    css_classes=["property", "mono"])
                self.region_group.add(row)
                self.region_rows.append(row)
        self.region_group.set_visible(bool(self.region_rows))
        attrs = details["attrs"]
        self.show("iommu", details["iommu_group"])
        self.show("iommu_members", tuple(details["iommu_members"]) if details["iommu_group"] else ())
        numa = attrs.get("numa_node")
        self.show("numa", numa if numa not in (None, "-1") else None)
        self.show("cpus", attrs.get("local_cpulist"))
        self.show("dma", f"{attrs['dma_mask_bits']} bits" if attrs.get("dma_mask_bits") else None)
        placement = any(self.rows[k].get_visible() for k in ("iommu", "numa", "cpus", "dma"))
        self.placement.set_visible(placement)
        self.empty.set_visible(not (details["irqs"] or self.region_rows or placement))


class _TablePage(Gtk.Box):
    """A table with a filter and a count above it."""

    def __init__(self, win, table, extra=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.table = table
        bar = Gtk.Box(spacing=12, css_classes=["filter-bar"])
        entry = Gtk.SearchEntry(placeholder_text="Filter", hexpand=True)
        entry.connect("search-changed", lambda e: table.set_filter_text(e.get_text()))
        self.count = Gtk.Label(css_classes=["dim", "caption", "numeric"])
        bar.append(entry)
        bar.append(self.count)
        if extra is not None:
            bar.append(extra)
        self.append(bar)
        self.append(Gtk.Separator())
        self.append(table)


class InterfacesPage(_TablePage):
    def __init__(self, win):
        columns = [
            Column("name", "Name", "name", width=200, sort=lambda d: natural_key(d["name"])),
            Column("type", "Type", "type", width=160),
            Column("devnode", "Device file", "devnode", width=220, css=("monospace-text",)),
            Column("users", "Used by", "users", width=200, expand=True),
        ]
        table = Table(columns, key=lambda d: (d["type"], d["name"]), filter_keys=("name", "type", "devnode", "users"))
        table.remember_sort(win.main.config, "device-interfaces", "name")
        super().__init__(win, table)

    def update(self, details):
        users = details["all_users"]
        rows = []
        for sub, name, devnode in self.win.row["classes"]:
            pids = sorted(users.get(devnode, ())) if devnode else []
            rows.append({"name": name, "type": CLASS_TYPES.get(sub, sub), "devnode": devnode,
                         "users": ", ".join(sorted({self.win.process_name(pid) for pid in pids}))})
        self.table.set_rows(rows)
        self.count.set_text(f"{len(rows)} interfaces")


class ProcessesPage(_TablePage):
    def __init__(self, win):
        columns = [
            Column("name", "Process", "name", width=200, icon=lambda d: d["icon"] or GENERIC_ICON),
            Column("pid", "PID", "pid", numeric=True, width=70),
            Column("user", "User", "user", width=110),
            Column("devnodes", "Device files", "devnodes", width=300, expand=True, css=("monospace-text",)),
        ]
        menu = Gio.Menu()
        menu.append("Go to Process", "devprops.goto")
        menu.append("Process Properties", "devprops.properties")
        table = Table(columns, key="pid", filter_keys=("name", "pid", "user", "devnodes"), menu_model=menu)
        table.remember_sort(win.main.config, "device-processes", "name")
        table.connect("activated", lambda _t, d: win.main.goto_process(d["pid"]))
        self.elevate = Gtk.Button(label="Check as Administrator", visible=False, css_classes=["flat"])
        self.elevate.connect("clicked", lambda *_: win.elevate())
        super().__init__(win, table, self.elevate)
        group = Gio.SimpleActionGroup()
        for name, handler in (("goto", win.main.goto_process), ("properties", win.main.open_properties)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_a, h=handler: self._with_selected(h))
            group.add_action(action)
        self.insert_action_group("devprops", group)

    def _with_selected(self, handler):
        data = self.table.selected()
        if data is not None:
            handler(data["pid"])

    def update(self, details):
        rows = []
        for pid, nodes in details["users"].items():
            item = self.win.main.tree.get(pid)
            rows.append({"pid": pid, "name": item.name if item is not None else f"PID {pid}",
                         "icon": item.icon if item is not None else None,
                         "user": username(item.rec["uid"]) if item is not None else "",
                         "devnodes": ", ".join(nodes)})
        self.table.set_rows(rows)
        self.count.set_text(f"{len(rows)} processes")
        denied = details["denied"]
        self.elevate.set_visible(bool(denied))
        self.elevate.set_tooltip_text(f"{denied} processes of other users were not checked" if denied else None)


class AttributesPage(_TablePage):
    def __init__(self, win):
        columns = [
            Column("source", "Source", "source", width=80),
            Column("name", "Name", "name", width=260, css=("monospace-text",)),
            Column("value", "Value", "value", width=420, expand=True, css=("monospace-text",),
                   tooltip=lambda d: d["value"] if len(d["value"]) > 40 else None),
        ]
        table = Table(columns, key=lambda d: (d["source"], d["name"]), filter_keys=("source", "name", "value"))
        table.remember_sort(win.main.config, "device-attributes")
        refresh = _flat_button("view-refresh-symbolic", "Read again", lambda: win.refresh(attributes=True))
        super().__init__(win, table, refresh)
        self.loaded = False

    def update(self, details):
        attributes = details.get("attributes")
        if attributes is None:
            return
        self.loaded = True
        self.table.set_rows([{"source": s, "name": n, "value": v} for s, n, v in attributes])
        self.count.set_text(f"{len(attributes)} attributes")


class DevicePropertiesWindow(Adw.Window):
    def __init__(self, main, row, ancestors):
        super().__init__(title=f"{row['name']} Properties")
        self.main = main
        self.row = row
        self.key = row["key"]
        self.ancestors = ancestors
        self.removed = False
        self._busy = False
        self._again = None  # a refresh asked for while one runs: whether it reads the attributes
        self._timer = 0
        self.set_default_size(960, 700)
        self.set_size_request(560, 400)
        self._bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 860sp"))
        self.add_breakpoint(self._bp)

        self.toasts = Adw.ToastOverlay()
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, hexpand=True)
        self.pages = {
            "general": GeneralPage(self),
            "connection": ConnectionPage(self),
            "power": PowerPage(self),
            "resources": ResourcesPage(self),
            "interfaces": InterfacesPage(self),
            "processes": ProcessesPage(self),
            "attributes": AttributesPage(self),
        }
        for name, title, _icon in PAGES:
            self.stack.add_titled(self.pages[name], name, title)
        self.stack.connect("notify::visible-child-name", self._on_page)

        body = Gtk.Box()
        body.append(self._build_sidebar())
        body.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        body.append(self.stack)
        self.toasts.set_child(body)

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title=row["name"], subtitle=" · ".join(
            filter(None, (row["type"], row["address"])))))
        self.banner = Adw.Banner(title="This device was removed. The values shown are the last ones read.")
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.add_top_bar(self.banner)
        toolbar.set_content(self.toasts)
        self.set_content(toolbar)

        self.connect("close-request", self._on_close)
        destroy_on_close(self)
        self.refresh()
        self._timer = GLib.timeout_add_seconds(REFRESH_SECONDS, self._on_timer)

    # ----- used by the pages
    def responsive(self, box):
        """Two side-by-side columns that stack vertically on narrow windows."""
        self._bp.add_setter(box, "orientation", GObject.Value(Gtk.Orientation, Gtk.Orientation.VERTICAL))
        self._bp.add_setter(box, "homogeneous", GObject.Value(bool, False))

    def copy(self, text):
        if text:
            pa.copy_text(text)
            self.toasts.add_toast(Adw.Toast(title="Copied to clipboard", timeout=2))

    def toast(self, message):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(message), timeout=4))

    def process_name(self, pid):
        item = self.main.tree.get(pid)
        return item.name if item is not None else f"PID {pid}"

    def elevate(self):
        self.main.devices_view.elevate(lambda: not is_destroyed(self) and self.refresh())

    # ----- sidebar
    def _build_sidebar(self):
        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.BROWSE, css_classes=["navigation-sidebar"])
        self._side_rows = {}
        self.badges = {}
        for name, title, icon in PAGES:
            head = Gtk.Box(spacing=10)
            head.append(Gtk.Image(icon_name=icon))
            head.append(Gtk.Label(label=title, xalign=0, hexpand=True))
            badge = Gtk.Label(css_classes=["dim", "caption", "numeric"], visible=False)
            head.append(badge)
            self.badges[name] = badge
            side = Gtk.ListBoxRow(child=head)
            side.page_name = name
            listbox.append(side)
            self._side_rows[name] = side
        listbox.connect("row-selected", self._on_side_selected)
        listbox.select_row(self._side_rows["general"])
        self._listbox = listbox
        scroller = Gtk.ScrolledWindow(child=listbox, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_size_request(190, -1)
        return scroller

    def _on_side_selected(self, _listbox, row):
        if not is_destroyed(self) and row is not None and (name := getattr(row, "page_name", None)):
            self.stack.set_visible_child_name(name)

    def set_badge(self, name, text):
        label = self.badges[name]
        label.set_text(text or "")
        label.set_visible(bool(text))

    def _on_page(self, *_):
        name = self.stack.get_visible_child_name()
        side = self._side_rows.get(name)
        if side is not None and not side.is_selected():
            self._listbox.select_row(side)
        if name == "attributes" and not self.pages["attributes"].loaded:
            self.refresh(attributes=True)

    # ----- reading
    def _on_timer(self):
        if not self.main.sampler.paused:
            self.refresh()
        return True

    def refresh(self, attributes=False):
        if self.removed:
            return
        if self._busy:
            self._again = bool(self._again) or attributes
            return
        self._busy = True
        elevated = getattr(self.main.devices_view, "_elevated", None)
        run_async(device_details, self._on_details, self.row, self.ancestors, attributes, elevated)

    def _on_details(self, details, error):
        if is_destroyed(self):
            return
        self._busy = False
        if error is None and details is not None:
            self._apply(details)
        if self._again is not None and not self.removed:
            attributes, self._again = self._again, None
            self.refresh(attributes)

    def _apply(self, details):
        if not details["exists"]:
            self._mark_removed()
            return
        for page in self.pages.values():
            page.update(details)
        self.set_badge("processes", str(len(details["users"])) if details["users"] else "")
        self.set_badge("interfaces", str(len(self.row["classes"])) if self.row["classes"] else "")
        self.set_badge("resources", str(len(details["irqs"])) if details["irqs"] else "")
        power = self.pages["power"]
        self.set_badge("power", f"{power.last:.1f} W" if power.measured and power.last is not None else "")

    def _mark_removed(self):
        self.removed = True
        self.banner.set_revealed(True)
        self.set_title(f"{self.row['name']} Properties — removed")
        self._stop()

    def _stop(self):
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0

    def _on_close(self, *_):
        self._stop()
        return False


def ancestors_of(rows, key):
    """The row data from the computer down to the parent of `key`, from a tree Table's rows."""
    chain = []
    row = rows.get(key)
    parent = row.parent_key if row is not None else None
    while parent is not None:
        node = rows.get(parent)
        if node is None:
            break
        chain.insert(0, node.data)
        parent = node.parent_key
    return chain

