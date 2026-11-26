"""Hardware devices: the kernel's device tree in /sys/devices, named from udev's database.

/sys/devices is laid out the way the hardware is connected: a PCI root, its bridges, a USB
controller behind one, its hubs, a device on a hub port, that device's interfaces, and below
them what the drivers made of it (a network interface, a disk, an input event device). Its
directories are "bus devices" (their `subsystem` link points into /sys/bus: pci, usb, i2c, …),
which drivers bind to, and "class devices" (into /sys/class: net, block, input, …), which are
what a driver offers to the rest of the system.

scan() turns the tree into rows for a table: by default the bus devices that matter, with the
class devices folded into the device that owns them; with show_all, every node. read_live()
reads what changes while a device stays plugged in (power state, PCIe link, interrupt counts),
and device_users() finds the processes that hold a device file open. All three run in a worker
and return plain data. UeventMonitor reports hotplug events on the main loop.
"""

import errno
import fnmatch
import os
import re
import socket
import stat
import threading
import time

from gi.repository import GLib

from .handles import _stat_info

SYS_DEVICES = "/sys/devices"
UDEV_DATA = "/run/udev/data"
DMI_ID = "/sys/class/dmi/id"
PCI_IDS = ("/usr/share/hwdata/pci.ids", "/usr/share/misc/pci.ids", "/usr/share/pci.ids")
USB_IDS = ("/usr/share/hwdata/usb.ids", "/usr/share/misc/usb.ids", "/usr/share/usb.ids")

ROOT_KEY = "/"  # the row of the computer itself, parent of everything

# Folders of attributes, never of devices: not walked
_ATTRIBUTE_DIRS = frozenset(("power", "msi_irqs", "statistics", "queues", "queue", "mq", "holders", "slaves",
                             "trace", "integrity", "firmware_node", "subsystem", "driver", "device", "of_node",
                             "iommu_group", "iommu", "physfn", "consumers", "suppliers"))
# Top-level folders that hold no hardware: walked only for "show all"
_SOFTWARE_TOP = ("system", "virtual", "faux", "LNXSYSTM")
# Bus devices that are parts of others rather than devices: shown only for "show all"
#   pci_express: the services of a PCIe port (power management events, hotplug, AER)
#   serial-base: the internal controller/port objects of a serial driver
#   wmi: the firmware's WMI methods and data blocks
#   event_source: performance counters; workqueue, cpu, memory, …: /sys/devices/system
#   acpi: the firmware's description of the hardware (/sys/devices/LNXSYSTM:00); the devices
#     themselves are platform, pnp or pci devices that point to it (firmware_node)
_HIDDEN_BUSES = frozenset(("acpi", "pci_express", "serial-base", "wmi", "event_source", "workqueue", "cpu", "memory",
                           "node", "machinecheck", "clockevents", "clocksource", "container", "gpio", "faux",
                           "nvmem", "regulator", "soc"))
# Buses whose devices are real hardware even without a driver: shown always, and "No driver"
# when no driver took them
_HARDWARE_BUSES = frozenset(("pci", "usb", "thunderbolt", "scsi", "mmc", "sdio", "virtio", "hid", "hdaudio"))

# Class devices named in a device's Interfaces column (the rest only count for "Used by")
_LISTED_CLASSES = frozenset(("net", "block", "drm", "sound", "input", "video4linux", "hidraw", "bluetooth",
                             "power_supply", "nvme", "tpm", "backlight", "usbmisc", "printer", "tty", "mmc_host"))

BUS_TYPES = {
    "pci": "PCI device", "usb": "USB device", "hid": "HID device", "hdaudio": "Audio codec",
    "i2c": "I²C device", "serio": "PS/2 device", "platform": "Platform device", "pnp": "Plug and Play device",
    "acpi": "ACPI object", "thunderbolt": "Thunderbolt", "mhi": "Modem host interface", "auxiliary": "Auxiliary device",
    "scsi": "SCSI device", "mmc": "Memory card", "sdio": "SDIO device", "virtio": "VirtIO device",
    "spi": "SPI device", "typec": "USB Type-C", "nvme": "NVMe", "mdio_bus": "Ethernet PHY", "soundwire": "SoundWire",
    "i3c": "I3C device", "ac97": "AC'97 codec", "media": "Media controller", "mei": "Management engine client",
    "wmi": "WMI block", "pci_express": "PCIe port service", "serial-base": "Serial port object",
    "event_source": "Performance counters",
}
CLASS_TYPES = {
    "net": "Network interface", "block": "Block device", "drm": "Graphics", "sound": "Sound", "input": "Input",
    "hidraw": "Raw HID", "video4linux": "Video", "bluetooth": "Bluetooth adapter", "power_supply": "Power supply",
    "nvme": "NVMe controller", "tpm": "TPM", "backlight": "Backlight", "tty": "Terminal", "usbmisc": "USB",
    "printer": "Printer", "mmc_host": "Card reader", "hwmon": "Sensors", "thermal": "Thermal",
    "leds": "LED", "rfkill": "Radio switch", "i2c-dev": "I²C device file", "misc": "Misc device",
}

PCI_CLASSES = {
    0x01: "Storage controller", 0x02: "Network controller", 0x03: "Display controller",
    0x04: "Multimedia controller", 0x05: "Memory controller", 0x06: "Bridge", 0x07: "Communication controller",
    0x08: "System peripheral", 0x09: "Input controller", 0x0a: "Docking station", 0x0b: "Processor",
    0x0c: "Serial bus controller", 0x0d: "Wireless controller", 0x0e: "Intelligent controller",
    0x0f: "Satellite controller", 0x10: "Encryption controller", 0x11: "Signal processing controller",
    0x12: "Processing accelerator", 0x13: "Instrumentation",
}
PCI_SUBCLASSES = {
    (0x01, 0x06): "SATA controller", (0x01, 0x08): "NVMe controller", (0x02, 0x00): "Ethernet controller",
    (0x02, 0x80): "Network controller", (0x03, 0x00): "VGA compatible controller", (0x04, 0x03): "Audio device",
    (0x06, 0x00): "Host bridge", (0x06, 0x01): "ISA bridge", (0x06, 0x04): "PCI bridge",
    (0x0c, 0x03): "USB controller", (0x0c, 0x05): "SMBus", (0x0c, 0x0a): "USB4 host interface",
}
USB_CLASSES = {
    0x01: "Audio", 0x02: "Communications", 0x03: "HID", 0x05: "Physical", 0x06: "Imaging", 0x07: "Printer",
    0x08: "Mass storage", 0x09: "Hub", 0x0a: "CDC data", 0x0b: "Smart card", 0x0d: "Content security",
    0x0e: "Video", 0x0f: "Healthcare", 0x10: "Audio/video", 0x11: "Billboard", 0x12: "USB Type-C bridge",
    0xdc: "Diagnostic", 0xe0: "Wireless", 0xef: "Miscellaneous", 0xfe: "Application specific",
    0xff: "Vendor specific",
}
# Well-known ACPI and PnP hardware IDs, for the platform devices the firmware describes
ACPI_NAMES = {
    "PNP0C0A": "Battery", "ACPI0003": "AC adapter", "PNP0C0D": "Lid switch", "PNP0C0C": "Power button",
    "LNXPWRBN": "Power button", "PNP0C0E": "Sleep button", "LNXSLPBN": "Sleep button",
    "PNP0C09": "Embedded controller", "PNP0B00": "Real-time clock", "PNP0103": "High precision event timer",
    "PNP0100": "System timer", "PNP0800": "PC speaker", "PNP0C04": "Math coprocessor", "PNP0C14": "WMI",
    "PNP0C0B": "Fan", "LNXTHERM": "Thermal zone", "LNXVIDEO": "Video bus", "PNP0A03": "PCI root bridge",
    "PNP0A08": "PCI Express root bridge", "PNP0C02": "Motherboard resources", "PNP0C01": "System board",
    "PNP0200": "DMA controller", "PNP0000": "Interrupt controller", "PNP0303": "PS/2 keyboard",
    "PNP0F13": "PS/2 mouse", "PNP0501": "Serial port", "PNP0400": "Parallel port", "PNP0401": "Parallel port",
    "MSFT0101": "TPM 2.0", "ACPI000E": "Time and alarm device", "ACPI0007": "Processor",
    "ACPI0010": "Processor container", "PNP0C50": "I²C HID device", "ACPI0008": "Ambient light sensor",
    "LNXSYSTM": "ACPI namespace", "LNXSYBUS": "ACPI system bus", "PNP0C0F": "PCI interrupt link", "USBC000": "USB Type-C connector", "AMDI0010": "I²C controller",
    "AMDI0030": "GPIO controller", "AMDI0007": "Power management controller", "INT33D5": "HID event filter",
    "INTC1051": "HID event filter", "INT0800": "Firmware hub", "PNP0C33": "Error device",
}
PLATFORM_NAMES = {
    "i8042": "PS/2 controller", "serial8250": "Serial ports", "pcspkr": "PC speaker", "rtc-efi": "EFI real-time clock",
    "efi-framebuffer": "EFI framebuffer", "simple-framebuffer": "Firmware framebuffer",
    "intel_rapl_msr": "RAPL power meter", "thinkpad_acpi": "ThinkPad extras", "thinkpad_hwmon": "ThinkPad sensors",
    "kgdboc": "Kernel debugger console", "alarmtimer": "Alarm timer", "coretemp": "CPU temperature sensor",
    "efivars": "EFI variables", "acpi-cpufreq": "CPU frequency control", "regulatory": "Wireless regulatory database",
}
# DMI strings that firmware leaves as placeholders
_DMI_JUNK = ("to be filled by o.e.m.", "system product name", "system manufacturer", "default string",
             "not applicable", "not specified", "none", "o.e.m.", "")

