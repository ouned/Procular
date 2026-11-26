import pytest

pytest.importorskip("gi")

from procular.widgets.sparkline import MIN_RANGE, value_range  # noqa: E402

MIB = 1 << 20


def _height(value, lo, hi):
    """0 = bottom edge, 1 = top edge."""
    return (value - lo) / (hi - lo)


def test_range_steady_value_is_a_line_in_the_middle():
    lo, hi = value_range([370 * MIB] * 60, "range")
    assert _height(370 * MIB, lo, hi) == pytest.approx(0.5)
    lo, hi = value_range([0] * 60, "range")  # kernel threads
    assert _height(0, lo, hi) == pytest.approx(0.5)


def test_range_small_change_is_a_small_step():
    # memory creeping up by a few pages must not send the older samples to the bottom edge
    data = [373 * MIB] * 58 + [373 * MIB + 8192, 373 * MIB + 16384]
    lo, hi = value_range(data, "range")
    heights = [_height(v, lo, hi) for v in data]
    assert all(0.49 < h < 0.51 for h in heights)
    # 1 % more memory: a tenth of the height
    data = [100 * MIB] * 30 + [101 * MIB] * 30
    lo, hi = value_range(data, "range")
    assert _height(101 * MIB, lo, hi) - _height(100 * MIB, lo, hi) == pytest.approx(0.01 / MIN_RANGE, rel=0.02)


def test_range_large_change_uses_the_full_height():
    data = [100 * MIB, 150 * MIB, 200 * MIB]
    assert value_range(data, "range") == (100 * MIB, 200 * MIB)


def test_range_has_no_jump_where_the_widening_ends():
    below = value_range([100.0, 100.0 * (1 + MIN_RANGE) - 0.01], "range")
    above = value_range([100.0, 100.0 * (1 + MIN_RANGE) + 0.01], "range")
    assert below == pytest.approx(above, abs=0.05)


def test_zero_mode_starts_at_zero_and_respects_the_floor():
    assert value_range([0.0, 1.0, 2.0], "zero", floor=6.25) == (0.0, 6.25)
    assert value_range([0.0, 30.0], "zero", floor=6.25) == (0.0, 30.0)


def test_nice_ceil_rounds_up_to_round_numbers():
    from procular.widgets.graph import nice_ceil

    assert nice_ceil(6.25) == 10
    assert nice_ceil(10) == 10
    assert nice_ceil(23) == 25
    assert nice_ceil(0) == 1
    # bytes: round within the binary unit, 64 KiB -> 100 KiB, not 100 000 B
    assert nice_ceil(64 * 1024, base=1024) == 100 * 1024
    assert nice_ceil(3 * MIB, base=1024) == 5 * MIB
