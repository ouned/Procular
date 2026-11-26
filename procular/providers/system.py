"""System wide statistics and their history (for graphs)."""

import glob
import os
import time
from collections import deque

import psutil

from ..util import linux
from ..util.timeline import SampleTime
from .diskinfo import DiskInfoReader
from .gpu import GpuMonitor
from .netinfo import NetInfoReader

_SKIP_DISK_PREFIXES = ("loop", "ram", "zram", "fd", "sr")
_SKIP_NIC_PREFIXES = ("lo",)


def _whole_disk(name, sys_block="/sys/block"):
    """A physical whole disk: not a partition, not loop/zram/..., and not stacked on other
    block devices (dm-crypt, LVM, md RAID), whose I/O would otherwise be counted twice."""
    if name.startswith(_SKIP_DISK_PREFIXES):
        return False
    path = os.path.join(sys_block, name)
    if not os.path.exists(path):
        return False
    try:
        return not os.listdir(os.path.join(path, "slaves"))
    except OSError:
        return True


def backing_disks(dev, sys_class="/sys/class/block"):
    """Physical whole disks a block device lives on: nvme0n1p3 -> [nvme0n1], LVM-on-LUKS dm-1 -> [nvme0n1]."""
    path = os.path.join(sys_class, dev)
    try:
        slaves = os.listdir(os.path.join(path, "slaves"))
    except OSError:
        slaves = []
    if slaves:
        return sorted({d for s in slaves for d in backing_disks(s, sys_class)})
    if os.path.exists(os.path.join(path, "partition")):
        return [os.path.basename(os.path.realpath(os.path.join(path, "..")))]
    return [dev]


def cpu_governor():
    """Current cpufreq scaling governor(s), e.g. "powersave", or None if unavailable.

    CPUs normally share a governor; if they differ, all distinct ones are listed.
    """
    governors = set()
    for path in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_governor"):
        try:
            with open(path) as fh:
                governors.add(fh.read().strip())
        except OSError:
            continue
    governors.discard("")
    return ", ".join(sorted(governors)) or None


_CACHE_SUFFIX = {"Data": "d", "Instruction": "i", "Unified": ""}
_SIZE_UNITS = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}


def _parse_cache_size(text):
    """sysfs cache size ("32K", "16384K") in bytes, or None."""
    if not text:
        return None
    mult = _SIZE_UNITS.get(text[-1].upper(), 1)
    try:
        return int(text.rstrip("KkMmGg")) * mult
    except ValueError:
        return None


def cpu_caches(sys_cpu="/sys/devices/system/cpu"):
    """CPU caches as [(name, [size, ...]), ...] sorted by level, e.g. ("L1d", [32768] * 8).

    One size per cache *instance*: caches shared between CPUs (shared_cpu_list) are counted
    once. Sizes may differ between instances on hybrid CPUs (P-/E-cores).
    """
    seen = {}
    for idx in glob.glob(os.path.join(sys_cpu, "cpu[0-9]*", "cache", "index[0-9]*")):
        level = _read_int(os.path.join(idx, "level"))
        ctype = _read_str(os.path.join(idx, "type"))
        size = _parse_cache_size(_read_str(os.path.join(idx, "size")))
        if level is None or ctype not in _CACHE_SUFFIX or not size:
            continue
        shared = _read_str(os.path.join(idx, "shared_cpu_list")) or idx
        seen[(level, ctype, shared)] = size
    caches = {}
    for (level, ctype, _), size in seen.items():
        caches.setdefault((level, ctype), []).append(size)
    order = {"Data": 0, "Instruction": 1, "Unified": 2}
    return [(f"L{level}{_CACHE_SUFFIX[ctype]}", sorted(sizes, reverse=True))
            for (level, ctype), sizes in sorted(caches.items(), key=lambda kv: (kv[0][0], order[kv[0][1]]))]


