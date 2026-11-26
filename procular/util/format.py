"""Human readable formatting helpers."""

import time
from datetime import datetime

_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def fmt_bytes(value, empty="—"):
    if value is None:
        return empty
    value = float(value)
    for unit in _UNITS:
        if abs(value) < 1024.0 or unit == _UNITS[-1]:
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}" if abs(value) < 100 else f"{value:.0f} {unit}"
        value /= 1024.0
    return f"{value:.1f} PiB"


def fmt_rate(value, empty="—"):
    if value is None:
        return empty
    if value <= 0:
        return ""
    return fmt_bytes(value) + "/s"


_BIT_UNITS = ("bit/s", "kbit/s", "Mbit/s", "Gbit/s", "Tbit/s")


def fmt_bit_rate(value, empty="—"):
    """A network rate given in bytes per second, shown in bits per second with decimal units
    ("12.5 Mbit/s"), as link speeds and network plans are quoted. Zero is "" like fmt_rate."""
    if value is None:
        return empty
    if value <= 0:
        return ""
    value = float(value) * 8
    for unit in _BIT_UNITS:
        if value < 1000.0 or unit == _BIT_UNITS[-1]:
            if unit == "bit/s":
                return f"{value:.0f} bit/s"
            return f"{value:.1f} {unit}" if value < 100 else f"{value:.0f} {unit}"
        value /= 1000.0


def fmt_short_bit_rate(value):
    """Compact network rate from bytes per second: "512b", "1.5k", "23M" bits (per second implied)."""
    value *= 8
    for unit in ("b", "k", "M", "G"):
        if value < 1000 or unit == "G":
            return f"{value:.0f}{unit}" if value >= 10 or unit == "b" else f"{value:.1f}{unit}"
        value /= 1000.0
    return ""


def fmt_short_rate(value):
    """Compact rate for tight spaces: "512B", "1.5K", "23M" (per second implied)."""
    for unit in ("B", "K", "M", "G"):
        if value < 1024 or unit == "G":
            return f"{value:.0f}{unit}" if value >= 10 or unit == "B" else f"{value:.1f}{unit}"
        value /= 1024.0
    return ""


def fmt_pct(value, empty=""):
    if value is None:
        return empty
    if value < 0.005:
        return ""
    if value < 10:
        return f"{value:.2f}"
    return f"{value:.1f}"


def fmt_pct_label(value):
    if value is None:
        return "—"
    return f"{value:.1f}%"


def fmt_duration(seconds, empty="—"):
    if seconds is None:
        return empty
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours:02}:{minutes:02}:{secs:02}"
    return f"{hours:02}:{minutes:02}:{secs:02}"


def fmt_cpu_time(seconds, empty="—"):
    if seconds is None:
        return empty
    whole, hundredths = divmod(round(seconds * 100), 100)  # (int(0.29 * 100) would be 28)
    hours, rem = divmod(whole, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02}:{secs:02}.{hundredths:02}"


def fmt_timestamp(ts, empty="—"):
    if not ts:
        return empty
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def fmt_clock(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def fmt_ago(ts):
    if not ts:
        return "—"
    return fmt_duration(max(0, time.time() - ts))


def fmt_relative(seconds, empty="—"):
    """Elapsed time in words: "just now", "4 seconds ago", "1 minute ago", "3 days ago"."""
    if seconds is None:
        return empty
    secs = int(max(0, seconds))
    if secs < 2:
        return "just now"
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60), ("second", 1)):
        if secs >= size:
            n = secs // size
            return f"{n} {unit}{'' if n == 1 else 's'} ago"
    return empty


def fmt_span(seconds, empty="—"):
    """Compact duration with the two most significant units: "42 s", "4 min", "2 h 05 min", "3 d 4 h"."""
    if seconds is None:
        return empty
    secs = int(max(0, seconds))
    if secs < 60:
        return f"{secs} s"
    if secs < 3600:
        return f"{secs // 60} min"
    if secs < 86400:
        return f"{secs // 3600} h {secs % 3600 // 60:02} min"
    return f"{secs // 86400} d {secs % 86400 // 3600} h"


def fmt_int(value, empty="—"):
    if value is None:
        return empty
    return f"{value:,}".replace(",", " ")
