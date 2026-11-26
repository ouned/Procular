import os

from procular.providers.diskinfo import (
    DiskInfoReader,
    disk_bus,
    disk_kind,
    holder_role,
    parse_proc_swaps,
    parse_scheduler,
    parse_udev_db,
    pcie_link,
    read_temps,
    sata_speed,
    split_lvm_name,
    usb_speed,
    volume_tree,
)

UDEV_NVME = """\
S:disk/by-id/nvme-Samsung_SSD_970_EVO_Plus_2TB_S4J4NM0T701641F
W:3
I:1234
E:ID_SERIAL_SHORT=S4J4NM0T701641F
E:ID_WWN=eui.002538572190096f
E:ID_MODEL=Samsung SSD 970 EVO Plus 2TB
E:ID_REVISION=2B2QEXM7
E:ID_PART_TABLE_TYPE=gpt
G:systemd
"""


def test_parse_udev_db():
    props = parse_udev_db(UDEV_NVME)
    assert props["ID_MODEL"] == "Samsung SSD 970 EVO Plus 2TB"
    assert props["ID_PART_TABLE_TYPE"] == "gpt"
    assert "disk/by-id/nvme-Samsung_SSD_970_EVO_Plus_2TB_S4J4NM0T701641F" not in props.values()
    assert len(props) == 5


def test_parse_scheduler():
    assert parse_scheduler("[none] mq-deadline ") == "none"
    assert parse_scheduler("mq-deadline [bfq] none") == "bfq"
    assert parse_scheduler("none") == "none"
    assert parse_scheduler("") is None
    assert parse_scheduler(None) is None


def test_parse_proc_swaps():
    text = ("Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority\n"
            "/swap.img                               file\t\t8388604\t\t1024\t\t-1\n"
            "/dev/nvme0n1p4                          partition\t1000\t\t0\t\t-2\n")
    assert parse_proc_swaps(text) == {"/swap.img": (8388604 * 1024, 1024 * 1024),
                                      "/dev/nvme0n1p4": (1000 * 1024, 0)}


def test_link_speeds():
    assert pcie_link("8.0 GT/s PCIe", "4") == "PCIe 3.0 x4"
    assert pcie_link("16.0 GT/s PCIe", "4") == "PCIe 4.0 x4"
    assert pcie_link("Unknown", "0") is None
    assert pcie_link(None, "4") is None
    assert usb_speed("480") == "USB 2.0 (480 Mbit/s)"
    assert usb_speed("5000") == "USB 3.2 Gen 1 (5 Gbit/s)"
    assert usb_speed("12345") == "USB (12.345 Gbit/s)"
    assert usb_speed(None) is None
    assert sata_speed("6.0 Gbps") == "SATA III (6.0 Gbit/s)"
    assert sata_speed("<unknown>") is None


def test_disk_bus_and_kind():
    assert disk_bus("nvme0n1", "/sys/devices/pci0000:00/0000:02:00.0/nvme/nvme0/nvme0n1") == "NVMe"
    usb = "/sys/devices/pci0000:00/0000:00:14.0/usb2/2-1/2-1:1.0/host3/target3:0:0/3:0:0:0/block/sdb"
    assert disk_bus("sdb", usb) == "USB"
    sata = "/sys/devices/pci0000:00/0000:00:17.0/ata3/host2/target2:0:0/2:0:0:0/block/sda"
    assert disk_bus("sda", sata) == "SATA"
    assert disk_bus("vda", "/sys/devices/pci0000:00/0000:00:04.0/virtio1/block/vda") == "VirtIO"
    assert disk_bus("sdc", "/sys/devices/somewhere/sdc", {"ID_BUS": "scsi"}) == "SCSI"
    assert disk_bus("sdc", "/sys/devices/somewhere/sdc") is None
    assert disk_kind("NVMe", False) == "NVMe SSD"
    assert disk_kind("SATA", True) == "Hard disk (HDD)"
    assert disk_kind("SATA", False) == "SSD"
    assert disk_kind("MMC", False, "SD") == "SD card"
    assert disk_kind("MMC", False, "MMC") == "eMMC"
    assert disk_kind(None, None) == "Disk"


def test_split_lvm_name():
    assert split_lvm_name("ubuntu--vg-ubuntu--lv") == "ubuntu-vg/ubuntu-lv"
    assert split_lvm_name("fedora-root") == "fedora/root"
    assert split_lvm_name("luks-1234") == "luks/1234"
    assert split_lvm_name("plain") == "plain"