_NUM_RE = re.compile(r"(\d+)")
_LINK_GEN = {2.5: "1.0", 5.0: "2.0", 8.0: "3.0", 16.0: "4.0", 32.0: "5.0", 64.0: "6.0"}


def natural_key(text):
    """Sort key that puts "usb2" before "usb10" and "1-3" before "1-3.1"."""
    return tuple((0, int(part), "") if part.isdigit() else (1, 0, part.casefold())
                 for part in _NUM_RE.split(text or "") if part)


def _read(path):
    try:
        with open(path, "rb") as fh:
            return fh.read().decode("utf-8", "replace").strip()
    except OSError:
        return None


def _readlink(path):
    try:
        return os.readlink(path)
    except OSError:
        return None


def parse_uevent(text):
    out = {}
    for line in (text or "").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key] = value
    return out


def parse_udev_data(text):
    """The E: (property) lines of a /run/udev/data file."""
    out = {}
    for line in (text or "").splitlines():
        if line.startswith("E:"):
            key, sep, value = line[2:].partition("=")
            if sep:
                out[key] = value
    return out


def parse_interrupts(text):
    """{irq number: count summed over the CPUs} from /proc/interrupts (named lines like NMI left out)."""
    lines = (text or "").splitlines()
    if not lines:
        return {}
    ncpu = len(lines[0].split())
    counts = {}
    for line in lines[1:]:
        head, sep, rest = line.partition(":")
        head = head.strip()
        if not sep or not head.isdigit():
            continue
        total = 0
        for field in rest.split()[:ncpu]:
            if not field.isdigit():
                break
            total += int(field)
        counts[int(head)] = total
    return counts


def lookup_ids(paths, wanted):
    """Names from a pci.ids / usb.ids file: {(vendor, device): (vendor name, device name)}.

    wanted: set of (vendor, device) ints; device None asks only for the vendor. The file is read
    once, for all of them."""
    found = {}
    if not wanted:
        return found
    by_vendor = {}
    for ven, dev in wanted:
        by_vendor.setdefault(f"{ven:04x}", set()).add(None if dev is None else f"{dev:04x}")
    for path in paths:
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            vendor = vendor_name = None
            for line in fh:
                if not line.strip() or line.startswith("#"):
                    continue
                if not line[0].isspace():
                    if line.startswith("C "):  # the class lists after the vendors
                        break
                    vendor = line[:4].lower()
                    if vendor in by_vendor:
                        vendor_name = line[4:].strip()
                        if None in by_vendor[vendor]:
                            found[(int(vendor, 16), None)] = (vendor_name, None)
                    else:
                        vendor = None
                elif vendor is not None and line.startswith("\t") and not line.startswith("\t\t"):
                    dev = line[1:5].lower()
                    if dev in by_vendor[vendor]:
                        found[(int(vendor, 16), int(dev, 16))] = (vendor_name, line[5:].strip())
        break
    return found


def _hex(text):
    try:
        return int(text, 16)
    except (TypeError, ValueError):
        return None


def fmt_usb_speed(text):
    """The `speed` attribute of a USB device (Mb/s) as "480 Mb/s", "5 Gb/s"."""
    try:
        mbps = float(text)
    except (TypeError, ValueError):
        return ""
    if mbps >= 1000:
        return f"{mbps / 1000:g} Gb/s"
    return f"{mbps:g} Mb/s"


def _pcie_speed(text):
    """"16.0 GT/s PCIe" -> 16.0; None for "Unknown" or a link that is down."""
    try:
        return float((text or "").split()[0])
    except (IndexError, ValueError):
        return None


def fmt_pcie_link(cur_speed, cur_width, max_speed, max_width):
    """("PCIe 4.0 x16", downgraded) from the four link attributes of a PCIe device.

    downgraded: the link runs slower or narrower than both ends can, which a GPU does to save
    power while idle, but a busy device should not."""
    speed, top = _pcie_speed(cur_speed), _pcie_speed(max_speed)
    try:
        width, top_width = int(cur_width), int(max_width)
    except (TypeError, ValueError):
        width = top_width = None
    if not speed or not width:
        return ("Link down" if top else ""), False

    def name(s, w):
        gen = _LINK_GEN.get(s)
        return f"PCIe {gen} x{w}" if gen else f"{s:g} GT/s x{w}"

    text = name(speed, width)
    downgraded = bool(top and top_width and (speed < top or width < top_width))
    if downgraded:
        text += f" (of {name(top, top_width).removeprefix('PCIe ')})"
    return text, downgraded


class _Node:
    __slots__ = ("rel", "path", "name", "parent", "kind", "sub", "driver", "ue", "shown", "owner", "_udev",
                 "classes", "ifaces")

    def __init__(self, rel, path, parent, kind, sub, driver, ue):
        self.rel = rel
        self.path = path
        self.name = os.path.basename(rel)
        self.parent = parent  # rel of the nearest ancestor that is a node, or None at the top
        self.kind = kind  # "bus", "class" or None (no subsystem: a container or a structural node)
        self.sub = sub
        self.driver = driver
        self.ue = ue
        self.shown = False
        self.owner = None  # the default-shown node that class devices and interfaces are folded into
        self._udev = None
        self.classes = []  # class devices owned
        self.ifaces = []  # USB interfaces owned

    @property
    def devtype(self):
        return self.ue.get("DEVTYPE", "")

    def attr(self, name):
        return _read(f"{self.path}/{name}")

    def udev(self, udev_dir):
        if self._udev is None:
            ue = self.ue
            if "MAJOR" in ue and "MINOR" in ue:
                ident = f"{'b' if self.sub == 'block' else 'c'}{ue['MAJOR']}:{ue['MINOR']}"
            elif self.sub == "net" and "IFINDEX" in ue:
                ident = f"n{ue['IFINDEX']}"
            else:
                ident = f"+{self.sub}:{self.name}"
            self._udev = parse_udev_data(_read(f"{udev_dir}/{ident}"))
        return self._udev

    @property
    def devnode(self):
        name = self.ue.get("DEVNAME")
        return f"/dev/{name}" if name and not name.startswith("/") else name


