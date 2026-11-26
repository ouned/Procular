"""The Procular logo with data flowing through it (About dialog).

The logo dances to the About theme. Every note of the tune's lead is a packet: it reaches the
pins at the left as the note starts, lights the root of the process tree on the chip, travels
along the traces to a child node that lights up as the note's echo is heard, and leaves on the
pins at the right in that node's colour. High notes take the upper pins and nodes, low notes
the lower ones. The activity LED blinks with the kick and the snare drum, and the pins at the
top and bottom shimmer like a clock line, once around per bar.

The picture is a function of the time alone: draw_logo() works out from `t` and the tune's cues
(chiptune.Track.cues()) which packets are under way and where, so there is no particle state to
keep and any moment can be rendered.
The logo is redrawn here instead of loading the SVG, because its parts move; the geometry
below mirrors data/io.github.ouned.Procular.svg (tests/test_logo_animation.py compares).
"""

import functools
import math
from bisect import bisect_left, bisect_right

from gi.repository import Gtk

from ..util.lifecycle import weak_method
from .graph import hex_to_rgb

SIZE = 128.0  # the icon's coordinate system
PIN_LANES = (36, 50, 64, 78, 92)  # centre lines of the five pins on each side
ROOT = (46, 44, 7, "#62a0ea")
NODES = {  # the root's children: x, y, radius, colour
    "green": (64, 60, 6, "#57e389"),
    "yellow": (80, 70, 5.5, "#f6d32d"),
    "red": (64, 80, 6, "#ed333b"),
}
LED = (96, 32, 3, "#77767b")
PIN_EDGE = ("#a36a00", 2.5)  # colour and width of the pins' outline: keeps them apart on a light background
TRACES = (((46, 46), (46, 80)), ((46, 60), (64, 60)), ((46, 80), (64, 80)), ((64, 60), (64, 70)),
          ((64, 70), (80, 70)))
# the way from the root to each child, along the traces
ROUTES = {
    "green": ((46, 44), (46, 60), (64, 60)),
    "yellow": ((46, 44), (46, 60), (64, 60), (64, 70), (80, 70)),
    "red": ((46, 44), (46, 80), (64, 80)),
}
# packets outside the chip are drawn on the window background, light or dark: stronger
# shades of the node colours than the ones that glow on the chip's dark face
STREAM_IN = "#3584e4"
STREAM_OUT = {"green": "#2ec27e", "yellow": "#e5a50a", "red": "#e01b24"}

STREAM_BEATS = 2.0  # beats a packet takes from the edge of the picture to the pins, and away again
DASH = 22.0  # length of a packet outside the chip
TRAIL = 15.0  # length of the lit piece of trace behind a packet
PULSE = 0.15  # seconds: how fast a node's flash dies away
HANDOVER = 0.06  # seconds between a node lighting up and its packet leaving the chip
BLINK = 0.1  # seconds: how fast the LED goes out again
LED_HITS = {"K": 1.0, "S": 0.7}  # the drums the LED blinks with, and how brightly


def route_length(points):
    return sum(math.dist(a, b) for a, b in zip(points, points[1:]))


def route_slice(points, start, end):
    """The part of the polyline `points` between the distances `start` and `end` from its beginning.

    A single point when start == end, nothing when the range lies beyond the line.
    """
    out = []
    walked = 0.0
    for a, b in zip(points, points[1:]):
        length = math.dist(a, b)
        lo, hi = max(start, walked), min(end, walked + length)
        if lo <= hi:
            for d in (lo, hi):
                f = (d - walked) / length if length else 0.0
                point = (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)
                if not out or out[-1] != point:
                    out.append(point)
        walked += length
    return out


def packet(note, low, high):
    """Where the packet of MIDI note `note` goes, in a lead from `low` to `high`: (pin lane in, child node, lane out).

    The higher the note, the higher up: lanes count from the top, the nodes are stacked green,
    yellow, red.
    """
    height = (note - low) / (high - low) if high > low else 0.5
    lane = 4 - round(min(max(height, 0.0), 1.0) * 4)
    return lane, ("red", "yellow", "green")[min(int(height * 3), 2)], lane


