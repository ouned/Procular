import os
from types import SimpleNamespace

import pytest

from procular.providers import devices


class FakeSys:
    """A small /sys: devices below <tmp>/sys/devices with subsystem, driver and module links."""

    def __init__(self, tmp_path):
        self.base = tmp_path / "sys"
        self.root = self.base / "devices"
        self.root.mkdir(parents=True)

    def dev(self, rel, kind=None, sub=None, driver=None, module=None, uevent=None, attrs=None):
        path = self.root / rel
        path.mkdir(parents=True, exist_ok=True)
        (path / "uevent").write_text("".join(f"{k}={v}\n" for k, v in (uevent or {}).items()))
        if sub is not None:
            target = self.base / kind / sub
            target.mkdir(parents=True, exist_ok=True)
            os.symlink(os.path.relpath(target, path), path / "subsystem")
        if driver is not None:
            drv = self.base / "bus" / (sub or "none") / "drivers" / driver
            drv.mkdir(parents=True, exist_ok=True)
            if module is not None and not (drv / "module").exists():
                mod = self.base / "module" / module
                mod.mkdir(parents=True, exist_ok=True)
                os.symlink(os.path.relpath(mod, drv), drv / "module")
            os.symlink(os.path.relpath(drv, path), path / "driver")
        for name, value in (attrs or {}).items():
            (path / name).parent.mkdir(parents=True, exist_ok=True)
            (path / name).write_text(value)
        return path

    def bus(self, rel, sub, driver=None, module=None, uevent=None, attrs=None):
        return self.dev(rel, "bus", sub, driver, module, uevent, attrs)

    def cls(self, rel, sub, uevent=None, attrs=None):
        return self.dev(rel, "class", sub, None, None, uevent, attrs)


PCI = "pci0000:00"
BRIDGE = f"{PCI}/0000:00:02.1"
NVME = f"{BRIDGE}/0000:01:00.0"
XHCI = f"{PCI}/0000:00:08.1"
RECEIVER = f"{XHCI}/usb1/1-1"
HID = f"{RECEIVER}/1-1:1.0/0003:046D:C52B.0001"
FINGERPRINT = f"{XHCI}/usb1/1-2"
HOST_BRIDGE = f"{PCI}/0000:00:00.0"
GPU = f"{BRIDGE}/0000:03:00.0"
BATTERY = "platform/PNP0C0A:00"