def _skip_top(root, name):
    if name.startswith(_SOFTWARE_TOP):
        return True
    link = _readlink(f"{root}/{name}/subsystem")
    return link is not None and os.path.basename(link) == "event_source"


def walk(root=SYS_DEVICES, show_all=False):
    """{rel path: _Node} of every directory with a uevent file, parents before children."""
    nodes = {}
    for dirpath, dirs, files in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        if rel == ".":
            if not show_all:
                dirs[:] = [d for d in dirs if not _skip_top(root, d)]
            continue
        dirs[:] = [d for d in dirs if d not in _ATTRIBUTE_DIRS and (show_all or d != "wakeup")]
        if "uevent" not in files and os.sep in rel:
            continue  # a folder that groups nodes; at the top it is a container (virtual, system)
        parent = os.path.dirname(rel)
        while parent and parent not in nodes:
            parent = os.path.dirname(parent)
        nodes[rel] = _make_node(dirpath, rel, parent or None)
    return nodes


def _make_node(path, rel, parent):
    link = _readlink(f"{path}/subsystem")
    kind = sub = None
    if link is not None:
        sub = os.path.basename(link)
        kind = "class" if "/class/" in link else "bus"
    driver = _readlink(f"{path}/driver")
    return _Node(rel, path, parent, kind, sub, driver and os.path.basename(driver), parse_uevent(_read(f"{path}/uevent")))


def _firmware_classes(node, nodes, root):
    """Class devices that hang below a device's ACPI node instead of below the device: the
    battery's power supply (BAT0) is one."""
    fw = os.path.realpath(f"{node.path}/firmware_node")
    if fw == os.path.realpath(f"{node.path}") or not os.path.isdir(f"{fw}/power_supply"):
        return []
    found = []
    for name in sorted(os.listdir(f"{fw}/power_supply")):
        path = f"{fw}/power_supply/{name}"
        rel = os.path.relpath(path, os.path.realpath(root))
        found.append(nodes.get(rel) or _make_node(path, rel, None))
    return found


def _default_rule(node):
    """True: shown, False: hidden, None: shown if something below it is."""
    if node.kind == "bus":
        if node.sub in _HIDDEN_BUSES or node.devtype == "usb_interface":
            return False
        if node.driver or node.sub in _HARDWARE_BUSES:
            return True
        return None
    if node.kind is None and node.parent is None:
        return None  # a top-level container: pci0000:00, platform, pnp0
    return False


def _mark_shown(nodes, root):
    """Set node.shown for the default view and node.owner for every node."""
    with_shown = set()
    for node in reversed(list(nodes.values())):  # children before parents
        rule = _default_rule(node)
        node.shown = rule if rule is not None else node.rel in with_shown
        if node.shown:
            parent = node.parent
            while parent is not None and parent not in with_shown:
                with_shown.add(parent)
                parent = nodes[parent].parent
    for node in nodes.values():  # parents first, so an owner is known before its descendants
        if node.shown:
            node.owner = node.rel
        else:
            node.owner = nodes[node.parent].owner if node.parent is not None else None
            if node.owner is not None:
                owner = nodes[node.owner]
                if node.devtype == "usb_interface" and node.parent == owner.rel:
                    owner.ifaces.append(node)
                elif node.kind == "class":
                    owner.classes.append(node)
    for node in nodes.values():
        if node.shown and node.sub in ("platform", "pnp"):
            node.classes += [c for c in _firmware_classes(node, nodes, root) if c not in node.classes]


def _listed(node):
    """Whether a class device is named in its owner's Interfaces column."""
    sub, name = node.sub, node.name
    if sub not in _LISTED_CLASSES:
        return False
    if sub == "block":
        return node.devtype == "disk"
    if sub == "drm":
        return node.devtype == "drm_minor"
    if sub == "sound":
        return name.startswith("card")
    if sub == "input":
        return name.startswith("event")
    if sub == "tty":
        return name.startswith(("ttyUSB", "ttyACM"))
    return True


def _unique(items):
    return tuple(dict.fromkeys(i for i in items if i))


