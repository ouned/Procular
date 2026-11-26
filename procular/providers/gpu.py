"""GPUs: discovery, utilisation, memory and sensors of every GPU, and GPU use per process.

Every GPU with a DRM render node is found in /sys/class/drm, NVIDIA GPUs through NVML.
Each gets a backend:

* AmdGpu     amdgpu sysfs (gpu_busy_percent, mem_info_*) and hwmon sensors
* NvidiaGpu  NVML (libnvidia-ml.so.1, loaded with ctypes; no nvidia-smi process per sample)
* DrmGpu     any other driver (Intel i915/xe, nouveau, Arm Mali, Adreno, VideoCore, ...):
             utilisation and memory from DRM fdinfo, clocks from sysfs/devfreq, hwmon sensors

GPU use per process comes from DRM fdinfo: the kernel lists the engine time and buffer
memory of every open DRM file in /proc/<pid>/fdinfo/<fd> (Documentation/gpu/drm-usage-stats.rst),
and from NVML for NVIDIA. Finding the DRM files means looking at every descriptor of every
process, so DrmClients does that on every sample only for new processes (which is when
programs open the GPU) and otherwise once every RESCAN_EVERY samples per process; reading
the fdinfo of the few known DRM files is cheap.

GPUs in runtime suspend (the idle discrete GPU of a hybrid laptop) are not touched: any
sysfs or NVML query could wake them up.
"""

import ctypes
import glob
import os
import platform
import re
import threading
import time

from .pci import pci_device_name

SYS_DRM = "/sys/class/drm"
PCI_DEVICES = "/sys/bus/pci/devices"

_PCI_VENDORS = {0x1002: "AMD", 0x8086: "Intel", 0x10DE: "NVIDIA", 0x1AF4: "Virtio", 0x15AD: "VMware",
                0x1234: "QEMU", 0x1A03: "ASPEED", 0x102B: "Matrox", 0x5143: "Qualcomm"}
_DRIVER_LABELS = {"panfrost": "Arm Mali", "panthor": "Arm Mali", "lima": "Arm Mali", "msm": "Qualcomm Adreno",
                  "v3d": "Broadcom VideoCore", "vc4": "Broadcom VideoCore", "etnaviv": "Vivante",
                  "powervr": "Imagination PowerVR", "asahi": "Apple", "virtio_gpu": "Virtio GPU"}
_SKIP_DRIVERS = {"vgem", "vkms"}  # software-only devices

# engines that encode/decode video; everything else counts as graphics/compute
_VIDEO_ENGINE = re.compile(r"video|vcn|vcs|vecs|dec|enc|jpeg|uvd|vce|vpe|media")


def is_video_engine(name):
    return bool(_VIDEO_ENGINE.search(name))


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


def gpu_display_name(vendor_label, raw):
    """Prefer the marketing name in brackets: "Rembrandt [Radeon 680M]" -> "AMD Radeon 680M"."""
    if "[" in raw and raw.endswith("]"):
        raw = raw[raw.index("[") + 1:-1]
    return f"{vendor_label} {raw}"


def _pci_static(dev, driver, fallback):
    """Name, driver and PCI identity of a PCI GPU from sysfs."""
    vendor = _read_str(os.path.join(dev, "vendor"))
    device = _read_str(os.path.join(dev, "device"))
    name = fallback
    try:
        vendor_id, device_id = int(vendor, 16), int(device, 16)
        label = _PCI_VENDORS.get(vendor_id, f"PCI {vendor_id:04x}")
        raw = pci_device_name(vendor_id, device_id)
        name = gpu_display_name(label, raw) if raw else f"{label} GPU {device_id:#06x}"
    except (TypeError, ValueError):
        pass
    return {
        "name": name,
        "driver": f"{driver} (Linux {platform.release()})",
        "vbios": _read_str(os.path.join(dev, "vbios_version")),
        "pci": os.path.basename(dev),
        "pci_id": f"{vendor[2:]}:{device[2:]}" if vendor and device else None,
    }


def _hwmon_dir(dev):
    found = sorted(glob.glob(os.path.join(dev, "hwmon", "hwmon*")))
    return found[0] if found else None


def read_hwmon_gpu(hwmon):
    """Sensors of a GPU hwmon dir: temperatures (°C, by label), power (W), clocks (Hz), fan, voltage."""
    out = {"temps": {}, "power": None, "power_label": None, "power_cap": None,
           "sclk": None, "mclk": None, "fan_rpm": None, "voltage": None}
    if not hwmon:
        return out
    for path in sorted(glob.glob(os.path.join(hwmon, "temp*_input"))):
        value = _read_int(path)
        if value is not None:
            label = _read_str(path.replace("_input", "_label")) or os.path.basename(path)[:-6]
            out["temps"][label] = value / 1000
    # power1_average on most dGPUs, power1_input on APUs / newer kernels (both in µW)
    for name in ("power1_average", "power1_input"):
        value = _read_int(os.path.join(hwmon, name))
        if value is not None:
            out["power"] = value / 1e6
            out["power_label"] = _read_str(os.path.join(hwmon, "power1_label"))
            break
    cap = _read_int(os.path.join(hwmon, "power1_cap")) or _read_int(os.path.join(hwmon, "power1_max"))
    out["power_cap"] = cap / 1e6 if cap else None
    for path in glob.glob(os.path.join(hwmon, "freq*_input")):
        label = _read_str(path.replace("_input", "_label"))
        if label in ("sclk", "mclk"):
            out[label] = _read_int(path)
    out["fan_rpm"] = _read_int(os.path.join(hwmon, "fan1_input"))
    for path in glob.glob(os.path.join(hwmon, "in*_input")):
        if _read_str(path.replace("_input", "_label")) == "vddgfx":
            mv = _read_int(path)
            out["voltage"] = mv / 1000 if mv is not None else None
    return out


def empty_info(key):
    """A GPU reading with every value unknown; all backends return these keys."""
    return {"key": key, "busy": None, "video": None, "suspended": False,
            "vram_used": None, "vram_total": None, "gtt_used": None, "gtt_total": None,
            "temps": {}, "power": None, "power_label": None, "power_cap": None,
            "sclk": None, "mclk": None, "fan_rpm": None, "fan_pct": None, "voltage": None,
            "perf_level": None, "link": None}


