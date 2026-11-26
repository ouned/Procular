"""Tiny history graphs: per row in the process list, and with a scale in the properties tiles."""

import math

from gi.repository import Gtk

from ..util.format import fmt_span
from ..util.lifecycle import own_method, weak_method
from ..util.timeline import nearest_sample, positions, segments, tail, visible_samples
from .graph import draw_trace, hex_to_rgb, nice_ceil


# mode "range": the graph's height stands for at least this share of the value
MIN_RANGE = 0.10
# on a light background the series colours are darkened by this factor: the thin line lies on
# tinted rows and tiles, where the colours of graph.py (3:1 on #fafafa) fall to below 3:1
LIGHT_SHADE = 0.78
SPARK_SECONDS = 60.0


def value_range(data, mode, floor=1.0):
    """(lo, hi): the values drawn at the bottom and the top edge.

    mode "zero":  0 .. max(peak, floor)
    mode "range": min .. max of the window, widened around its middle to MIN_RANGE of the
        value. A steady value is then a flat line in the middle and a small change a small
        step; without the widening every wiggle would fill the whole height.
    """
    if mode != "range":
        return 0.0, max(max(data), floor)
    lo, hi = min(data), max(data)
    slack = abs(hi) * MIN_RANGE - (hi - lo)
    if slack > 0:
        lo, hi = lo - slack / 2, hi + slack / 2
    if hi <= lo:  # all zero
        lo, hi = lo - 1, hi + 1
    return lo, hi