class _Describer:
    """Names, types and icons; reads udev's database and the ids files at most once per node."""

    def __init__(self, nodes, udev_dir, pci_ids, usb_ids):
        self.nodes = nodes
        self.udev_dir = udev_dir
        self.pci_ids = pci_ids
        self.usb_ids = usb_ids
        self._ids = None

    def udev(self, node):
        return node.udev(self.udev_dir)

    def ids(self, node):
        """(vendor name, device name) from pci.ids / usb.ids, for nodes udev did not name."""
        if self._ids is None:
            pci, usb = set(), set()
            for n in self.nodes.values():
                if not n.shown or self.udev(n).get("ID_MODEL_FROM_DATABASE"):
                    continue
                ven, dev = self._vendor_device(n)
                if ven is not None:
                    (pci if n.sub == "pci" else usb).add((ven, dev))
            self._ids = {"pci": lookup_ids(self.pci_ids, pci), "usb": lookup_ids(self.usb_ids, usb)}
        ven, dev = self._vendor_device(node)
        if ven is None or node.sub not in ("pci", "usb"):
            return None, None
        return self._ids[node.sub].get((ven, dev), (None, None))

    @staticmethod
    def _vendor_device(node):
        if node.sub == "pci":
            ven, _, dev = node.ue.get("PCI_ID", "").partition(":")
            return _hex(ven), _hex(dev)
        if node.sub == "usb" and node.devtype == "usb_device":
            parts = node.ue.get("PRODUCT", "").split("/")
            if len(parts) >= 2:
                return _hex(parts[0]), _hex(parts[1])
        return None, None

    def _input_name(self, node):
        for c in node.classes:
            if c.sub == "input" and c.name.startswith("input"):
                name = c.attr("name")
                if name:
                    return name
        return None

    @staticmethod
    def _acpi_name(node):
        """The name of a well-known ACPI / PnP hardware ID of the node, or None."""
        if node.sub == "pnp":
            ids = (node.attr("id") or "").split()  # a vendor's own ID, then the standard ones
        elif node.sub in ("platform", "acpi"):
            ids = [node.name.split(":")[0]]
        else:
            return None
        for hid in ids:
            name = ACPI_NAMES.get(hid.upper())
            if name:
                return name
        return None

    def is_root_hub(self, node):
        return node.sub == "usb" and node.devtype == "usb_device" and node.name.startswith("usb")

    def name(self, node):
        sub, udev = node.sub, self.udev(node)
        if sub == "pci":
            return (udev.get("ID_MODEL_FROM_DATABASE") or self.ids(node)[1]
                    or f"PCI device {node.ue.get('PCI_ID', '').lower()}")
        if sub == "usb" and node.devtype == "usb_device":
            if self.is_root_hub(node):
                try:
                    return f"USB {float(node.attr('version')):.1f} root hub"
                except (TypeError, ValueError):
                    return "USB root hub"
            ven, dev = self._vendor_device(node)
            return (node.attr("product") or udev.get("ID_MODEL_FROM_DATABASE") or self.ids(node)[1]
                    or self._class_name(node)
                    or (f"USB device {ven:04x}:{dev:04x}" if ven is not None and dev is not None else "USB device"))
        if sub == "usb" and node.devtype == "usb_interface":
            parts = node.ue.get("INTERFACE", "").split("/")
            cls = int(parts[0]) if parts and parts[0].isdigit() else None
            if cls == 0xe0 and parts[1:3] == ["1", "1"]:
                return "Bluetooth interface"
            return f"{USB_CLASSES.get(cls, 'USB')} interface"
        if sub == "hid" and node.ue.get("HID_NAME"):
            return node.ue["HID_NAME"]
        if sub == "hdaudio":
            name = " ".join(filter(None, (node.attr("vendor_name"), node.attr("chip_name"))))
            if name:
                return name
        if sub == "thunderbolt":
            if node.devtype == "thunderbolt_domain":
                return "Thunderbolt domain"
            name = node.attr("device_name")
            if name:
                return name
        if sub == "scsi":
            model = node.attr("model")
            if model:
                return model
        if sub == "serio":
            name = self._input_name(node) or node.attr("description")
            if name:
                return name
        acpi = self._acpi_name(node)
        if acpi:
            return acpi
        if sub == "platform":
            base = re.sub(r"(\.(\d+|auto))+$", "", node.name)
            if base in PLATFORM_NAMES:
                return PLATFORM_NAMES[base]
        if node.kind == "class":
            return node.attr("name") if sub == "input" and node.name.startswith("input") else node.name
        if node.kind is None and node.parent is None:
            return _CONTAINER_NAMES.get(node.name) or _container_name(node.name)
        return (node.attr("name") or self._input_name(node) or udev.get("ID_MODEL_FROM_DATABASE")
                or self._class_name(node) or node.name)

    @staticmethod
    def _class_name(node):
        """A name for a device that has none of its own, from what its driver made of it."""
        for c in node.classes:
            if c.sub in CLASS_DEVICE_NAMES:
                return CLASS_DEVICE_NAMES[c.sub]
        return None

    def vendor(self, node):
        sub, udev = node.sub, self.udev(node)
        if sub == "pci":
            return udev.get("ID_VENDOR_FROM_DATABASE") or self.ids(node)[0] or ""
        if sub == "usb" and node.devtype == "usb_device":
            if self.is_root_hub(node):
                return ""
            manufacturer = node.attr("manufacturer")
            return manufacturer or udev.get("ID_VENDOR_FROM_DATABASE") or self.ids(node)[0] or ""
        if sub in ("hdaudio", "thunderbolt"):
            return node.attr("vendor_name") or ""
        if sub == "scsi":
            return node.attr("vendor") or ""
        # for others udev guesses the maker from the first letters of an ACPI ID ("AMDI0010" ->
        # "Amdek Corporation"), which is wrong as often as not
        return ""

    def usb_classes(self, node):
        """Interface classes of a USB device, from its interfaces (or the device's own class)."""
        codes = []
        for iface in node.ifaces:
            parts = iface.ue.get("INTERFACE", "").split("/")
            if parts and parts[0].isdigit():
                codes.append((int(parts[0]), int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0,
                              int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0))
        if not codes:
            parts = node.ue.get("TYPE", "").split("/")
            if parts and parts[0].isdigit() and int(parts[0]):
                codes.append((int(parts[0]), 0, 0))
        return codes

    def type(self, node):
        sub = node.sub
        if node.kind is None:
            return "Bus" if node.parent is None else ""
        if node.kind == "class":
            return CLASS_TYPES.get(sub, sub)
        if sub == "pci":
            udev = self.udev(node)
            if udev.get("ID_PCI_SUBCLASS_FROM_DATABASE"):
                return udev["ID_PCI_SUBCLASS_FROM_DATABASE"]
            code = _hex(node.ue.get("PCI_CLASS"))
            if code is None:
                return BUS_TYPES["pci"]
            base, subclass = code >> 16, (code >> 8) & 0xff
            return PCI_SUBCLASSES.get((base, subclass)) or PCI_CLASSES.get(base, BUS_TYPES["pci"])
        if sub == "usb" and node.devtype == "usb_device":
            if self.is_root_hub(node):
                return "USB root hub"
            names = []
            for cls, subclass, proto in self.usb_classes(node):
                if cls == 0xe0 and subclass == 1 and proto == 1:
                    names.append("Bluetooth")
                else:
                    names.append(USB_CLASSES.get(cls, f"Class {cls:02x}"))
            return ", ".join(_unique(names)) or BUS_TYPES["usb"]
        if sub == "usb" and node.devtype == "usb_interface":
            return "USB interface"
        if sub == "i2c" and node.devtype == "i2c_adapter":
            return "I²C adapter"
        if sub == "thunderbolt":
            return {"thunderbolt_domain": "Thunderbolt domain", "thunderbolt_device": "Thunderbolt router",
                    "thunderbolt_retimer": "Thunderbolt retimer"}.get(node.devtype, "Thunderbolt")
        return BUS_TYPES.get(sub, sub or "")

    def icon(self, node):
        """A symbolic icon name for what the device is."""
        if node.kind is None:
            return "preferences-system-devices-symbolic" if node.parent is None else "application-x-firmware-symbolic"
        classes = node.classes if node.kind == "bus" else [node]
        subs = {c.sub for c in classes}
        if "drm" in subs:
            return "video-display-symbolic"
        for c in classes:
            if c.sub == "net":
                return "network-wireless-symbolic" if c.devtype == "wlan" else "network-wired-symbolic"
        if "bluetooth" in subs:
            return "bluetooth-symbolic"
        if "video4linux" in subs:
            return "camera-web-symbolic"
        for c in classes:
            if c.sub == "block" and c.devtype == "disk":
                if c.attr("removable") == "1":
                    return "media-removable-symbolic"
                return "drive-harddisk-solidstate-symbolic" if c.attr("queue/rotational") == "0" \
                    else "drive-harddisk-symbolic"
        if "sound" in subs or node.sub == "hdaudio":
            return "audio-card-symbolic"
        for c in classes:
            if c.sub == "power_supply":
                return "battery-symbolic" if c.attr("type") == "Battery" else "ac-adapter-symbolic"
        inputs = [self.udev(c) for c in classes if c.sub == "input" and c.name.startswith("event")]
        for flag, icon in (("ID_INPUT_TOUCHPAD", "input-touchpad-symbolic"),
                           ("ID_INPUT_JOYSTICK", "input-gaming-symbolic"),
                           ("ID_INPUT_MOUSE", "input-mouse-symbolic"),
                           ("ID_INPUT_KEYBOARD", "input-keyboard-symbolic")):
            if any(u.get(flag) == "1" for u in inputs):
                return icon
        if "printer" in subs or ("usbmisc" in subs and any(c.name.startswith("lp") for c in classes)):
            return "printer-symbolic"
        if node.sub == "thunderbolt":
            return "thunderbolt-symbolic"
        if node.sub == "pci":
            code = _hex(node.ue.get("PCI_CLASS"))
            base = code >> 16 if code is not None else None
            icon = {0x01: "drive-harddisk-symbolic", 0x02: "network-wired-symbolic", 0x03: "video-display-symbolic",
                    0x04: "audio-card-symbolic", 0x0d: "network-wireless-symbolic"}.get(base)
            if icon:
                return icon
        if node.sub == "usb" and node.devtype == "usb_device":
            for cls, subclass, proto in self.usb_classes(node):
                if cls == 0xe0 and subclass == 1 and proto == 1:
                    return "bluetooth-symbolic"
                icon = {0x01: "audio-card-symbolic", 0x0e: "camera-web-symbolic", 0x08: "media-removable-symbolic",
                        0x07: "printer-symbolic"}.get(cls)
                if icon:
                    return icon
        return "application-x-firmware-symbolic"