def _pcie_link(dev):
    speed = _read_str(os.path.join(dev, "current_link_speed"))
    width = _read_str(os.path.join(dev, "current_link_width"))
    return f"{speed} x{width}" if speed and width else None


# ---------------------------------------------------------------------------
# DRM fdinfo


_UNITS = {"": 1, "B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3}


class FdinfoStats:
    """The usage figures of one DRM client (one open DRM file) from its fdinfo."""

    __slots__ = ("client_id", "engines", "cycles", "total_cycles", "capacity", "memory")

    def __init__(self):
        self.client_id = None
        self.engines = {}  # engine -> busy time (ns)
        self.cycles = {}  # engine -> busy GPU cycles (xe, msm)
        self.total_cycles = {}  # engine -> GPU timestamp the cycles relate to (xe)
        self.capacity = {}  # engine -> number of identical engines behind the name
        self.memory = {}  # region -> resident bytes

    @property
    def engine_names(self):
        return self.engines.keys() | (self.cycles.keys() & self.total_cycles.keys())


# only the keys used below; amdgpu alone prints ~45 drm- lines per file (total/shared/purgeable
# memory per region, ...), and this runs for every DRM file on every sample
_FDINFO_KEYS = re.compile(
    r"^drm-(client-id|engine-capacity-|engine-|total-cycles-|cycles-|resident-|memory-)([^:\s]*):\s*(\d+)[ \t]*(\w*)",
    re.MULTILINE)


def parse_fdinfo(text):
    """/proc/<pid>/fdinfo/<fd> of a DRM file -> FdinfoStats, or None for any other file."""
    if "drm-driver:" not in text and "drm-client-id:" not in text:
        return None
    st = FdinfoStats()
    legacy_mem = {}  # drm-memory-<region>: older kernels, before drm-resident-<region>
    for kind, name, value, unit in _FDINFO_KEYS.findall(text):
        value = int(value)
        if kind == "engine-":
            st.engines[name] = value
        elif kind == "resident-":
            st.memory[name] = value * _UNITS.get(unit, 1)
        elif kind == "client-id":
            st.client_id = value
        elif kind == "cycles-":
            st.cycles[name] = value
        elif kind == "total-cycles-":
            st.total_cycles[name] = value
        elif kind == "engine-capacity-":
            st.capacity[name] = max(1, value)
        else:
            legacy_mem[name] = value * _UNITS.get(unit, 1)
    if not st.memory:
        st.memory = legacy_mem
    return st


class DeviceUsage:
    """What the visible DRM clients of one GPU did during the last interval."""

    __slots__ = ("engines", "memory", "clients", "has_engines")

    def __init__(self):
        self.engines = {}  # engine -> busy % (all clients together, at most 100)
        self.memory = {}  # region -> resident bytes (all clients together)
        self.clients = 0
        self.has_engines = False  # the driver reports engine time at all


def _engine_pct(st, prev, engine, elapsed_ns):
    """Busy % of `engine` between two readings of a client, per engine instance."""
    cap = st.capacity.get(engine, 1)
    if engine in st.total_cycles and engine in prev.total_cycles and engine in prev.cycles:
        dt = st.total_cycles[engine] - prev.total_cycles[engine]
        if dt > 0:
            return min(100.0, max(0, st.cycles[engine] - prev.cycles[engine]) * 100.0 / dt / cap)
        return 0.0
    if engine in st.engines and engine in prev.engines and elapsed_ns:
        return min(100.0, max(0, st.engines[engine] - prev.engines[engine]) * 100.0 / elapsed_ns / cap)
    return 0.0


class DrmClients:
    """Finds the DRM files of all readable processes and turns their fdinfo into usage figures.

    `nodes` maps DRM node names ("renderD128", "card1") to device keys. Processes whose fd
    table cannot be read (other users', unless running as root) are invisible.
    """

    # samples between two looks at an older process's descriptors: each look is a readlink per
    # descriptor (~10 µs); 30 keeps it at ~1 ms per sample on a desktop with ~4000 descriptors
    RESCAN_EVERY = 30
    YOUNG = 10.0  # seconds: a new process's descriptors are looked at on every sample

    def __init__(self, nodes, proc="/proc"):
        self.nodes = nodes
        self.proc = proc
        self._fds = {}  # pid -> (starttime, [(fd, device key), ...])
        self._client_of = {}  # (pid, fd) -> (device key, client id) as last read
        self._prev = {}  # (device key, client id) -> FdinfoStats
        self._last = None
        self._tick = 0
        self.breakdown = {}  # pid -> ({(key, engine): %}, {(key, region): bytes}) of the last sample
        # Procular itself holds ~3 descriptors per process (the scanner keeps /proc files open):
        # far too many to look through on every sample while it is young
        self._own_pid = os.getpid()

    def forget(self):
        """Not sampled for a while: the next sample must not average over the whole gap."""
        self._prev = {}
        self._last = None
        self.breakdown = {}

    def _scan_fds(self, pid):
        base = f"{self.proc}/{pid}/fd/"
        try:
            names = os.listdir(base)
        except OSError:
            return []
        found = []
        nodes = self.nodes
        for fd in names:
            try:
                target = os.readlink(base + fd)
            except OSError:
                continue
            if target.startswith("/dev/dri/"):
                key = nodes.get(target[9:])
                if key is not None:
                    found.append((fd, key))
        return found

    def sample(self, records, now=None, wall=None, skip=frozenset()):
        """records: pid -> process record (pid, starttime, create_time, kthread). skip: keys of
        GPUs whose clients are not to be read now (reading them would wake the GPU).

        Returns ({device key: DeviceUsage}, {pid: (busy %, memory bytes)}). A process's busy %
        is that of its busiest engine; nothing is attributed on the first sample of a client.
        """
        now = time.monotonic() if now is None else now
        wall = time.time() if wall is None else wall
        elapsed_ns = (now - self._last) * 1e9 if self._last is not None else None
        self._last = now
        self._tick += 1
        tick = self._tick

        fds = {}
        rescanned = set()
        old_fds = self._fds
        for pid, rec in records.items():
            if rec.get("kthread"):
                continue
            entry = old_fds.get(pid)
            young = wall - rec["create_time"] < self.YOUNG and pid != self._own_pid
            if entry is None or entry[0] != rec["starttime"] or young or (pid + tick) % self.RESCAN_EVERY == 0:
                entry = (rec["starttime"], self._scan_fds(pid))
                rescanned.add(pid)
            fds[pid] = entry  # also without DRM files: then it is skipped until its next look
        self._fds = fds

        # Descriptors are often duplicates of one DRM file (dup(), inherited by a child): a
        # desktop easily has three times as many DRM descriptors as clients, and the kernel
        # formats the same fdinfo for each of them. A descriptor whose client was already read
        # this sample is not read again. Its process's descriptors are all read again when they
        # are looked at anew, which is also when a descriptor that now holds another file is noticed.
        clients = {}  # (key, client id) -> (pid, stats); a client shared by processes counts once
        old_client_of = self._client_of
        client_of = {}
        for pid, (_start, files) in fds.items():
            fresh = pid in rescanned
            for fd, key in files:
                if key in skip:
                    continue
                if not fresh:
                    ck = old_client_of.get((pid, fd))
                    held = clients.get(ck) if ck is not None else None
                    if held is not None:
                        client_of[(pid, fd)] = ck
                        if pid < held[0]:
                            clients[ck] = (pid, held[1])
                        continue
                try:
                    with open(f"{self.proc}/{pid}/fdinfo/{fd}", encoding="utf-8", errors="replace") as fh:
                        st = parse_fdinfo(fh.read())
                except OSError:
                    continue
                if st is None or st.client_id is None:
                    continue
                ck = (key, st.client_id)
                client_of[(pid, fd)] = ck
                held = clients.get(ck)
                if held is None or pid < held[0]:
                    clients[ck] = (pid, st)
        self._client_of = client_of

        devices = {}
        procs = {}  # pid -> [{(key, engine): %}, memory, {(key, region): bytes}]
        prev = self._prev
        for ck, (pid, st) in clients.items():
            key = ck[0]
            dev = devices.get(key)
            if dev is None:
                dev = devices[key] = DeviceUsage()
            dev.clients += 1
            names = st.engine_names
            if names:
                dev.has_engines = True
            entry = procs.get(pid)
            if entry is None:
                entry = procs[pid] = [{}, 0, {}]
            for region, amount in st.memory.items():
                dev.memory[region] = dev.memory.get(region, 0) + amount
                entry[1] += amount
                entry[2][(key, region)] = entry[2].get((key, region), 0) + amount
            p = prev.get(ck)
            for engine in names:
                pct = _engine_pct(st, p, engine, elapsed_ns) if p is not None else 0.0
                dev.engines[engine] = dev.engines.get(engine, 0.0) + pct
                entry[0][(key, engine)] = entry[0].get((key, engine), 0.0) + pct
        self._prev = {ck: st for ck, (_pid, st) in clients.items()}

        for dev in devices.values():
            for engine, pct in dev.engines.items():
                dev.engines[engine] = min(100.0, pct)
        per_pid = {pid: (min(100.0, max(engines.values(), default=0.0)), mem)
                   for pid, (engines, mem, _regions) in procs.items()}
        # the breakdown by GPU, engine and memory region, for the process details window
        self.breakdown = {pid: (engines, regions) for pid, (engines, _mem, regions) in procs.items()}
        return devices, per_pid


# ---------------------------------------------------------------------------
# devices


class RuntimePm:
    """Lets an idle GPU runtime-suspend although it is being watched.

    Reading a GPU's utilisation or sensors (amdgpu's busy files and hwmon, xe's fdinfo)
    restarts its autosuspend timer, so reading it once per sample keeps an idle dGPU awake for
    as long as Procular runs, once anything has woken it. After IDLE_SAMPLES samples in a row
    found it idle, nothing that touches it is read for a little longer than its autosuspend
    delay; it reads as idle meanwhile. If it neither suspended nor got busy in that time,
    something else keeps it awake: it is read normally again until it has been seen suspended.

    Used by the sampler thread; quiet() may be called from any thread.
    """

    IDLE_SAMPLES = 3
    IDLE_BUSY = 1.0  # % of utilisation below which a sample counts as idle
    MARGIN = 1.0  # seconds beyond the autosuspend delay

    def __init__(self, sysdev):
        self._power = os.path.join(sysdev, "power") if sysdev else None
        delay = self._read_int("autosuspend_delay_ms")
        # control "auto": runtime PM is allowed; a negative delay: no autosuspend
        self.enabled = (self._power is not None and _read_str(os.path.join(self._power, "control")) == "auto"
                        and delay is not None and delay >= 0)
        self.window = (delay or 0) / 1000 + self.MARGIN
        self._idle = 0
        self._armed = True  # False: something else keeps the GPU awake
        self._slept_before = None  # runtime_suspended_time when the quiet window began
        self.quiet_until = 0.0  # time.monotonic(); before it, the GPU is left alone

    def _read_int(self, name):
        return _read_int(os.path.join(self._power, name)) if self._power else None

    def quiet(self, now=None):
        return (time.monotonic() if now is None else now) < self.quiet_until

    def begin(self, suspended, now=None):
        """Once per sample, before the GPU is read. True: leave it alone this time."""
        if not self.enabled:
            return False
        now = time.monotonic() if now is None else now
        if suspended:  # it does suspend: when woken, our reads may be what keeps it awake
            self._armed, self._idle, self.quiet_until = True, 0, 0.0
            return False
        if not self.quiet_until:
            return False
        if now < self.quiet_until:
            return True
        self.quiet_until = 0.0
        slept = self._read_int("runtime_suspended_time")
        if slept is None or self._slept_before is None or slept <= self._slept_before:
            self._armed = False  # left alone, and still awake: not because of us
        return False

    def reading(self, busy, now=None):
        """After a sample that read the GPU: its utilisation in % (None: not known)."""
        if not self.enabled:
            return
        if busy is None or busy >= self.IDLE_BUSY:
            self._idle = 0
            return
        self._idle += 1
        if self._idle >= self.IDLE_SAMPLES and self._armed:
            self._idle = 0
            self._slept_before = self._read_int("runtime_suspended_time")
            self.quiet_until = (time.monotonic() if now is None else now) + self.window

    def wake(self):
        """It got busy during a quiet window (seen without touching it): read it normally."""
        self.quiet_until = 0.0
        self._idle = 0


class GpuDevice:
    """One GPU. `key` (the PCI address, or the sysfs device name of a platform GPU) is stable."""

    uses_fdinfo = False  # utilisation and memory come from DRM fdinfo (DrmClients)
    fdinfo_wakes = False  # reading a client's fdinfo resumes the GPU (xe)

    def __init__(self, key, sysdev):
        self.key = key
        self.sysdev = sysdev  # /sys/devices/... directory, or None
        self.index = 0
        self.static = {}
        self.has_video = False
        self.has_gtt = False
        self.pm = RuntimePm(None)  # disabled; backends that can leave the GPU alone set their own
        self._state = None  # from begin_sample(), for the next read()

    def begin_sample(self):
        """Before a sample reads anything: "suspended", "quiet" (leave it alone) or "awake"."""
        suspended = self.suspended()
        quiet = self.pm.begin(suspended)
        self._state = "suspended" if suspended else "quiet" if quiet else "awake"
        return self._state

    def _take_state(self):
        state = self._state or self.begin_sample()
        self._state = None
        return state

    @property
    def name(self):
        return self.static.get("name", "GPU")

    def suspended(self):
        """In runtime suspend: reading most of its files (or NVML) would wake it up."""
        if self.sysdev is None:
            return False
        return _read_str(os.path.join(self.sysdev, "power", "runtime_status")) == "suspended"

    def set_polling(self, detailed, background):
        pass

    def set_paused(self, paused):
        pass

    def read(self, details, usage):
        """Current values (see empty_info); `usage` is this GPU's DeviceUsage or None."""
        raise NotImplementedError

    process_engines = {}  # pid -> {engine: %} from process_usage(), where the driver tells
    process_memory = {}  # pid -> {region: bytes} from process_usage(), where the driver tells

    def process_usage(self):
        """{pid: (busy %, memory bytes)} from the driver itself (NVML); fdinfo covers the rest."""
        return {}

    def _suspended_info(self):
        info = empty_info(self.key)
        info["suspended"] = True
        info["busy"] = 0.0
        return info


class _BusyAverager:
    """Polls instantaneous busy-% files in the background and averages them.

    amdgpu's gpu_busy_percent (and vcn_busy_percent) are point-in-time snapshots (on APUs
    they flip between 0 and 100 within milliseconds), so a single read per sampler tick is
    close to random. Polling at ~20 Hz and averaging between reads tracks real engine time
    closely. `gate` is a runtime_status file: while it says "suspended" nothing is read, and
    neither while `pm` (a RuntimePm) says to leave the GPU alone.
    """

    def __init__(self, paths, period=0.05, gate=None, pm=None):
        self.paths = list(paths)
        self.period = period  # may be changed at any time
        self.gate = gate
        self.pm = pm
        self._lock = threading.Lock()
        self._sum = [0] * len(self.paths)
        self._count = [0] * len(self.paths)
        self._thread = None
        self._running = threading.Event()
        self._running.set()
        self._stop = threading.Event()

    def set_paused(self, paused):
        """Stop polling while nobody reads the values (read() then returns instant readings)."""
        if paused:
            self._running.clear()
            with self._lock:  # a read while paused is an instant one, not the pre-pause average
                self._sum = [0] * len(self.paths)
                self._count = [0] * len(self.paths)
        else:
            self._running.set()

    def stop(self):
        """End the polling thread and wait for it (the app polls for as long as it runs; tests stop it)."""
        self._stop.set()
        self._running.set()  # a paused thread wakes up to end
        if self._thread is not None:
            self._thread.join()

    def _poll(self):
        while True:
            self._running.wait()
            if self._stop.is_set():
                return
            if (self.pm is None or not self.pm.quiet()) and (self.gate is None or _read_str(self.gate) != "suspended"):
                values = [_read_int(p) for p in self.paths]
                with self._lock:
                    for i, value in enumerate(values):
                        if value is not None:
                            self._sum[i] += value
                            self._count[i] += 1
            self._stop.wait(self.period)

    def read(self):
        """Mean busy % per path since the previous call (instant readings on the first call)."""
        if self._thread is None:
            self._thread = threading.Thread(target=self._poll, name="gpu-busy", daemon=True)
            self._thread.start()
        with self._lock:
            sums, counts = self._sum, self._count
            self._sum = [0] * len(self.paths)
            self._count = [0] * len(self.paths)
        return [s / c if c else _read_int(p) for s, c, p in zip(sums, counts, self.paths)]


# Without details only what the history graphs record is read: utilisation and memory.
BUSY_PERIOD = 0.05  # seconds between amdgpu utilisation polls while the details are shown ...
BUSY_PERIOD_IDLE = 0.25  # ... while they only feed the history
BUSY_PERIOD_BACKGROUND = 1.0  # ... and while no window is visible at all


class AmdGpu(GpuDevice):
    def __init__(self, key, sysdev):
        super().__init__(key, sysdev)
        paths = [os.path.join(sysdev, "gpu_busy_percent")]
        video = os.path.join(sysdev, "vcn_busy_percent")
        self.has_video = os.path.exists(video)
        if self.has_video:
            paths.append(video)
        self.pm = RuntimePm(sysdev)
        self._busy = _BusyAverager(paths, BUSY_PERIOD_IDLE, gate=os.path.join(sysdev, "power", "runtime_status"),
                                   pm=self.pm)
        self.has_gtt = os.path.exists(os.path.join(sysdev, "mem_info_gtt_total"))
        self.hwmon = _hwmon_dir(sysdev)
        self._mem_total = None  # (vram, gtt) sizes
        self.static = _pci_static(sysdev, "amdgpu", "AMD GPU")

    def set_polling(self, detailed, background):
        self._busy.period = BUSY_PERIOD_BACKGROUND if background else BUSY_PERIOD if detailed else BUSY_PERIOD_IDLE

    def set_paused(self, paused):
        self._busy.set_paused(paused)

    def read(self, details, usage):
        state = self._take_state()
        if state == "suspended":
            return self._suspended_info()
        dev = self.sysdev
        info = empty_info(self.key)
        if state == "quiet" and usage is not None and max(usage.engines.values(), default=0.0) >= RuntimePm.IDLE_BUSY:
            self.pm.wake()  # amdgpu's fdinfo (read without touching the GPU) shows it busy again
            state = "awake"
        if state == "quiet":  # left alone so that it can suspend; it was idle when last read
            busy = [0.0, 0.0]
        else:
            busy = self._busy.read()
            if busy[0] is None:
                return info
            self.pm.reading(max(busy[0], busy[1] or 0.0) if len(busy) > 1 else busy[0])
        if self._mem_total is None:  # fixed sizes
            self._mem_total = (_read_int(os.path.join(dev, "mem_info_vram_total")),
                               _read_int(os.path.join(dev, "mem_info_gtt_total")))
        info.update({
            "busy": busy[0],
            "video": busy[1] if self.has_video else None,
            "vram_used": _read_int(os.path.join(dev, "mem_info_vram_used")),
            "vram_total": self._mem_total[0],
            "gtt_used": _read_int(os.path.join(dev, "mem_info_gtt_used")),
            "gtt_total": self._mem_total[1],
        })
        if details and state == "awake":  # these wake the GPU's firmware (SMU)
            info["perf_level"] = _read_str(os.path.join(dev, "power_dpm_force_performance_level"))
            info["link"] = _pcie_link(dev)
            info.update(read_hwmon_gpu(self.hwmon))
        return info


# memory regions of dedicated GPU memory in fdinfo; others (gtt, system, cpu) are system RAM
_LOCAL_REGIONS = ("vram", "local", "lmem", "stolen")


class DrmGpu(GpuDevice):
    """A GPU of any driver that reports DRM fdinfo statistics (Intel, nouveau, Arm, ...).

    Utilisation and memory are those of the processes whose DRM files can be read: as a
    normal user, other users' processes (another login's desktop) are missing.
    """

    uses_fdinfo = True

    def __init__(self, key, sysdev, card, driver):
        super().__init__(key, sysdev)
        self.card = card  # /sys/class/drm/cardN or None
        self.driver = driver
        self.hwmon = _hwmon_dir(sysdev)
        self.is_pci = os.path.exists(os.path.join(sysdev, "vendor"))
        self.fdinfo_wakes = driver == "xe"  # its fdinfo reads the GPU's timestamp register
        self.pm = RuntimePm(sysdev)
        self._last_mem = (None, None)  # (vram_used, gtt_used) of the last reading of its fdinfo
        self._energy = None  # (µJ, monotonic time) of the previous hwmon energy reading
        if self.is_pci:
            self.static = _pci_static(sysdev, driver, f"{_DRIVER_LABELS.get(driver, driver)} GPU")
        else:
            compatible = (_read_str(os.path.join(sysdev, "of_node", "compatible")) or "").split("\0")[0]
            label = _DRIVER_LABELS.get(driver, driver)
            self.static = {"name": f"{label} GPU" if "GPU" not in label else label,
                           "driver": f"{driver} (Linux {platform.release()})",
                           "pci": None, "pci_id": None, "vbios": None, "model": compatible or None}
        self.has_gtt = True  # most of these GPUs keep (nearly) everything in system RAM

    def _clock(self):
        """Current GPU clock in Hz from i915 / xe / devfreq sysfs, or None."""
        if self.card:
            for name in ("gt_act_freq_mhz", "gt_cur_freq_mhz"):  # i915
                mhz = _read_int(os.path.join(self.card, name))
                if mhz:
                    return mhz * 1e6
        xe = [_read_int(p) for p in glob.glob(os.path.join(self.sysdev, "tile*", "gt*", "freq0", "act_freq"))]
        xe = [v for v in xe if v]
        if xe:
            return max(xe) * 1e6
        for path in glob.glob(os.path.join(self.sysdev, "devfreq", "*", "cur_freq")):
            hz = _read_int(path)
            if hz:
                return float(hz)
        return None

    def _energy_power(self):
        """Power in W from the hwmon energy counter (Intel dGPUs have no power1_input)."""
        if not self.hwmon:
            return None
        uj = _read_int(os.path.join(self.hwmon, "energy1_input"))
        if uj is None:
            return None
        now = time.monotonic()
        prev, self._energy = self._energy, (uj, now)
        if prev is None or now <= prev[1] or uj < prev[0]:
            return None
        return (uj - prev[0]) / 1e6 / (now - prev[1])

    def read(self, details, usage):
        state = self._take_state()
        if state == "suspended":
            return self._suspended_info()
        info = empty_info(self.key)
        quiet = state == "quiet"
        if quiet and self.fdinfo_wakes:  # its clients were not looked at (see GpuMonitor.sample)
            info["busy"] = 0.0
            info["vram_used"], info["gtt_used"] = self._last_mem
            return info
        if usage is None or not usage.clients:
            info["busy"] = 0.0  # nobody (visible) has the GPU open
        elif usage.has_engines:
            gfx = [pct for engine, pct in usage.engines.items() if not is_video_engine(engine)]
            video = [pct for engine, pct in usage.engines.items() if is_video_engine(engine)]
            info["busy"] = max(gfx, default=0.0)
            if video:
                self.has_video = True
                info["video"] = max(video)
        if usage is not None:
            local = sum(v for r, v in usage.memory.items() if r.startswith(_LOCAL_REGIONS))
            shared = sum(v for r, v in usage.memory.items() if not r.startswith(_LOCAL_REGIONS))
            info["vram_used"] = local if any(r.startswith(_LOCAL_REGIONS) for r in usage.memory) else None
            info["gtt_used"] = shared
        self._last_mem = (info["vram_used"], info["gtt_used"])
        level = None if info["busy"] is None else max(info["busy"], info["video"] or 0.0)
        if not quiet:
            self.pm.reading(level)
        elif level is not None and level >= RuntimePm.IDLE_BUSY:
            self.pm.wake()  # busy again: fdinfo showed it without touching the GPU
            quiet = False
        if details and not quiet:  # sensors and clocks wake the GPU
            info.update(read_hwmon_gpu(self.hwmon))
            if info["sclk"] is None:
                info["sclk"] = self._clock()
            if info["power"] is None:
                info["power"] = self._energy_power()
            if self.is_pci:
                info["link"] = _pcie_link(self.sysdev)
        return info


# ---------------------------------------------------------------------------
# NVIDIA (NVML)


NVML_SUCCESS = 0
NVML_ERROR_NOT_FOUND = 6
NVML_ERROR_INSUFFICIENT_SIZE = 7
NVML_VALUE_NOT_AVAILABLE = 0xFFFFFFFFFFFFFFFF


class _PciInfo(ctypes.Structure):
    _fields_ = [("busIdLegacy", ctypes.c_char * 16), ("domain", ctypes.c_uint), ("bus", ctypes.c_uint),
                ("device", ctypes.c_uint), ("pciDeviceId", ctypes.c_uint), ("pciSubSystemId", ctypes.c_uint),
                ("busId", ctypes.c_char * 32)]


class _Utilization(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


class _Memory(ctypes.Structure):
    _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong)]


