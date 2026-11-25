"""Parse numeric console output and retain bounded telemetry in host time."""

import json
import math
import re
from collections import deque

MAX_LINE_LENGTH = 4096
MAX_NAME_LENGTH = 64
MAX_SERIES = 16
WINDOW_SECONDS = 60
WINDOW_POINTS = 2048

_FIELD = re.compile(
    r"(?:^|(?<=[\s,;]))([A-Za-z_][A-Za-z0-9_.-]{0,63})\s*[=:]\s*"
    r"([+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)(?=$|[\s,;])"
)


def parse_line(line):
    """Return finite numeric fields from a single console line."""
    if len(line) > MAX_LINE_LENGTH:
        return {}
    line = line.strip()
    if line.startswith(("{", "[")):
        try:
            obj = json.loads(line, parse_int=float)
        except (ValueError, RecursionError):
            if line.startswith("{"):
                return {}
        else:
            if not isinstance(obj, dict):
                return {}
            return {
                name: value
                for name, value in obj.items()
                if 0 < len(name) <= MAX_NAME_LENGTH
                and name.isprintable()
                and type(value) is float
                and math.isfinite(value)
            }

    fields = {}
    for name, raw in _FIELD.findall(line):
        value = float(raw)
        if math.isfinite(value):
            fields[name] = value
    return fields


class LineBuffer:
    """Yield complete sample lines from chunks, discarding overlong lines."""

    def __init__(self):
        self._pending = ""

    def _append(self, text):
        if self._pending is not None:
            if len(self._pending) + len(text) <= MAX_LINE_LENGTH:
                self._pending += text
            else:
                self._pending = None

    def feed(self, chunk):
        *lines, tail = chunk.split("\n")
        for line in lines:
            self._append(line)
            fields = parse_line(self._pending) if self._pending is not None else {}
            self._pending = ""
            if fields:
                yield fields
        self._append(tail)


class SeriesWindow:
    """Sliding time window with points stored in arrival order per series."""

    def __init__(self):
        self._series = {}

    def add(self, timestamp, fields):
        for name, value in fields.items():
            if name not in self._series:
                if len(self._series) >= MAX_SERIES:
                    self._expire(timestamp)
                    if len(self._series) >= MAX_SERIES:
                        continue
                self._series[name] = deque(maxlen=WINDOW_POINTS)
            self._series[name].append((timestamp, value))

    def clear(self):
        self._series.clear()

    def _expire(self, now):
        cutoff = now - WINDOW_SECONDS
        for name, points in list(self._series.items()):
            while points and points[0][0] < cutoff:
                points.popleft()
            if not points:
                del self._series[name]

    def snapshot(self, now, seconds=WINDOW_SECONDS):
        self._expire(now)
        cutoff = now - seconds
        return {
            name: [(stamp, value) for stamp, value in points if stamp >= cutoff]
            for name, points in self._series.items()
        }


class SeriesStats:
    """Online population statistics with an optional tail of timestamped points."""

    def __init__(self, points=0):
        self._points = deque(maxlen=points)
        self._first_time = None
        self._last_time = None
        self._count = 0
        self._min = math.inf
        self._max = -math.inf
        self._last = None
        self._scale = self._mean = self._m2 = 0.0

    def add(self, timestamp, value):
        if self._first_time is None:
            self._first_time = timestamp
        self._last_time = timestamp
        self._count += 1
        self._min = min(self._min, value)
        self._max = max(self._max, value)
        self._last = value
        self._points.append((timestamp, value))

        # Welford's running variance, scaled to keep even extreme floats finite.
        magnitude = abs(value)
        if magnitude > self._scale:
            ratio = self._scale / magnitude
            self._mean *= ratio
            self._m2 *= ratio * ratio
            self._scale = magnitude
        normalized = value / self._scale if self._scale else 0.0
        delta = normalized - self._mean
        self._mean += delta / self._count
        self._m2 += delta * (normalized - self._mean)

    def summary(self):
        if not self._count:
            return {"count": 0}
        span = self._last_time - self._first_time
        result = {
            "last": self._last,
            "min": self._min,
            "max": self._max,
            "mean": max(-1.0, min(1.0, self._mean)) * self._scale,
            "stddev": math.sqrt(max(0.0, min(1.0, self._m2 / self._count))) * self._scale,
            "count": self._count,
            "rate_hz": (self._count - 1) / span if span > 0 else None,
        }
        if self._points.maxlen:
            result["points"] = list(self._points)
        return result
