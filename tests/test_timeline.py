import pytest

from procular.util.timeline import SampleTime, nearest_sample, positions, segments, time_average, visible_samples


def test_positions_use_elapsed_seconds_not_sample_count_or_wall_clock():
    times = [SampleTime(100, 1000), SampleTime(101, 1001), SampleTime(106, 900, interval=5)]
    xs = positions(times, 201, 10)
    assert xs == pytest.approx([80, 100, 200])
    assert nearest_sample(times, xs, 101) == 1
    assert visible_samples(times, 5) == times[1:]


def test_gaps_and_missing_values_break_traces_and_hover():
    times = [SampleTime(100, 1000), SampleTime(101, 1001), SampleTime(130, 1030, gap=True),
             SampleTime(131, 1031), SampleTime(132, 1032)]
    xs = [4, 6, 64, 66, 68]
    assert list(segments(times, xs, [9, 10, 20, None, 30])) == [
        [(4, 9), (6, 10)], [(64, 20)], [(68, 30)],
    ]
    assert list(segments(times, xs, [25, 30])) == [[(66, 25), (68, 30)]]
    assert nearest_sample(times, xs, 30) is None
    assert nearest_sample(times, xs, 63) == 2


def test_average_weights_slow_samples_and_excludes_gaps():
    times = [SampleTime(t, 1000 + t, gap=t == 70) for t in (0, 1, 5, 70, 71)]
    assert time_average(times, [0, 10, 10, 20, 20]) == pytest.approx((5 + 40 + 20) / 6)
    assert time_average(times, [None, 20]) is None


def test_graph_draws_no_midpoint_label_and_uses_the_actual_hover_time(gtk, monkeypatch):
    cairo = pytest.importorskip("cairo")
    from procular.util.format import fmt_clock
    from procular.widgets import graph

    stamps = [SampleTime(100, 1000), SampleTime(104, 1004), SampleTime(120, 1020, gap=True)]
    g = graph.Graph([graph.Series([12, 25, 37], graph.CPU)], max_value=100, times=stamps, fmt=lambda v: f"{v:g}%")
    labels = []
    monkeypatch.setattr(graph, "_label", lambda _cr, text, *_args, **_kw: labels.append(text) or (0, 0, 0, 0))
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 201, 100)
    g._draw(g, cairo.Context(surface), 201, 100)
    assert "100%" in labels
    assert "50%" not in labels
    assert g._hover_lines(1) == [("25%", None), (fmt_clock(1004), None)]
