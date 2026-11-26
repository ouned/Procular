from procular.widgets.graph import find_peaks


def test_flat_line_has_no_peaks():
    assert find_peaks([5.0] * 100, 100, 10) == []


def test_single_spike():
    values = [5.0] * 100
    values[40] = 90.0
    assert find_peaks(values, 100, 10) == [40]


def test_small_bump_ignored():
    values = [5.0] * 100
    values[40] = 12.0  # below 15% of scale
    assert find_peaks(values, 100, 10) == []


def test_peaks_ordered_highest_first_and_spaced():
    values = [5.0] * 100
    values[20] = 60.0
    values[22] = 70.0  # same spike as index 20, only the top survives
    values[70] = 95.0
    assert find_peaks(values, 100, 10) == [70, 22]


def test_max_peaks_limit():
    values = [0.0] * 200
    for i in range(10, 200, 20):
        values[i] = 80.0
    assert len(find_peaks(values, 100, 5, max_peaks=3)) == 3


def test_short_or_unscaled_input():
    assert find_peaks([1, 2], 100, 3) == []
    assert find_peaks([0, 50, 0, 0], 0, 1) == []


def test_fmt_span():
    from procular.widgets.graph import fmt_span

    assert [fmt_span(s) for s in (45, 99.6, 390, 600, 1200, 9000)] == ["45 s", "1.7 min", "6.5 min", "10 min",
                                                                         "20 min", "2.5 h"]


def test_tidy_drops_empty_decimals_only():
    from procular.widgets.graph import tidy

    assert [tidy(t) for t in ("100.0%", "50.00%", "0.50%", "15.1 GiB", "10.0 GiB")] == ["100%", "50%", "0.50%",
                                                                                        "15.1 GiB", "10 GiB"]


def test_scale_rounds_in_display_units():
    from types import SimpleNamespace

    from procular.widgets.graph import Graph, Series

    # No real Graph: constructing a GTK widget without a display segfaults on GTK 4.24 (CI containers)
    def scale(scale_unit, min_scale=0):
        graph = SimpleNamespace(max_value=None, series=[Series(data, (1, 0, 0), "x")], scale_unit=scale_unit,
                                min_scale=min_scale)
        return Graph._scale(graph, 10)

    data = [3_000_000.0]  # bytes/s: 24 Mbit/s
    assert scale(1) == 5_000_000  # 40 Mbit/s: not round
    assert scale(8) * 8 == 25_000_000  # 25 Mbit/s
    # a floor keeps small values from filling the graph (pressure: fractions of a percent)
    data = [0.3]
    assert scale(1) == 0.5
    assert scale(1, min_scale=10) == 10