# names for a device known only by what it offers (a TPM chip on an unnamed ACPI ID, a USB
# Bluetooth adapter without a product string)
CLASS_DEVICE_NAMES = {"tpm": "Trusted Platform Module", "bluetooth": "Bluetooth adapter", "video4linux": "Camera",
                      "net": "Network adapter", "mmc_host": "Card reader", "printer": "Printer"}

_CONTAINER_NAMES = {"platform": "Platform devices", "virtual": "Virtual devices", "system": "System",
                    "isa": "ISA bus", "faux": "Software devices", "software": "Software events"}


def _container_name(name):
    if name.startswith("pci"):
        return f"PCI bus {name[3:]}"
    if name.startswith("pnp"):
        return "Plug and Play devices"
    if name.startswith("LNXSYSTM"):
        return "ACPI namespace"
    return name


def machine_name(dmi=DMI_ID):
    """(name, vendor) of the computer from its firmware, e.g. ("ThinkPad T14s Gen 3", "LENOVO")."""
    def clean(attr):
        value = _read(f"{dmi}/{attr}") or ""
        return "" if value.casefold() in _DMI_JUNK else value

    vendor, product, version = clean("sys_vendor"), clean("product_name"), clean("product_version")
    # some vendors (Lenovo) keep the model name in the version and a part number in the product
    if version and " " in version and " " not in product:
        product = version
    return product or clean("board_name") or "This computer", vendor


# PCI functions that no driver binds to by design: host bridges (the root complex, AMD's data
# fabric), the LPC/ISA bridge, the IOMMU (the kernel drives it without binding)
_PCI_DRIVERLESS = frozenset((0x0600, 0x0601, 0x0806))


def _bus_status(node):
    """(bound, could bind): a driver took the device, and the device is one a driver could take."""
    if node.sub == "usb" and node.devtype == "usb_device":
        bound = any(i.driver for i in node.ifaces)
        return bound, bound or any("MODALIAS" in i.ue for i in node.ifaces)
    if node.sub == "pci" and not node.driver:
        code = _hex(node.ue.get("PCI_CLASS"))
        if code is not None and code >> 8 in _PCI_DRIVERLESS:
            return False, False
    return bool(node.driver), bool(node.driver) or "MODALIAS" in node.ue


def _irqs(node):
    if node.sub != "pci":
        return ()
    try:
        msi = sorted(int(n) for n in os.listdir(f"{node.path}/msi_irqs") if n.isdigit())
    except OSError:
        msi = []
    if msi:
        return tuple(msi)
    irq = node.attr("irq")
    return (int(irq),) if irq and irq.isdigit() and int(irq) > 0 else ()


def scan(show_all=False, root=SYS_DEVICES, udev_dir=UDEV_DATA, dmi=DMI_ID, pci_ids=PCI_IDS, usb_ids=USB_IDS):
    """The rows of the device tree: a list of plain dicts, a parent before its children.

    Each row has a "key" (its path below /sys/devices, ROOT_KEY for the computer) and a
    "parent" key (None for the computer). Live values come from read_live()."""
    nodes = walk(root, show_all)
    _mark_shown(nodes, root)
    # a device that a driver below it uses is not missing one (an I²C adapter, a modem's MHI bus)
    bound_below = set()
    for node in nodes.values():
        if node.driver:
            parent = node.parent
            while parent is not None and parent not in bound_below:
                bound_below.add(parent)
                parent = nodes[parent].parent
    describe = _Describer(nodes, udev_dir, pci_ids, usb_ids)
    name, vendor = machine_name(dmi)
    rows = [{
        "key": ROOT_KEY, "parent": None, "name": name, "icon": "computer-symbolic", "type": "Computer",
        "bus": "", "driver": "", "module": "", "vendor": vendor, "address": os.uname().nodename,
        "interfaces": (), "devnodes": (), "syspath": root, "modalias": "", "bound": False, "can_bind": False,
        "pm": False, "pcie": False, "link": "", "irqs": (), "primary": False, "classes": (),
        "power_sensor": None, "battery": None,
    }]
    shown = {rel for rel, node in nodes.items() if show_all or node.shown}
    for rel, node in nodes.items():
        if rel not in shown:
            continue
        parent = node.parent
        while parent is not None and parent not in shown:
            parent = nodes[parent].parent
        # what is folded into a device: its class devices (all of them for "Used by", the main ones
        # by name), and for a USB device the drivers of its interfaces
        owned = node.classes if node.kind == "bus" or node.parent is None else []
        devnodes = _unique([node.devnode] + [c.devnode for c in owned])
        interfaces = tuple(sorted(_unique(c.name for c in owned if _listed(c)), key=natural_key))
        if node.kind == "class":
            interfaces = ()
        if node.sub == "usb" and node.devtype == "usb_device":
            drivers = _unique(i.driver for i in node.ifaces)
            modules = _unique(_module(i) for i in node.ifaces if i.driver)
        else:
            drivers = (node.driver,) if node.driver else ()
            modules = (_module(node),) if node.driver else ()
        bound, can_bind = _bus_status(node) if node.kind == "bus" else (bool(node.driver), False)
        can_bind = can_bind and rel not in bound_below
        link = ""
        pcie = node.sub == "pci" and os.path.exists(f"{node.path}/current_link_speed")
        if node.sub == "usb" and node.devtype == "usb_device":
            link = fmt_usb_speed(node.attr("speed"))
        rows.append({
            "key": rel, "parent": parent if parent is not None else ROOT_KEY,
            "name": describe.name(node), "icon": describe.icon(node), "type": describe.type(node),
            "bus": node.sub or "", "driver": ", ".join(drivers), "module": ", ".join(m for m in modules if m),
            "vendor": describe.vendor(node), "address": node.name if node.kind != "class" else "",
            "interfaces": interfaces, "devnodes": devnodes, "syspath": node.path,
            "modalias": node.ue.get("MODALIAS", ""), "bound": bound, "can_bind": can_bind,
            "pm": node.kind == "bus" and os.path.exists(f"{node.path}/power/runtime_status"),
            "pcie": pcie, "link": link, "irqs": _irqs(node),
            "primary": node.shown,  # one of the devices of the default view
            # (class, name, device file) of everything its drivers made of it
            "classes": tuple((c.sub, c.name, c.devnode or "") for c in owned) if node.kind != "class" else (),
            "power_sensor": _power_sensor(owned) if node.kind != "class" else None,
            "battery": next((c.path for c in owned if c.sub == "power_supply" and c.attr("type") == "Battery"),
                            None) if node.kind != "class" else None,
        })
    return rows


def _module(node):
    """The kernel module of a node's driver; "built-in" for a driver compiled into the kernel."""
    link = _readlink(f"{node.path}/driver/module")
    return os.path.basename(link) if link else "built-in"


def status_text(row, runtime_status):
    """"Active", "Suspended", "No driver", or "" where a status does not apply (containers,
    nodes no driver could take)."""
    if not row["bound"]:
        return "No driver" if row["can_bind"] else ""
    if runtime_status == "suspended":
        return "Suspended"
    return "Active"


_POWER_FILE = re.compile(r"power(\d+)_(input|average)$")


