from procular.util.format import fmt_bit_rate, fmt_relative, fmt_short_bit_rate, fmt_span


def test_fmt_relative():
    assert fmt_relative(None) == "—"
    assert fmt_relative(0.5) == "just now"
    assert fmt_relative(4) == "4 seconds ago"
    assert fmt_relative(60) == "1 minute ago"
    assert fmt_relative(3 * 3600 + 59) == "3 hours ago"
    assert fmt_relative(86400) == "1 day ago"
    assert fmt_relative(-5) == "just now"  # clock skew


def test_fmt_span():
    assert fmt_span(None) == "—"
    assert fmt_span(42) == "42 s"
    assert fmt_span(4 * 60 + 59) == "4 min"
    assert fmt_span(2 * 3600 + 5 * 60) == "2 h 05 min"
    assert fmt_span(3 * 86400 + 4 * 3600 + 30) == "3 d 4 h"


def test_fmt_bit_rate():
    assert fmt_bit_rate(None) == "—"
    assert fmt_bit_rate(0) == ""  # like fmt_rate: an idle rate stays empty
    assert fmt_bit_rate(10) == "80 bit/s"
    assert fmt_bit_rate(125) == "1.0 kbit/s"  # decimal units, as network speeds are quoted
    assert fmt_bit_rate(1_562_500) == "12.5 Mbit/s"
    assert fmt_bit_rate(125_000_000) == "1.0 Gbit/s"
    assert fmt_bit_rate(12_500_000) == "100 Mbit/s"


def test_fmt_short_bit_rate():
    assert fmt_short_bit_rate(0) == "0b"
    assert fmt_short_bit_rate(64) == "512b"
    assert fmt_short_bit_rate(187.5) == "1.5k"
    assert fmt_short_bit_rate(2_875_000) == "23M"


def test_fmt_cpu_time_rounds_hundredths():
    from procular.util.format import fmt_cpu_time
    assert fmt_cpu_time(29 / 100) == "0:00:00.29"  # 0.29 * 100 is 28.999…
    assert fmt_cpu_time(59.999) == "0:01:00.00"
    assert fmt_cpu_time(3661.5) == "1:01:01.50"
