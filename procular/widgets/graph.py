"""History graph drawn with cairo (System Informer style)."""

import math
import re
from bisect import bisect_left, bisect_right

from gi.repository import Gtk

from ..util.format import fmt_clock
from ..util.lifecycle import weak_method
from ..util.timeline import nearest_sample, positions, segments, tail, time_average, visible_samples


# Series colours. Checked with the dataviz palette validator against the dark (#141417) and the light
# (#fafafa) graph background: the two series of a graph stay apart with red-green colour blindness
# too, and every colour has at least 3:1 contrast on both backgrounds.
CPU = "#26a269"
KERNEL = "#e01b24"
MEM = "#c48100"
CACHE = "#5b8dd9"  # drawn with MEM: used + cache, working set
SWAP = "#c64600"
READ = "#3584e4"
WRITE = "#d56199"
RX = "#1899ab"
TX = "#e66100"
GPU = "#9141ac"
VIDEO = "#e66100"
SIGNAL = "#1c71d8"
BUSY = "#1c71d8"


def hex_to_rgb(color):
    color = color.lstrip("#")
    return tuple(int(color[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def find_peaks(values, scale, min_gap, max_peaks=4, min_rise=0.10, *, xs=None):
    """Indices of prominent peaks in `values`, highest first.

    A peak must be a maximum within `min_gap` on either side, reach at least
    15% of `scale` and rise `min_rise * scale` above the window average. Picks are
    spaced apart in the coordinates `xs` (or sample indices).
    """
    present = [v for v in values if v is not None]
    if len(present) < 3 or not scale:
        return []
    xs = range(len(values)) if xs is None else xs
    avg = sum(present) / len(present)
    floor = max(0.15 * scale, avg + min_rise * scale)
    candidates = []
    for i, v in enumerate(values):
        if v is None or v < floor:
            continue
        lo, hi = bisect_left(xs, xs[i] - min_gap), bisect_right(xs, xs[i] + min_gap)
        if v >= max(value for value in values[lo:hi] if value is not None):
            candidates.append(i)
    candidates.sort(key=lambda i: (-values[i], -i))  # higher first, then the more recent
    picked = []
    for i in candidates:
        if all(abs(xs[i] - xs[j]) > min_gap for j in picked):
            picked.append(i)
            if len(picked) == max_peaks:
                break
    return picked


def nice_ceil(value, base=10):
    """`value` rounded up to 1, 2, 2.5 or 5 times a power of ten (1 when value <= 0).

    base 1024: rounds within the binary unit the value falls in (KiB, MiB, ...), so that
    byte values read as round numbers ("100K", not "97.7K").
    """
    if value <= 0:
        return 1
    unit = 1
    if base == 1024:
        while value >= 1024:
            value /= 1024
            unit *= 1024
    exp = 10 ** math.floor(math.log10(value))
    for mult in (1, 2, 2.5, 5, 10):
        if value <= mult * exp * (1 + 1e-9):
            return mult * exp * unit
    return value * unit


def fmt_span(seconds):
    """How far back a graph reaches: "45 s", "6.5 min", "1.2 h"."""
    seconds = round(seconds)
    if seconds < 100:
        return f"{seconds} s"
    value, unit = (seconds / 60, "min") if seconds < 5400 else (seconds / 3600, "h")
    return (f"{value:.1f}".rstrip("0").rstrip(".") if value < 10 else f"{value:.0f}") + " " + unit


def tidy(text):
    """A scale value without empty decimals: "100.0%" -> "100%", "50.00%" -> "50%"."""
    return re.sub(r"\.0+(?![0-9])", "", text)


def _pill(cr, x, y, w, h, bg, alpha=0.88):
    """Rounded patch in the background colour: keeps a label readable on top of the curves."""
    r = min(4.0, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()
    cr.set_source_rgba(*bg, alpha)
    cr.fill()


def _label(cr, text, x, baseline, bg, fg, alpha=1.0, right=False):
    """Text on a pill at the current font size; right: x is the right end. Returns the pill's box."""
    w = cr.text_extents(text).x_advance
    if right:
        x -= w
    box = (x - 4, baseline - 10, x + w + 4, baseline + 4)
    _pill(cr, box[0], box[1], box[2] - box[0], box[3] - box[1], bg)
    cr.set_source_rgba(*fg, alpha)
    cr.move_to(x, baseline)
    cr.show_text(text)
    return box


def draw_trace(cr, points, color, bottom, fill=0.0, line_width=1.2):
    """Draw one continuous run. A sample on its own is a dot, not a line across a gap."""
    if len(points) == 1:
        cr.set_source_rgb(*color)
        cr.arc(*points[0], line_width, 0, 2 * math.pi)
        cr.fill()
        return
    if fill:
        cr.move_to(points[0][0], bottom)
        for x, y in points:
            cr.line_to(x, y)
        cr.line_to(points[-1][0], bottom)
        cr.close_path()
        cr.set_source_rgba(*color, fill)
        cr.fill()
    cr.move_to(*points[0])
    for x, y in points[1:]:
        cr.line_to(x, y)
    cr.set_source_rgb(*color)
    cr.set_line_width(line_width)
    cr.stroke()


class Series:
    def __init__(self, data, color, label="", fill=True):
        self.data = data  # deque or list (oldest first)
        self.color = hex_to_rgb(color) if isinstance(color, str) else color
        self.label = label
        self.fill = fill


class Graph(Gtk.DrawingArea):
    """Draws one or more series against elapsed time (newest sample at the right edge).

    max_value: fixed scale (e.g. 100 for percent) or None for auto scale.
    min_scale: the smallest auto scale, so that noise near zero does not fill the graph.
    fmt: formats a value for labels and the hover box.
    times: SampleTime history; shorter series align with its newest samples.
    step: pixels per second.
    tooltip: callable(index_from_end) -> extra hover box line (e.g. top process).
    peak_label: callable(index_from_end) -> short text drawn at prominent peaks of the
        first series (e.g. the process that caused the spike), or "" to skip.
    """

    def __init__(self, series=None, max_value=None, fmt=None, title="", step=2, tooltip=None, *,
                 times, compact=False, min_height=60, show_avg=False, legend=True, avg_series=0,
                 peak_label=None, scale_unit=1, min_scale=0):
        super().__init__()
        self.series = series or []
        self.max_value = max_value
        self.min_scale = min_scale
        # what fmt multiplies values by (8 for bytes shown as bits): the scale is rounded in those units
        self.scale_unit = scale_unit
        self.fmt = fmt or (lambda v: f"{v:.1f}")
        self.title = title
        self.step = step
        self.tooltip_func = tooltip
        self.times = times
        self.compact = compact
        self.overlay_text = ""
        self.caption = ""  # compact graphs: small label drawn in the corner
        self.show_avg = show_avg  # dashed average line of the first series over the visible window
        self.legend = legend
        self.avg_series = avg_series
        self.peak_label = peak_label
        self.set_content_height(min_height)
        self.set_hexpand(True)
        # weak: GTK holds these callbacks, a strong reference to self would keep the graph alive forever
        self.set_draw_func(weak_method(self._draw))
        self.add_css_class("graph")
        # colours come from _is_dark(), not CSS: repaint when the style switches, even while paused
        self._dark_handler = 0
        self.connect("map", weak_method(self._on_map))
        self.connect("unmap", weak_method(self._on_unmap))
        # pointer position while hovering; the sample under it gets a marker and an info box
        # drawn in the graph itself (GTK tooltips wait for the pointer to rest and restart that
        # delay on every motion and redraw, so on a live graph they often never appeared)
        self._hover = None
        if not compact:
            motion = Gtk.EventControllerMotion()
            motion.connect("enter", weak_method(self._on_motion))
            motion.connect("motion", weak_method(self._on_motion))
            motion.connect("leave", weak_method(self._on_leave))
            self.add_controller(motion)

    def _on_map(self, _widget):
        try:
            from gi.repository import Adw
        except ImportError:
            return
        if not self._dark_handler:
            self._dark_handler = Adw.StyleManager.get_default().connect(
                "notify::dark", weak_method(self._on_dark_changed))

    def _on_unmap(self, _widget):
        if self._dark_handler:
            from gi.repository import Adw

            Adw.StyleManager.get_default().disconnect(self._dark_handler)
            self._dark_handler = 0

    def _on_dark_changed(self, *_):
        self.queue_draw()

    def _on_motion(self, _ctl, x, y):
        self._hover = (x, y)
        self.queue_draw()

    def _on_leave(self, _ctl):
        self._hover = None
        self.queue_draw()

    def _scale(self, count):
        if self.max_value is not None:
            return self.max_value
        peak = 0
        for s in self.series:
            peak = max(peak, max((v for v in tail(s.data, count) if v is not None), default=0))
        return max(self.min_scale, nice_ceil(peak * self.scale_unit) / self.scale_unit)

    def _draw(self, _area, cr, width, height):
        dark = self._is_dark()
        bg = (0.08, 0.08, 0.09) if dark else (0.98, 0.98, 0.98)
        grid = (0.17, 0.17, 0.19) if dark else (0.91, 0.91, 0.92)
        fg = (0.85, 0.85, 0.85) if dark else (0.25, 0.25, 0.25)
        cr.set_source_rgb(*bg)
        cr.rectangle(0, 0, width, height)
        cr.fill()

        span = max(1, width - 1) / self.step
        samples = visible_samples(self.times, span)
        xs = positions(samples, width, span)
        scale = self._scale(len(samples)) or 1

        if not self.compact:
            # light grid: quarters vertically, a coarse column grid that moves with the data
            cr.set_source_rgb(*grid)
            cr.set_line_width(1)
            cell = 60
            offset = (samples[-1].elapsed * self.step) % cell if samples else 0
            x = width - 1 - offset
            while x > 0:
                cr.move_to(int(x) + 0.5, 0)
                cr.line_to(int(x) + 0.5, height)
                x -= cell
            for quarter in (1, 2, 3):
                y = int(height * quarter / 4) + 0.5
                cr.move_to(0, y)
                cr.line_to(width, y)
            cr.stroke()

        for series in self.series:
            values = tail(series.data, len(samples))
            for run in segments(samples, xs, values):
                points = [(px, max(0.0, height - value / scale * (height - 1))) for px, value in run]
                draw_trace(cr, points, series.color, height, fill=0.30 if series.fill else 0)

        cr.select_font_face("Sans")
        if self.compact:
            if self.caption:
                cr.set_font_size(9)
                ext = cr.text_extents(self.caption)
                _pill(cr, 2, 2, ext.x_advance + 6, 12, bg, alpha=0.88)
                cr.set_source_rgb(*fg)
                cr.move_to(5, 11)
                cr.show_text(self.caption)
            return

        # the dashed average line of one series over the visible window; its label follows below
        avg = avg_y = None
        if self.show_avg and len(self.series) > self.avg_series:
            avg = time_average(samples, tail(self.series[self.avg_series].data, len(samples)))
        if avg is not None:
            avg_y = height - (avg / scale) * (height - 1) if scale else height
            cr.set_source_rgba(*fg, 0.55)
            cr.set_line_width(1)
            cr.set_dash([4, 3])
            cr.move_to(0, avg_y)
            cr.line_to(width, avg_y)
            cr.stroke()
            cr.set_dash([])

        # Every label sits on a pill of the background colour, so no line runs through the text.
        # `taken` collects the boxes of what is drawn, for the labels that have to avoid them.
        taken = []
        cr.set_font_size(10)
        roomy = width >= 180 and height >= 70  # enough space to label the scale
        span_w = 0
        top = tidy(self.fmt(scale))
        if roomy:
            # scale maximum at the right edge, the time span at the left
            taken.append(_label(cr, top, width - 6, 14, bg, fg, 0.7, right=True))
            if samples:
                box = _label(cr, fmt_span(span) + " ago", 6, height - 6, bg, fg, 0.55)
                taken.append(box)
                span_w = box[2]
        elif self.max_value is None:
            taken.append(_label(cr, top, width - 6, 14, bg, fg, right=True))

        # title and legend: measured first, they share one pill
        text = self.overlay_text or self.title
        entries = [s for s in self.series if s.label] if self.legend and len(self.series) > 1 else []
        legend_w = 0
        if text:
            cr.set_font_size(11)
            legend_w += cr.text_extents(text).x_advance + (12 if entries else 0)
        cr.set_font_size(10)
        for series in entries:
            legend_w += 11 + cr.text_extents(series.label).x_advance + 10
        if entries:
            legend_w -= 10
        legend_box = (2, 3, 6 + legend_w + 4, 19) if legend_w else None

        if avg is not None:
            # label at the left end (new data arrives at the right edge): above the line, or below it
            # where it would run into the title and legend row or leave the graph
            ty = avg_y - 5
            limit = legend_box[3] + 1 if legend_box is not None else 2  # also: not cut off at the top edge
            if ty - 10 < limit:
                ty = max(avg_y + 14, limit + 11)
            ty = min(ty, height - 6)
            avg_text = "avg " + (self.fmt(avg) or "0")
            # move to the empty bottom-right corner when it would crowd the bottom-left time span
            if span_w and ty + 4 > height - 17 and roomy:
                taken.append(_label(cr, avg_text, width - 6, ty, bg, fg, 0.75, right=True))
            else:
                tx = span_w + 8 if span_w and ty + 4 > height - 17 else 6
                taken.append(_label(cr, avg_text, tx, ty, bg, fg, 0.75))

        if legend_box is not None:
            taken.append(legend_box)
        if self.peak_label is not None and self.series:
            self._draw_peaks(cr, width, height, xs, scale, bg, fg, taken)

        if legend_box is not None:
            _pill(cr, legend_box[0], legend_box[1], legend_box[2] - legend_box[0], legend_box[3] - legend_box[1], bg)
            x = 6
            if text:
                cr.set_source_rgb(*fg)
                cr.set_font_size(11)
                cr.move_to(x, 14)
                cr.show_text(text)
                x += cr.text_extents(text).x_advance + 12
            cr.set_font_size(10)
            for series in entries:
                cr.set_source_rgb(*series.color)
                cr.rectangle(x, 6, 8, 8)
                cr.fill()
                cr.set_source_rgb(*fg)
                cr.move_to(x + 11, 14)
                cr.show_text(series.label)
                x += 11 + cr.text_extents(series.label).x_advance + 10

        # last: the info box lies on top of everything
        if self._hover is not None:
            index = nearest_sample(samples, xs, self._hover[0])
            if index is not None:
                self._draw_hover(cr, width, height, scale, bg, fg, len(xs) - 1 - index, xs[index])

    def _draw_peaks(self, cr, width, height, xs, scale, bg, fg, taken=()):
        """Mark prominent peaks of the first series with a dot and a label.

        taken: boxes of labels that are already there; a peak label that would cover one is left out."""
        values = tail(self.series[0].data, len(xs))
        xs = xs[len(xs) - len(values):]
        cr.set_font_size(10)
        placed = list(taken)  # (x0, y0, x1, y1) of drawn labels
        for i in find_peaks(values, scale, 30, xs=xs):
            idx_from_end = len(values) - 1 - i
            text = self.peak_label(idx_from_end)
            if not text:
                continue
            px = xs[i]
            py = max(0.0, height - (values[i] / scale) * (height - 1))
            ext = cr.text_extents(text)
            tw, th = ext.x_advance, 10
            tx = min(max(6.0, px - tw / 2), width - tw - 6.0)
            # above the peak, or below it when another label is in the way there
            for ty in (py - 7, min(py + 7 + th, height - 5)):
                box = (tx - 4, ty - th, tx + tw + 4, ty + 4)
                if box[1] >= 1 and not any(box[0] < b[2] and b[0] < box[2] and box[1] < b[3] and b[1] < box[3]
                                           for b in placed):
                    break
            else:
                continue
            placed.append(box)
            cr.set_source_rgb(*fg)
            cr.arc(px, py, 2.5, 0, 2 * math.pi)
            cr.fill()
            _pill(cr, box[0], box[1], box[2] - box[0], box[3] - box[1], bg)
            cr.set_source_rgb(*fg)
            cr.move_to(tx, ty)
            cr.show_text(text)

    def _hover_lines(self, idx):
        """Info box lines for the sample idx (from the newest): [(text, swatch color or None)]."""
        lines = []
        for series in self.series:
            n = len(series.data)
            if idx < n:
                v = series.data[n - 1 - idx]
                if v is None:
                    continue
                value = self.fmt(v) or "0"  # rate formats leave zero empty
                text = f"{series.label}: {value}" if series.label else value
                lines.append((text, series.color if len(self.series) > 1 else None))
        if not lines:
            return []
        if self.tooltip_func is not None:
            extra = self.tooltip_func(idx)
            if extra:
                lines.append((extra, None))
        if idx < len(self.times):
            stamp = self.times[-1 - idx]
            clock = fmt_clock(stamp.wall)
            if stamp.interval < 1:
                clock += f".{int(stamp.wall % 1 * 1000):03d}"
            lines.append((clock, None))
        return lines

    def _draw_hover(self, cr, width, height, scale, bg, fg, idx, px):
        """Guide line, a dot on every series and an info box at the hovered sample."""
        _hx, hy = self._hover
        lines = self._hover_lines(idx)
        if not lines:
            return  # pointer left of the recorded history
        cr.set_source_rgba(*fg, 0.35)
        cr.set_line_width(1)
        cr.move_to(int(px) + 0.5, 0)
        cr.line_to(int(px) + 0.5, height)
        cr.stroke()
        for series in self.series:
            n = len(series.data)
            if idx >= n:
                continue
            v = series.data[n - 1 - idx]
            if v is None:
                continue
            py = height - (v / scale) * (height - 1) if scale else height
            py = max(0.0, min(height - 1.0, py))
            cr.arc(px, py, 3.5, 0, 2 * math.pi)
            cr.set_source_rgb(*bg)
            cr.fill_preserve()
            cr.set_source_rgb(*series.color)
            cr.set_line_width(2)
            cr.stroke()

        # info box beside the marker: right of it unless that runs off the edge
        cr.set_font_size(10)
        line_h, pad, swatch = 12, 4, 10
        text_w = max(cr.text_extents(t).x_advance + (swatch if c else 0) for t, c in lines)
        box_w = text_w + 2 * pad
        box_h = len(lines) * line_h + 2 * pad - 2
        bx = px + 10 if px + 10 + box_w <= width - 2 else px - 10 - box_w
        bx = max(2.0, bx)
        by = min(max(2.0, hy - box_h / 2), max(2.0, height - box_h - 2))
        cr.rectangle(bx + 0.5, by + 0.5, box_w, box_h)
        cr.set_source_rgba(*bg, 0.92)
        cr.fill_preserve()
        cr.set_source_rgba(*fg, 0.3)
        cr.set_line_width(1)
        cr.stroke()
        y = by + pad + 9
        for text, color in lines:
            x = bx + pad
            if color:
                cr.set_source_rgb(*color)
                cr.rectangle(x, y - 7, 7, 7)
                cr.fill()
                x += swatch
            cr.set_source_rgb(*fg)
            cr.move_to(x, y)
            cr.show_text(text)
            y += line_h

    def _is_dark(self):
        try:
            from gi.repository import Adw

            return Adw.StyleManager.get_default().get_dark()
        except Exception:  # noqa: BLE001
            return False
