import os
import shutil
import subprocess
import time

import pytest

from procular.providers.gpu import (
    AmdGpu,
    DrmClients,
    DrmGpu,
    GpuMonitor,
    Nvml,
    _BusyAverager,
    discover,
    engine_label,
    gpu_display_name,
    is_video_engine,
    merge_process_usage,
    nvml_bus_key,
    parse_fdinfo,
    read_hwmon_gpu,
    region_label,
)
from procular.providers import gpu as gpu_module
from procular.providers.pci import pci_device_name
from procular.providers.system import History
from procular.util.timeline import SampleTime

PCI_IDS = """\
# comment
1001  Kolter Electronic
\t0010  ISA Card
1002  Advanced Micro Devices, Inc. [AMD/ATI]
\t1681  Rembrandt [Radeon 680M]
\t\t17aa 50b4  Some subsystem
\t1638  Cezanne
1003  ULSI Systems
\t1681  Not this one
C 00  Unclassified device
"""


def test_pci_device_name(tmp_path):
    ids = tmp_path / "pci.ids"
    ids.write_text(PCI_IDS)
    assert pci_device_name(0x1002, 0x1681, [str(ids)]) == "Rembrandt [Radeon 680M]"
    assert pci_device_name(0x1002, 0x1638, [str(ids)]) == "Cezanne"
    assert pci_device_name(0x1002, 0xFFFF, [str(ids)]) is None
    assert pci_device_name(0xBEEF, 0x1681, [str(ids)]) is None


def test_pci_device_name_missing_file(tmp_path):
    assert pci_device_name(0x1002, 0x1681, [str(tmp_path / "nope")]) is None


def test_gpu_display_name_prefers_bracketed_marketing_name():
    assert gpu_display_name("AMD", "Rembrandt [Radeon 680M]") == "AMD Radeon 680M"
    assert gpu_display_name("AMD", "Cezanne") == "AMD Cezanne"


@pytest.fixture(autouse=True)
def _stop_busy_averagers(monkeypatch):
    """A _BusyAverager polls in a thread from its first read() on: end those threads with the
    test, or they go on reading files and turn up in other tests (counted reads, CPU time)."""
    created = []
    init = _BusyAverager.__init__

    def tracking_init(self, *args, **kwargs):
        init(self, *args, **kwargs)
        created.append(self)

    monkeypatch.setattr(_BusyAverager, "__init__", tracking_init)
    yield
    for averager in created:
        averager.stop()


def test_busy_averager_averages_between_reads(tmp_path):
    busy = tmp_path / "gpu_busy_percent"
    video = tmp_path / "vcn_busy_percent"
    busy.write_text("100\n")
    video.write_text("40\n")
    avg = _BusyAverager([str(busy), str(video)], period=0.005)
    assert avg.read() == [100, 40]  # first call: instant values, starts polling
    time.sleep(0.05)
    busy.write_text("0\n")
    time.sleep(0.05)
    gfx, vid = avg.read()
    assert 10 < gfx < 90  # mixed 100s and 0s, not a single snapshot
    assert vid == 40
    time.sleep(0.05)
    assert avg.read()[0] == 0  # window was reset by the previous read


def test_busy_averager_missing_file(tmp_path):
    assert _BusyAverager([str(tmp_path / "nope")]).read() == [None]


def test_busy_averager_pause_and_period(tmp_path):
    busy = tmp_path / "gpu_busy_percent"
    busy.write_text("100\n")
    avg = _BusyAverager([str(busy)], period=0.005)
    avg.read()
    avg.set_paused(True)
    time.sleep(0.03)
    avg.read()  # drop what was collected before the pause took effect
    busy.write_text("50\n")
    time.sleep(0.03)
    assert avg._count == [0]  # nothing polled meanwhile
    assert avg.read() == [50]  # ... so the instant value is returned
    avg.set_paused(False)
    time.sleep(0.03)
    assert avg._count[0] > 0


def test_busy_averager_does_not_poll_a_suspended_gpu(tmp_path):
    busy = tmp_path / "gpu_busy_percent"
    gate = tmp_path / "runtime_status"
    busy.write_text("100\n")
    gate.write_text("suspended\n")
    avg = _BusyAverager([str(busy)], period=0.005, gate=str(gate))
    avg.read()
    time.sleep(0.03)
    assert avg._count == [0]
    gate.write_text("active\n")
    time.sleep(0.03)
    assert avg._count[0] > 0


def _write(base, files):
    base.mkdir(parents=True, exist_ok=True)
    for name, value in files.items():
        (base / name).write_text(f"{value}\n")


def test_read_hwmon_gpu_dgpu(tmp_path):
    _write(tmp_path, {
        "temp1_input": 52000, "temp1_label": "edge", "temp2_input": 61000, "temp2_label": "junction",
        "power1_average": 123456789, "power1_label": "PPT", "power1_cap": 250000000,
        "freq1_input": 2400000000, "freq1_label": "sclk", "freq2_input": 1000000000, "freq2_label": "mclk",
        "fan1_input": 1500, "in0_input": 1050, "in0_label": "vddgfx",
    })
    info = read_hwmon_gpu(str(tmp_path))
    assert info["temps"] == {"edge": 52.0, "junction": 61.0}
    assert round(info["power"], 1) == 123.5 and info["power_cap"] == 250.0 and info["power_label"] == "PPT"
    assert info["sclk"] == 2_400_000_000 and info["mclk"] == 1_000_000_000
    assert info["fan_rpm"] == 1500 and info["voltage"] == 1.05


