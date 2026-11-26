import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest

pytest.importorskip("gi")

from procular.util import chiptune  # noqa: E402
from procular.widgets import logo_animation as logo  # noqa: E402

SVG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data",
                   "io.github.ouned.Procular.svg")


def test_geometry_matches_the_icon():
    # the animation redraws the icon: a changed icon must not leave it showing the old one
    with open(SVG, encoding="utf-8") as fh:
        svg = fh.read()
    circles = {(float(x), float(y), float(r), color) for x, y, r, color in
               re.findall(r'<circle cx="([\d.]+)" cy="([\d.]+)" r="([\d.]+)" fill="(#\w+)"/>', svg)}
    ours = {tuple(map(float, node[:3])) + (node[3],) for node in (logo.ROOT, logo.LED, *logo.NODES.values())}
    assert ours == circles
    pins = re.findall(r'<rect x="(\d+)" y="8" width="8" height="16"', svg)
    assert tuple(int(x) + 4 for x in pins) == logo.PIN_LANES
    color, width = logo.PIN_EDGE
    assert f'<g fill="url(#pin)" stroke="{color}" stroke-width="{width:g}">' in svg


def test_routes_run_along_the_traces_to_their_node():
    for name, route in logo.ROUTES.items():
        assert route[0] == logo.ROOT[:2]
        assert route[-1] == logo.NODES[name][:2]
        for a, b in zip(route, route[1:]):
            assert a[0] == b[0] or a[1] == b[1]  # traces are horizontal or vertical


def test_route_slice():
    route = ((0, 0), (0, 10), (10, 10))
    assert logo.route_length(route) == 20
    assert logo.route_slice(route, 0, 20) == [(0, 0), (0, 10), (10, 10)]
    assert logo.route_slice(route, 5, 12) == [(0, 5), (0, 10), (2, 10)]
    assert logo.route_slice(route, 12, 15) == [(12 - 10, 10), (5, 10)]
    assert logo.route_slice(route, 3, 3) == [(0, 3)]
    assert logo.route_slice(route, 10, 10) == [(0, 10)]
    assert logo.route_slice(route, 25, 30) == []


def test_high_notes_take_the_upper_lanes_and_nodes():
    assert logo.packet(84, 60, 84) == (0, "green", 0)
    assert logo.packet(72, 60, 84) == (2, "yellow", 2)
    assert logo.packet(60, 60, 84) == (4, "red", 4)
    assert logo.packet(70, 70, 70) == (2, "yellow", 2)  # a lead of one note


def test_every_lane_and_node_is_used_by_every_track():
    for track in chiptune.TRACKS.values():
        cues = track.cues()
        seen = [logo.packet(note, cues.low, cues.high) for _s, note in cues.notes]
        assert {p[0] for p in seen} == set(range(5)) and {p[1] for p in seen} == set(logo.NODES)


def test_between_repeats_the_cues_every_loop():
    cues = ((0.0, "a"), (1.0, "b"), (2.5, "c"))
    assert list(logo.between(cues, 3.0, 0.5, 2.5)) == [(1.0, "b"), (2.5, "c")]
    assert list(logo.between(cues, 3.0, 2.0, 4.0)) == [(2.5, "c"), (3.0, "a"), (4.0, "b")]
    assert list(logo.between(cues, 3.0, -1.0, -0.4)) == [(-0.5, "c")]
    assert list(logo.between(cues, 3.0, 1.1, 2.4)) == []


def _render(t, cues, width=300, height=120):
    cairo = pytest.importorskip("cairo")
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    logo.draw_logo(cairo.Context(surface), width, height, t, cues)
    surface.flush()
    return surface


def _pixel(surface, x, y, width=300, height=120):
    """The colour at icon coordinates (x, y), the logo drawn as large as the height allows."""
    scale = height / logo.SIZE
    px, py = round(width / 2 - height / 2 + x * scale), round(y * scale)
    data = surface.get_data()
    offset = py * surface.get_stride() + px * 4
    return tuple(data[offset:offset + 3])  # B, G, R


def test_nodes_light_up_with_the_notes_and_their_echoes():
    cues = chiptune.TRACKS["classic"].cues()
    start, note = cues.notes[3]
    _lane_in, child, _lane_out = logo.packet(note, cues.low, cues.high)
    root = logo.ROOT[:2]
    node = logo.NODES[child][:2]
    before, onset = _render(start - 0.03, cues), _render(start + 0.01, cues)
    assert sum(_pixel(onset, *root)) > sum(_pixel(before, *root)) + 40  # the root lights up as the note starts
    echo, after = _render(start + cues.travel + 0.01, cues), _render(start + cues.travel - 0.04, cues)
    assert sum(_pixel(echo, *node)) > sum(_pixel(after, *node)) + 40  # the child, as its echo is heard


def test_draws_any_moment_and_the_still_logo():
    cues = chiptune.TRACKS["classic"].cues()
    for t in (None, -3.0, 0.0, 0.1, 7.77, 1e6):
        _render(t, cues)
    still = _render(None, cues)
    assert bytes(still.get_data()) == bytes(_render(2.0, None).get_data())  # no cues: no movement
    moving = _render(2.0, cues)
    assert bytes(still.get_data()) != bytes(moving.get_data())
    # nothing is drawn beside the still logo; packets fly there while it is animated
    width, stride = 40, still.get_stride()
    margin = lambda s: b"".join(bytes(s.get_data()[row * stride:row * stride + width * 4]) for row in range(120))  # noqa: E731
    assert not any(margin(still)) and any(margin(moving))
