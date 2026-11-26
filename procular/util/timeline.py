"""Elapsed-time coordinates shared by history graphs and sparklines."""

import time
from bisect import bisect_left
from itertools import islice, pairwise
from typing import NamedTuple


class SampleTime(NamedTuple):
    elapsed: float  # seconds since boot, including suspend; unaffected by wall clock corrections
    wall: float  # local clock time for the hover label
    interval: float = 1.0  # expected seconds between samples at the time of collection
    gap: bool = False  # do not draw a line from the previous sample to this one

    @classmethod
    def now(cls):
        return cls(time.clock_gettime(time.CLOCK_BOOTTIME), time.time())


def visible_samples(times, seconds):
    """The sample times in the last `seconds`, oldest first."""
    if not times:
        return []
    cutoff = times[-1].elapsed - seconds
    samples = []
    for stamp in reversed(times):
        if stamp.elapsed < cutoff:
            break
        samples.append(stamp)
    samples.reverse()
    return samples


def positions(samples, width, seconds):
    """X coordinate of each sample, with the newest at the right edge."""
    if not samples:
        return []
    right = max(0, width - 1)
    end = samples[-1].elapsed
    return [right * (1 - (end - t.elapsed) / seconds) for t in samples]


def tail(values, count):
    """Last `count` values in chronological order (efficient for deques)."""
    result = list(islice(reversed(values), count))
    result.reverse()
    return result


def segments(samples, xs, values):
    """Continuous runs of (x, value), with shorter series right-aligned to the timeline."""
    start = len(samples) - len(values)
    points = []
    for i, value in enumerate(values, start):
        if (samples[i].gap or value is None) and points:
            yield points
            points = []
        if value is not None:
            points.append((xs[i], value))
    if points:
        yield points


def nearest_sample(samples, xs, x):
    """Index of the point nearest x, or None over an empty area or sampling gap."""
    if not xs or x < xs[0] - 2:
        return None
    i = bisect_left(xs, x)
    if i == 0 or i == len(xs):
        return min(i, len(xs) - 1)
    left, right = x - xs[i - 1], xs[i] - x
    if samples[i].gap and min(left, right) > 2:
        return None
    return i - 1 if left < right else i


def time_average(samples, values):
    """Average of the plotted line, weighted by elapsed time and excluding gaps."""
    total = duration = 0.0
    for (p, a), (t, b) in pairwise(zip(samples[len(samples) - len(values):], values)):
        if not t.gap and a is not None and b is not None:
            dt = t.elapsed - p.elapsed
            total += (a + b) / 2 * dt
            duration += dt
    return total / duration if duration else None