class _ProcessSample(ctypes.Structure):
    _fields_ = [("pid", ctypes.c_uint), ("timeStamp", ctypes.c_ulonglong), ("smUtil", ctypes.c_uint),
                ("memUtil", ctypes.c_uint), ("encUtil", ctypes.c_uint), ("decUtil", ctypes.c_uint)]


class _ProcessInfo(ctypes.Structure):  # nvmlProcessInfo_v2_t, used by the _v2 and _v3 functions
    _fields_ = [("pid", ctypes.c_uint), ("usedGpuMemory", ctypes.c_ulonglong),
                ("gpuInstanceId", ctypes.c_uint), ("computeInstanceId", ctypes.c_uint)]


def nvml_bus_key(bus_id):
    """NVML bus id "00000000:01:00.0" -> sysfs PCI address "0000:01:00.0"."""
    domain, _, rest = bus_id.partition(":")
    try:
        return f"{int(domain, 16):04x}:{rest.lower()}"
    except ValueError:
        return bus_id.lower()


class Nvml:
    """The few NVML calls Procular needs. Methods return None for unavailable values."""

    def __init__(self, lib):
        self.lib = lib

    @classmethod
    def load(cls, name="libnvidia-ml.so.1"):
        try:
            lib = ctypes.CDLL(name)
            init = lib.nvmlInitWithFlags if hasattr(lib, "nvmlInitWithFlags") else None
            rc = init(ctypes.c_uint(0)) if init is not None else lib.nvmlInit_v2()
        except (OSError, AttributeError):
            return None
        return cls(lib) if rc == NVML_SUCCESS else None

    def _uint(self, fn, *args):
        value = ctypes.c_uint()
        try:
            rc = getattr(self.lib, fn)(*args, ctypes.byref(value))
        except AttributeError:
            return None
        return value.value if rc == NVML_SUCCESS else None

    def _text(self, fn, size, *args):
        buf = ctypes.create_string_buffer(size)
        try:
            rc = getattr(self.lib, fn)(*args, buf, ctypes.c_uint(size))
        except AttributeError:
            return None
        return buf.value.decode(errors="replace") if rc == NVML_SUCCESS else None

    def count(self):
        return self._uint("nvmlDeviceGetCount_v2") or 0

    def handle(self, index):
        h = ctypes.c_void_p()
        rc = self.lib.nvmlDeviceGetHandleByIndex_v2(ctypes.c_uint(index), ctypes.byref(h))
        return h if rc == NVML_SUCCESS else None

    def driver_version(self):
        return self._text("nvmlSystemGetDriverVersion", 80)

    def name(self, h):
        return self._text("nvmlDeviceGetName", 96, h)

    def vbios(self, h):
        return self._text("nvmlDeviceGetVbiosVersion", 32, h)

    def pci(self, h):
        """(PCI address, "vendor:device") or (None, None)."""
        fn = getattr(self.lib, "nvmlDeviceGetPciInfo_v3", None) or getattr(self.lib, "nvmlDeviceGetPciInfo_v2", None)
        if fn is None:
            return None, None
        info = _PciInfo()
        if fn(h, ctypes.byref(info)) != NVML_SUCCESS:
            return None, None
        ids = info.pciDeviceId
        return nvml_bus_key(info.busId.decode()), f"{ids & 0xFFFF:04x}:{ids >> 16:04x}"

    def utilization(self, h):
        u = _Utilization()
        return u.gpu if self.lib.nvmlDeviceGetUtilizationRates(h, ctypes.byref(u)) == NVML_SUCCESS else None

    def memory(self, h):
        m = _Memory()
        if self.lib.nvmlDeviceGetMemoryInfo(h, ctypes.byref(m)) != NVML_SUCCESS:
            return None, None
        return m.used, m.total

    def codec(self, h, fn):
        util, period = ctypes.c_uint(), ctypes.c_uint()
        try:
            rc = getattr(self.lib, fn)(h, ctypes.byref(util), ctypes.byref(period))
        except AttributeError:
            return None
        return util.value if rc == NVML_SUCCESS else None

    def temperature(self, h):
        return self._uint("nvmlDeviceGetTemperature", h, ctypes.c_uint(0))  # NVML_TEMPERATURE_GPU

    def clock(self, h, which):
        return self._uint("nvmlDeviceGetClockInfo", h, ctypes.c_uint(which))  # 0 graphics, 2 memory (MHz)

    def process_utilization(self, h, since):
        """[(pid, timestamp µs, sm %, enc %, dec %), ...] of the samples newer than `since`."""
        fn = getattr(self.lib, "nvmlDeviceGetProcessUtilization", None)
        if fn is None:
            return []
        count = ctypes.c_uint(0)
        rc = fn(h, None, ctypes.byref(count), ctypes.c_ulonglong(since))
        if rc != NVML_ERROR_INSUFFICIENT_SIZE or not count.value:
            return []
        count.value = count.value * 2 + 5  # new samples may arrive in between
        samples = (_ProcessSample * count.value)()
        if fn(h, samples, ctypes.byref(count), ctypes.c_ulonglong(since)) != NVML_SUCCESS:
            return []
        return [(s.pid, s.timeStamp, s.smUtil, s.encUtil, s.decUtil) for s in samples[:count.value]]

    def running_processes(self, h):
        """{pid: used GPU memory bytes or None} of the graphics and compute processes."""
        out = {}
        for base in ("nvmlDeviceGetGraphicsRunningProcesses", "nvmlDeviceGetComputeRunningProcesses"):
            fn = getattr(self.lib, base + "_v3", None) or getattr(self.lib, base + "_v2", None)
            if fn is None:
                continue
            count = ctypes.c_uint(0)
            rc = fn(h, ctypes.byref(count), None)
            if rc != NVML_ERROR_INSUFFICIENT_SIZE:
                continue
            count.value = count.value * 2 + 5  # processes may start in between
            procs = (_ProcessInfo * count.value)()
            if fn(h, ctypes.byref(count), procs) != NVML_SUCCESS:
                continue
            for p in procs[:count.value]:
                mem = None if p.usedGpuMemory == NVML_VALUE_NOT_AVAILABLE else p.usedGpuMemory
                out[p.pid] = max(mem or 0, out.get(p.pid) or 0) if mem is not None else out.get(p.pid)
        return out