@functools.lru_cache(maxsize=16)
def _times(cues):
    return tuple(s for s, _ in cues)


def between(cues, loop, start, end):
    """The cues (seconds into the round, value) that fall from `start` to `end`, the tune played in a loop.

    Yields (seconds, value) with the seconds counted from the start of the first round.
    """
    times = _times(cues)
    for number in range(math.floor(start / loop), math.floor(end / loop) + 1):
        offset = number * loop
        for i in range(bisect_left(times, start - offset), bisect_right(times, end - offset)):
            yield offset + times[i], cues[i][1]


def _rounded_rect(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def _vertical_gradient(y0, y1, top, bottom):
    import cairo

    gradient = cairo.LinearGradient(0, y0, 0, y1)
    gradient.add_color_stop_rgb(0, *hex_to_rgb(top))
    gradient.add_color_stop_rgb(1, *hex_to_rgb(bottom))
    return gradient


def _dash(cr, head, y, direction, rgb, alpha):
    """A packet outside the chip: a bright head that drags a fading tail. direction: +1 flies right."""
    import cairo

    tail = head - direction * DASH
    gradient = cairo.LinearGradient(tail, 0, head, 0)
    gradient.add_color_stop_rgba(0, *rgb, 0)
    gradient.add_color_stop_rgba(1, *rgb, alpha)
    cr.set_source(gradient)
    cr.set_line_width(3.4)
    cr.move_to(tail, y)
    cr.line_to(head, y)
    cr.stroke()
    cr.set_source_rgba(1, 1, 1, 0.85 * alpha)
    cr.arc(head, y, 1.3, 0, math.tau)
    cr.fill()


def _node(cr, x, y, radius, color, flash):
    """A tree node; flash 0..1 makes it swell and glow."""
    import cairo

    rgb = hex_to_rgb(color)
    if flash > 0.01:
        glow = cairo.RadialGradient(x, y, radius * 0.6, x, y, radius * 2.8)
        glow.add_color_stop_rgba(0, *rgb, 0.7 * flash)
        glow.add_color_stop_rgba(1, *rgb, 0)
        cr.set_source(glow)
        cr.arc(x, y, radius * 2.8, 0, math.tau)
        cr.fill()
    cr.set_source_rgb(*(c + (1 - c) * 0.45 * flash for c in rgb))
    cr.arc(x, y, radius * (1 + 0.25 * flash), 0, math.tau)
    cr.fill()


def draw_logo(cr, width, height, t=None, cues=None, size=None):
    """Draw the logo centred in width x height. t: seconds into the tune, None for the still logo.

    cues: what happens when in the tune (chiptune.Cues); without them the logo stands still.
    size: edge length of the logo, by default as large as the height allows.
    """
    if cues is None:
        t = None
    size = size or min(height, width)
    scale = size / SIZE
    cr.save()
    cr.translate(width / 2 - size / 2, height / 2 - size / 2)
    cr.scale(scale, scale)
    left_edge = (size / 2 - width / 2) / scale  # the picture's left and right border in icon units
    right_edge = SIZE - left_edge

    # ---- which packets are under way
    flights = []  # (age since reaching the pins, lane in, child, lane out, speed on the traces)
    root_flash = 0.0
    child_flash = dict.fromkeys(NODES, 0.0)
    pin_in = [0.0] * 5
    pin_out = [0.0] * 5
    pin_out_color = [STREAM_IN] * 5
    blink = 0.0
    if t is not None:
        stream = STREAM_BEATS * cues.beat
        travel = cues.travel  # the way from the root to a child takes from a note until its echo
        for start, note in between(cues.notes, cues.loop, t - travel - HANDOVER - stream, t + stream):
            age = t - start
            lane_in, child, lane_out = packet(note, cues.low, cues.high)
            flights.append((age, lane_in, child, lane_out, route_length(ROUTES[child]) / travel))
            if age >= 0:
                pin_in[lane_in] = max(pin_in[lane_in], math.exp(-age / PULSE))
                root_flash = max(root_flash, math.exp(-age / PULSE))
            if age >= travel:
                child_flash[child] = max(child_flash[child], math.exp(-(age - travel) / PULSE))
            if age >= travel + HANDOVER:
                glow = math.exp(-(age - travel - HANDOVER) / PULSE)
                if glow > pin_out[lane_out]:
                    pin_out[lane_out], pin_out_color[lane_out] = glow, STREAM_OUT[child]
        for start, hit in between(cues.hits, cues.loop, t - 6 * BLINK, t):
            if hit in LED_HITS:
                blink = max(blink, LED_HITS[hit] * math.exp(-(t - start) / BLINK))

    # ---- pins: a pin takes the colour of the packet passing through it; the ones at the top
    # and bottom shimmer like a clock line, once around per bar
    pins = _vertical_gradient(8, 120, "#f8e45c", "#e5a50a")
    sweep = (t / cues.bar) % 1 * 10 if t is not None else None
    for i, lane in enumerate(PIN_LANES):
        for x, y, w, h, glow, color in (
            (lane - 4, 8, 8, 16, 0.0 if sweep is None else 0.5 * max(0.0, 1 - abs((sweep - i + 5) % 10 - 5)),
             "#ffffff"),
            (lane - 4, 104, 8, 16, 0.0 if sweep is None else 0.5 * max(0.0, 1 - abs((sweep - 9 + i + 5) % 10 - 5)),
             "#ffffff"),
            (8, lane - 4, 16, 8, pin_in[i], STREAM_IN),
            (104, lane - 4, 16, 8, pin_out[i], pin_out_color[i]),
        ):
            _rounded_rect(cr, x, y, w, h, 2)
            cr.set_source(pins)
            cr.fill_preserve()
            cr.set_source_rgba(*hex_to_rgb(color), 0.85 * glow)
            cr.fill_preserve()
            cr.set_source_rgb(*hex_to_rgb(PIN_EDGE[0]))
            cr.set_line_width(PIN_EDGE[1])
            cr.stroke()

    # ---- packets outside the chip (the chip body, drawn next, hides where they dive under it)
    cr.set_line_cap(1)  # round
    for age, lane_in, child, lane_out, speed in flights:
        travel = route_length(ROUTES[child]) / speed
        if -stream <= age < 0.08:
            progress = 1 + age / stream  # 0 at the picture's edge, 1 at the chip
            head = left_edge + (24 - left_edge) * progress
            _dash(cr, head, PIN_LANES[lane_in], 1, hex_to_rgb(STREAM_IN), min(1.0, progress * 3))
        leaving = age - travel - HANDOVER
        if 0 <= leaving < stream:
            progress = leaving / stream
            head = 104 + (right_edge + DASH - 104) * progress
            _dash(cr, head, PIN_LANES[lane_out], 1, hex_to_rgb(STREAM_OUT[child]), min(1.0, (1 - progress) * 2.5))

    # ---- chip
    _rounded_rect(cr, 18, 22, 92, 88, 14)
    cr.set_source_rgb(*hex_to_rgb("#241f31"))
    cr.fill()
    _rounded_rect(cr, 18, 18, 92, 88, 14)
    cr.set_source(_vertical_gradient(18, 106, "#5e5c64", "#3d3846"))
    cr.fill()
    _rounded_rect(cr, 30, 30, 68, 64, 8)
    cr.set_source_rgb(*hex_to_rgb("#1a1625"))
    cr.fill()

    # ---- traces, and the lit piece behind every packet that is on one
    cr.set_line_join(1)
    cr.set_line_width(3)
    cr.set_source_rgb(*hex_to_rgb("#77767b"))
    for a, b in TRACES:
        cr.move_to(*a)
        cr.line_to(*b)
    cr.stroke()
    heads = []
    for age, _lane_in, child, _lane_out, speed in flights:
        route = ROUTES[child]
        travel = route_length(route) / speed
        if 0 <= age < travel + TRAIL / speed:
            distance = age * speed
            lit = route_slice(route, max(0.0, distance - TRAIL), min(distance, route_length(route)))
            if len(lit) > 1:
                cr.set_source_rgba(*hex_to_rgb(NODES[child][3]), 0.95)
                cr.move_to(*lit[0])
                for point in lit[1:]:
                    cr.line_to(*point)
                cr.stroke()
            if age < travel and lit:
                heads.append(lit[-1])

    # ---- nodes
    _node(cr, *ROOT, root_flash)
    for name, (x, y, radius, color) in NODES.items():
        _node(cr, x, y, radius, color, child_flash[name])
    cr.set_source_rgba(1, 1, 1, 0.95)
    for x, y in heads:
        cr.arc(x, y, 1.9, 0, math.tau)
        cr.fill()

    # ---- activity LED: blinks with the drums
    x, y, radius, color = LED
    _node(cr, x, y, radius, color if blink < 0.02 else "#57e389", blink)
    if blink >= 0.02:  # fade back to the unlit grey
        cr.set_source_rgba(*hex_to_rgb(color), 1 - blink)
        cr.arc(x, y, radius, 0, math.tau)
        cr.fill()
    cr.restore()


class AnimatedLogo(Gtk.DrawingArea):
    """draw_logo() on the frame clock. Stands still when the desktop has animations turned off."""

    def __init__(self, cues, height=144):
        super().__init__(content_height=height, hexpand=True)
        self.cues = cues
        self._now = None  # frame time in seconds; None until the first frame, and without animations
        self._origin = None  # frame time at which the animation's t is 0
        self._frozen = None  # a fixed t: paused, waiting for the music, or a screenshot
        self.set_draw_func(weak_method(self._draw))  # weak: see Graph
        self._tick = 0
        if Gtk.Settings.get_default().props.gtk_enable_animations:
            self._tick = self.add_tick_callback(weak_method(self._on_tick))

    def sync(self, origin):
        """Start the tune over at `origin` (monotonic seconds, the clock of time.monotonic()): when its first sample
        is heard. None: at the next frame. Ends a hold()."""
        self._origin = origin
        self._frozen = None

    def position(self):
        """Seconds into the tune that are shown now (0 before the first frame)."""
        if self._frozen is not None:
            t = self._frozen
        elif self._now is not None and self._origin is not None:
            t = self._now - self._origin
        else:
            t = 0.0
        return t % self.cues.loop

    def go_on(self):
        """Go on from the moment shown now, without music to follow (ends a hold())."""
        t = self.position()
        self._frozen = None
        self._origin = None if self._now is None else self._now - t

    def hold(self, t):
        """Stand still at the moment `t` until the next sync(). Without animations there is nothing to hold."""
        if self._tick:
            self.freeze(t)

    def set_cues(self, cues):
        """Dance to another tune."""
        self.cues = cues
        self.queue_draw()

    def freeze(self, t):
        """Show the moment `t` and stay there."""
        self._frozen = t
        self.queue_draw()

    def teardown(self):
        if self._tick:
            self.remove_tick_callback(self._tick)
            self._tick = 0

    def _on_tick(self, _widget, frame_clock):
        self._now = frame_clock.get_frame_time() / 1e6
        if self._origin is None:
            self._origin = self._now
        if self._frozen is None:
            self.queue_draw()
        return True

    def _draw(self, _area, cr, width, height):
        t = self._frozen
        if t is None and self._now is not None:
            t = self._now - self._origin
        draw_logo(cr, width, height, t, self.cues)