@pytest.fixture
def system(tmp_path):
    fs = FakeSys(tmp_path)
    fs.dev(PCI)
    fs.bus(HOST_BRIDGE, "pci", uevent={"PCI_CLASS": "60000", "PCI_ID": "1022:14B5", "MODALIAS": "pci:v1022"})
    fs.bus(BRIDGE, "pci", driver="pcieport", uevent={"PCI_CLASS": "60400", "PCI_ID": "1022:14BA"},
           attrs={"current_link_speed": "8.0 GT/s PCIe", "current_link_width": "4",
                  "max_link_speed": "16.0 GT/s PCIe", "max_link_width": "4",
                  "power/runtime_status": "active", "msi_irqs/40": ""})
    fs.bus(f"{BRIDGE}/0000:00:02.1:pcie001", "pci_express", driver="pcie_pme")
    fs.bus(NVME, "pci", driver="nvme", module="nvme",
           uevent={"PCI_CLASS": "10802", "PCI_ID": "144D:A808",
                   "MODALIAS": "pci:v0000144Dd0000A808sv0000144Dsd0000A801bc01sc08i02"},
           attrs={"current_link_speed": "8.0 GT/s PCIe", "current_link_width": "4",
                  "max_link_speed": "8.0 GT/s PCIe", "max_link_width": "4",
                  "power/runtime_status": "suspended", "msi_irqs/41": "", "msi_irqs/42": ""})
    fs.cls(f"{NVME}/nvme/nvme0", "nvme", uevent={"MAJOR": "240", "MINOR": "0", "DEVNAME": "nvme0"})
    fs.cls(f"{NVME}/nvme/nvme0/nvme0n1", "block", uevent={"MAJOR": "259", "MINOR": "0", "DEVNAME": "nvme0n1",
                                                           "DEVTYPE": "disk"}, attrs={"queue/rotational": "0"})
    fs.bus(GPU, "pci", driver="amdgpu", module="amdgpu", uevent={"PCI_CLASS": "30000", "PCI_ID": "1002:1681"},
           attrs={"power/runtime_status": "active", "power/control": "on", "power/runtime_active_time": "9000",
                  "power/runtime_suspended_time": "0"})
    fs.cls(f"{GPU}/hwmon/hwmon8", "hwmon", attrs={"name": "amdgpu", "temp1_input": "40000",
                                                 "power1_average": "2000000", "power1_input": "2726000",
                                                 "power1_label": "PPT"})
    fs.bus(XHCI, "pci", driver="xhci_hcd", module="xhci_pci", uevent={"PCI_CLASS": "C0330", "PCI_ID": "1022:161D"})
    fs.bus(f"{XHCI}/usb1", "usb", driver="usb", uevent={"DEVTYPE": "usb_device", "PRODUCT": "1d6b/2/612"},
           attrs={"version": " 2.00", "speed": "480", "power/runtime_status": "active"})
    fs.bus(f"{XHCI}/usb1/1-0:1.0", "usb", driver="hub", module="usbcore",
           uevent={"DEVTYPE": "usb_interface", "INTERFACE": "9/0/0", "MODALIAS": "usb:hub"})
    fs.bus(RECEIVER, "usb", driver="usb",
           uevent={"DEVTYPE": "usb_device", "PRODUCT": "46d/c52b/1203", "MAJOR": "189", "MINOR": "1",
                   "DEVNAME": "bus/usb/001/002"},
           attrs={"product": "Unifying Receiver", "manufacturer": "Logitech", "speed": "12"})
    fs.bus(f"{RECEIVER}/1-1:1.0", "usb", driver="usbhid", module="usbhid",
           uevent={"DEVTYPE": "usb_interface", "INTERFACE": "3/1/2", "MODALIAS": "usb:hid"})
    fs.bus(HID, "hid", driver="hid-generic", uevent={"HID_NAME": "Logitech USB Receiver"})
    fs.cls(f"{HID}/input/input5", "input", attrs={"name": "Logitech USB Receiver"})
    fs.cls(f"{HID}/input/input5/event3", "input", uevent={"MAJOR": "13", "MINOR": "67", "DEVNAME": "input/event3"})
    fs.bus(FINGERPRINT, "usb", driver="usb", uevent={"DEVTYPE": "usb_device", "PRODUCT": "27c6/521d/0"},
           attrs={"speed": "12", "idVendor": "27c6", "idProduct": "521d"})
    fs.bus(f"{FINGERPRINT}/1-2:1.0", "usb", uevent={"DEVTYPE": "usb_interface", "INTERFACE": "255/0/0",
                                                     "MODALIAS": "usb:fp"})
    fs.dev("platform")
    acpi_battery = fs.bus("LNXSYSTM:00/PNP0C0A:00", "acpi")
    fs.cls("LNXSYSTM:00/PNP0C0A:00/power_supply/BAT0", "power_supply",
           attrs={"type": "Battery", "status": "Discharging", "power_now": "5955000"})
    battery = fs.bus(BATTERY, "platform", driver="battery", module="battery")
    os.symlink(os.path.relpath(acpi_battery, battery), battery / "firmware_node")
    fs.bus("platform/pcspkr", "platform", uevent={"MODALIAS": "platform:pcspkr"})
    fs.bus("platform/serial8250", "platform", driver="serial8250", module="8250")
    fs.bus("platform/serial8250/serial8250:0", "serial-base", driver="ctrl")
    fs.cls("virtual/net/lo", "net", uevent={"INTERFACE": "lo", "IFINDEX": "1"})
    fs.bus("system/cpu/cpu0", "cpu", driver="processor")
    fs.dev("software", "bus", "event_source")

    udev = tmp_path / "udev"
    udev.mkdir()
    (udev / "+pci:0000:01:00.0").write_text("E:ID_MODEL_FROM_DATABASE=SSD 970 EVO\n"
                                             "E:ID_VENDOR_FROM_DATABASE=Samsung\n"
                                             "E:ID_PCI_SUBCLASS_FROM_DATABASE=Non-Volatile memory controller\n")
    (udev / "c13:67").write_text("E:ID_INPUT=1\nE:ID_INPUT_MOUSE=1\n")
    ids = tmp_path / "pci.ids"
    ids.write_text("# comment\n1022  Advanced Micro Devices, Inc. [AMD]\n\t14b5  Root Complex\n"
                   "\t14ba  GPP Bridge\n\t161d  USB controller\n144d  Samsung\nC 00  Unclassified\n")
    dmi = tmp_path / "dmi"
    dmi.mkdir()
    for name, value in (("sys_vendor", "LENOVO"), ("product_name", "21CQCTO1WW"),
                        ("product_version", "ThinkPad T14s Gen 3"), ("board_name", "21CQ")):
        (dmi / name).write_text(value + "\n")
    return SimpleNamespace(root=str(fs.root), udev=str(udev), dmi=str(dmi), ids=(str(ids),), tmp=tmp_path)