def _dev(root, name, size=None, majmin=None, holders=(), partition=None, parent=None, dm=None, udev=None,
         udev_dir=None):
    """Fake /sys/class/block/<name> (and /sys/block/<disk>/<part> for partitions)."""
    if parent:
        path = root / "block" / parent / name
        path.mkdir(parents=True)
        os.symlink(path, root / "class" / name)
    else:
        path = root / "class" / name
        path.mkdir(parents=True)
        if partition is None and dm is None:
            os.symlink(path, root / "block" / name)
    if partition is not None:
        (path / "partition").write_text(f"{partition}\n")
    if size is not None:
        (path / "size").write_text(f"{size}\n")
    (path / "holders").mkdir()
    for h in holders:
        (path / "holders" / h).mkdir()
    if dm:
        (path / "dm").mkdir()
        (path / "dm" / "name").write_text(dm[0] + "\n")
        (path / "dm" / "uuid").write_text(dm[1] + "\n")
    if majmin:
        (path / "dev").write_text(majmin + "\n")
        if udev:
            (udev_dir / f"b{majmin}").write_text("".join(f"E:{k}={v}\n" for k, v in udev.items()))
    return path


def _fake_tree(tmp_path):
    for d in ("block", "class", "udev"):
        (tmp_path / d).mkdir()
    udev = tmp_path / "udev"
    _dev(tmp_path, "nvme0n1", size=3907029168, majmin="259:0", udev={"ID_PART_TABLE_TYPE": "gpt"}, udev_dir=udev)
    _dev(tmp_path, "nvme0n1p1", size=2201600, majmin="259:1", partition=1, parent="nvme0n1",
         udev={"ID_FS_TYPE": "vfat", "ID_FS_VERSION": "FAT32", "ID_FS_LABEL": "EFI"}, udev_dir=udev)
    _dev(tmp_path, "nvme0n1p3", size=3900000000, majmin="259:3", partition=3, parent="nvme0n1",
         holders=["dm-0"], udev={"ID_FS_TYPE": "crypto_LUKS", "ID_FS_VERSION": "2"}, udev_dir=udev)
    _dev(tmp_path, "nvme0n1p2", size=4194304, majmin="259:2", partition=2, parent="nvme0n1",
         udev={"ID_FS_TYPE": "swap"}, udev_dir=udev)
    _dev(tmp_path, "dm-0", size=3899990000, majmin="252:0", holders=["dm-1"],
         dm=("luks-abc", "CRYPT-LUKS2-abc-luks-abc"), udev={"ID_FS_TYPE": "LVM2_member"}, udev_dir=udev)
    _dev(tmp_path, "dm-1", size=3899980000, majmin="252:1", dm=("vg0-root", "LVM-xyz"),
         udev={"ID_FS_TYPE": "ext4"}, udev_dir=udev)
    _dev(tmp_path, "sdb", size=1000, majmin="8:16")  # empty disk
    return tmp_path


def test_holder_role(tmp_path):
    root = _fake_tree(tmp_path)
    cls = str(root / "class")
    assert holder_role("dm-0", cls) == ("luks-abc", "decrypted LUKS")
    assert holder_role("dm-1", cls) == ("vg0/root", "LVM volume")
    assert holder_role("nvme0n1p1", cls) == ("nvme0n1p1", None)


