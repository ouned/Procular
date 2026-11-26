"""Per-disk details: identity, interface link, capacity, temperature, partitions and file systems.

Everything comes from sysfs, the udev database (/run/udev/data), /proc/swaps and the mount
table, all readable without privileges. SMART data (health, wear, power-on hours) needs
root and is not read. The helpers take their sysfs roots as arguments so they can be
tested against a fake tree.
"""

import glob
import os
import re
import time

import psutil

from ..util.linux import clean_text
from .netinfo import _read_int, _read_str

SYS_BLOCK = "/sys/block"
SYS_CLASS_BLOCK = "/sys/class/block"
ATA_LINKS = "/sys/class/ata_link"
UDEV_DATA = "/run/udev/data"


def parse_udev_db(text):
    """udev database record -> {"ID_MODEL": ..., ...} (only the E: property lines)."""
    props = {}
    for line in text.splitlines():
        if line.startswith("E:") and "=" in line:
            key, value = line[2:].split("=", 1)
            props[key] = value
    return props


def udev_props(dev, sys_class=SYS_CLASS_BLOCK, udev_dir=UDEV_DATA):
    majmin = _read_str(os.path.join(sys_class, dev, "dev"))
    if not majmin:
        return {}
    try:
        with open(os.path.join(udev_dir, f"b{majmin}"), encoding="utf-8", errors="replace") as fh:
            return parse_udev_db(fh.read())
    except OSError:
        return {}


def parse_scheduler(text):
    """"[none] mq-deadline kyber" -> "none"."""
    if not text:
        return None
    m = re.search(r"\[([^\]]+)\]", text)
    return m.group(1) if m else text.split()[0] if text.split() else None


def parse_proc_swaps(text):
    """/proc/swaps -> {path: (size bytes, used bytes)} for swap partitions and files."""
    out = {}
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 4:
            try:
                out[parts[0]] = (int(parts[2]) * 1024, int(parts[3]) * 1024)
            except ValueError:
                continue
    return out


# ---------------------------------------------------------------------------
# interface / link

_PCIE_GEN = {"2.5": "1.0", "5.0": "2.0", "8.0": "3.0", "16.0": "4.0", "32.0": "5.0", "64.0": "6.0"}


def pcie_link(speed, width):
    """("8.0 GT/s PCIe", "4") -> "PCIe 3.0 x4"."""
    if not speed or speed.startswith("Unknown"):
        return None
    gts = speed.split()[0]
    gen = _PCIE_GEN.get(gts)
    text = f"PCIe {gen}" if gen else f"PCIe {gts} GT/s"
    if width and width not in ("0", "255"):
        text += f" x{width}"
    return text


_USB_NAMES = {1.5: "USB 1.0", 12: "USB 1.1", 480: "USB 2.0", 5000: "USB 3.2 Gen 1", 10000: "USB 3.2 Gen 2",
              20000: "USB 3.2 Gen 2x2", 40000: "USB4", 80000: "USB4 v2"}


def usb_speed(text):
    """sysfs USB "speed" (Mbit/s) -> "USB 3.2 Gen 1 (5 Gbit/s)"."""
    try:
        mbps = float(text)
    except (TypeError, ValueError):
        return None
    rate = f"{mbps / 1000:g} Gbit/s" if mbps >= 1000 else f"{mbps:g} Mbit/s"
    name = _USB_NAMES.get(mbps)
    return f"{name} ({rate})" if name else f"USB ({rate})"


_SATA_GEN = {"1.5": "SATA I", "3.0": "SATA II", "6.0": "SATA III"}


def sata_speed(text):
    """ata_link "sata_spd" ("6.0 Gbps") -> "SATA III (6.0 Gbit/s)"; None when the link is down."""
    m = re.match(r"([\d.]+)\s*Gbps", text or "")
    if not m:
        return None
    return f"{_SATA_GEN.get(m.group(1), 'SATA')} ({m.group(1)} Gbit/s)"


def _usb_device(path):
    """The USB device directory (the one with idVendor) above a sysfs device path, or None."""
    while path and path != "/" and "/usb" in path:
        if os.path.exists(os.path.join(path, "idVendor")):
            return path
        path = os.path.dirname(path)
    return None