def _scan(system, show_all=False):
    rows = devices.scan(show_all, root=system.root, udev_dir=system.udev, dmi=system.dmi, pci_ids=system.ids,
                        usb_ids=())
    return {row["key"]: row for row in rows}


def test_default_tree_shows_the_hardware_where_it_is_connected(system):
    rows = _scan(system)
    assert set(rows) == {devices.ROOT_KEY, PCI, HOST_BRIDGE, BRIDGE, NVME, GPU, XHCI, f"{XHCI}/usb1", RECEIVER, HID,
                         FINGERPRINT, "platform", BATTERY, "platform/serial8250"}
    assert rows[devices.ROOT_KEY]["parent"] is None
    assert rows[PCI]["parent"] == devices.ROOT_KEY
    assert rows[NVME]["parent"] == BRIDGE
    # the USB interface is folded into its device: the HID device hangs below the receiver
    assert rows[HID]["parent"] == RECEIVER
    assert rows[RECEIVER]["parent"] == f"{XHCI}/usb1"


def test_names_types_and_drivers(system):
    rows = _scan(system)
    computer = rows[devices.ROOT_KEY]
    assert (computer["name"], computer["vendor"]) == ("ThinkPad T14s Gen 3", "LENOVO")
    assert rows[PCI]["name"] == "PCI bus 0000:00"
    assert rows["platform"]["name"] == "Platform devices"
    nvme = rows[NVME]
    assert (nvme["name"], nvme["vendor"], nvme["type"]) == ("SSD 970 EVO", "Samsung", "Non-Volatile memory controller")
    assert (nvme["driver"], nvme["module"]) == ("nvme", "nvme")
    assert nvme["interfaces"] == ("nvme0", "nvme0n1")
    assert nvme["devnodes"] == ("/dev/nvme0", "/dev/nvme0n1")
    assert nvme["icon"] == "drive-harddisk-solidstate-symbolic"
    assert nvme["irqs"] == (41, 42)
    # without udev: names from pci.ids, the type from the class code
    bridge = rows[BRIDGE]
    assert (bridge["name"], bridge["vendor"], bridge["type"]) == ("GPP Bridge", "Advanced Micro Devices, Inc. [AMD]",
                                                                  "PCI bridge")
    assert (bridge["driver"], bridge["module"]) == ("pcieport", "built-in")
    hub = rows[f"{XHCI}/usb1"]
    assert (hub["name"], hub["type"], hub["driver"], hub["link"]) == ("USB 2.0 root hub", "USB root hub", "hub",
                                                                      "480 Mb/s")
    receiver = rows[RECEIVER]
    # a USB device shows the drivers of its interfaces, not the generic "usb"
    assert (receiver["name"], receiver["vendor"], receiver["type"]) == ("Unifying Receiver", "Logitech", "HID")
    assert (receiver["driver"], receiver["module"], receiver["link"]) == ("usbhid", "usbhid", "12 Mb/s")
    assert receiver["devnodes"] == ("/dev/bus/usb/001/002",)
    hid = rows[HID]
    assert hid["name"] == "Logitech USB Receiver"
    assert hid["interfaces"] == ("event3",)
    assert hid["devnodes"] == ("/dev/input/event3",)
    assert hid["icon"] == "input-mouse-symbolic"
    assert rows[FINGERPRINT]["name"] == "USB device 27c6:521d"
    # the battery's power supply hangs below its ACPI node, which is not walked by default
    battery = rows[BATTERY]
    assert (battery["name"], battery["interfaces"], battery["icon"]) == ("Battery", ("BAT0",), "battery-symbolic")
    assert rows["platform/serial8250"]["name"] == "Serial ports"


def test_status_tells_missing_drivers_from_functions_that_need_none(system):
    rows = _scan(system)
    live = devices.read_live(list(rows.values()), proc=str(system.tmp))
    assert live[FINGERPRINT]["status"] == "No driver"  # no driver took its interface
    assert live[HOST_BRIDGE]["status"] == ""  # host bridges have no driver by design
    assert live[PCI]["status"] == ""
    assert live[BRIDGE]["status"] == "Active"
    assert live[NVME]["status"] == "Suspended"