def test_read_hwmon_gpu_apu_and_missing(tmp_path):
    _write(tmp_path, {"temp1_input": 42000, "power1_input": 3445000, "power1_label": "PPT"})
    info = read_hwmon_gpu(str(tmp_path))
    assert info["temps"] == {"temp1": 42.0}  # no label file -> sensor name
    assert info["power"] == 3.445 and info["power_cap"] is None
    assert info["sclk"] is None and info["fan_rpm"] is None
    assert read_hwmon_gpu(None)["temps"] == {}


# ---------------------------------------------------------------------------
# fdinfo

AMDGPU_FDINFO = """\
pos:\t0
flags:\t02104002
mnt_id:\t660
ino:\t1266
drm-driver:\tamdgpu
drm-client-id:\t1589
drm-pdev:\t0000:33:00.0
pasid:\t1587
drm-total-gtt:\t24452 KiB
drm-resident-gtt:\t24452 KiB
drm-total-vram:\t340176 KiB
drm-shared-vram:\t96692 KiB
drm-resident-vram:\t340176 KiB
drm-memory-vram:\t340176 KiB
drm-memory-gtt: \t24452 KiB
amd-evicted-vram:\t0 KiB
drm-engine-gfx:\t149867590664 ns
drm-engine-compute:\t696382312 ns
"""

I915_FDINFO = """\
drm-driver:\ti915
drm-pdev:\t0000:00:02.0
drm-client-id:\t7
drm-engine-render:\t2000000000 ns
drm-engine-copy:\t0 ns
drm-engine-video:\t500000000 ns
drm-engine-capacity-video:\t2
drm-engine-video-enhance:\t0 ns
drm-total-system0:\t64 MiB
drm-resident-system0:\t48 MiB
"""

XE_FDINFO = """\
drm-driver:\txe
drm-client-id:\t3
drm-resident-vram0:\t1 GiB
drm-resident-gtt:\t2048
drm-cycles-rcs:\t1000
drm-total-cycles-rcs:\t10000
drm-cycles-vcs:\t0
drm-total-cycles-vcs:\t10000
drm-engine-capacity-vcs:\t2
"""


def test_parse_fdinfo_amdgpu():
    st = parse_fdinfo(AMDGPU_FDINFO)
    assert st.client_id == 1589
    assert st.engines == {"gfx": 149867590664, "compute": 696382312}
    # drm-resident-* wins over the older drm-memory-* lines
    assert st.memory == {"gtt": 24452 * 1024, "vram": 340176 * 1024}


def test_parse_fdinfo_i915_and_xe():
    st = parse_fdinfo(I915_FDINFO)
    assert st.capacity == {"video": 2}
    assert st.memory == {"system0": 48 << 20}
    assert set(st.engine_names) == {"render", "copy", "video", "video-enhance"}
    xe = parse_fdinfo(XE_FDINFO)
    assert xe.engines == {} and set(xe.engine_names) == {"rcs", "vcs"}
    assert xe.memory == {"vram0": 1 << 30, "gtt": 2048}


def test_parse_fdinfo_legacy_memory_and_non_drm():
    st = parse_fdinfo("drm-driver:\tamdgpu\ndrm-client-id:\t5\ndrm-memory-vram:\t1024 KiB\n")
    assert st.memory == {"vram": 1 << 20}
    assert parse_fdinfo("pos:\t0\nflags:\t02\nmnt_id:\t1\neventfd-count:\t0\n") is None


def test_video_engines():
    assert all(is_video_engine(e) for e in ("video", "video-enhance", "vcn", "dec", "enc_1", "jpeg", "vcs", "vecs"))
    assert not any(is_video_engine(e) for e in ("gfx", "compute", "render", "copy", "rcs", "bcs", "ccs", "gpu"))


class FakeProc:
    """A /proc with DRM files: fd symlinks (dangling is fine for readlink) and fdinfo files."""

    def __init__(self, root):
        self.root = root

    def add_fd(self, pid, fd, target, fdinfo=""):
        (self.root / str(pid) / "fd").mkdir(parents=True, exist_ok=True)
        (self.root / str(pid) / "fdinfo").mkdir(parents=True, exist_ok=True)
        link = self.root / str(pid) / "fd" / str(fd)
        if link.is_symlink():
            link.unlink()
        link.symlink_to(target)
        (self.root / str(pid) / "fdinfo" / str(fd)).write_text(fdinfo)

    def set_fdinfo(self, pid, fd, fdinfo):
        (self.root / str(pid) / "fdinfo" / str(fd)).write_text(fdinfo)


def _client(cid, gfx_ns, video_ns=0, vram_kib=0, driver="i915"):
    return (f"drm-driver:\t{driver}\ndrm-client-id:\t{cid}\ndrm-engine-render:\t{gfx_ns} ns\n"
            f"drm-engine-video:\t{video_ns} ns\ndrm-resident-local0:\t{vram_kib} KiB\n")


def _rec(pid, starttime=1, age=100.0, wall=1000.0, kthread=False):
    return {"pid": pid, "starttime": starttime, "create_time": wall - age, "kthread": kthread}


