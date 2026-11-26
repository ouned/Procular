"""Periodic sampling of processes and system statistics."""

import os
import time
from concurrent.futures import ThreadPoolExecutor

from gi.repository import GLib, GObject

from .gpu import merge_process_usage
from .processes import ProcessScanner, top_cpu_consumer
from .system import History, SystemSampler


# update interval while no window is visible (if the configured one is shorter)
BACKGROUND_INTERVAL_MS = 5000


class Sampler(GObject.Object):
    """Emits 'tick' on the main thread after every completed sample."""

    __gsignals__ = {
        "tick": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "paused-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, config):
        super().__init__()
        self.config = config
        self.scanner = ProcessScanner()
        self.scanner.binary_names = config.get("binary_names")
        self.system = SystemSampler()
        self.ncpu = self.system.ncpu
        self.history = History(config.get("history_samples"), self.ncpu,
                               [d.key for d in self.system.gpu.devices])
        self.records = {}
        self.snapshot = None
        self.total_threads = 0
        self.tick_count = 0
        self.paused = False
        self.background = False  # no window is visible: sample at a slow rate
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sampler")
        self._busy = False
        self._late = 0  # timer ticks skipped because the previous sample was still running
        self._timer = 0
        # pids whose records carry GPU figures; replaced by the sampler thread, never changed in place
        self._gpu_pids = set()
        config.connect("changed", self._on_config_changed)

    def start(self):
        self.refresh()
        self._restart_timer()

    def _restart_timer(self):
        if self._timer:
            GLib.source_remove(self._timer)
        self._timer = GLib.timeout_add(self._interval_ms(), self._on_timer)

    def _interval_ms(self):
        interval = self.config.get("interval_ms")
        if self.background and self.config.get("background_slowdown"):
            interval = max(interval, BACKGROUND_INTERVAL_MS)
        return interval

    def _on_config_changed(self, _config, key):
        if key in ("interval_ms", "background_slowdown") and self._timer:
            self._restart_timer()
        elif key == "binary_names":
            self.scanner.binary_names = self.config.get("binary_names")  # read once per scan
            if not self.paused:
                self.refresh()  # show the new names now, not at the next tick

    def set_background(self, background):
        """Tell the sampler whether anybody can see the result (see ProcularApp._update_background)."""
        if background == self.background:
            return
        self.background = background
        self.system.set_background(background)
        if self._timer:
            self._restart_timer()
            if not background and not self.paused:
                self.refresh()  # visible again: don't show data that is seconds old

    def set_paused(self, paused):
        if paused != self.paused:
            self.paused = paused
            self.system.set_paused(paused)
            self.emit("paused-changed")

    def _on_timer(self):
        if not self.paused:
            if self._busy:
                self._late += 1
            else:
                self.refresh()
        return True

    def refresh(self):
        if self._busy:
            return
        self._busy = True
        # A sample that takes longer than the interval makes the timer skip the next one. The
        # samples are then that much further apart: a slower pace, not a gap in the history
        # (which the graphs would show as missing data on every sample).
        interval = self.interval * (1 + self._late)
        self._late = 0
        future = self._executor.submit(self._work)
        future.add_done_callback(lambda f: GLib.idle_add(self._apply, f, interval))

    def _work(self):
        records, threads = self.scanner.scan()
        snap = self.system.sample(records)
        self._gpu_pids = merge_process_usage(records, snap.pop("gpu_procs"), self._gpu_pids)
        return records, threads, snap

    def _apply(self, future, interval):
        self._busy = False
        try:
            records, threads, snap = future.result()
        except Exception:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            return False
        snap["io_read"] = snap["disk_read"]
        snap["io_write"] = snap["disk_write"]
        self.records = records
        self.total_threads = threads
        self.snapshot = snap
        self.tick_count += 1

        top_mem = top_io = top_gpu = None
        best_mem = best_io = best_gpu = -1
        for rec in records.values():
            if rec["rss"] > best_mem:
                best_mem, top_mem = rec["rss"], rec
            io = rec["disk_total"] or 0
            if io > best_io:
                best_io, top_io = io, rec
        # (only processes watched by a details window have figures otherwise: not a "top" process)
        for pid in self._gpu_pids if self.system.gpu.tracks_all else ():
            rec = records.get(pid)
            if rec is not None and (rec.get("gpu") or 0) > best_gpu:
                best_gpu, top_gpu = rec["gpu"], rec

        def summary(rec, value):
            return (rec["name"], rec["pid"], value) if rec else None

        self.history.push(
            snap,
            top_cpu_consumer(records),  # (name, pid, pct, count)
            summary(top_mem, best_mem),
            summary(top_io, best_io),
            summary(top_gpu, best_gpu) if best_gpu > 0 else None,
            interval=interval,
        )
        self.emit("tick")
        return False

    @property
    def interval(self):
        return self._interval_ms() / 1000.0

    @staticmethod
    def now():
        return time.time()


def own_uid():
    return os.getuid()