class NvidiaGpu(GpuDevice):
    def __init__(self, nvml, handle, key, sysdev):
        super().__init__(key, sysdev)
        self.nvml = nvml
        self.handle = handle
        self._last_sample = 0  # NVML timestamp (µs) of the newest per-process sample seen
        self.process_engines = {}  # pid -> {"sm", "enc", "dec": %} of the last process_usage()
        self.process_memory = {}  # pid -> {"vram": bytes} of the last process_usage()
        name = nvml.name(handle)
        _addr, pci_id = nvml.pci(handle)
        self.static = {"name": name if name and name.startswith("NVIDIA") else f"NVIDIA {name or 'GPU'}",
                       "driver": f"nvidia {nvml.driver_version() or ''}".strip(),
                       "vbios": nvml.vbios(handle), "pci": key, "pci_id": pci_id}
        self.has_video = True

    def read(self, details, usage):
        if self.suspended():
            return self._suspended_info()
        nv, h = self.nvml, self.handle
        info = empty_info(self.key)
        info["busy"] = nv.utilization(h)
        info["vram_used"], info["vram_total"] = nv.memory(h)
        codecs = [v for v in (nv.codec(h, "nvmlDeviceGetEncoderUtilization"),
                              nv.codec(h, "nvmlDeviceGetDecoderUtilization")) if v is not None]
        info["video"] = max(codecs) if codecs else None
        if details:
            temp = nv.temperature(h)
            info["temps"] = {"GPU": float(temp)} if temp is not None else {}
            power = nv._uint("nvmlDeviceGetPowerUsage", h)
            limit = nv._uint("nvmlDeviceGetEnforcedPowerLimit", h)
            info["power"] = power / 1000 if power is not None else None
            info["power_cap"] = limit / 1000 if limit else None
            gr, mem = nv.clock(h, 0), nv.clock(h, 2)
            info["sclk"] = gr * 1e6 if gr is not None else None
            info["mclk"] = mem * 1e6 if mem is not None else None
            info["fan_pct"] = nv._uint("nvmlDeviceGetFanSpeed", h)
            pstate = nv._uint("nvmlDeviceGetPerformanceState", h)
            info["perf_level"] = f"P{pstate}" if pstate is not None and pstate < 32 else None
            gen = nv._uint("nvmlDeviceGetCurrPcieLinkGeneration", h)
            width = nv._uint("nvmlDeviceGetCurrPcieLinkWidth", h)
            info["link"] = f"PCIe gen {gen} x{width}" if gen and width else None
        return info

    def process_usage(self):
        if self.suspended():
            self.process_engines = {}
            self.process_memory = {}
            return {}
        samples = self.nvml.process_utilization(self.handle, self._last_sample)
        busy = {}  # pid -> [busiest engine, sm, enc, dec sums, count]
        for pid, stamp, sm, enc, dec in samples:
            self._last_sample = max(self._last_sample, stamp)
            acc = busy.setdefault(pid, [0.0, 0.0, 0.0, 0.0, 0])
            for i, value in enumerate((max(sm, enc, dec), sm, enc, dec)):
                acc[i] += value
            acc[4] += 1
        out = {pid: (min(100.0, acc[0] / acc[4]), None) for pid, acc in busy.items()}
        self.process_engines = {pid: {"sm": acc[1] / acc[4], "enc": acc[2] / acc[4], "dec": acc[3] / acc[4]}
                                for pid, acc in busy.items()}
        running = self.nvml.running_processes(self.handle)
        self.process_memory = {pid: {"vram": mem} for pid, mem in running.items() if mem is not None}
        for pid, mem in running.items():
            out[pid] = (out.get(pid, (0.0, None))[0], mem)
        return out