def _read_str(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def _read_int(path):
    text = _read_str(path)
    try:
        return int(text) if text is not None else None
    except ValueError:
        return None


PSI_RESOURCES = ("cpu", "memory", "io")
# the stall shares a sample carries; system-wide "full" CPU pressure is always zero and left out
PSI_KEYS = ("cpu_some", "memory_some", "memory_full", "io_some", "io_full")


def _read_psi(paths):
    result = {}
    for resource, path in paths:
        try:
            parsed = linux.parse_pressure(linux.read_text(path))
        except OSError:
            continue
        if parsed:
            result[resource] = parsed
    return result


def read_pressure(base="/proc/pressure"):
    """{resource: parse_pressure()} of PSI_RESOURCES. Empty without PSI (a kernel built without
    it, or booted with psi=0)."""
    return _read_psi((r, f"{base}/{r}") for r in PSI_RESOURCES)


def read_cgroup_pressure(cgroup, root="/sys/fs/cgroup"):
    """The pressure of one cgroup v2 group (path as in /proc/<pid>/cgroup), see read_pressure.
    A group without a resource's controller lacks that resource."""
    if not cgroup or not cgroup.startswith("/") or "/../" in cgroup + "/":
        return {}
    base = root + cgroup.rstrip("/")
    return _read_psi((r, f"{base}/{r}.pressure") for r in PSI_RESOURCES)


class PressureRates:
    """The share of each interval in which tasks stalled, from the PSI time counters.

    The kernel's own averages (avg10 …) trail by up to 10 seconds; the counters give the
    share for exactly the time between two samples, like the other graphs. The first reading
    has nothing to compare with and uses avg10."""

    def __init__(self):
        self._last = None

    def update(self, readings, elapsed):
        """readings: read_pressure(); returns {PSI_KEYS: percent}, or None without PSI."""
        if not readings:
            self._last = None
            return None
        last, self._last = self._last, readings
        result = {}
        for key in PSI_KEYS:
            resource, kind = key.rsplit("_", 1)
            now = readings.get(resource, {}).get(kind)
            if now is None:
                continue
            before = (last or {}).get(resource, {}).get(kind)
            if before is None or not elapsed or now["total"] < before["total"]:
                result[key] = now.get("avg10", 0.0)
            else:
                result[key] = min(100.0, (now["total"] - before["total"]) / (elapsed * 1e4))
        return result


class History:
    """Ring buffers of system samples. Accessed from the main thread only."""

    def __init__(self, size=1200, ncpu=1, gpu_keys=()):
        self.size = size
        d = lambda: deque(maxlen=size)  # noqa: E731
        self.times = d()
        self.cpu_total = d()
        self.cpu_kernel = d()
        self.cpu_cores = [d() for _ in range(ncpu)]
        self.mem_used = d()
        self.mem_cache = d()
        self.swap_used = d()
        self.io_read = d()
        self.io_write = d()
        self.disk_read = d()
        self.disk_write = d()
        self.net_rx = d()
        self.net_tx = d()
        self.pressure = {key: d() for key in PSI_KEYS}  # percent of each interval stalled
        # GPU key -> {"busy", "video", "vram", "gtt": deque}; the GPUs are known from the start
        self.gpus = {key: {"busy": d(), "video": d(), "vram": d(), "gtt": d()} for key in gpu_keys}
        self.top_cpu = d()  # (name, pid, pct)
        self.top_mem = d()
        self.top_io = d()
        self.top_gpu = d()  # (name, pid, pct) while process GPU use is collected, else None
        # Per device. Every deque is as long as `times`, so a sample's position gives its time:
        # a device that is gone (an unplugged disk, a VPN that is down) keeps getting zeros. It
        # is dropped once nothing but those is left, so that devices whose names never come back
        # (the veth interfaces of containers) do not pile up.
        self.per_disk = {}  # name -> (read deque, write deque)
        self.per_disk_busy = {}  # name -> deque of active time %
        self.per_nic = {}  # name -> (rx deque, tx deque)
        self.wifi_signal = {}  # name -> deque of signal quality % (Wi-Fi interfaces only)
        self._missed = {}  # (table id, name) -> samples in a row without the device

    def push(self, snap, top_cpu, top_mem, top_io, top_gpu=None, *, interval=1.0):
        stamp = snap["time"]
        previous = self.times[-1] if self.times else None
        # A slower configured/background cadence is still continuous. A missed update, a
        # pause, or system suspend should not be painted as a long interpolated line.
        gap = previous is not None and stamp.elapsed - previous.elapsed > 1.5 * max(interval, previous.interval)
        self.times.append(stamp._replace(interval=interval, gap=gap))
        self.cpu_total.append(snap["cpu_total"])
        self.cpu_kernel.append(snap["cpu_kernel"])
        cores = snap["cpu_cores"]
        for i, queue in enumerate(self.cpu_cores):
            queue.append(cores[i] if i < len(cores) else None)
        self.mem_used.append(snap["mem_used"])
        self.mem_cache.append(snap["mem_used"] + snap["mem_cache"])
        self.swap_used.append(snap["swap_used"])
        self.io_read.append(snap["io_read"])
        self.io_write.append(snap["io_write"])
        self.disk_read.append(snap["disk_read"])
        self.disk_write.append(snap["disk_write"])
        self.net_rx.append(snap["net_rx"])
        self.net_tx.append(snap["net_tx"])
        pressure = snap.get("pressure") or {}
        for key, queue in self.pressure.items():
            queue.append(pressure.get(key) or 0.0)
        for info in snap.get("gpus") or ():
            h = self.gpus.get(info["key"])
            if h is not None:
                h["busy"].append(info["busy"] or 0)
                h["video"].append(info["video"] or 0)
                h["vram"].append(info["vram_used"] or 0)
                h["gtt"].append(info["gtt_used"] or 0)
        self.top_cpu.append(top_cpu)
        self.top_mem.append(top_mem)
        self.top_io.append(top_io)
        self.top_gpu.append(top_gpu)
        n = len(self.times)
        for name, rd in snap["disks"].items():
            if name not in self.per_disk:
                self.per_disk[name] = (self._padded(n - 1), self._padded(n - 1))
            self.per_disk[name][0].append(rd["read_rate"])
            self.per_disk[name][1].append(rd["write_rate"])
            if name not in self.per_disk_busy:
                self.per_disk_busy[name] = self._padded(n - 1)
            self.per_disk_busy[name].append(rd["busy"])
        for name, nic in snap["nics"].items():
            if name not in self.per_nic:
                self.per_nic[name] = (self._padded(n - 1), self._padded(n - 1))
            self.per_nic[name][0].append(nic["rx_rate"])
            self.per_nic[name][1].append(nic["tx_rate"])
        wifi = snap.get("wifi_quality") or {}
        for name, quality in wifi.items():
            if name not in self.wifi_signal:
                self.wifi_signal[name] = self._padded(n - 1)
            self.wifi_signal[name].append(quality or 0)
        for table, present in ((self.per_disk, snap["disks"]), (self.per_disk_busy, snap["disks"]),
                               (self.per_nic, snap["nics"]), (self.wifi_signal, wifi)):
            if len(table) > len(present) or self._missed:
                self._fill_absent(table, present)

    def _fill_absent(self, table, present):
        """Give the devices of `table` that are not in `present` a zero sample, or drop them."""
        missed = self._missed
        for name in list(table):
            key = (id(table), name)
            if name in present:
                missed.pop(key, None)
                continue
            count = missed.get(key, 0) + 1
            if count >= self.size:  # its whole history has scrolled out
                del table[name]
                missed.pop(key, None)
                continue
            missed[key] = count
            queues = table[name]
            for queue in (queues if isinstance(queues, tuple) else (queues,)):
                queue.append(0)

    def _padded(self, count):
        q = deque(maxlen=self.size)
        q.extend([0] * min(count, self.size))
        return q


class SystemSampler:
    def __init__(self):
        self.ncpu = psutil.cpu_count() or 1
        self._prev_cpu = psutil.cpu_times()
        self._prev_cores = psutil.cpu_times(percpu=True)
        self._prev_disk = psutil.disk_io_counters(perdisk=True) or {}
        self._prev_nic = psutil.net_io_counters(pernic=True) or {}
        self._prev_stats = psutil.cpu_stats()
        self._prev_time = time.monotonic()
        self.gpu = GpuMonitor()
        self.netinfo = NetInfoReader(_SKIP_NIC_PREFIXES)
        self.diskinfo = DiskInfoReader()
        self.pressure = PressureRates()
        self.detailed = False
        self.background = False

    def set_background(self, background):
        """No window is visible (the sampler runs at a slow rate)."""
        self.background = background
        self.gpu.set_polling(self.detailed, background)

    def set_detailed(self, detailed):
        """Collect what only the System Information window shows: per-interface and per-disk
        details, GPU sensors, CPU frequency and governor. All of it costs time on every sample
        (several hundred file reads, two `iw` runs), so it is left out while nobody looks."""
        self.detailed = detailed
        self.diskinfo.temps_wanted = detailed
        self.gpu.set_polling(detailed, self.background)

    def set_paused(self, paused):
        """No samples are being taken: background polling can stop too."""
        self.gpu.set_paused(paused)

    @staticmethod
    def _busy(prev, cur):
        def total(t):
            return sum(t) - getattr(t, "guest", 0) - getattr(t, "guest_nice", 0)

        dt = total(cur) - total(prev)
        if dt <= 0:
            return 0.0, 0.0
        idle = (cur.idle - prev.idle) + (getattr(cur, "iowait", 0) - getattr(prev, "iowait", 0))
        kernel = (cur.system - prev.system) + (getattr(cur, "irq", 0) - getattr(prev, "irq", 0)) + \
            (getattr(cur, "softirq", 0) - getattr(prev, "softirq", 0))
        return max(0.0, min(100.0, 100.0 * (dt - idle) / dt)), max(0.0, min(100.0, 100.0 * kernel / dt))

    def sample(self, records=None):
        """System figures; with the process records also the GPU use per process ("gpu_procs")."""
        stamp = SampleTime.now()
        now = time.monotonic()
        elapsed = max(1e-3, now - self._prev_time)
        self._prev_time = now

        cpu = psutil.cpu_times()
        cores = psutil.cpu_times(percpu=True)
        total, kernel = self._busy(self._prev_cpu, cpu)
        core_values = [self._busy(p, c)[0] for p, c in zip(self._prev_cores, cores)]
        self._prev_cpu, self._prev_cores = cpu, cores

        stats = psutil.cpu_stats()
        ctx_rate = (stats.ctx_switches - self._prev_stats.ctx_switches) / elapsed
        intr_rate = (stats.interrupts - self._prev_stats.interrupts) / elapsed
        self._prev_stats = stats

        try:
            meminfo = linux.parse_kv_kb(linux.read_text("/proc/meminfo"))
        except OSError:
            meminfo = {}
        mem_total = meminfo.get("MemTotal", 0)
        mem_avail = meminfo.get("MemAvailable", 0)
        cache = meminfo.get("Cached", 0) + meminfo.get("Buffers", 0) + meminfo.get("SReclaimable", 0) \
            - meminfo.get("Shmem", 0)
        mem_used = mem_total - mem_avail
        swap_total = meminfo.get("SwapTotal", 0)
        swap_used = swap_total - meminfo.get("SwapFree", 0)
        # three small files: cheap enough for every sample, and the history needs every one
        pressure_avg = read_pressure()
        pressure = self.pressure.update(pressure_avg, elapsed)

        disks = {}
        cur_disk = psutil.disk_io_counters(perdisk=True) or {}
        disk_read = disk_write = 0.0
        for name, c in cur_disk.items():
            if not _whole_disk(name):
                continue
            p = self._prev_disk.get(name)
            if p is None:
                continue
            rr = max(0, c.read_bytes - p.read_bytes) / elapsed
            wr = max(0, c.write_bytes - p.write_bytes) / elapsed
            busy = min(100.0, max(0, getattr(c, "busy_time", 0) - getattr(p, "busy_time", 0)) / (elapsed * 10))
            nr = max(0, c.read_count - p.read_count)
            nw = max(0, c.write_count - p.write_count)
            disks[name] = {
                "read_rate": rr, "write_rate": wr, "busy": busy,
                "read_total": c.read_bytes, "write_total": c.write_bytes,
                "reads": c.read_count, "writes": c.write_count,
                "read_iops": nr / elapsed, "write_iops": nw / elapsed,
                # mean time per request (ms) during the interval, None when idle
                "read_lat": max(0, c.read_time - p.read_time) / nr if nr else None,
                "write_lat": max(0, c.write_time - p.write_time) / nw if nw else None,
            }
            disk_read += rr
            disk_write += wr
        self._prev_disk = cur_disk

        nics = {}
        cur_nic = psutil.net_io_counters(pernic=True) or {}
        net_rx = net_tx = 0.0
        for name, c in cur_nic.items():
            if name.startswith(_SKIP_NIC_PREFIXES):
                continue
            p = self._prev_nic.get(name)
            if p is None:
                continue
            rx = max(0, c.bytes_recv - p.bytes_recv) / elapsed
            tx = max(0, c.bytes_sent - p.bytes_sent) / elapsed
            nics[name] = {"rx_rate": rx, "tx_rate": tx, "rx_total": c.bytes_recv, "tx_total": c.bytes_sent}
            net_rx += rx
            net_tx += tx
        self._prev_nic = cur_nic
        detailed = self.detailed
        net_info = disk_info = freq = governor = None
        if detailed:
            try:
                net_info = self.netinfo.read(cur_nic)
            except Exception:  # noqa: BLE001 - details are optional, never break the sample
                pass
            try:
                disk_info = self.diskinfo.read(disks)
            except Exception:  # noqa: BLE001 - details are optional, never break the sample
                pass
            try:
                freq = psutil.cpu_freq()
            except (OSError, NotImplementedError, AttributeError):
                pass
            governor = cpu_governor()
        try:
            wifi_quality = self.netinfo.wifi_quality()  # recorded all the time, for the history graph
        except Exception:  # noqa: BLE001
            wifi_quality = {}

        gpus, gpu_procs, gpu_detail = self.gpu.sample(detailed, records) if self.gpu.available else ([], {}, {})

        return {
            "time": stamp,
            "cpu_total": total,
            "cpu_kernel": kernel,
            "cpu_cores": core_values,
            "ctx_rate": ctx_rate,
            "intr_rate": intr_rate,
            "freq": freq.current if freq else None,
            "governor": governor,
            "load": os.getloadavg(),
            "uptime": time.time() - psutil.boot_time(),
            "mem_total": mem_total,
            "mem_used": mem_used,
            "mem_avail": mem_avail,
            "mem_cache": max(0, cache),
            "swap_total": swap_total,
            "swap_used": swap_used,
            "meminfo": meminfo,
            "pressure": pressure,  # {PSI_KEYS: % of this interval}, None without PSI
            "pressure_avg": pressure_avg,  # read_pressure(): the kernel's averages
            "disks": disks,
            "disk_read": disk_read,
            "disk_write": disk_write,
            "disk_info": disk_info,
            "io_read": 0.0,
            "io_write": 0.0,
            "nics": nics,
            "net_rx": net_rx,
            "net_tx": net_tx,
            "net_info": net_info,
            "wifi_quality": wifi_quality,
            "gpus": gpus,  # one reading per GPU, in the order of self.gpu.devices
            "gpu_procs": gpu_procs,  # pid -> (busy %, memory), consumed by the Sampler
            "gpu_detail": gpu_detail,  # pid -> GPU use by GPU / engine / region, for watched processes
        }