def disk_bus(name, devpath, props=None):
    """"NVMe", "SATA", "USB", "MMC", "VirtIO", "SCSI", ... from the device's sysfs path."""
    if name.startswith("nvme") or "/nvme/" in devpath:
        return "NVMe"
    if "/usb" in devpath:
        return "USB"
    if name.startswith("mmcblk") or "/mmc_host/" in devpath:
        return "MMC"
    if "/virtio" in devpath or name.startswith("vd"):
        return "VirtIO"
    if re.search(r"/ata\d+/", devpath):
        return "SATA"
    bus = (props or {}).get("ID_BUS")
    return {"ata": "SATA", "scsi": "SCSI", "usb": "USB"}.get(bus, bus.upper() if bus else None)


def disk_kind(bus, rotational, mmc_type=None):
    if bus == "NVMe":
        return "NVMe SSD"
    if bus == "USB":
        return "USB drive"
    if bus == "MMC":
        return {"SD": "SD card", "MMC": "eMMC"}.get(mmc_type, "Memory card")
    if bus == "VirtIO":
        return "Virtual disk"
    if rotational is None:
        return "Disk"
    return "Hard disk (HDD)" if rotational else "SSD"


# ---------------------------------------------------------------------------
# stacked devices (LUKS, LVM, RAID)


def split_lvm_name(name):
    """Device-mapper name of an LVM volume -> "vg/lv" ("ubuntu--vg-ubuntu--lv" -> "ubuntu-vg/ubuntu-lv")."""
    parts = re.split(r"(?<!-)-(?!-)", name)
    if len(parts) != 2:
        return name
    return "/".join(p.replace("--", "-") for p in parts)


def holder_role(dev, sys_class=SYS_CLASS_BLOCK):
    """(display name, role) of a stacked block device, e.g. ("ubuntu-vg/root", "LVM volume")."""
    base = os.path.join(sys_class, dev)
    dm_name = _read_str(os.path.join(base, "dm", "name"))
    if dm_name:
        uuid = _read_str(os.path.join(base, "dm", "uuid")) or ""
        if uuid.startswith("CRYPT-"):
            return dm_name, "decrypted LUKS" if uuid.startswith("CRYPT-LUKS") else "decrypted dm-crypt"
        if uuid.startswith("LVM-"):
            return split_lvm_name(dm_name), "LVM volume"
        return dm_name, "device mapper"
    level = _read_str(os.path.join(base, "md", "level"))
    if level:
        return dev, level.upper().replace("RAID", "RAID ")
    return dev, None


def _partitions(disk, sys_block):
    base = os.path.join(sys_block, disk)
    try:
        entries = os.listdir(base)
    except OSError:
        return []
    parts = [e for e in entries if e.startswith(disk) and os.path.exists(os.path.join(base, e, "partition"))]
    return sorted(parts, key=lambda p: _read_int(os.path.join(base, p, "partition")) or 0)


def _holders(dev, sys_class):
    try:
        return sorted(os.listdir(os.path.join(sys_class, dev, "holders")))
    except OSError:
        return []


def volume_tree(disk, mounts, swaps, sys_block=SYS_BLOCK, sys_class=SYS_CLASS_BLOCK, udev_dir=UDEV_DATA):
    """Partitions of a disk and what is stacked on them, as a flat list in display order.

    Each row: dev, name, role, depth (0 = partition), size, fstype, fsversion, label, mounts,
    and "swap" (True when it is an active swap partition). mounts: {dev: [(mountpoint, fstype)]},
    swaps: {dev: (size, used)}.
    """
    rows = []

    def add(dev, depth, seen):
        props = udev_props(dev, sys_class, udev_dir)
        if depth:
            name, role = holder_role(dev, sys_class)
        else:
            name, role = dev, None
        mounted = mounts.get(dev, [])
        sectors = _read_int(os.path.join(sys_class, dev, "size"))
        rows.append({
            "dev": dev, "name": name, "role": role, "depth": depth,
            "size": sectors * 512 if sectors is not None else None,
            "fstype": props.get("ID_FS_TYPE") or (mounted[0][1] if mounted else None),
            "fsversion": props.get("ID_FS_VERSION"),
            "label": props.get("ID_FS_LABEL") or props.get("ID_PART_ENTRY_NAME"),
            "mounts": [m for m, _ in mounted],
            "swap": dev in swaps,
        })
        for holder in _holders(dev, sys_class):
            if holder not in seen:
                add(holder, depth + 1, seen | {holder})

    parts = _partitions(disk, sys_block)
    if parts:
        for part in parts:
            add(part, 0, {part})
    elif _holders(disk, sys_class) or disk in mounts or disk in swaps:
        add(disk, 0, {disk})  # file system (or LUKS, ...) directly on the whole disk
    return rows