# ---------------------------------------------------------------------------


def discover(sys_drm=SYS_DRM, nvml=None, pci_devices=PCI_DEVICES):
    """All GPUs, ordered by key, and the DRM node name -> device key map for DrmClients."""
    try:
        entries = sorted(os.listdir(sys_drm))
    except OSError:
        entries = []
    nodes = {}  # node name -> device dir
    for entry in entries:
        if not ((entry.startswith("card") and entry[4:].isdigit())
                or (entry.startswith("renderD") and entry[7:].isdigit())):
            continue
        dev = os.path.realpath(os.path.join(sys_drm, entry, "device"))
        if os.path.isdir(dev):
            nodes[entry] = dev
    cards = {dev: os.path.join(sys_drm, name) for name, dev in sorted(nodes.items(), reverse=True)
             if name.startswith("card")}

    devices = {}
    for name, dev in nodes.items():
        if not name.startswith("renderD"):  # a GPU has a render node; display-only devices don't
            continue
        key = os.path.basename(dev)
        if key in devices:
            continue
        driver = os.path.basename(os.path.realpath(os.path.join(dev, "driver")))
        if driver in _SKIP_DRIVERS or driver == "nvidia":  # NVIDIA: through NVML below
            continue
        if driver == "amdgpu" and os.path.exists(os.path.join(dev, "gpu_busy_percent")):
            devices[key] = AmdGpu(key, dev)
        else:
            devices[key] = DrmGpu(key, dev, cards.get(dev), driver)

    if nvml is not None:
        for i in range(nvml.count()):
            h = nvml.handle(i)
            if h is None:
                continue
            key, _ids = nvml.pci(h)
            key = key or f"nvidia{i}"
            sysdev = os.path.join(pci_devices, key)
            devices[key] = NvidiaGpu(nvml, h, key, sysdev if os.path.isdir(sysdev) else None)

    ordered = [devices[k] for k in sorted(devices)]
    for i, dev in enumerate(ordered):
        dev.index = i
    node_keys = {name: os.path.basename(dev) for name, dev in nodes.items() if os.path.basename(dev) in devices}
    return ordered, node_keys


