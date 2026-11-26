"""Devices tab: the hardware as the kernel sees it, where each device is connected, the driver
that runs it, and which processes use it."""

import time

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from ..providers.devices import (ROOT_KEY, CounterRates, UeventMonitor, device_users, natural_key, read_live, scan,
                                 system_power_text)
from ..providers.handles import elevated_snapshot
from ..util import privileged
from ..util.format import fmt_clock
from ..util.tasks import run_async
from . import process_actions as pa
from .table import Column, Table

REFRESH_SECONDS = 2
# without hotplug events (no netlink in a sandbox): look at the tree again every this many refreshes
RESCAN_TICKS_WITHOUT_EVENTS = 5
MAX_USER_ITEMS = 12  # processes listed in the context menu

_STATUS_DOT = {"Active": "running", "Suspended": "active", "No driver": "changing"}
_ICONS = {}


def _icon(data):
    name = data["icon"]
    icon = _ICONS.get(name)
    if icon is None:
        icon = _ICONS[name] = Gio.ThemedIcon.new(name)
    return icon


def _status_tooltip(data):
    status = data["status"]
    if status == "No driver":
        return "No driver has taken this device: it does nothing until one does"
    if not status:
        return None
    pm = data.get("runtime_pm")
    if status == "Suspended":
        return "Suspended by runtime power management until it is needed"
    if pm == "active":
        return "Working; may be suspended when idle"
    return "Working"


def _driver_tooltip(data):
    if not data["driver"]:
        return None
    module = data["module"]
    if module == "built-in":
        return f"Driver {data['driver']}, built into the kernel"
    return f"Driver {data['driver']}, from the kernel module {module}" if module else None


def _link_tooltip(data):
    if data.get("link_downgraded"):
        return "The link runs below what both ends can do. Devices lower it to save power while idle."
    if data.get("pcie") and data.get("runtime_pm") not in ("", "active", "unsupported"):
        return "As last read: the link of a suspended device is not read, as that would wake it"
    return None


def _suspended_text(data):
    if data.get("pm_control") == "on" and data["status"] in ("Active", "Suspended"):
        return "Always on"
    share = data.get("suspended_share")
    return "" if share is None or not data["status"] else f"{share * 100:.0f} %"


def _suspended_sort(data):
    text = _suspended_text(data)
    return -1 if text == "Always on" else (data.get("suspended_share") if text else -2)


def _suspended_tooltip(data):
    if _suspended_text(data) == "Always on":
        return "Runtime power management is off for this device: it never suspends"
    if data.get("suspended_share") is None or not data["status"]:
        return None
    return ("Share of the time since it appeared that the kernel kept it suspended. NVMe drives and Wi-Fi "
            "adapters also save power on their own, which is not counted here.")


def _power_text(data):
    power = data.get("power")
    return "" if power is None else f"{power:.1f} W"


def _power_tooltip(data):
    if data.get("power") is None:
        return None
    if data.get("battery"):
        status = data.get("battery_status") or ""
        return {"Discharging": "What the battery delivers: the whole computer's power draw",
                "Charging": "What the battery takes in while charging"}.get(status, f"Battery: {status}")
    label = data.get("power_label") or ""
    if label == "PPT":
        return ("Measured by the driver (PPT, package power tracking). On a processor with integrated "
                "graphics this is the whole chip: processor cores and graphics.")
    return f"Measured by the driver{f' ({label})' if label else ''}"


def _interfaces(data):
    return ", ".join(data["interfaces"])


def _devnodes_tooltip(data):
    return "\n".join(data["devnodes"]) or None


def _fmt_rate(value):
    if value is None:
        return ""
    return f"{value:.0f}" if value >= 10 or value == 0 else f"{value:.1f}"


DEVICE_FILTER_FIELDS = (_power_text, "name", "type", "driver", "module", "vendor", "address", "status", "link", "used_by",
                        _interfaces, lambda d: " ".join(d["devnodes"]), "modalias")