def test_live_values_leave_suspended_devices_asleep(system):
    (system.tmp / "interrupts").write_text("           CPU0       CPU1\n"
                                           "  40:         10          5  IR-PCI-MSI  pcieport\n"
                                           "  41:        100        200  IR-PCI-MSI  nvme0q0\n"
                                           "  42:          1          2  IR-PCI-MSI  nvme0q1\n"
                                           " NMI:          3          4  Non-maskable interrupts\n")
    rows = _scan(system)
    live = devices.read_live(list(rows.values()), proc=str(system.tmp))
    assert live[BRIDGE]["link"] == "PCIe 3.0 x4 (of 4.0 x4)"
    assert live[BRIDGE]["link_downgraded"] is True
    assert live[BRIDGE]["irq_count"] == 15
    # reading the link registers would wake the suspended SSD: not read
    assert live[NVME]["link"] is None
    assert live[NVME]["irq_count"] == 303
    assert live[RECEIVER]["link"] == "12 Mb/s"


def test_show_all_lists_every_node(system):
    rows = _scan(system, show_all=True)
    for key in ("platform/pcspkr", f"{BRIDGE}/0000:00:02.1:pcie001", f"{RECEIVER}/1-1:1.0",
                f"{HID}/input/input5/event3", "virtual/net/lo", "system/cpu/cpu0", "LNXSYSTM:00/PNP0C0A:00",
                "platform/serial8250/serial8250:0"):
        assert key in rows, key
    assert rows[f"{RECEIVER}/1-1:1.0"]["parent"] == RECEIVER
    assert rows[f"{HID}/input/input5/event3"]["parent"] == f"{HID}/input/input5"
    assert rows[f"{HID}/input/input5/event3"]["devnodes"] == ("/dev/input/event3",)
    assert rows["virtual/net/lo"]["type"] == "Network interface"
    # top-level folders without a uevent file are containers too
    assert rows["virtual/net/lo"]["parent"] == "virtual"
    assert rows["virtual"]["parent"] == devices.ROOT_KEY
    assert rows["virtual"]["name"] == "Virtual devices"
    interface = rows[f"{RECEIVER}/1-1:1.0"]
    assert (interface["name"], interface["type"], interface["primary"]) == ("HID interface", "USB interface", False)
    assert rows[RECEIVER]["primary"]
    # the folded values stay with the device
    assert rows[RECEIVER]["driver"] == "usbhid"
    assert rows[HID]["interfaces"] == ("event3",)


def _stat(pid, flags=0, starttime=500):
    fields = ["S", "1", pid, pid, "0", "-1", str(flags)] + ["0"] * 12 + [str(starttime)] + ["0"] * 10
    return f"{pid} (proc) " + " ".join(str(f) for f in fields)