class GpuMonitor:
    """All GPUs of the machine and the GPU use of processes.

    want_processes: also work out the GPU use of every process (the process list shows it).
    watch(pid): work out the GPU use of that process, broken down by GPU, engine and memory
    region (a details window shows it); only its descriptors are looked at for this.
    Without either, DRM files are only looked at when a GPU needs them for its own figures.
    """

    def __init__(self, sys_drm=SYS_DRM, proc="/proc", nvml_loader=Nvml.load, pci_devices=PCI_DEVICES):
        nvml = nvml_loader() if nvml_loader is not None else None
        self.devices, nodes = discover(sys_drm, nvml, pci_devices)
        self.clients = DrmClients(nodes, proc)
        self.want_processes = False
        # replaced, never changed in place: the sampler thread reads it
        self._watch_counts = {}
        self.watched = frozenset()
        self._fdinfo_needed = any(d.uses_fdinfo for d in self.devices)

    def watch(self, pid, on=True):
        if on:
            self._watch_counts[pid] = self._watch_counts.get(pid, 0) + 1
        else:
            count = self._watch_counts.get(pid, 0) - 1
            if count > 0:
                self._watch_counts[pid] = count
            else:
                self._watch_counts.pop(pid, None)
        self.watched = frozenset(self._watch_counts)

    @property
    def tracks_all(self):
        """The per-process figures cover every (readable) process, not only watched ones."""
        return self.want_processes

    @property
    def available(self):
        return bool(self.devices)

    def set_polling(self, detailed, background):
        for dev in self.devices:
            dev.set_polling(detailed, background)

    def set_paused(self, paused):
        for dev in self.devices:
            dev.set_paused(paused)

    def sample(self, details, records=None):
        """([reading per device, in device order], {pid: (busy %, memory bytes)}, {pid: breakdown}).

        Per-process figures are collected for every process while want_processes is set,
        otherwise for the watched ones only. The breakdown (watched processes only) is a list
        of {"key", "index", "name", "busy", "engines": {engine: %}, "memory": {region: bytes}},
        one entry per GPU the process uses.
        """
        watched = self.watched
        # first which GPUs are asleep or to be left alone, so that reading fdinfo doesn't wake them
        skip = frozenset(dev.key for dev in self.devices if dev.begin_sample() != "awake" and dev.fdinfo_wakes)
        usage, per_pid = {}, {}
        if records is not None and (self.want_processes or self._fdinfo_needed):
            usage, per_pid = self.clients.sample(records, skip=skip)
        elif records is not None and watched:
            usage, per_pid = self.clients.sample({pid: records[pid] for pid in watched if pid in records}, skip=skip)
        else:
            self.clients.forget()
        infos = []
        for dev in self.devices:
            try:
                infos.append(dev.read(details, usage.get(dev.key)))
            except Exception:  # noqa: BLE001 - a broken sensor must not break the sample
                infos.append(empty_info(dev.key))
        if not self.want_processes and not watched:
            return infos, {}, {}
        for dev in self.devices:
            try:
                extra = dev.process_usage()
            except Exception:  # noqa: BLE001
                continue
            for pid, (pct, mem) in extra.items():
                old_pct, old_mem = per_pid.get(pid, (0.0, None))
                total_mem = (old_mem or 0) + mem if mem is not None else old_mem
                per_pid[pid] = (max(old_pct or 0.0, pct or 0.0), total_mem)
        if not self.want_processes:
            per_pid = {pid: v for pid, v in per_pid.items() if pid in watched}
        return infos, per_pid, {pid: self._breakdown(pid) for pid in watched}

    def _breakdown(self, pid):
        engines, regions = self.clients.breakdown.get(pid, ({}, {}))
        out = []
        for dev in self.devices:
            dev_engines = {e: pct for (k, e), pct in engines.items() if k == dev.key}
            dev_memory = {r: b for (k, r), b in regions.items() if k == dev.key}
            nv = dev.process_engines.get(pid)
            if nv is not None:
                dev_engines.update(nv)
            nv_mem = dev.process_memory.get(pid)
            if nv_mem is not None:
                dev_memory.update(nv_mem)
            if not dev_engines and not dev_memory:
                continue
            out.append({"key": dev.key, "index": dev.index, "name": dev.name, "engines": dev_engines,
                        "memory": dev_memory, "busy": min(100.0, max(dev_engines.values(), default=0.0))})
        return out