class DevicesView(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.config = win.config
        self._active = False
        self._loading = False
        self._refresh_again = False
        self._timer = 0
        self._ticks = 0
        self._static = None  # rows of the last scan; None: scan again on the next refresh
        self._last_link = {}  # key -> (link, downgraded) as last read (not read while suspended)
        self._last_power = {}  # key -> watts as last read (a sensor is not read on every refresh)
        self._irq_rates = CounterRates()
        self._elevated = None  # other users' descriptors read as administrator; replaced, never changed
        self._elevated_at = None
        self.monitor = UeventMonitor(self._on_hotplug, lambda: self.show_all.get_active())

        self.banner = Adw.Banner(button_label="Check as Administrator")
        self.banner.connect("button-clicked", self._elevate)
        self.append(self.banner)

        bar = Gtk.Box(spacing=6, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        self.show_all = Gtk.CheckButton(label="Show all devices", active=self.config.get("devices_show_all"),
                                        tooltip_text="Also the kernel's virtual devices, the firmware's ACPI objects, "
                                                     "processors, memory blocks and the parts of devices")
        self.show_all.connect("toggled", self._on_show_all)
        bar.append(self.show_all)
        bar.append(Gtk.Box(hexpand=True))
        self.count = Gtk.Label(css_classes=["dim"])
        bar.append(self.count)
        self.append(bar)

        columns = [
            Column("name", "Name", "name", width=340, icon=_icon, tooltip=lambda d: d["syspath"]),
            Column("status", "Status", "status", width=105, indicator=lambda d: _STATUS_DOT.get(d["status"], "none"),
                   tooltip=_status_tooltip),
            Column("suspended", "Time suspended", _suspended_text, width=115, numeric=True, sort=_suspended_sort,
                   tooltip=_suspended_tooltip),
            Column("power", "Power", _power_text, width=75, numeric=True,
                   sort=lambda d: d.get("power") if d.get("power") is not None else -1, tooltip=_power_tooltip),
            Column("driver", "Driver", "driver", width=130, tooltip=_driver_tooltip),
            Column("type", "Type", "type", width=170),
            Column("interfaces", "Interfaces", _interfaces, width=160, tooltip=_devnodes_tooltip,
                   sort=lambda d: natural_key(_interfaces(d))),
            Column("used_by", "Used by", "used_by", width=180, expand=True,
                   tooltip=lambda d: d["used_by_detail"] or None, sort=lambda d: (len(d["users"]), d["used_by"])),
            Column("link", "Link", "link", width=165, tooltip=_link_tooltip),
            Column("irq_rate", "Interrupts/s", "irq_rate", fmt=_fmt_rate, numeric=True, width=95),
            Column("vendor", "Vendor", "vendor", width=170),
            Column("address", "Address", "address", width=130, sort=lambda d: natural_key(d["address"]),
                   ellipsize=Pango.EllipsizeMode.MIDDLE, tooltip=lambda d: d["address"] or None),
        ]
        self._menu_top = Gio.Menu()
        self._user_items = []
        self.table = Table(columns, key="key", parent_key="parent", filter_keys=DEVICE_FILTER_FIELDS,
                           menu_model=self._build_menu())
        self.table.remember_sort(self.config, "devices", "address")
        self.table.connect("selection-changed", lambda *_: self._sync_menu())
        self.table.connect("activated", lambda _t, data: self._open_properties(data))
        self.table.popover.connect("show", lambda *_: self._sync_menu())  # its processes may have changed
        self.append(self.table)
        self._install_actions()

    # ------------------------------------------------------------------ menu
    def _build_menu(self):
        # The "Go to" items of the device's processes follow Properties in one section: as a
        # section of their own it would turn empty and non-empty, which leaves the menu
        # measured too small (see Table.set_menu).
        menu = Gio.Menu()
        self._menu_top.append("Properties", "dev.properties")
        menu.append_section(None, self._menu_top)
        copy = Gio.Menu()
        for name, label in (("copy", "Row"), ("copy-name", "Name"), ("copy-path", "sysfs Path"),
                            ("copy-devnodes", "Device Files"), ("copy-modalias", "Modalias")):
            copy.append(label, f"dev.{name}")
        section = Gio.Menu()
        section.append_submenu("Copy", copy)
        menu.append_section(None, section)
        return menu

    def _install_actions(self):
        group = Gio.SimpleActionGroup()
        goto = Gio.SimpleAction.new("goto", GLib.VariantType.new("i"))
        goto.connect("activate", lambda _a, pid: self.win.goto_process(pid.get_int32()))
        group.add_action(goto)
        props = Gio.SimpleAction.new("properties", None)
        props.connect("activate", lambda *_: self._open_properties(self.table.selected()))
        group.add_action(props)
        self._props_action = props
        self._copy_actions = []
        for name, value in (("copy-name", lambda d: d["name"]), ("copy-path", lambda d: d["syspath"]),
                            ("copy-devnodes", lambda d: "\n".join(d["devnodes"])),
                            ("copy-modalias", lambda d: d["modalias"])):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_a, v=value: self._copy(v))
            group.add_action(action)
            self._copy_actions.append((action, value))
        row = Gio.SimpleAction.new("copy", None)
        row.connect("activate", lambda *_: self.table.copy_selected())
        group.add_action(row)
        self.insert_action_group("dev", group)
        self._sync_menu()

    def _open_properties(self, data):
        if data is not None and data["key"] != ROOT_KEY:
            self.win.open_device_properties(data["key"])

    def _copy(self, value):
        data = self.table.selected()
        text = value(data) if data else None
        if text:
            pa.copy_text(text)

    def _sync_menu(self):
        """The processes of the selected device in the context menu, and which copies apply."""
        data = self.table.selected()
        for action, value in self._copy_actions:
            action.set_enabled(bool(data and value(data)))
        self._props_action.set_enabled(bool(data and data["key"] != ROOT_KEY))
        users = data["users"] if data else []
        items = []
        for pid in users[:MAX_USER_ITEMS]:
            item = self.win.tree.get(pid)
            name = item.name if item is not None else str(pid)
            items.append((f"Go to {name} ({pid})", f"dev.goto({pid})"))
        if len(users) > MAX_USER_ITEMS:
            items.append((f"{len(users) - MAX_USER_ITEMS} more", None))
        if items == self._user_items:
            return
        self._user_items = items
        for i in range(self._menu_top.get_n_items() - 1, 0, -1):  # item 0 is Properties
            self._menu_top.remove(i)
        for label, action in items:
            self._menu_top.append(label, action)

    # ------------------------------------------------------------------ polling
    def set_active(self, active):
        if active == self._active:
            return
        self._active = active
        if active:
            self._static = None  # devices may have come and gone meanwhile
            self.monitor.start()
            self.refresh(force=True)
            if not self._timer:
                self._timer = GLib.timeout_add_seconds(REFRESH_SECONDS, self._on_timer)
        else:
            self.monitor.stop()
            if self._timer:
                GLib.source_remove(self._timer)
                self._timer = 0
            self._irq_rates.reset()

    def teardown(self):
        self.set_active(False)

    def _on_timer(self):
        if self._active and not self.win.sampler.paused:
            self._ticks += 1
            if not self.monitor.running and self._ticks % RESCAN_TICKS_WITHOUT_EVENTS == 0:
                self._static = None
            self.refresh()
        return True

    def _on_hotplug(self):
        self._static = None
        if self._active:
            self.refresh(force=True)

    def _on_show_all(self, button):
        self.config.set("devices_show_all", button.get_active())
        self._static = None
        self.table.clear()  # a different set of rows, not devices that came: no flashing
        self.refresh(force=True)

    def refresh(self, force=False):
        """Read the live values again; force (F5, hotplug): also the tree."""
        if force:
            self._static = None
        if self._loading:
            if force:
                self._refresh_again = True
            return
        self._loading = True
        show_all = self.show_all.get_active()
        run_async(_load, lambda result, error: self._on_loaded(result, error, show_all),
                  self._static, show_all, self._elevated)

    def _on_loaded(self, result, error, show_all):
        self._loading = False
        again, self._refresh_again = self._refresh_again, False
        if not self._active:
            return
        if again:
            self.refresh()
        if error or result is None:
            return
        static, live, (users, denied) = result
        if show_all != self.show_all.get_active():
            return  # toggled meanwhile: the next refresh scans for the new setting
        if not again:  # else the tree is scanned again already
            self._static = static
        self.table.set_rows(self._rows(static, live, users), highlight=self.config.get("highlight_seconds") or None,
                            now=time.monotonic())
        self._update_status(static, live, denied)

    def _rows(self, static, live, users):
        rates = self._irq_rates.update({key: values["irq_count"] for key, values in live.items()})
        rows = []
        present = {base["key"] for base in static}
        for cache in (self._last_link, self._last_power):
            for key in [k for k in cache if k not in present]:
                del cache[key]
        for base in static:
            key = base["key"]
            values = live[key]
            if values["link"] is None:
                link, downgraded = self._last_link.get(key, ("", False))
            else:
                link, downgraded = values["link"], values["link_downgraded"]
                self._last_link[key] = (link, downgraded)
            power = values.get("power")
            if values.get("power_read", True):
                self._last_power[key] = power
            elif values["status"] != "Suspended":
                power = self._last_power.get(key)  # left alone this time so that it can suspend
            sensor = base.get("power_sensor")
            pids = sorted({pid for node in base["devnodes"] for pid in users.get(node, ())})
            names, detail = self._describe_users(pids, base["devnodes"], users)
            rows.append(dict(base, status=values["status"], runtime_pm=values["runtime_pm"], link=link,
                             link_downgraded=downgraded, irq_rate=rates.get(key), users=tuple(pids),
                             used_by=names, used_by_detail=detail, power=power,
                             power_label=sensor[1] if sensor else "", battery_status=values.get("battery_status"),
                             pm_control=values.get("pm_control", ""),
                             suspended_share=values.get("suspended_share")))
        return rows

    def _describe_users(self, pids, devnodes, users):
        """("pipewire, wireplumber", one line per process with the files it holds)."""
        if not pids:
            return "", ""
        names = {}
        lines = []
        for pid in pids:
            item = self.win.tree.get(pid)
            name = item.name if item is not None else f"PID {pid}"
            names[name] = names.get(name, 0) + 1
            held = [node for node in devnodes if pid in users.get(node, ())]
            lines.append(f"{name} ({pid}): {', '.join(held)}")
        text = ", ".join(f"{name} ×{n}" if n > 1 else name for name, n in sorted(names.items(),
                                                                                   key=lambda i: i[0].casefold()))
        return text, "\n".join(lines)

    def _update_status(self, static, live, denied):
        devices = sum(1 for row in static if row["bus"])
        # counted among the devices of the default view: "show all" adds parts that never get one
        missing = sum(1 for row in static if row["primary"] and live[row["key"]]["status"] == "No driver")
        text = f"{devices} devices"
        if missing:
            text += f" · {missing} without a driver"
        power = system_power_text(static, live)
        if power:
            text += f" · {power}"
        self.count.set_text(text)
        if denied and self._elevated is not None:
            self.banner.set_title(f"{denied} processes started since the check as administrator were not checked "
                                  "for open devices")
            self.banner.set_button_label("Check Again as Administrator")
        elif denied:
            self.banner.set_title(f"{denied} processes of other users were not checked for open devices")
            self.banner.set_button_label("Check as Administrator")
        elif self._elevated is not None:
            self.banner.set_title(f"Other users' processes as they were at {fmt_clock(self._elevated_at)}")
            self.banner.set_button_label("Check Again as Administrator")
        self.banner.set_revealed(bool(denied) or self._elevated is not None)

    def _elevate(self, *_):
        self.elevate()

    def elevate(self, then=None):
        """Read other users' descriptors as administrator; then() runs once they are in."""
        def done(result, error):
            if error:
                self.win.toast(f"Failed: {error}")
                return
            self._elevated = elevated_snapshot(result)
            self._elevated_at = time.time()
            if self._active:
                self.refresh()
            if then is not None:
                then()

        privileged.run(["handles"], done)

    def set_filter_text(self, text):
        self.table.set_filter_text(text)


def _load(static, show_all, elevated):
    """Worker: the tree (scanned again unless static is given), its live values and who uses it."""
    if static is None:
        static = scan(show_all)
    return static, read_live(static), device_users(elevated=elevated)