def test_drm_clients_utilisation_per_device_and_process(tmp_path):
    proc = FakeProc(tmp_path)
    proc.add_fd(100, 5, "/dev/dri/renderD128", _client(1, 0, 0, 1024))
    proc.add_fd(100, 6, "/home/user/file.txt")
    proc.add_fd(100, 7, "/dev/dri/card0", _client(2, 0))  # a second client of the same process
    proc.add_fd(200, 3, "/dev/dri/renderD128", _client(3, 0, 0, 2048))
    clients = DrmClients({"renderD128": "0000:00:02.0", "card0": "0000:00:02.0"}, str(tmp_path))
    records = {100: _rec(100), 200: _rec(200), 2: _rec(2, kthread=True)}

    devices, per_pid = clients.sample(records, now=10.0, wall=1000.0)
    dev = devices["0000:00:02.0"]
    assert dev.clients == 3 and dev.has_engines
    assert dev.engines["render"] == 0.0  # no attribution on a client's first reading
    assert per_pid == {100: (0.0, 1024 * 1024), 200: (0.0, 2048 * 1024)}

    proc.set_fdinfo(100, 5, _client(1, 300_000_000, 0, 1024))  # 30 % of one second
    proc.set_fdinfo(100, 7, _client(2, 200_000_000))  # +20 %: the process uses 50 % of render
    proc.set_fdinfo(200, 3, _client(3, 100_000_000, 600_000_000, 2048))
    devices, per_pid = clients.sample(records, now=11.0, wall=1001.0)
    dev = devices["0000:00:02.0"]
    assert dev.engines["render"] == pytest.approx(60.0)
    assert dev.engines["video"] == pytest.approx(60.0)
    assert per_pid[100][0] == pytest.approx(50.0)
    assert per_pid[200][0] == pytest.approx(60.0)  # its busiest engine: video
    assert dev.memory == {"local0": 3072 * 1024}


def test_drm_clients_capacity_and_cycles(tmp_path):
    proc = FakeProc(tmp_path)
    proc.add_fd(10, 3, "/dev/dri/renderD128", I915_FDINFO.replace("500000000", "0"))
    proc.add_fd(11, 3, "/dev/dri/renderD129", XE_FDINFO)
    clients = DrmClients({"renderD128": "igpu", "renderD129": "xe"}, str(tmp_path))
    records = {10: _rec(10), 11: _rec(11)}
    clients.sample(records, now=1.0, wall=1000.0)
    proc.set_fdinfo(10, 3, I915_FDINFO.replace("500000000", "1000000000"))  # 1 s on 2 video engines
    proc.set_fdinfo(11, 3, XE_FDINFO.replace("drm-cycles-rcs:\t1000", "drm-cycles-rcs:\t3500")
                    .replace("rcs:\t10000", "rcs:\t15000"))
    devices, _ = clients.sample(records, now=3.0, wall=1002.0)  # 2 s
    assert devices["igpu"].engines["video"] == pytest.approx(25.0)  # 1 s / (2 s * 2 engines)
    assert devices["xe"].engines["rcs"] == pytest.approx(50.0)  # 2500 of 5000 GPU cycles
    assert devices["xe"].engines["vcs"] == 0.0  # total cycles did not advance


def test_drm_clients_shared_client_counts_once(tmp_path):
    proc = FakeProc(tmp_path)
    for pid in (300, 301):  # e.g. a descriptor inherited by a child
        proc.add_fd(pid, 4, "/dev/dri/renderD128", _client(9, 0, 0, 4096))
    clients = DrmClients({"renderD128": "gpu"}, str(tmp_path))
    devices, per_pid = clients.sample({301: _rec(301), 300: _rec(300)}, now=1.0, wall=1000.0)
    assert devices["gpu"].clients == 1 and devices["gpu"].memory == {"local0": 4096 * 1024}
    assert set(per_pid) == {300}  # attributed to the lowest pid