def _power_sensor(classes):
    """(file, label) of the first power sensor (µW) among a device's hwmon class devices, or None.

    Only a few devices measure their power: graphics cards (amdgpu's "PPT" is the whole chip, CPU
    and GPU, on processors with integrated graphics), power supplies of servers, some USB-C ports."""
    for c in classes:
        if c.sub != "hwmon":
            continue
        try:
            names = sorted(os.listdir(c.path), key=natural_key)
        except OSError:
            continue
        found = {}
        for name in names:
            m = _POWER_FILE.match(name)
            # power1_input (now) before power1_average where a driver has both
            if m and (m.group(2) == "input" or int(m.group(1)) not in found):
                found[int(m.group(1))] = name
        if found:
            index = min(found)
            return f"{c.path}/{found[index]}", _read(f"{c.path}/power{index}_label") or ""
    return None


def battery_power(path):
    """(watts, status) of a battery: what it delivers while discharging, takes while charging."""
    status = _read(f"{path}/status") or ""
    power = _read(f"{path}/power_now")
    try:
        if power is not None:
            return abs(int(power)) / 1e6, status
        current, voltage = _read(f"{path}/current_now"), _read(f"{path}/voltage_now")
        if current is not None and voltage is not None:
            return abs(int(current)) * int(voltage) / 1e12, status
    except ValueError:
        pass
    return None, status


def system_power_text(rows, live):
    """"5.9 W from battery", "charging at 30.2 W", or "" (on mains power with a full battery, no battery)."""
    for row in rows:
        values = live.get(row["key"]) or {}
        status, power = values.get("battery_status"), values.get("power")
        if power is None or status is None:
            continue
        if status == "Discharging":
            return f"{power:.1f} W from battery"
        if status == "Charging" and power > 0:
            return f"charging at {power:.1f} W"
    return ""


class SensorPacer:
    """Decides when a device's power sensor may be read without keeping the device awake.

    A driver like amdgpu restarts a device's autosuspend timer on every sensor read: read every
    2 seconds, an idle graphics card would never suspend. A device that may suspend is read again
    only once its autosuspend delay has passed since the last read, so it can go to sleep in
    between; a suspended one is not read at all. Shared by the workers of the tab and the
    Properties windows."""

    MARGIN = 1.0  # seconds beyond the autosuspend delay

    def __init__(self):
        self._last = {}
        self._lock = threading.Lock()

    def may_read(self, key, runtime, control, delay_ms, now=None):
        now = time.monotonic() if now is None else now
        if runtime in ("suspended", "suspending", "resuming"):
            return False
        if runtime in (None, "", "unsupported") or control != "auto":
            return True  # it never suspends
        try:
            delay = int(delay_ms) / 1000
        except (TypeError, ValueError):
            delay = 0
        if delay < 0:
            return True  # no autosuspend: nothing to keep awake
        with self._lock:
            last = self._last.get(key)
            if last is not None and now - last < delay + self.MARGIN:
                return False
            self._last[key] = now
            return True


POWER_PACER = SensorPacer()


def _read_power(row, runtime, path, pacer):
    """(watts or None, read, battery_status): the device's measured power; read is False where it was not read now."""
    if row["battery"]:
        watts, status = battery_power(row["battery"])
        return watts, True, status
    sensor = row["power_sensor"]
    if sensor is None:
        return None, True, None
    control = _read(f"{path}/power/control") if row["pm"] else None
    delay = _read(f"{path}/power/autosuspend_delay_ms") if control == "auto" else None
    if not pacer.may_read(row["key"], runtime, control, delay):
        return None, False, None
    value = _read(sensor[0])
    try:
        return int(value) / 1e6, True, None
    except (TypeError, ValueError):
        return None, True, None


def suspended_share(active_ms, suspended_ms):
    """The share of the time a device was suspended (0..1), from its runtime PM counters."""
    try:
        active, suspended = int(active_ms), int(suspended_ms)
    except (TypeError, ValueError):
        return None
    total = active + suspended
    return suspended / total if total > 0 else None


def read_live(rows, proc="/proc", pacer=POWER_PACER):
    """{key: {"status", "runtime_pm", "pm_control", "suspended_share", "link", "link_downgraded",
    "irq_count", "power", "power_read", "battery_status"}}: what changes while a device stays.

    "link" is None where it was not read: reading a PCIe link register wakes a device from
    runtime power saving, so it is read only from devices that are awake. "power" (W) is measured
    only by a few devices; power_read is False where its sensor was left alone (see SensorPacer)."""
    try:
        with open(f"{proc}/interrupts", encoding="ascii", errors="replace") as fh:
            irq_counts = parse_interrupts(fh.read())
    except OSError:
        irq_counts = {}
    live = {}
    for row in rows:
        path = row["syspath"]
        runtime = _read(f"{path}/power/runtime_status") if row["pm"] else None
        link, downgraded = row["link"], False
        if row["pcie"] and runtime not in (None, "active", "unsupported"):
            link = None
        elif row["pcie"]:
            link, downgraded = fmt_pcie_link(_read(f"{path}/current_link_speed"), _read(f"{path}/current_link_width"),
                                             _read(f"{path}/max_link_speed"), _read(f"{path}/max_link_width"))
        count = None
        if row["irqs"]:
            count = sum(irq_counts.get(irq, 0) for irq in row["irqs"])
        control = share = None
        if row["pm"]:
            control = _read(f"{path}/power/control")
            if runtime not in (None, "unsupported"):
                share = suspended_share(_read(f"{path}/power/runtime_active_time"),
                                        _read(f"{path}/power/runtime_suspended_time"))
        power, power_read, battery_status = _read_power(row, runtime, path, pacer)
        live[row["key"]] = {"status": status_text(row, runtime), "runtime_pm": runtime or "",
                            "pm_control": control or "", "suspended_share": share,
                            "link": link, "link_downgraded": downgraded, "irq_count": count,
                            "power": power, "power_read": power_read,
                            "battery_status": battery_status}
    return live


def device_users(proc="/proc", elevated=None):
    """({device file: {pid, …}}, denied): which processes hold a file below /dev open.

    elevated: {(pid, starttime): (fds, …)} read through the helper (handles.elevated_snapshot),
    used for the processes that cannot be read directly. denied counts the processes neither
    covered (other users'; kernel threads hold nothing and are not counted)."""
    users = {}
    denied = 0
    try:
        names = os.listdir(proc)
    except OSError:
        return users, denied
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        base = f"{proc}/{name}/fd"
        try:
            fds = os.listdir(base)
        except FileNotFoundError:
            continue
        except OSError:
            info = _stat_info(f"{proc}/{name}")
            if info is None or info[2]:
                continue
            snapshot = (elevated or {}).get((pid, info[0]))
            if snapshot is None:
                denied += 1
                continue
            for _fd, target in snapshot[0]:
                if target.startswith("/dev/"):
                    users.setdefault(target, set()).add(pid)
            continue
        for fd in fds:
            try:
                target = os.readlink(f"{base}/{fd}")
            except OSError:
                continue
            if target.startswith("/dev/"):
                users.setdefault(target, set()).add(pid)
    return users, denied


class CounterRates:
    """Per-second rates of growing counters, from two readings apart in time."""

    def __init__(self):
        self.reset()

    def reset(self):
        self._last = {}
        self._when = None

    def update(self, counts, now=None):
        """counts: {key: total or None} -> {key: rate}; a key without an earlier reading gets None."""
        now = time.monotonic() if now is None else now
        elapsed = None if self._when is None else now - self._when
        rates = {}
        for key, count in counts.items():
            old = self._last.get(key)
            if elapsed and elapsed > 0 and count is not None and old is not None and count >= old:
                rates[key] = (count - old) / elapsed
            else:
                rates[key] = None
        self._last = dict(counts)
        self._when = now
        return rates


NETLINK_KOBJECT_UEVENT = 15
_STRUCTURE_ACTIONS = frozenset(("add", "remove", "bind", "unbind", "move"))


