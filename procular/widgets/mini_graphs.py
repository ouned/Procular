"""Small CPU / memory / I/O graphs for the header bar."""

from gi.repository import Gtk

from ..util.format import fmt_bytes, fmt_rate, fmt_short_rate
from .graph import CPU as CPU_COLOR
from .graph import KERNEL as KERNEL_COLOR
from .graph import MEM as MEM_COLOR
from .graph import READ as READ_COLOR
from .graph import WRITE as WRITE_COLOR
from .graph import Graph, Series


class MiniGraphs(Gtk.Button):
    def __init__(self, sampler):
        super().__init__()
        self.sampler = sampler
        self.add_css_class("flat")
        self.add_css_class("mini-graphs")
        self.set_tooltip_text("System Information (Ctrl+I)")
        h = sampler.history
        box = Gtk.Box(spacing=4)
        self.cpu = Graph([Series(h.cpu_total, CPU_COLOR), Series(h.cpu_kernel, KERNEL_COLOR)],
                         max_value=100, times=h.times, compact=True, min_height=28, step=1)
        self.mem = Graph([Series(h.mem_used, MEM_COLOR)], max_value=1, times=h.times, compact=True,
                         min_height=28, step=1)
        self.io = Graph([Series(h.disk_read, READ_COLOR), Series(h.disk_write, WRITE_COLOR)],
                        times=h.times, compact=True, min_height=28, step=1)
        self.cpu.caption, self.mem.caption, self.io.caption = "CPU", "MEM", "I/O"
        for g in (self.cpu, self.mem, self.io):
            g.set_content_width(72)
            g.set_hexpand(False)
            g.add_css_class("mini-graph")
            box.append(g)
        self.set_child(box)
        sampler.connect("tick", self._on_tick)

    def _on_tick(self, sampler):
        snap = sampler.snapshot
        self.mem.max_value = snap["mem_total"] or 1
        self.cpu.caption = f"CPU {snap['cpu_total']:.0f}%"
        self.mem.caption = f"MEM {100 * snap['mem_used'] / (snap['mem_total'] or 1):.0f}%"
        io = (snap["disk_read"] or 0) + (snap["disk_write"] or 0)
        self.io.caption = "I/O " + (fmt_short_rate(io) if io else "0")
        self.set_tooltip_text(
            f"CPU {snap['cpu_total']:.1f}%   Memory {fmt_bytes(snap['mem_used'])} / {fmt_bytes(snap['mem_total'])}"
            f"   Disk R {fmt_rate(snap['disk_read']) or '0'} W {fmt_rate(snap['disk_write']) or '0'}"
            "\nClick for System Information (Ctrl+I)")
        for g in (self.cpu, self.mem, self.io):
            g.queue_draw()