def test_drm_clients_reads_a_duplicated_client_once(tmp_path, monkeypatch):
    proc = FakeProc(tmp_path)
    for fd in (5, 6, 7):  # dup()ed descriptors of one DRM file
        proc.add_fd(100, fd, "/dev/dri/renderD128", _client(1, 0, 0, 1024))
    proc.add_fd(200, 3, "/dev/dri/renderD128", _client(1, 0, 0, 1024))  # inherited from 100
    proc.add_fd(200, 4, "/dev/dri/renderD128", _client(2, 0, 0, 2048))
    clients = DrmClients({"renderD128": "gpu"}, str(tmp_path))
    records = {200: _rec(200), 100: _rec(100)}  # the higher pid comes first
    clients.sample(records, now=1.0, wall=1000.0)

    reads = []
    real_open = open

    def counting_open(path, *args, **kwargs):
        reads.append(path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(gpu_module, "open", counting_open, raising=False)
    for i in (5, 6, 7):
        proc.set_fdinfo(100, i, _client(1, 500_000_000, 0, 1024))
    proc.set_fdinfo(200, 3, _client(1, 500_000_000, 0, 1024))
    devices, per_pid = clients.sample(records, now=2.0, wall=1001.0)
    assert len(reads) == 2  # one descriptor per client
    assert devices["gpu"].clients == 2 and devices["gpu"].engines["render"] == pytest.approx(50.0)
    assert per_pid == {100: (pytest.approx(50.0), 1024 * 1024), 200: (0.0, 2048 * 1024)}  # still the lowest pid

    # a descriptor that now holds another DRM file is noticed when its process is looked at anew
    proc.add_fd(100, 6, "/dev/dri/renderD128", _client(3, 0, 0, 512))
    counts = [clients.sample(records, now=3.0 + i, wall=1002.0 + i)[0]["gpu"].clients
              for i in range(DrmClients.RESCAN_EVERY)]
    assert counts[-1] == 3 and counts[0] == 2


def test_drm_clients_rescans_old_processes_only_now_and_then(tmp_path):
    proc = FakeProc(tmp_path)
    proc.add_fd(400, 1, "/home/user/file.txt")
    clients = DrmClients({"renderD128": "gpu"}, str(tmp_path))
    old = {400: _rec(400, age=3600)}
    clients.sample(old, now=1.0, wall=1000.0)
    proc.add_fd(400, 2, "/dev/dri/renderD128", _client(1, 0))  # opens the GPU later in life
    seen = []
    for i in range(DrmClients.RESCAN_EVERY):
        _, per_pid = clients.sample(old, now=2.0 + i, wall=1001.0 + i)
        seen.append(400 in per_pid)
    assert seen.count(True) >= 1 and not all(seen)  # found within one rotation, not on every sample
    # a young process is looked at on every sample, and a reused pid at once
    proc.add_fd(500, 1, "/home/user/file.txt")
    clients.sample({500: _rec(500, age=1, wall=1100.0)}, now=20.0, wall=1100.0)
    proc.add_fd(500, 2, "/dev/dri/renderD128", _client(2, 0))
    assert 500 in clients.sample({500: _rec(500, age=2, wall=1101.0)}, now=21.0, wall=1101.0)[1]
    proc.add_fd(400, 2, "/home/user/other.txt")
    reused = {400: _rec(400, starttime=99, age=3600, wall=1102.0)}
    assert 400 not in clients.sample(reused, now=22.0, wall=1102.0)[1]


def test_merge_process_usage_replaces_changed_records_only():
    idle = {"pid": 1, "cpu": 0.0}
    busy = {"pid": 2, "cpu": 0.0}
    stopped = {"pid": 3, "gpu": 40.0, "gpu_mem": 100}
    records = {1: idle, 2: busy, 3: stopped}
    pids = merge_process_usage(records, {2: (25.0, 4096)}, previous={3})
    assert records[1] is idle  # untouched: still the very same dict
    assert records[2] is not busy and records[2]["gpu"] == 25.0 and records[2]["gpu_mem"] == 4096
    assert "gpu" not in busy  # the scanner's dict was not changed in place
    assert records[3]["gpu"] is None and records[3]["gpu_mem"] is None
    assert pids == {2}
    again = records[2]
    merge_process_usage(records, {2: (25.0, 4096)}, previous=pids)
    assert records[2] is again  # same figures: no new dict


# ---------------------------------------------------------------------------
# discovery and devices


def _sysfs_gpu(sys_root, name, driver, files, nodes, pci=True):
    """A device dir with a driver link and DRM node entries pointing at it."""
    devdir = sys_root / "devices" / name
    _write(devdir, files)
    drv = sys_root / "drivers" / driver
    drv.mkdir(parents=True, exist_ok=True)
    (devdir / "driver").symlink_to(drv)
    if pci:
        _write(devdir, {"vendor": files.get("vendor", "0x8086"), "device": files.get("device", "0x46a6")})
    for node in nodes:
        (sys_root / "class" / "drm" / node).mkdir(parents=True, exist_ok=True)
        (sys_root / "class" / "drm" / node / "device").symlink_to(devdir)
    return devdir


def _fake_sysfs(tmp_path):
    root = tmp_path / "sys"
    (root / "class" / "drm").mkdir(parents=True)
    _sysfs_gpu(root, "0000:33:00.0", "amdgpu", {"gpu_busy_percent": 12, "mem_info_vram_used": 100,
                                                "mem_info_vram_total": 1000, "mem_info_gtt_used": 20,
                                                "mem_info_gtt_total": 2000, "vendor": "0x1002",
                                                "device": "0x1681", "power_dpm_force_performance_level": "auto"},
               ["card1", "renderD129"])
    _sysfs_gpu(root, "0000:00:02.0", "i915", {}, ["card0", "renderD128"])
    _sysfs_gpu(root, "0000:00:01.0", "simpledrm", {}, ["card2"])  # display only: no render node
    _sysfs_gpu(root, "vgem", "vgem", {}, ["renderD130"], pci=False)
    _sysfs_gpu(root, "0000:01:00.0", "nvidia", {"vendor": "0x10de"}, ["card3", "renderD131"])
    _sysfs_gpu(root, "fb000000.gpu", "panthor", {}, ["card4", "renderD132"], pci=False)
    (root / "class" / "drm" / "card0" / "gt_act_freq_mhz").write_text("1300\n")
    return root


def test_discover_finds_render_capable_gpus(tmp_path):
    root = _fake_sysfs(tmp_path)
    devices, nodes = discover(str(root / "class" / "drm"))
    assert [d.key for d in devices] == ["0000:00:02.0", "0000:33:00.0", "fb000000.gpu"]
    assert [d.index for d in devices] == [0, 1, 2]
    intel, amd, mali = devices
    assert isinstance(intel, DrmGpu) and isinstance(amd, AmdGpu) and isinstance(mali, DrmGpu)
    assert intel.card.endswith("card0") and intel.static["pci_id"] == "8086:46a6"
    assert mali.name == "Arm Mali GPU" and mali.static["pci"] is None
    assert nodes == {"card0": "0000:00:02.0", "renderD128": "0000:00:02.0", "card1": "0000:33:00.0",
                     "renderD129": "0000:33:00.0", "card4": "fb000000.gpu", "renderD132": "fb000000.gpu"}


def test_amd_gpu_without_details_skips_sensors(tmp_path):
    dev = tmp_path / "0000:33:00.0"
    _write(dev, {"gpu_busy_percent": 12, "mem_info_vram_used": 100, "mem_info_vram_total": 1000,
                 "mem_info_gtt_used": 20, "mem_info_gtt_total": 2000, "power_dpm_force_performance_level": "auto",
                 "vendor": "0x1002", "device": "0x1681"})
    _write(dev / "hwmon" / "hwmon3", {"temp1_input": 45000, "power1_average": 15000000})
    gpu = AmdGpu("0000:33:00.0", str(dev))
    gpu.set_paused(True)  # no polling thread in the test: instant readings
    full = gpu.read(True, None)
    assert full["temps"] and full["power"] == 15 and full["perf_level"] == "auto"
    lean = gpu.read(False, None)
    assert (lean["busy"], lean["vram_used"], lean["vram_total"], lean["gtt_total"]) == (12, 100, 1000, 2000)
    assert lean["temps"] == {} and lean["power"] is None and lean["perf_level"] is None
    assert set(lean) == set(full)  # same keys either way: the UI formats both
    gpu.set_polling(detailed=True, background=False)
    assert gpu._busy.period == 0.05
    gpu.set_polling(detailed=True, background=True)
    assert gpu._busy.period == 1.0


def test_suspended_gpu_is_not_read(tmp_path):
    dev = tmp_path / "0000:03:00.0"
    _write(dev, {"gpu_busy_percent": 99, "mem_info_vram_used": 5})
    _write(dev / "power", {"runtime_status": "suspended"})
    gpu = AmdGpu("0000:03:00.0", str(dev))
    gpu.set_paused(True)
    info = gpu.read(True, None)
    assert info["suspended"] and info["busy"] == 0.0 and info["vram_used"] is None


def test_drm_gpu_reading(tmp_path):
    root = _fake_sysfs(tmp_path)
    devices, _ = discover(str(root / "class" / "drm"))
    intel = devices[0]
    _write(tmp_path / "sys" / "devices" / "0000:00:02.0" / "hwmon" / "hwmon5", {"energy1_input": 1_000_000})
    intel.hwmon = str(tmp_path / "sys" / "devices" / "0000:00:02.0" / "hwmon" / "hwmon5")
    clients = DrmClients({"renderD128": intel.key}, str(tmp_path / "proc"))
    proc = FakeProc(tmp_path / "proc")
    proc.add_fd(10, 3, "/dev/dri/renderD128", I915_FDINFO)
    clients.sample({10: _rec(10)}, now=1.0, wall=1000.0)
    proc.set_fdinfo(10, 3, I915_FDINFO.replace("render:\t2000000000", "render:\t2400000000")
                    .replace("video:\t500000000", "video:\t700000000"))
    usage, _ = clients.sample({10: _rec(10)}, now=2.0, wall=1001.0)
    info = intel.read(True, usage[intel.key])
    assert info["busy"] == pytest.approx(40.0)
    assert info["video"] == pytest.approx(10.0) and intel.has_video  # 0.2 s on 2 engines
    assert info["vram_used"] is None and info["gtt_used"] == 48 << 20  # all in system RAM
    assert info["sclk"] == 1300e6
    assert info["power"] is None  # energy needs two readings
    (tmp_path / "sys" / "devices" / "0000:00:02.0" / "hwmon" / "hwmon5" / "energy1_input").write_text("3000000\n")
    intel._energy = (1_000_000, time.monotonic() - 2.0)
    assert intel.read(True, usage[intel.key])["power"] == pytest.approx(1.0, rel=0.05)
    idle = intel.read(False, None)
    assert idle["busy"] == 0.0 and idle["sclk"] is None


def test_monitor_skips_fdinfo_when_nobody_needs_it(tmp_path, monkeypatch):
    root = tmp_path / "sys"
    (root / "class" / "drm").mkdir(parents=True)
    _sysfs_gpu(root, "0000:33:00.0", "amdgpu", {"gpu_busy_percent": 12, "vendor": "0x1002"}, ["renderD128"])
    mon = GpuMonitor(str(root / "class" / "drm"), str(tmp_path / "proc"), nvml_loader=None)
    mon.devices[0].set_paused(True)
    calls = []
    monkeypatch.setattr(mon.clients, "sample", lambda records, skip=frozenset(): calls.append(1) or ({}, {7: (5.0, 1)}))
    infos, per_pid, _ = mon.sample(False, {})
    assert infos[0]["busy"] == 12 and per_pid == {} and calls == []  # amdgpu: sysfs is enough
    mon.want_processes = True
    assert mon.sample(False, {})[1] == {7: (5.0, 1)} and calls == [1]



def test_watched_process_gets_a_breakdown_without_tracking_all(tmp_path):
    root = tmp_path / "sys"
    (root / "class" / "drm").mkdir(parents=True)
    _sysfs_gpu(root, "0000:33:00.0", "amdgpu", {"gpu_busy_percent": 12, "vendor": "0x1002"}, ["renderD128"])
    proc = FakeProc(tmp_path / "proc")
    for pid in (10, 11):
        proc.add_fd(pid, 3, "/dev/dri/renderD128", AMDGPU_FDINFO.replace("1589", str(pid)))
    mon = GpuMonitor(str(root / "class" / "drm"), str(tmp_path / "proc"), nvml_loader=None)
    mon.devices[0].set_paused(True)
    now = time.time()
    records = {pid: {"pid": pid, "starttime": 1, "create_time": now - 100, "kthread": False} for pid in (10, 11)}
    mon.watch(10)
    mon.sample(False, records)
    proc.set_fdinfo(10, 3, AMDGPU_FDINFO.replace("1589", "10").replace("149867590664", "149967590664"))
    _, per_pid, detail = mon.sample(False, records)
    assert set(per_pid) == {10} and set(mon.clients._fds) == {10}  # the other process is not looked at
    (gpu,) = detail[10]
    assert gpu["key"] == "0000:33:00.0" and gpu["index"] == 0
    assert set(gpu["engines"]) == {"gfx", "compute"} and gpu["engines"]["gfx"] > 0
    assert gpu["busy"] == gpu["engines"]["gfx"]
    assert gpu["memory"] == {"vram": 340176 * 1024, "gtt": 24452 * 1024}
    mon.watch(10, False)
    assert mon.sample(False, records)[1:] == ({}, {})


def test_watch_refcounts_same_pid(tmp_path):
    root = tmp_path / "sys"
    (root / "class" / "drm").mkdir(parents=True)
    mon = GpuMonitor(str(root / "class" / "drm"), str(tmp_path / "proc"), nvml_loader=None)
    mon.watch(42)
    mon.watch(42)
    mon.watch(42, False)
    assert 42 in mon.watched
    mon.watch(42, False)
    assert 42 not in mon.watched
    mon.watch(42, False)  # extra unwatch is a no-op
    assert 42 not in mon.watched


def test_engine_and_region_labels():
    assert engine_label("gfx") == "Graphics" and engine_label("rcs") == "Render" and engine_label("enc") == "Video encode"
    assert engine_label("some_new-engine") == "Some new engine"
    assert region_label("vram") == "VRAM" and region_label("system0") == "System RAM" and region_label("x9") == "x9"


def test_drm_clients_forget_restarts_the_interval(tmp_path):
    proc = FakeProc(tmp_path)
    proc.add_fd(10, 3, "/dev/dri/renderD128", _client(1, 0))
    clients = DrmClients({"renderD128": "gpu"}, str(tmp_path))
    clients.sample({10: _rec(10)}, now=1.0, wall=1000.0)
    clients.forget()  # e.g. the GPU columns were hidden for a minute
    proc.set_fdinfo(10, 3, _client(1, 30_000_000_000))
    devices, per_pid = clients.sample({10: _rec(10)}, now=61.0, wall=1060.0)
    assert per_pid[10][0] == 0.0  # a fresh start, not 50 % averaged over the gap


def test_history_keeps_one_series_per_gpu():
    h = History(size=5, gpu_keys=["a", "b"])
    snap = {"time": SampleTime(0, 0), "cpu_total": 0, "cpu_kernel": 0, "cpu_cores": [], "mem_used": 0, "mem_cache": 0,
            "swap_used": 0, "io_read": 0, "io_write": 0, "disk_read": 0, "disk_write": 0, "net_rx": 0,
            "net_tx": 0, "disks": {}, "nics": {},
            "gpus": [{"key": "a", "busy": 30.0, "video": None, "vram_used": 5, "gtt_used": None},
                     {"key": "b", "busy": None, "video": 2.0, "vram_used": None, "gtt_used": 7}]}
    h.push(snap, None, None, None, ("game", 42, 30.0))
    assert list(h.gpus["a"]["busy"]) == [30.0] and list(h.gpus["b"]["busy"]) == [0]
    assert list(h.gpus["b"]["gtt"]) == [7] and list(h.top_gpu) == [("game", 42, 30.0)]


# ---------------------------------------------------------------------------
# NVML, against a stub libnvidia-ml built from the NVML struct layouts

NVML_STUB = r"""
#include <string.h>
typedef struct { char busIdLegacy[16]; unsigned int domain, bus, device, pciDeviceId, pciSubSystemId;
                 char busId[32]; } nvmlPciInfo_t;
typedef struct { unsigned int gpu, memory; } nvmlUtilization_t;
typedef struct { unsigned long long total, free, used; } nvmlMemory_t;
typedef struct { unsigned int pid; unsigned long long timeStamp; unsigned int smUtil, memUtil, encUtil, decUtil; }
    nvmlProcessUtilizationSample_t;
typedef struct { unsigned int pid; unsigned long long usedGpuMemory; unsigned int gpuInstanceId, computeInstanceId; }
    nvmlProcessInfo_t;
typedef int *nvmlDevice_t;
static int devs[2];
static int idx(nvmlDevice_t h) { return (int)(h - devs); }
int nvmlInitWithFlags(unsigned int flags) { return 0; }
int nvmlDeviceGetCount_v2(unsigned int *n) { *n = 2; return 0; }
int nvmlDeviceGetHandleByIndex_v2(unsigned int i, nvmlDevice_t *h) { if (i > 1) return 2; *h = &devs[i]; return 0; }
int nvmlSystemGetDriverVersion(char *b, unsigned int n) { strncpy(b, "550.54.14", n); return 0; }
int nvmlDeviceGetName(nvmlDevice_t h, char *b, unsigned int n) {
    strncpy(b, idx(h) ? "NVIDIA GeForce RTX 4070" : "NVIDIA GeForce RTX 3060", n); return 0; }
int nvmlDeviceGetVbiosVersion(nvmlDevice_t h, char *b, unsigned int n) { strncpy(b, "94.06.25.00.1A", n); return 0; }
int nvmlDeviceGetPciInfo_v3(nvmlDevice_t h, nvmlPciInfo_t *p) {
    memset(p, 0, sizeof *p); strcpy(p->busId, idx(h) ? "00000000:02:00.0" : "00000000:01:00.0");
    p->pciDeviceId = (0x2504u << 16) | 0x10de; return 0; }
int nvmlDeviceGetUtilizationRates(nvmlDevice_t h, nvmlUtilization_t *u) { u->gpu = idx(h) ? 5 : 37; u->memory = 12; return 0; }
int nvmlDeviceGetMemoryInfo(nvmlDevice_t h, nvmlMemory_t *m) {
    m->total = 12884901888ULL; m->used = 1073741824ULL; m->free = m->total - m->used; return 0; }
int nvmlDeviceGetEncoderUtilization(nvmlDevice_t h, unsigned int *u, unsigned int *p) { *u = 3; *p = 1000; return 0; }
int nvmlDeviceGetDecoderUtilization(nvmlDevice_t h, unsigned int *u, unsigned int *p) { *u = 9; *p = 1000; return 0; }
int nvmlDeviceGetTemperature(nvmlDevice_t h, unsigned int sensor, unsigned int *t) { *t = 55; return sensor ? 2 : 0; }
int nvmlDeviceGetPowerUsage(nvmlDevice_t h, unsigned int *mw) { *mw = 31500; return 0; }
int nvmlDeviceGetEnforcedPowerLimit(nvmlDevice_t h, unsigned int *mw) { *mw = 170000; return 0; }
int nvmlDeviceGetClockInfo(nvmlDevice_t h, unsigned int type, unsigned int *mhz) { *mhz = type == 0 ? 1320 : 7300; return 0; }
int nvmlDeviceGetFanSpeed(nvmlDevice_t h, unsigned int *s) { return 3; }
int nvmlDeviceGetPerformanceState(nvmlDevice_t h, unsigned int *p) { *p = 2; return 0; }
int nvmlDeviceGetCurrPcieLinkGeneration(nvmlDevice_t h, unsigned int *g) { *g = 4; return 0; }
int nvmlDeviceGetCurrPcieLinkWidth(nvmlDevice_t h, unsigned int *w) { *w = 16; return 0; }
static nvmlProcessUtilizationSample_t samples[] = {
    {4242, 1000, 40, 0, 0, 0}, {4242, 2000, 60, 0, 0, 10}, {777, 1500, 0, 0, 30, 0}};
int nvmlDeviceGetProcessUtilization(nvmlDevice_t h, nvmlProcessUtilizationSample_t *s, unsigned int *count,
                                    unsigned long long since) {
    unsigned int n = 0, i;
    if (idx(h)) return 6;
    for (i = 0; i < 3; i++) if (samples[i].timeStamp > since) n++;
    if (!n) return 6;
    if (!s || *count < n) { *count = n; return 7; }
    for (i = 0, n = 0; i < 3; i++) if (samples[i].timeStamp > since) s[n++] = samples[i];
    *count = n; return 0; }
static int procs(nvmlDevice_t h, unsigned int *count, nvmlProcessInfo_t *p, unsigned int pid, unsigned long long mem) {
    if (idx(h)) { *count = 0; return 0; }
    if (!p || *count < 1) { *count = 1; return 7; }
    p[0].pid = pid; p[0].usedGpuMemory = mem; *count = 1; return 0; }
int nvmlDeviceGetGraphicsRunningProcesses_v3(nvmlDevice_t h, unsigned int *c, nvmlProcessInfo_t *p) {
    return procs(h, c, p, 4242, 268435456ULL); }
int nvmlDeviceGetComputeRunningProcesses_v3(nvmlDevice_t h, unsigned int *c, nvmlProcessInfo_t *p) {
    return procs(h, c, p, 999, 0xFFFFFFFFFFFFFFFFULL); }
"""


@pytest.fixture(scope="module")
def nvml_stub(tmp_path_factory):
    cc = shutil.which("cc") or shutil.which("gcc")
    if cc is None:
        pytest.skip("no C compiler for the NVML stub")
    d = tmp_path_factory.mktemp("nvml")
    (d / "nvml.c").write_text(NVML_STUB)
    subprocess.run([cc, "-shared", "-fPIC", "-o", str(d / "libnvidia-ml.so.1"), str(d / "nvml.c")], check=True)
    return str(d / "libnvidia-ml.so.1")


def test_nvml_bus_key():
    assert nvml_bus_key("00000000:01:00.0") == "0000:01:00.0"
    assert nvml_bus_key("00000001:0A:00.0") == "0001:0a:00.0"


def test_nvidia_gpus_through_nvml(nvml_stub, tmp_path):
    pci = tmp_path / "pci"
    _write(pci / "0000:01:00.0" / "power", {"runtime_status": "active"})
    _write(pci / "0000:02:00.0" / "power", {"runtime_status": "suspended"})
    sys_drm = tmp_path / "drm"
    sys_drm.mkdir()
    mon = GpuMonitor(str(sys_drm), str(tmp_path / "proc"), nvml_loader=lambda: Nvml.load(nvml_stub),
                     pci_devices=str(pci))
    assert [d.key for d in mon.devices] == ["0000:01:00.0", "0000:02:00.0"]
    gpu = mon.devices[0]
    assert gpu.name == "NVIDIA GeForce RTX 3060" and gpu.static["driver"] == "nvidia 550.54.14"
    assert gpu.static["pci_id"] == "10de:2504" and gpu.static["vbios"] == "94.06.25.00.1A"

    infos, _, _ = mon.sample(True, {})
    info = infos[0]
    assert info["busy"] == 37 and info["video"] == 9
    assert info["vram_used"] == 1 << 30 and info["vram_total"] == 12 << 30
    assert info["temps"] == {"GPU": 55.0} and info["power"] == 31.5 and info["power_cap"] == 170
    assert info["sclk"] == 1320e6 and info["mclk"] == 7300e6 and info["fan_pct"] is None
    assert info["perf_level"] == "P2" and info["link"] == "PCIe gen 4 x16"
    assert infos[1]["suspended"] and infos[1]["vram_used"] is None  # not queried: it would wake up
    lean = mon.sample(False, {})[0][0]
    assert lean["busy"] == 37 and lean["temps"] == {} and lean["power"] is None

    mon.want_processes = True
    _, per_pid, _ = mon.sample(False, {})
    assert per_pid[4242] == (50.0, 256 << 20)  # mean of the samples' busiest engine
    assert per_pid[777] == (30.0, None)
    assert per_pid[999] == (0.0, None)  # memory not available
    _, per_pid, _ = mon.sample(False, {})
    assert per_pid[4242] == (0.0, 256 << 20)  # no new samples since the last call

    mon.want_processes = False
    mon.devices[0]._last_sample = 0  # replay the stub's samples
    mon.watch(4242)
    _, per_pid, detail = mon.sample(False, {})
    assert set(per_pid) == {4242}
    (gpu,) = detail[4242]
    assert gpu["engines"] == {"sm": 50.0, "enc": 0.0, "dec": 5.0} and gpu["key"] == "0000:01:00.0"
    assert gpu["memory"] == {"vram": 256 << 20}
    # idle NVIDIA process that still holds VRAM remains visible in the breakdown
    _, _, detail = mon.sample(False, {})
    (gpu,) = detail[4242]
    assert gpu["engines"] == {} and gpu["memory"] == {"vram": 256 << 20}


def test_nvml_missing_library():
    assert Nvml.load("libdoes-not-exist.so.1") is None


def test_real_machine_smoke():
    """Discovery and one sample on this machine must not fail, whatever GPUs it has."""
    mon = GpuMonitor()
    mon.want_processes = True
    records = {int(p): {"pid": int(p), "starttime": 0, "create_time": 0.0, "kthread": False}
               for p in os.listdir("/proc") if p.isdigit()}
    infos, per_pid, _ = mon.sample(True, records)
    assert len(infos) == len(mon.devices)
    for pct, _mem in per_pid.values():
        assert 0.0 <= pct <= 100.0


# ---------------------------------------------------------------------------
# runtime PM: an idle GPU is left alone long enough to suspend


def _pm_dev(tmp_path, name="0000:03:00.0", **power):
    dev = tmp_path / name
    _write(dev, {"gpu_busy_percent": 0, "mem_info_vram_used": 5, "mem_info_vram_total": 10,
                 "mem_info_gtt_used": 1, "mem_info_gtt_total": 2})
    _write(dev / "power", {"control": "auto", "autosuspend_delay_ms": 5000, "runtime_status": "active",
                           "runtime_suspended_time": 100, **power})
    return dev


def test_runtime_pm_leaves_an_idle_gpu_alone_then_gives_up_if_it_stays_awake(tmp_path):
    from procular.providers.gpu import RuntimePm
    dev = _pm_dev(tmp_path)
    pm = RuntimePm(str(dev))
    assert pm.enabled and pm.window == 6.0
    for t in (0, 1, 2):
        assert not pm.begin(False, now=t)
        pm.reading(0.0, now=t)
    assert pm.begin(False, now=3) and pm.quiet(now=3) and pm.begin(False, now=7.9)
    assert not pm.begin(False, now=8.1)  # window over: still awake, never suspended meanwhile
    for t in (9, 10, 11, 12):
        pm.reading(0.0, now=t)
    assert not pm.begin(False, now=13)  # something else keeps it awake: read normally
    assert not pm.begin(True, now=14)  # suspended once: it is worth trying again after it wakes
    for t in (15, 16, 17):
        pm.begin(False, now=t)
        pm.reading(0.0, now=t)
    assert pm.begin(False, now=18)


def test_runtime_pm_stays_armed_when_the_gpu_slept_during_the_window(tmp_path):
    from procular.providers.gpu import RuntimePm
    dev = _pm_dev(tmp_path)
    pm = RuntimePm(str(dev))
    for t in (0, 1, 2):
        pm.reading(0.0, now=t)
    (dev / "power" / "runtime_suspended_time").write_text("4100\n")  # it slept, then was woken
    assert not pm.begin(False, now=9)
    for t in (9, 10, 11):
        pm.reading(0.0, now=t)
    assert pm.begin(False, now=12)
    pm.reading(50.0, now=13)  # a busy reading resets the count
    pm.wake()
    assert not pm.quiet(now=13)


def test_runtime_pm_off_without_autosuspend(tmp_path):
    from procular.providers.gpu import RuntimePm
    assert not RuntimePm(str(_pm_dev(tmp_path, "a", control="on"))).enabled
    assert not RuntimePm(str(_pm_dev(tmp_path, "b", autosuspend_delay_ms=-1))).enabled
    assert not RuntimePm(None).enabled


def test_amd_gpu_is_not_read_while_left_alone(tmp_path):
    dev = _pm_dev(tmp_path)
    _write(dev / "hwmon" / "hwmon3", {"temp1_input": 45000})
    gpu = AmdGpu("0000:03:00.0", str(dev))
    gpu.set_paused(True)  # no polling thread: instant readings
    for _ in range(3):
        assert gpu.read(True, None)["busy"] == 0
    (dev / "gpu_busy_percent").write_text("99\n")
    quiet = gpu.read(True, None)
    assert quiet["busy"] == 0 and quiet["temps"] == {}  # neither busy file nor sensors were read
    assert quiet["vram_used"] == 5  # memory counters live in the driver: still shown
    gpu.pm.quiet_until = 1.0  # window over, it never suspended: read normally from now on
    assert gpu.read(True, None)["busy"] == 99


def test_xe_fdinfo_is_not_read_while_the_gpu_sleeps(tmp_path):
    root = tmp_path / "sys"
    (root / "class" / "drm").mkdir(parents=True)
    devdir = _sysfs_gpu(root, "0000:03:00.0", "xe", {}, ["renderD128"])
    _write(devdir / "power", {"control": "auto", "autosuspend_delay_ms": 1000, "runtime_status": "suspended",
                              "runtime_suspended_time": 100})
    mon = GpuMonitor(str(root / "class" / "drm"), str(tmp_path / "proc"), nvml_loader=None)
    xe = mon.devices[0]
    assert xe.fdinfo_wakes
    proc = FakeProc(tmp_path / "proc")
    proc.add_fd(10, 3, "/dev/dri/renderD128", I915_FDINFO)
    seen = []
    real = mon.clients.sample
    mon.clients.sample = lambda records, skip=frozenset(): seen.append(skip) or real(records, skip=skip)
    infos, _, _ = mon.sample(False, {10: _rec(10)})
    assert infos[0]["suspended"] and seen[-1] == {xe.key}
    (devdir / "power" / "runtime_status").write_text("active\n")
    mon.sample(False, {10: _rec(10)})
    assert seen[-1] == frozenset()