def parse_uevent_message(data):
    """(action, devpath) of a kernel uevent message ("add@/devices/…\\0ACTION=add\\0…"), or None."""
    head = data.split(b"\0", 1)[0].decode("utf-8", "replace")
    action, sep, devpath = head.partition("@")
    if not sep:
        return None
    return action, devpath


class UeventMonitor:
    """Calls changed() on the main loop when devices come, go, or get or lose a driver.

    The kernel sends its event before udev has updated its database, which names the device;
    so changed() runs 0.5 s after the last event of a burst, and again 3 s after it."""

    DELAYS_MS = (500, 3000)

    def __init__(self, changed, include_software=lambda: False):
        self.changed = changed
        self.include_software = include_software
        self._sock = None
        self._watch = 0
        self._timers = [0, 0]

    @property
    def running(self):
        return self._sock is not None

    def start(self):
        """Listen; False if the kernel does not let this process (sandboxes, containers)."""
        if self._sock is not None:
            return True
        try:
            sock = socket.socket(socket.AF_NETLINK, socket.SOCK_DGRAM | socket.SOCK_NONBLOCK | socket.SOCK_CLOEXEC,
                                 NETLINK_KOBJECT_UEVENT)
            sock.bind((0, 1))  # multicast group 1: the kernel's own events
        except (OSError, AttributeError):
            return False
        self._sock = sock
        channel = GLib.IOChannel.unix_new(sock.fileno())
        self._watch = GLib.io_add_watch(channel, GLib.PRIORITY_DEFAULT, GLib.IOCondition.IN, self._on_readable)
        return True

    def stop(self):
        if self._watch:
            GLib.source_remove(self._watch)
            self._watch = 0
        for i, timer in enumerate(self._timers):
            if timer:
                GLib.source_remove(timer)
                self._timers[i] = 0
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def _on_readable(self, *_):
        relevant = False
        while self._sock is not None:
            try:
                data = self._sock.recv(65536)
            except BlockingIOError:
                break
            except OSError as err:
                if err.errno == errno.ENOBUFS:  # events were lost, so look again anyway
                    relevant = True
                    continue
                self._watch = 0  # GLib removes the watch when this callback returns False
                self.stop()
                return False
            event = parse_uevent_message(data)
            if event is None or event[0] not in _STRUCTURE_ACTIONS:
                continue
            if not self.include_software() and event[1].startswith(("/devices/virtual/", "/devices/system/")):
                continue  # loop devices of snaps, CPUs going on- and offline, …
            relevant = True
        if relevant:
            for i, delay in enumerate(self.DELAYS_MS):
                if self._timers[i]:
                    GLib.source_remove(self._timers[i])
                self._timers[i] = GLib.timeout_add(delay, self._fire, i)
        return True

    def _fire(self, i):
        self._timers[i] = 0
        self.changed()
        return False


# ---------------------------------------------------------------------------
# one device in detail (the Properties window)

# Attributes the kernel answers from what it keeps in memory: safe to read from a suspended device
_ID_ATTRS = ("vendor", "device", "subsystem_vendor", "subsystem_device", "revision", "class", "boot_vga",
             "numa_node", "local_cpulist", "dma_mask_bits", "power_state", "d3cold_allowed", "enable",
             "idVendor", "idProduct", "bcdDevice", "manufacturer", "product", "serial", "version", "speed",
             "busnum", "devnum", "maxchild", "bMaxPower", "bNumInterfaces", "bDeviceClass", "removable",
             "authorized", "vendor_id", "subsystem_id", "revision_id", "chip_name", "vendor_name", "name",
             "description", "id", "device_name", "unique_id", "model", "rev", "firmware_rev", "serial_number")
_POWER_ATTRS = ("runtime_status", "control", "runtime_active_time", "runtime_suspended_time",
                "autosuspend_delay_ms", "wakeup", "wakeup_count")
# Attributes the Attributes page leaves out: binary, write-only triggers, mmap-only regions
_SKIP_ATTRS = frozenset(("config", "rom", "vpd", "remove", "rescan", "reset", "new_id", "remove_id", "bind",
                         "unbind", "descriptors", "bConfigurationValue_store"))
_RESOURCE_FILE = re.compile(r"resource\d+(_wc)?$")
# Attributes read from the device's registers (PCI config space): reading them wakes a suspended device
_WAKING_ATTRS = frozenset(("current_link_speed", "current_link_width", "max_link_speed", "max_link_width",
                           "secondary_bus_number", "subordinate_bus_number"))
_LINK_ATTRS = ("current_link_speed", "current_link_width", "max_link_speed", "max_link_width")
MAX_ATTRIBUTE_BYTES = 4096

# IORESOURCE flags of a PCI region (include/linux/ioport.h)
_IORESOURCE_IO, _IORESOURCE_MEM = 0x100, 0x200
_IORESOURCE_PREFETCH, _IORESOURCE_MEM_64 = 0x2000, 0x00100000


def parse_resource(text):
    """The regions of a PCI device's `resource` file: [(index, start, end, flags)] of the used ones."""
    regions = []
    for index, line in enumerate((text or "").splitlines()):
        try:
            start, end, flags = (int(v, 16) for v in line.split()[:3])
        except ValueError:
            continue
        if start or end:
            regions.append((index, start, end, flags))
    return regions


def describe_region(start, end, flags):
    """("Memory", "64-bit, prefetchable") of a region."""
    kind = "I/O ports" if flags & _IORESOURCE_IO else "Memory" if flags & _IORESOURCE_MEM else "Region"
    notes = []
    if flags & _IORESOURCE_MEM_64:
        notes.append("64-bit")
    if flags & _IORESOURCE_PREFETCH:
        notes.append("prefetchable")
    return kind, ", ".join(notes)


def parse_interrupt_lines(text):
    """{irq: (count, chip, handlers)} from /proc/interrupts: the controller ("IR-PCI-MSIX-0000:33:00.0")
    and the names of the drivers that handle it ("amdgpu")."""
    lines = (text or "").splitlines()
    if not lines:
        return {}
    ncpu = len(lines[0].split())
    out = {}
    for line in lines[1:]:
        head, sep, rest = line.partition(":")
        head = head.strip()
        if not sep or not head.isdigit():
            continue
        fields = rest.split()
        counts = fields[:ncpu]
        tail = fields[ncpu:]
        try:
            total = sum(int(c) for c in counts)
        except ValueError:
            continue
        chip = tail[0] if tail else ""
        handlers = " ".join(tail[2:]) if len(tail) > 2 else ""
        out[int(head)] = (total, chip, handlers)
    return out


class ModuleAliases:
    """Which kernel modules declare that they can drive a device, from modules.alias."""

    def __init__(self, path=None):
        self.path = path or f"/lib/modules/{os.uname().release}/modules.alias"
        self._table = None
        self._lock = threading.Lock()  # workers of several Properties windows

    def _load(self):
        table = {}
        try:
            fh = open(self.path, encoding="utf-8", errors="replace")
        except OSError:
            return table
        with fh:
            for line in fh:
                parts = line.split()
                if len(parts) != 3 or parts[0] != "alias":
                    continue
                pattern = parts[1]
                # the part before the first wildcard: most patterns name a vendor and device, and
                # comparing that rejects them without fnmatch (1 s for all of them otherwise)
                literal = re.split(r"[*?\[]", pattern, maxsplit=1)[0]
                table.setdefault(pattern.split(":", 1)[0], []).append((literal, pattern, parts[2]))
        return table

    def modules(self, modalias):
        """Names of the modules whose aliases match modalias, in file order."""
        if not modalias:
            return []
        with self._lock:
            if self._table is None:
                self._table = self._load()
            table = self._table
        found = []
        for literal, pattern, module in table.get(modalias.split(":", 1)[0], ()):
            if modalias.startswith(literal) and module not in found and fnmatch.fnmatchcase(modalias, pattern):
                found.append(module)
        return found