class Sparkline(Gtk.DrawingArea):
    """Draws up to a minute of samples, newest at the right edge.

    mode "zero":  0 .. max(peak, floor)     (CPU: flat when idle)
    mode "range": around the window's values (memory: shows the trend; flat when stable)
    See value_range().

    axis_fmt turns on the detailed look (properties window): a light grid, the values of
    the top and bottom edge in a gutter on the right ("zero" mode rounds the top to a nice
    number) and, on hover, the sample under the pointer and its age. value_fmt formats
    that sample. base: 1024 for bytes.
    """

    def __init__(self, color, mode="zero", floor=1.0, describe=None, axis_fmt=None, value_fmt=None, base=10):
        super().__init__()
        self.rgb = hex_to_rgb(color)
        self.mode = mode
        self.floor = floor
        self.describe = describe  # callable(data) -> tooltip text
        self.axis_fmt = axis_fmt
        self.value_fmt = value_fmt or axis_fmt
        self.base = base
        self.data = None
        self.times = None
        self._hover = None  # pointer x while hovering (detailed look only)
        self.set_content_height(16)
        self.set_hexpand(True)
        self.set_valign(Gtk.Align.CENTER)
        # not weak_method (see Graph): the list cells are sparklines nothing in Python keeps
        self.set_draw_func(own_method("_draw"))
        if axis_fmt is None:
            self.set_has_tooltip(True)
            self.connect("query-tooltip", own_method("_on_tooltip"))
        else:
            # drawn in the graph, not a GTK tooltip: see Graph
            motion = Gtk.EventControllerMotion()
            motion.connect("enter", weak_method(self._on_motion))
            motion.connect("motion", weak_method(self._on_motion))
            motion.connect("leave", weak_method(self._on_leave))
            self.add_controller(motion)

    def _line_rgb(self):
        """The series colour, a shade deeper while the text colour says the background is light."""
        fg = self.get_color()
        if fg.red + fg.green + fg.blue > 1.5:  # light text: dark style
            return self.rgb
        return tuple(c * LIGHT_SHADE for c in self.rgb)

    def set_data(self, data, times=None):
        self.data = data
        self.times = times
        self.queue_draw()

    def _on_motion(self, _ctl, x, _y):
        self._hover = x
        self.queue_draw()

    def _on_leave(self, _ctl):
        self._hover = None
        self.queue_draw()

    def _draw(self, _area, cr, width, height):
        if self.axis_fmt is not None:
            self._draw_detailed(cr, width, height)
            return
        if not self.data or width <= 2:
            return
        samples, values, present = self._window()
        if not present or (self.mode == "zero" and max(present) <= 0):
            return  # idle: draw nothing rather than a line on every row
        lo, hi = value_range(present, self.mode, self.floor)
        span = (hi - lo) or 1.0
        usable = height - 2

        def y_of(v):
            y = 1 + usable - (v - lo) / span * usable
            # on a pixel row's centre: a flat line between two rows would be smeared over both at
            # half strength, a pale line in the light style
            return math.floor(min(height - 1.0, max(1.0, y))) + 0.5

        self._draw_runs(cr, width, height, samples, values, y_of)

    def _window(self):
        samples = visible_samples(self.times, SPARK_SECONDS)
        values = tail(self.data or (), len(samples))
        return samples, values, [v for v in values if v is not None]

    def _draw_runs(self, cr, width, bottom, samples, values, y_of, line_width=1.2):
        xs = positions(samples, width, SPARK_SECONDS)
        rgb = self._line_rgb()
        fill = 0.28 if self.mode == "zero" else 0
        for run in segments(samples, xs, values):
            draw_trace(cr, [(x, y_of(v)) for x, v in run], rgb, bottom, fill=fill, line_width=line_width)
        return xs

    def _on_tooltip(self, _w, _x, _y, _kbd, tooltip):
        if self.describe is None:
            return False
        _samples, _values, present = self._window()
        if not present:
            return False
        tooltip.set_text(self.describe(present))
        return True

    # ---- detailed look -------------------------------------------------------------

    def _draw_detailed(self, cr, width, height):
        samples, values, present = self._window()
        if present:
            lo, hi = value_range(present, self.mode, self.floor)
        else:
            lo, hi = 0.0, self.floor
        if self.mode == "zero":
            hi = nice_ceil(hi, self.base)
        span = (hi - lo) or 1.0
        fg = self.get_color()
        fg = (fg.red, fg.green, fg.blue)

        # right gutter with the values of the top and bottom grid line
        cr.select_font_face("Sans")
        cr.set_font_size(9)
        top_text = self.axis_fmt(hi)
        bottom_text = self.axis_fmt(max(lo, 0.0))
        gutter = max(cr.text_extents(t).x_advance for t in (top_text, bottom_text)) + 6
        pw = width - gutter
        if pw <= 10:
            return
        top, bottom = 4.5, height - 4.5
        usable = bottom - top

        cr.set_source_rgba(*fg, 0.6)
        cr.move_to(pw + 5, top + 3)
        cr.show_text(top_text)
        cr.move_to(pw + 5, bottom + 3)
        cr.show_text(bottom_text)

        # grid: top and middle dashed, the bottom line solid
        cr.set_line_width(1)
        cr.set_source_rgba(*fg, 0.15)
        cr.set_dash([2, 2])
        for y in (top, round(top + usable / 2) + 0.5):
            cr.move_to(0, y)
            cr.line_to(pw, y)
        cr.stroke()
        cr.set_dash([])
        cr.set_source_rgba(*fg, 0.25)
        cr.move_to(0, bottom)
        cr.line_to(pw, bottom)
        cr.stroke()
        if not present:
            return

        def y_of(v):
            return min(bottom, max(top, bottom - (v - lo) / span * usable))

        xs = self._draw_runs(cr, pw, bottom, samples, values, y_of, line_width=1.4)
        if self._hover is not None and self._hover <= pw:
            i = nearest_sample(samples, xs, self._hover)
            if i is not None and (j := i - (len(samples) - len(values))) >= 0 and values[j] is not None:
                age = samples[-1].elapsed - samples[i].elapsed
                self._draw_hover(cr, pw, height, xs[i], y_of(values[j]), values[j], age, fg)

    def _draw_hover(self, cr, pw, height, px, py, value, age, fg):
        """Guide line, a dot on the sample and a small box with its value and age."""
        cr.set_source_rgba(*fg, 0.35)
        cr.set_line_width(1)
        cr.move_to(int(px) + 0.5, 0)
        cr.line_to(int(px) + 0.5, height)
        cr.stroke()
        dark = sum(fg) > 1.5  # light text: dark theme
        bg = (0.16, 0.16, 0.18) if dark else (1.0, 1.0, 1.0)
        cr.arc(px, py, 3, 0, 2 * math.pi)
        cr.set_source_rgb(*bg)
        cr.fill_preserve()
        cr.set_source_rgb(*self._line_rgb())
        cr.set_line_width(1.5)
        cr.stroke()

        elapsed = f"{age:.2f} s" if 0 < age < 1 else fmt_span(age)
        lines = [self.value_fmt(value), f"{elapsed} ago" if age else "latest sample"]
        cr.set_font_size(9)
        pad, line_h = 3, 11
        box_w = max(cr.text_extents(t).x_advance for t in lines) + 2 * pad
        box_h = len(lines) * line_h + 2 * pad - 2
        # beside the guide, on the side with more room
        bx = px + 6 if px + 6 + box_w <= pw else px - 6 - box_w
        bx = max(1.0, bx)
        by = max(1.0, (height - box_h) / 2)
        cr.rectangle(int(bx) + 0.5, int(by) + 0.5, box_w, box_h)
        cr.set_source_rgba(*bg, 0.92)
        cr.fill_preserve()
        cr.set_source_rgba(*fg, 0.3)
        cr.stroke()
        cr.set_source_rgb(*fg)
        y = int(by) + pad + 8
        for text in lines:
            cr.move_to(int(bx) + pad + 0.5, y)
            cr.show_text(text)
            y += line_h