def test_device_users(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    for pid, links in ((100, {"3": "/dev/input/event3", "4": "/dev/null", "5": "socket:[7]"}),
                       (200, {}), (300, {})):
        fd = proc / str(pid) / "fd"
        fd.mkdir(parents=True)
        for num, target in links.items():
            os.symlink(target, fd / num)
    (proc / "200" / "stat").write_text(_stat(200, starttime=777))
    (proc / "300" / "stat").write_text(_stat(300, flags=0x00200000))  # a kernel thread
    (proc / "self").mkdir()
    listdir = os.listdir

    def guarded(path):
        if str(path).endswith(("/200/fd", "/300/fd")):
            raise PermissionError(path)
        return listdir(path)

    monkeypatch.setattr(devices.os, "listdir", guarded)
    users, denied = devices.device_users(str(proc))
    assert users == {"/dev/input/event3": {100}, "/dev/null": {100}}
    assert denied == 1  # 200; the kernel thread holds nothing
    elevated = {(200, 777): ([(5, "/dev/nvme0")], [], "", "", "")}
    users, denied = devices.device_users(str(proc), elevated)
    assert users["/dev/nvme0"] == {200}
    assert denied == 0


@pytest.mark.parametrize("args, expected", [
    (("16.0 GT/s PCIe", "16", "16.0 GT/s PCIe", "16"), ("PCIe 4.0 x16", False)),
    (("2.5 GT/s PCIe", "1", "8.0 GT/s PCIe", "4"), ("PCIe 1.0 x1 (of 3.0 x4)", True)),
    (("Unknown", "0", "8.0 GT/s PCIe", "4"), ("Link down", False)),
    ((None, None, None, None), ("", False)),
    (("20.0 GT/s PCIe", "2", "20.0 GT/s PCIe", "2"), ("20 GT/s x2", False)),
])
def test_fmt_pcie_link(args, expected):
    assert devices.fmt_pcie_link(*args) == expected


def test_small_parsers():
    assert devices.fmt_usb_speed("480") == "480 Mb/s"
    assert devices.fmt_usb_speed("5000") == "5 Gb/s"
    assert devices.fmt_usb_speed("1.5") == "1.5 Mb/s"
    assert devices.fmt_usb_speed(None) == ""
    assert sorted(["usb10", "usb2", "1-3.1", "1-3"], key=devices.natural_key) == ["1-3", "1-3.1", "usb2", "usb10"]
    assert devices.parse_interrupts("") == {}
    assert devices.parse_uevent_message(b"add@/devices/pci0000:00/usb1/1-1\0ACTION=add\0") == \
        ("add", "/devices/pci0000:00/usb1/1-1")
    assert devices.parse_uevent_message(b"libudev\0junk") is None
    assert devices.parse_udev_data("I:1\nE:A=b=c\nS:link\n") == {"A": "b=c"}


def test_lookup_ids_reads_vendors_and_devices_once(tmp_path):
    ids = tmp_path / "usb.ids"
    ids.write_text("046d  Logitech, Inc.\n\tc52b  Unifying Receiver\n\t\t0001 sub\n27c6  Shenzhen Goodix\n"
                   "C 00  (Defined at Interface level)\n\t01  not a device\n")
    found = devices.lookup_ids([str(tmp_path / "missing"), str(ids)], {(0x046d, 0xc52b), (0x27c6, None),
                                                                        (0x27c6, 0x521d), (0x1234, 1)})
    assert found == {(0x046d, 0xc52b): ("Logitech, Inc.", "Unifying Receiver"),
                     (0x27c6, None): ("Shenzhen Goodix", None)}


def test_machine_name_skips_firmware_placeholders(tmp_path):
    for name, value in (("sys_vendor", "System manufacturer"), ("product_name", "System Product Name"),
                        ("board_name", "PRIME X570-P")):
        (tmp_path / name).write_text(value)
    assert devices.machine_name(str(tmp_path)) == ("PRIME X570-P", "")


def test_counter_rates():
    rates = devices.CounterRates()
    assert rates.update({"a": 10, "b": None}, now=1.0) == {"a": None, "b": None}
    assert rates.update({"a": 30, "b": 5, "c": 1}, now=3.0) == {"a": 10.0, "b": None, "c": None}
    assert rates.update({"a": 5}, now=4.0) == {"a": None}  # the counter went back: no rate


class _FakeSocket:
    def __init__(self, messages):
        self.messages = list(messages)

    def recv(self, _size):
        if not self.messages:
            raise BlockingIOError
        return self.messages.pop(0)

    def close(self):
        pass


@pytest.mark.parametrize("message, relevant", [
    (b"add@/devices/pci0000:00/0000:00:08.1/usb1/1-1\0ACTION=add\0", True),
    (b"unbind@/devices/pci0000:00/0000:01:00.0\0", True),
    (b"change@/devices/pci0000:00/0000:00:08.1/drm/card1\0", False),  # a monitor plugged in
    (b"add@/devices/virtual/block/loop7\0", False),  # a snap mounted
    (b"remove@/devices/system/cpu/cpu3\0", False),
])
def test_uevent_monitor_reacts_to_devices_that_come_and_go(message, relevant):
    monitor = devices.UeventMonitor(lambda: None)
    monitor._sock = _FakeSocket([message])
    try:
        monitor._on_readable()
        assert all(monitor._timers) == relevant
    finally:
        monitor.stop()
    assert monitor._timers == [0, 0] and not monitor.running


def test_devices_view_rows_keep_the_last_link_and_name_users():
    from procular.widgets.devices_view import DevicesView

    names = {1: "pipewire", 2: "pipewire", 3: "gnome-shell"}
    view = SimpleNamespace(_irq_rates=devices.CounterRates(), _last_link={}, _last_power={},
                           win=SimpleNamespace(tree=SimpleNamespace(get=lambda pid: SimpleNamespace(name=names[pid]))))
    view._describe_users = lambda *args: DevicesView._describe_users(view, *args)
    static = [{"key": "gpu", "devnodes": ("/dev/dri/card1", "/dev/dri/renderD128")}]
    users = {"/dev/dri/card1": {3}, "/dev/dri/renderD128": {1, 2}}
    live = {"gpu": {"status": "Active", "runtime_pm": "active", "link": "PCIe 4.0 x16", "link_downgraded": False,
                    "irq_count": 10}}
    (row,) = DevicesView._rows(view, static, live, users)
    assert row["used_by"] == "gnome-shell, pipewire ×2"
    assert row["users"] == (1, 2, 3)
    assert row["used_by_detail"].splitlines()[0] == "pipewire (1): /dev/dri/renderD128"
    live["gpu"].update(status="Suspended", runtime_pm="suspended", link=None, irq_count=10)
    (row,) = DevicesView._rows(view, static, live, {})
    assert row["link"] == "PCIe 4.0 x16"  # as last read: not read while suspended
    assert (row["used_by"], row["users"]) == ("", ())


def _ancestors(rows, key):
    chain = []
    parent = rows[key]["parent"]
    while parent is not None:
        chain.insert(0, rows[parent])
        parent = rows[parent]["parent"]
    return chain


def _aliases(tmp_path):
    path = tmp_path / "modules.alias"
    path.write_text("# Aliases extracted from modules themselves.\n"
                    "alias pci:v0000144Dd0000A808sv*sd*bc*sc*i* nvme\n"
                    "alias pci:v*d*sv*sd*bc01sc08i02* nvme\n"
                    "alias usb:v046DpC52Bd*dc*dsc*dp*ic*isc*ip*in* hid_logitech_dj\n"
                    "alias usb:v*p*d*dc*dsc*dp*ic03isc*ip*in* usbhid\n"
                    "alias usb:v8086p*d*dc*dsc*dp*ic*isc*ip*in* not_this\n"
                    "malformed line\n")
    return devices.ModuleAliases(str(path))


def test_module_aliases_match_like_the_kernel(tmp_path):
    aliases = _aliases(tmp_path)
    assert aliases.modules("pci:v0000144Dd0000A808sv0000144Dsd0000A801bc01sc08i02") == ["nvme"]
    assert aliases.modules("usb:v046DpC52Bd1203dc00dsc00dp00ic03isc01ip02in00") == ["hid_logitech_dj", "usbhid"]
    assert aliases.modules("usb:v27C6p521Dd0100dcEFdsc00dp00icFFisc00ip00in00") == []
    assert aliases.modules("") == []
    assert devices.ModuleAliases(str(tmp_path / "missing")).modules("pci:v1") == []


def test_resource_and_interrupt_parsers():
    text = ("0x00000008e0000000 0x00000008efffffff 0x000000000014220c\n"
            "0x0000000000000000 0x0000000000000000 0x0000000000000000\n"
            "0x0000000000001000 0x00000000000010ff 0x0000000000040101\n")
    regions = devices.parse_resource(text)
    assert [(i, s, e) for i, s, e, _f in regions] == [(0, 0x8e0000000, 0x8efffffff), (2, 0x1000, 0x10ff)]
    assert devices.describe_region(*regions[0][1:]) == ("Memory", "64-bit, prefetchable")
    assert devices.describe_region(*regions[1][1:]) == ("I/O ports", "")
    lines = devices.parse_interrupt_lines(
        "           CPU0       CPU1\n"
        "   1:          0       1226  IR-IO-APIC    1-edge      i8042\n"
        " 123:        100         57  IR-PCI-MSIX-0000:33:00.0    0-edge      amdgpu\n"
        " 130:          1          2  PCI-MSI-0000:00:14.0   0-edge   xhci_hcd, usb_extra\n"
        " NMI:          0          0   Non-maskable interrupts\n")
    assert lines[1] == (1226, "IR-IO-APIC", "i8042")
    assert lines[123] == (157, "IR-PCI-MSIX-0000:33:00.0", "amdgpu")
    assert lines[130][2] == "xhci_hcd, usb_extra"


def test_device_details_of_a_suspended_ssd(system):
    (system.tmp / "interrupts").write_text("    CPU0\n 41: 7 PCI-MSI 0-edge nvme0q0\n 42: 3 PCI-MSI 1-edge nvme0q1\n")
    proc_dev = system.tmp / "1" / "fd"
    proc_dev.mkdir(parents=True)
    os.symlink("/dev/nvme0n1", proc_dev / "3")
    rows = _scan(system)
    nvme = rows[NVME]
    details = devices.device_details(nvme, _ancestors(rows, NVME), attributes=True, proc=str(system.tmp),
                                     aliases=_aliases(system.tmp), sys_module=str(system.tmp / "sys" / "module"),
                                     udev_dir=system.udev)
    assert details["exists"]
    assert (details["driver"], details["module"]) == ("nvme", "nvme")
    assert details["candidates"] == [("nvme", True)]
    assert [i["irq"] for i in details["irqs"]] == [41, 42]
    assert [(i["count"], i["handlers"]) for i in details["irqs"]] == [(7, "nvme0q0"), (3, "nvme0q1")]
    assert details["users"] == {1: ["/dev/nvme0n1"]}
    assert details["all_users"] == {"/dev/nvme0": set(), "/dev/nvme0n1": {1}}
    # suspended: the link registers are not read, neither for the Connection page nor as attributes
    assert details["link"] is None
    attributes = {(source, name): value for source, name, value in details["attributes"]}
    assert attributes[("sysfs", "current_link_speed")] == "(not read: it would wake the device)"
    assert attributes[("udev", "ID_MODEL_FROM_DATABASE")] == "SSD 970 EVO"
    assert attributes[("uevent", "PCI_ID")] == "144D:A808"
    assert attributes[("sysfs", "power/runtime_status")] == "suspended"
    assert attributes[("link", "driver")] == "nvme"
    # its parent bridge is awake: its link is read again for the path
    assert details["live"][BRIDGE]["link"] == "PCIe 3.0 x4 (of 4.0 x4)"


def test_device_details_of_a_usb_device_and_a_removed_one(system):
    rows = _scan(system)
    details = devices.device_details(rows[RECEIVER], aliases=_aliases(system.tmp), proc=str(system.tmp),
                                     sys_module=str(system.tmp / "sys" / "module"))
    assert details["interfaces"] == [{"name": "1-1:1.0", "class": "HID", "driver": "usbhid", "module": "usbhid",
                                      "modalias": "usb:hid"}]
    assert details["attrs"]["product"] == "Unifying Receiver"
    gone = dict(rows[RECEIVER], syspath=str(system.tmp / "nowhere"))
    assert devices.device_details(gone) == {"exists": False}


def test_missing_driver_text_names_the_cause():
    from procular.windows.device_properties import missing_driver_text

    assert "No kernel module" in missing_driver_text([])
    assert "not loaded" in missing_driver_text([("goodix", False)])
    assert "did not take the device" in missing_driver_text([("goodix", True)])


def test_device_properties_window_shows_the_details(gtk, system, tmp_path):
    from procular.config import Config
    from procular.windows.device_properties import DevicePropertiesWindow

    rows = _scan(system)
    config = Config(str(tmp_path / "settings.json"))
    names = {1: SimpleNamespace(name="fprintd", icon=None, rec={"uid": 0})}
    main = SimpleNamespace(config=config, tree=SimpleNamespace(get=names.get),
                           sampler=SimpleNamespace(paused=True), devices_view=SimpleNamespace(_elevated=None),
                           open_device_properties=lambda key: None, goto_process=lambda pid: None,
                           open_properties=lambda pid: None)
    proc_fd = system.tmp / "1" / "fd"
    proc_fd.mkdir(parents=True)
    os.symlink("/dev/bus/usb/001/003", proc_fd / "4")
    row = dict(rows[FINGERPRINT], devnodes=("/dev/bus/usb/001/003",))
    win = DevicePropertiesWindow(main, row, _ancestors(rows, FINGERPRINT))
    try:
        details = devices.device_details(row, win.ancestors, attributes=True, proc=str(system.tmp),
                                         aliases=_aliases(system.tmp), udev_dir=system.udev)
        win._apply(details)
        general = win.pages["general"]
        assert general.missing.get_visible()
        assert "No kernel module" in general.missing_text.get_text()
        assert general.rows["ids"].value.get_text() == "27c6:521d"
        assert not general.rows["driver"].get_visible()  # a USB device: the interfaces' drivers instead
        connection = win.pages["connection"]
        assert list(connection.hops) == [devices.ROOT_KEY, PCI, XHCI, f"{XHCI}/usb1", FINGERPRINT]
        processes = win.pages["processes"].table.all_rows()
        assert [(p["name"], p["devnodes"]) for p in processes] == [("fprintd", "/dev/bus/usb/001/003")]
        assert win.badges["processes"].get_text() == "1"
        assert win.pages["attributes"].loaded
        win._apply({"exists": False})
        assert win.removed and win.banner.get_revealed() and not win._timer
    finally:
        win.close()
    config.save_now()


def test_power_is_shown_where_it_is_measured(system):
    rows = _scan(system)
    gpu = rows[GPU]
    assert gpu["power_sensor"] == (f"{system.root}/{GPU}/hwmon/hwmon8/power1_input", "PPT")  # now, not the average
    assert rows[BATTERY]["battery"].endswith("power_supply/BAT0")
    assert rows[NVME]["power_sensor"] is None and rows[NVME]["battery"] is None
    live = devices.read_live(list(rows.values()), proc=str(system.tmp), pacer=devices.SensorPacer())
    assert live[GPU]["power"] == pytest.approx(2.726)
    assert (live[BATTERY]["power"], live[BATTERY]["battery_status"]) == (pytest.approx(5.955), "Discharging")
    assert live[NVME]["power"] is None and live[NVME]["power_read"]
    assert devices.system_power_text(list(rows.values()), live) == "6.0 W from battery"
    # suspended share: the SSD sleeps; the GPU never suspends ("on")
    assert live[GPU]["pm_control"] == "on" and live[GPU]["suspended_share"] == 0.0


def test_battery_power_from_current_and_voltage(tmp_path):
    for name, value in (("status", "Charging"), ("current_now", "-2000000"), ("voltage_now", "15000000")):
        (tmp_path / name).write_text(value)
    assert devices.battery_power(str(tmp_path)) == (30.0, "Charging")
    rows = [{"key": "bat"}]
    assert devices.system_power_text(rows, {"bat": {"power": 30.0, "battery_status": "Charging"}}) == \
        "charging at 30.0 W"
    assert devices.system_power_text(rows, {"bat": {"power": 0.0, "battery_status": "Full"}}) == ""


def test_sensor_pacer_lets_an_idle_device_suspend():
    pacer = devices.SensorPacer()
    # never suspends: read on every refresh
    assert all(pacer.may_read("igpu", "active", "on", None, now=t) for t in (0, 2, 4))
    assert pacer.may_read("x", "unsupported", "auto", "5000", now=0)
    # may suspend after 5 s idle: read, then left alone for longer than that
    assert pacer.may_read("dgpu", "active", "auto", "5000", now=0)
    assert not pacer.may_read("dgpu", "active", "auto", "5000", now=2)
    assert not pacer.may_read("dgpu", "active", "auto", "5000", now=5.9)
    assert pacer.may_read("dgpu", "active", "auto", "5000", now=6.1)
    assert not pacer.may_read("dgpu", "suspended", "auto", "5000", now=20)
    assert pacer.may_read("dgpu", "active", "auto", "-1", now=21)  # no autosuspend


def test_suspended_share():
    assert devices.suspended_share("1000", "3000") == 0.75
    assert devices.suspended_share("0", "0") is None
    assert devices.suspended_share(None, "5") is None


def test_devices_view_keeps_a_paced_reading():
    from procular.widgets.devices_view import DevicesView, _power_text, _suspended_text

    view = SimpleNamespace(_irq_rates=devices.CounterRates(), _last_link={}, _last_power={},
                           win=SimpleNamespace(tree=SimpleNamespace(get=lambda pid: None)))
    view._describe_users = lambda *args: DevicesView._describe_users(view, *args)
    static = [{"key": "gpu", "devnodes": (), "power_sensor": ("/x/power1_input", "PPT"), "battery": None}]
    live = {"gpu": {"status": "Active", "runtime_pm": "active", "pm_control": "auto", "suspended_share": 0.25,
                    "link": "", "link_downgraded": False, "irq_count": None, "power": 12.5, "power_read": True,
                    "battery_status": None}}
    (row,) = DevicesView._rows(view, static, live, {})
    assert (_power_text(row), _suspended_text(row), row["power_label"]) == ("12.5 W", "25 %", "PPT")
    live["gpu"].update(power=None, power_read=False)  # left alone so that it can suspend
    (row,) = DevicesView._rows(view, static, live, {})
    assert _power_text(row) == "12.5 W"
    live["gpu"].update(status="Suspended", runtime_pm="suspended")
    (row,) = DevicesView._rows(view, static, live, {})
    assert _power_text(row) == ""
    live["gpu"].update(status="Active", pm_control="on")
    (row,) = DevicesView._rows(view, static, live, {})
    assert _suspended_text(row) == "Always on"


def test_uevent_monitor_stops_on_fatal_socket_error():
    import errno

    class BrokenSocket:
        def __init__(self, err):
            self.err = err
            self.closed = False

        def recv(self, _n):
            raise OSError(self.err, "broken")

        def close(self):
            self.closed = True

    sock = BrokenSocket(errno.EBADF)
    monitor = devices.UeventMonitor(lambda: None)
    monitor._sock = sock
    assert monitor._on_readable() is False
    assert sock.closed and not monitor.running


def test_device_properties_elevate_ignores_destroyed_window():
    from procular.windows.device_properties import DevicePropertiesWindow

    called = []
    callbacks = []
    fake = SimpleNamespace(
        _destroyed=True,
        main=SimpleNamespace(devices_view=SimpleNamespace(elevate=lambda cb: callbacks.append(cb))),
        refresh=lambda: called.append(True),
    )
    DevicePropertiesWindow.elevate(fake)
    assert len(callbacks) == 1
    callbacks[0]()
    assert not called