def test_volume_tree(tmp_path):
    root = _fake_tree(tmp_path)
    mounts = {"nvme0n1p1": [("/boot/efi", "vfat")], "dm-1": [("/", "ext4"), ("/var/snap", "ext4")]}
    swaps = {"nvme0n1p2": (100, 0)}
    rows = volume_tree("nvme0n1", mounts, swaps, str(root / "block"), str(root / "class"), str(root / "udev"))
    assert [(r["dev"], r["name"], r["depth"]) for r in rows] == [
        ("nvme0n1p1", "nvme0n1p1", 0),
        ("nvme0n1p2", "nvme0n1p2", 0),
        ("nvme0n1p3", "nvme0n1p3", 0),
        ("dm-0", "luks-abc", 1),
        ("dm-1", "vg0/root", 2),
    ]
    efi, swap, luks, crypt, lv = rows
    assert efi["fstype"] == "vfat" and efi["label"] == "EFI" and efi["mounts"] == ["/boot/efi"]
    assert efi["size"] == 2201600 * 512
    assert swap["swap"] and not swap["mounts"]
    assert luks["fstype"] == "crypto_LUKS" and luks["fsversion"] == "2"
    assert crypt["role"] == "decrypted LUKS" and crypt["fstype"] == "LVM2_member"
    assert lv["mounts"] == ["/", "/var/snap"] and lv["role"] == "LVM volume"
    assert volume_tree("sdb", {}, {}, str(root / "block"), str(root / "class"), str(root / "udev")) == []
    # a file system directly on the whole disk
    rows = volume_tree("sdb", {"sdb": [("/media/stick", "exfat")]}, {}, str(root / "block"), str(root / "class"),
                       str(root / "udev"))
    assert [(r["dev"], r["fstype"], r["mounts"]) for r in rows] == [("sdb", "exfat", ["/media/stick"])]


def test_read_temps(tmp_path):
    hw = tmp_path / "device" / "hwmon3"
    hw.mkdir(parents=True)
    for idx, (label, value) in enumerate((("Composite", 43850), ("Sensor 1", 43850), ("Sensor 2", 39850)), 1):
        (hw / f"temp{idx}_input").write_text(f"{value}\n")
        (hw / f"temp{idx}_label").write_text(label + "\n")
    (hw / "temp1_crit").write_text("84850\n")
    temps, crit = read_temps(str(tmp_path / "device"))
    assert list(temps) == ["Composite", "Sensor 1", "Sensor 2"]
    assert temps["Composite"] == 43.85 and crit == 84.85
    # drivetemp (SATA) puts its hwmon one level deeper and has no labels
    dt = tmp_path / "sata" / "hwmon" / "hwmon5"
    dt.mkdir(parents=True)
    (dt / "temp1_input").write_text("31000\n")
    assert read_temps(str(tmp_path / "sata")) == ({"Drive": 31.0}, None)
    assert read_temps(str(tmp_path / "nothing")) == ({}, None)


def test_reader_static_info(tmp_path):
    root = _fake_tree(tmp_path)
    base = root / "block" / "nvme0n1"
    (base / "queue").mkdir()
    for name, value in (("rotational", "0"), ("scheduler", "[none] mq-deadline"), ("logical_block_size", "512"),
                        ("physical_block_size", "4096"), ("discard_max_bytes", "2199023255040"),
                        ("write_cache", "write back")):
        (base / "queue" / name).write_text(value + "\n")
    (base / "device").mkdir()
    (base / "device" / "model").write_text("Samsung SSD 970 EVO Plus 2TB   \n")
    (base / "device" / "serial").write_text("S4J4NM0T701641F  \n")
    (base / "inflight").write_text("       1        2\n")
    reader = DiskInfoReader(str(root / "block"), str(root / "class"), str(root / "udev"), str(root / "ata"))
    reader._mounts_and_swaps = lambda: ({}, {})  # keep the real mount table out of the fake tree
    info = reader.read(["nvme0n1"])["nvme0n1"]
    assert info["bus"] == "NVMe" and info["kind"] == "NVMe SSD"
    assert info["model"] == "Samsung SSD 970 EVO Plus 2TB" and info["serial"] == "S4J4NM0T701641F"
    assert info["size"] == 3907029168 * 512
    assert (info["logical_sector"], info["physical_sector"]) == (512, 4096)
    assert info["scheduler"] == "none" and info["discard"] is True and info["rotational"] is False
    assert info["ptable"] == "GPT"
    assert info["inflight"] == (1, 2)
    assert info["temps"] == {}  # only read while the UI asks for them
    hw = base / "device" / "hwmon1"
    hw.mkdir()
    (hw / "temp1_input").write_text("40000\n")
    reader.temps_wanted = True
    assert reader.read(["nvme0n1"])["nvme0n1"]["temps"] == {"Drive": 40.0}
    (hw / "temp1_input").write_text("50000\n")
    assert reader.read(["nvme0n1"])["nvme0n1"]["temps"] == {"Drive": 40.0}  # cached for TEMP_TTL
    assert [v["dev"] for v in info["volumes"]] == ["nvme0n1p1", "nvme0n1p2", "nvme0n1p3", "dm-0", "dm-1"]
    assert reader.read([]) == {} and not reader._static