_ENGINE_LABELS = {
    "gfx": "Graphics", "compute": "Compute", "dma": "Copy", "dec": "Video decode", "enc": "Video encode",
    "enc_1": "Video encode 2", "jpeg": "JPEG", "vpe": "Video processing", "vcn": "Video", "uvd": "Video decode",
    "vce": "Video encode", "render": "Render", "copy": "Copy", "video": "Video", "video-enhance": "Video enhance",
    "rcs": "Render", "bcs": "Copy", "ccs": "Compute", "vcs": "Video", "vecs": "Video enhance", "gpu": "GPU",
    "fragment": "Fragment", "vertex-tiler": "Vertex/tiler", "bin": "Binning", "csd": "Compute", "tfu": "Texture",
    "sm": "Graphics/compute",
}
_REGION_LABELS = {"vram": "VRAM", "gtt": "GTT", "cpu": "System RAM", "system": "System RAM", "system0": "System RAM",
                  "local0": "VRAM", "vram0": "VRAM", "stolen-system0": "Stolen", "stolen0": "Stolen"}


def engine_label(name):
    """Driver engine name -> readable name: "gfx" -> "Graphics", "rcs" -> "Render"."""
    return _ENGINE_LABELS.get(name, name.replace("_", " ").replace("-", " ").capitalize())


def region_label(name):
    """fdinfo memory region -> readable name: "vram" -> "VRAM", "system0" -> "System RAM"."""
    return _REGION_LABELS.get(name, name)


def merge_process_usage(records, usage, previous):
    """Put the per-process GPU figures into the scanner's records as "gpu" and "gpu_mem".

    Idle processes keep the very same record dict from scan to scan (the tree model skips
    unchanged ones), so a record is replaced by a copy instead of being changed in place.
    `previous` is what the last call returned: the pids that had figures. Returns the new set.
    """
    for pid in previous | usage.keys():
        rec = records.get(pid)
        if rec is None:
            continue
        pct, mem = usage.get(pid, (None, None))
        if rec.get("gpu") != pct or rec.get("gpu_mem") != mem:
            rec = dict(rec)
            rec["gpu"] = pct
            rec["gpu_mem"] = mem
            records[pid] = rec
    return set(usage)
