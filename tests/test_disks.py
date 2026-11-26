import os

from procular.providers.system import _whole_disk, backing_disks


def _blockdev(root, name, slaves=(), partition_of=None):
    if partition_of:
        path = root / partition_of / name
        path.mkdir(parents=True)
        (path / "partition").write_text("3\n")
        os.symlink(path, root / name)  # /sys/class/block/<part> -> .../<disk>/<part>
    else:
        path = root / name
        path.mkdir(parents=True)
    (path / "slaves").mkdir()
    for s in slaves:
        (path / "slaves" / s).mkdir()


def test_stacked_devices_are_not_whole_disks(tmp_path):
    # nvme0n1 -> nvme0n1p3 -> dm-0 (LUKS) -> dm-1 (LVM)
    _blockdev(tmp_path, "nvme0n1")
    _blockdev(tmp_path, "dm-0", slaves=["nvme0n1p3"])
    _blockdev(tmp_path, "dm-1", slaves=["dm-0"])
    _blockdev(tmp_path, "md0", slaves=["sda1", "sdb1"])
    assert _whole_disk("nvme0n1", str(tmp_path))
    assert not _whole_disk("dm-0", str(tmp_path))
    assert not _whole_disk("dm-1", str(tmp_path))
    assert not _whole_disk("md0", str(tmp_path))
    assert not _whole_disk("loop0", str(tmp_path))
    assert not _whole_disk("sdz", str(tmp_path))


def test_backing_disks_resolves_partitions_and_stacks(tmp_path):
    _blockdev(tmp_path, "nvme0n1")
    _blockdev(tmp_path, "sda")
    _blockdev(tmp_path, "sdb")
    _blockdev(tmp_path, "nvme0n1p3", partition_of="nvme0n1")
    _blockdev(tmp_path, "sda1", partition_of="sda")
    _blockdev(tmp_path, "sdb1", partition_of="sdb")
    _blockdev(tmp_path, "dm-0", slaves=["nvme0n1p3"])
    _blockdev(tmp_path, "dm-1", slaves=["dm-0"])
    _blockdev(tmp_path, "md0", slaves=["sda1", "sdb1"])
    root = str(tmp_path)
    assert backing_disks("nvme0n1p3", root) == ["nvme0n1"]
    assert backing_disks("dm-1", root) == ["nvme0n1"]
    assert backing_disks("md0", root) == ["sda", "sdb"]
    assert backing_disks("nvme0n1", root) == ["nvme0n1"]