MODULE_ALIASES = ModuleAliases()


def module_loaded(name, sys_module="/sys/module"):
    return os.path.isdir(f"{sys_module}/{name.replace('-', '_')}")


def _module_info(name, sys_module="/sys/module"):
    base = f"{sys_module}/{name}"
    return {"version": _read(f"{base}/version") or "", "srcversion": _read(f"{base}/srcversion") or "",
            "taint": _read(f"{base}/taint") or "", "refcnt": _read(f"{base}/refcnt") or ""}


def _usb_interfaces(path):
    """The interfaces of a USB device: [{name, class, driver, module, modalias}]."""
    out = []
    try:
        names = sorted(os.listdir(path), key=natural_key)
    except OSError:
        return out
    for name in names:
        sub = f"{path}/{name}"
        if ":" not in name or not os.path.isfile(f"{sub}/uevent"):
            continue
        ue = parse_uevent(_read(f"{sub}/uevent"))
        if ue.get("DEVTYPE") != "usb_interface":
            continue
        parts = ue.get("INTERFACE", "").split("/")
        cls = int(parts[0]) if parts and parts[0].isdigit() else None
        driver = _readlink(f"{sub}/driver")
        module = _readlink(f"{sub}/driver/module")
        out.append({"name": name, "class": USB_CLASSES.get(cls, "") if cls is not None else "",
                    "driver": os.path.basename(driver) if driver else "",
                    "module": os.path.basename(module) if module else ("built-in" if driver else ""),
                    "modalias": ue.get("MODALIAS", "")})
    return out


def _attributes(path, ue, udev, awake):
    """[(source, name, value)]: udev's properties, the uevent, and every readable sysfs attribute of
    the device (and its power/ folder) that is safe to read."""
    rows = [("udev", key, value) for key, value in sorted(udev.items())]
    rows += [("uevent", key, value) for key, value in sorted(ue.items())]
    for folder, prefix in ((path, ""), (f"{path}/power", "power/")):
        try:
            entries = sorted(os.scandir(folder), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            if name in _SKIP_ATTRS or name == "uevent" or _RESOURCE_FILE.match(name):
                continue
            if name in _WAKING_ATTRS and not awake:
                rows.append(("sysfs", prefix + name, "(not read: it would wake the device)"))
                continue
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode):
                target = _readlink(entry.path)
                if target and name not in ("subsystem", "driver", "firmware_node", "iommu_group", "device"):
                    continue  # links to other devices: below the device in the tree
                rows.append(("link", prefix + name, os.path.basename(target or "")))
                continue
            if not stat.S_ISREG(st.st_mode) or not st.st_mode & 0o444:
                continue
            try:
                with open(entry.path, "rb") as fh:
                    data = fh.read(MAX_ATTRIBUTE_BYTES + 1)
            except OSError as exc:
                rows.append(("sysfs", prefix + name, f"({exc.strerror or 'not readable'})"))
                continue
            if b"\0" in data:
                value = f"({len(data)} bytes of binary data)"
            else:
                value = data[:MAX_ATTRIBUTE_BYTES].decode("utf-8", "replace").strip()
                if len(data) > MAX_ATTRIBUTE_BYTES:
                    value += " …"
            rows.append(("sysfs", prefix + name, value))
    return rows


def device_details(row, ancestors=(), attributes=False, elevated=None, proc="/proc", aliases=MODULE_ALIASES,
                   sys_module="/sys/module", udev_dir=UDEV_DATA, iommu_groups="/sys/kernel/iommu_groups"):
    """Everything the Properties window shows about the device of a scan() row. Worker.

    ancestors: the rows from the computer down to the device's parent; their status and link are
    read again. attributes: also read every attribute (the Attributes page)."""
    path = row["syspath"]
    out = {"exists": row["key"] == ROOT_KEY or os.path.isfile(f"{path}/uevent")}
    if not out["exists"]:
        return out
    ue = parse_uevent(_read(f"{path}/uevent"))
    live = read_live([*ancestors, row], proc)
    out["live"] = live
    runtime = live[row["key"]]["runtime_pm"]
    awake = runtime in ("", "active", "unsupported")
    out["attrs"] = {name: value for name in _ID_ATTRS if (value := _read(f"{path}/{name}")) is not None}
    out["power"] = {name: value for name in _POWER_ATTRS if (value := _read(f"{path}/power/{name}")) is not None}
    out["link"] = None
    if row["pcie"] and awake:
        out["link"] = {name: _read(f"{path}/{name}") for name in _LINK_ATTRS}

    driver = _readlink(f"{path}/driver")
    out["driver"] = os.path.basename(driver) if driver else ""
    module = _readlink(f"{path}/driver/module")
    out["module"] = os.path.basename(module) if module else ("built-in" if driver else "")
    out["interfaces"] = _usb_interfaces(path) if ue.get("DEVTYPE") == "usb_device" else []
    modules = [out["module"]] + [i["module"] for i in out["interfaces"]]
    out["modules"] = {name: _module_info(name, sys_module) for name in _unique(modules) if name != "built-in"}
    modaliases = _unique([ue.get("MODALIAS", "")] + [i["modalias"] for i in out["interfaces"]])
    candidates = _unique(m for alias in modaliases for m in aliases.modules(alias))
    out["candidates"] = [(name, module_loaded(name, sys_module)) for name in candidates]

    try:
        with open(f"{proc}/interrupts", encoding="ascii", errors="replace") as fh:
            irq_lines = parse_interrupt_lines(fh.read())
    except OSError:
        irq_lines = {}
    out["irqs"] = []
    for irq in row["irqs"]:
        count, chip, handlers = irq_lines.get(irq, (None, "", ""))
        affinity = _read(f"{proc}/irq/{irq}/effective_affinity_list") or _read(f"{proc}/irq/{irq}/smp_affinity_list")
        out["irqs"].append({"irq": irq, "count": count, "chip": chip, "handlers": handlers,
                            "affinity": affinity or ""})
    out["regions"] = parse_resource(_read(f"{path}/resource")) if row["bus"] == "pci" else []
    group = _readlink(f"{path}/iommu_group")
    out["iommu_group"] = os.path.basename(group) if group else None
    out["iommu_members"] = []
    if group:
        try:
            members = os.listdir(f"{iommu_groups}/{out['iommu_group']}/devices")
        except OSError:
            members = []
        out["iommu_members"] = sorted((m for m in members if m != os.path.basename(path)), key=natural_key)

    users, denied = device_users(proc, elevated)
    holders = {}
    for node in row["devnodes"]:
        for pid in users.get(node, ()):
            holders.setdefault(pid, []).append(node)
    out["users"] = holders
    out["denied"] = denied
    out["all_users"] = {node: users.get(node, set()) for node in row["devnodes"]}
    if attributes:
        udev = parse_udev_data(_read(_udev_path(udev_dir, row, ue)))
        out["attributes"] = _attributes(path, ue, udev, awake)
    return out


def _udev_path(udev_dir, row, ue):
    name = os.path.basename(row["syspath"])
    if "MAJOR" in ue and "MINOR" in ue:
        return f"{udev_dir}/{'b' if row['bus'] == 'block' else 'c'}{ue['MAJOR']}:{ue['MINOR']}"
    if row["bus"] == "net" and "IFINDEX" in ue:
        return f"{udev_dir}/n{ue['IFINDEX']}"
    return f"{udev_dir}/+{row['bus']}:{name}"