def read_temps(dev_dir):
    """Drive temperatures from the hwmon dir(s) below a block device's "device" (nvme, drivetemp).

    -> ({label: °C}, critical °C or None)
    """
    temps, crit = {}, None
    hwmons = glob.glob(os.path.join(dev_dir, "hwmon[0-9]*")) + glob.glob(os.path.join(dev_dir, "hwmon", "hwmon*"))
    for hw in sorted(hwmons):
        inputs = glob.glob(os.path.join(hw, "temp*_input"))
        for path in sorted(inputs, key=lambda p: int(re.sub(r"\D", "", os.path.basename(p)) or 0)):
            value = _read_int(path)
            if value is None:
                continue
            label = _read_str(path.replace("_input", "_label")) or "Drive"
            temps[label] = value / 1000
            if crit is None:
                c = _read_int(path.replace("_input", "_crit"))
                crit = c / 1000 if c and 0 < c < 200000 else None
        if temps:
            break
    return temps, crit


# ---------------------------------------------------------------------------


class DiskInfoReader:
    """Collects disk details on every sampler tick (runs in the sampler thread)."""

    STATIC_TTL = 30.0  # model, serial, link: practically constant
    TREE_TTL = 5.0  # partitions and mounts: pick up a mounted USB stick quickly
    # Reading a drive temperature sends a command to the drive (~3 ms per sensor on NVMe) and
    # can keep it from idling or spinning down, so temperatures are only read while someone
    # looks at them (temps_wanted, set by the UI) and not more often than this.
    TEMP_TTL = 10.0

    def __init__(self, sys_block=SYS_BLOCK, sys_class=SYS_CLASS_BLOCK, udev_dir=UDEV_DATA, ata_links=ATA_LINKS):
        self.sys_block = sys_block
        self.sys_class = sys_class
        self.udev_dir = udev_dir
        self.ata_links = ata_links
        self._static = {}  # disk -> (time, dict)
        self._tree = {}  # disk -> (time, rows)
        self._temps = {}  # disk -> (time, (temps, crit))
        self.temps_wanted = False

    def static_info(self, name):
        base = os.path.join(self.sys_block, name)
        dev_dir = os.path.join(base, "device")
        devpath = os.path.realpath(base)
        props = udev_props(name, self.sys_class, self.udev_dir)
        bus = disk_bus(name, devpath, props)
        rot = _read_int(os.path.join(base, "queue", "rotational"))
        rotational = None if rot is None else bool(rot)
        usb = _usb_device(devpath) if bus == "USB" else None

        model = props.get("ID_MODEL") or _read_str(os.path.join(dev_dir, "model")) \
            or _read_str(os.path.join(dev_dir, "name"))
        model = model.replace("_", " ").strip() if model else None
        vendor = props.get("ID_VENDOR") or _read_str(os.path.join(dev_dir, "vendor"))
        vendor = vendor.replace("_", " ").strip() if vendor else None
        if usb:
            product = _read_str(os.path.join(usb, "product"))
            maker = _read_str(os.path.join(usb, "manufacturer"))
            vendor = maker or vendor
            model = model or product
        if vendor in ("ATA", "NVMe") or (vendor and model and vendor.lower() in model.lower()):
            vendor = None

        link = None
        if bus == "NVMe":
            pci = os.path.join(dev_dir, "device")
            cur = pcie_link(_read_str(os.path.join(pci, "current_link_speed")),
                            _read_str(os.path.join(pci, "current_link_width")))
            top = pcie_link(_read_str(os.path.join(pci, "max_link_speed")),
                            _read_str(os.path.join(pci, "max_link_width")))
            link = f"{cur} (max {top[5:]})" if cur and top and cur != top else cur or top
        elif bus == "USB" and usb:
            link = usb_speed(_read_str(os.path.join(usb, "speed")))
        elif bus == "SATA":
            m = re.search(r"/ata(\d+)/", devpath)
            if m:
                link = sata_speed(_read_str(os.path.join(self.ata_links, f"link{m.group(1)}", "sata_spd")))

        sectors = _read_int(os.path.join(base, "size"))
        discard = _read_int(os.path.join(base, "queue", "discard_max_bytes"))
        rpm = props.get("ID_ATA_ROTATION_RATE_RPM")
        return {
            "bus": bus,
            "kind": disk_kind(bus, rotational, _read_str(os.path.join(dev_dir, "type"))),
            "model": model,
            "vendor": vendor,
            "serial": props.get("ID_SERIAL_SHORT") or _read_str(os.path.join(dev_dir, "serial")),
            "firmware": props.get("ID_REVISION") or _read_str(os.path.join(dev_dir, "firmware_rev"))
            or _read_str(os.path.join(dev_dir, "rev")),
            "wwn": props.get("ID_WWN") or _read_str(os.path.join(base, "wwid")),
            "link": link,
            "size": sectors * 512 if sectors is not None else None,
            "logical_sector": _read_int(os.path.join(base, "queue", "logical_block_size")),
            "physical_sector": _read_int(os.path.join(base, "queue", "physical_block_size")),
            "rotational": rotational,
            "rpm": int(rpm) if rpm and rpm.isdigit() and int(rpm) > 1 else None,
            "removable": _read_int(os.path.join(base, "removable")) == 1,
            "read_only": _read_int(os.path.join(base, "ro")) == 1,
            "scheduler": parse_scheduler(_read_str(os.path.join(base, "queue", "scheduler"))),
            "write_cache": _read_str(os.path.join(base, "queue", "write_cache")),
            "discard": bool(discard) if discard is not None else None,
            "ptable": {"gpt": "GPT", "dos": "MBR"}.get(props.get("ID_PART_TABLE_TYPE"),
                                                      props.get("ID_PART_TABLE_TYPE")),
        }

    def _mounts_and_swaps(self):
        mounts = {}
        try:
            for part in psutil.disk_partitions(all=False):
                if part.device.startswith("/dev/loop"):
                    continue
                dev = os.path.basename(os.path.realpath(part.device))
                mounts.setdefault(dev, []).append((clean_text(part.mountpoint), part.fstype))
        except OSError:
            pass
        for entries in mounts.values():
            entries.sort(key=lambda m: (len(m[0]), m[0]))  # "/" before "/var/lib/..." bind mounts
        swaps = {}
        try:
            with open("/proc/swaps") as fh:
                for path, usage in parse_proc_swaps(fh.read()).items():
                    if path.startswith("/dev/"):
                        swaps[os.path.basename(os.path.realpath(path))] = usage
        except OSError:
            pass
        return mounts, swaps

    def read(self, disks):
        """disks: iterable of whole-disk names -> {name: details incl. "temps", "inflight", "volumes"}."""
        now = time.monotonic()
        disks = list(disks)
        mounts = swaps = None
        out = {}
        for name in disks:
            cached = self._static.get(name)
            if not cached or now - cached[0] >= self.STATIC_TTL:
                cached = (now, self.static_info(name))
                self._static[name] = cached
            info = dict(cached[1])

            tree = self._tree.get(name)
            if not tree or now - tree[0] >= self.TREE_TTL:
                if mounts is None:
                    mounts, swaps = self._mounts_and_swaps()
                tree = (now, volume_tree(name, mounts, swaps, self.sys_block, self.sys_class, self.udev_dir))
                self._tree[name] = tree
            info["volumes"] = self._usage(tree[1])

            base = os.path.join(self.sys_block, name)
            temps = self._temps.get(name)
            if self.temps_wanted and (not temps or now - temps[0] >= self.TEMP_TTL):
                temps = (now, read_temps(os.path.join(base, "device")))
                self._temps[name] = temps
            info["temps"], info["temp_crit"] = temps[1] if temps else ({}, None)
            inflight = (_read_str(os.path.join(base, "inflight")) or "").split()
            info["inflight"] = tuple(int(x) for x in inflight[:2]) if len(inflight) >= 2 else None
            out[name] = info
        for cache in (self._static, self._tree, self._temps):
            for name in [n for n in cache if n not in out]:
                del cache[name]
        return out

    @staticmethod
    def _usage(rows):
        """Copy of the volume rows with current used / total of mounted file systems and swap."""
        swaps = None
        result = []
        for row in rows:
            row = dict(row, used=None, total=None)
            if row["mounts"]:
                try:
                    usage = psutil.disk_usage(row["mounts"][0])
                    row["used"], row["total"] = usage.used, usage.used + usage.free
                except OSError:
                    pass
            elif row["swap"]:
                if swaps is None:
                    try:
                        with open("/proc/swaps") as fh:
                            swaps = {os.path.basename(os.path.realpath(p)): u
                                     for p, u in parse_proc_swaps(fh.read()).items()}
                    except OSError:
                        swaps = {}
                if row["dev"] in swaps:
                    row["total"], row["used"] = swaps[row["dev"]]
            result.append(row)
        return result
