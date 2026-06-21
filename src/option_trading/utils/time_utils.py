"""Time manipulation helpers."""
from __future__ import annotations

import datetime as _dt
from datetime import datetime, time, timedelta


def parse_time(time_str: str, fmt: str = "%H:%M:%S") -> time:
    """Parse a time string into a datetime.time object."""
    return datetime.strptime(time_str, fmt).time()


def now_time() -> time:
    """Return the current wall-clock time (no date component)."""
    return datetime.now().time()


def time_in_range(start: time, end: time, current: time) -> bool:
    """
    Return True if *current* falls within [start, end] inclusive.

    Works even when start > end (crosses midnight).
    """
    if start <= end:
        return start <= current <= end
    # Crosses midnight
    return current >= start or current <= end


def add_seconds_to_time(t: time, seconds: int) -> time:
    """Return a new time that is *seconds* ahead of *t*."""
    base = datetime.combine(_dt.date.today(), t)
    result = base + timedelta(seconds=seconds)
    return result.time()


def subtract_seconds_from_time(t: time, seconds: int) -> time:
    """Return a new time that is *seconds* before *t*."""
    base = datetime.combine(_dt.date.today(), t)
    result = base - timedelta(seconds=seconds)
    return result.time()
