"""Watch program output and heartbeat deadlines in Thonny's main thread."""

import math
import re
import time
from collections import deque

DEFAULTS = {
    "enabled": True,
    "patterns": [r"Traceback|MemoryError|OSError: \[Errno"],
    "heartbeat": None,
    "heartbeat_timeout": 5.0,
}
OUTPUT_OVERLAP = 1024


def _compile(pattern, field):
    if not isinstance(pattern, str) or not pattern:
        raise ValueError(f"{field} must be a non-empty regex string")
    try:
        return re.compile(pattern)
    except (re.error, OverflowError, RecursionError) as e:
        raise ValueError(f"{field}: {e}") from e


class Watchdog:
    def __init__(self, on_trigger, clock=time.monotonic):
        self._on_trigger = on_trigger
        self._clock = clock
        self._project = {}
        self._overrides = {}
        self._settings = {}
        self._history = deque(maxlen=10)
        self._tails = {}
        self._beat = None
        self._last_beat = None
        self.running = False
        self.tripped = False
        self._apply(DEFAULTS)

    @property
    def enabled(self):
        return self._settings["enabled"]

    def _apply(self, settings):
        if not isinstance(settings["enabled"], bool):
            raise ValueError("enabled must be a boolean")
        if not isinstance(settings["patterns"], list):
            raise ValueError("patterns must be an array of regex strings")
        patterns = [_compile(pattern, "patterns") for pattern in settings["patterns"]]
        heartbeat = settings["heartbeat"]
        beat = None if heartbeat in (None, "") else _compile(heartbeat, "heartbeat")
        timeout = settings["heartbeat_timeout"]
        try:
            valid_timeout = type(timeout) in (int, float) and math.isfinite(timeout) and timeout > 0
        except OverflowError:
            valid_timeout = False
        if not valid_timeout:
            raise ValueError("heartbeat_timeout must be a finite positive number")
        if settings == self._settings:
            return

        if (settings["enabled"], beat, timeout) != (
            self._settings.get("enabled"), self._beat,
            self._settings.get("heartbeat_timeout"),
        ):
            self._last_beat = self._clock()
        self._tails.clear()
        self._settings = {**settings, "patterns": list(settings["patterns"])}
        self._patterns, self._beat = patterns, beat

    @staticmethod
    def _validate_fields(settings):
        if not isinstance(settings, dict):
            raise ValueError("watchdog must be an object")
        unknown = settings.keys() - DEFAULTS.keys()
        if unknown:
            raise ValueError(f"unknown watchdog setting: {', '.join(sorted(unknown))}")

    def load_project(self, settings):
        self._validate_fields(settings)
        self._apply({**DEFAULTS, **settings, **self._overrides})
        self._project = settings

    def request(self, action, settings):
        if action != "configure" and settings:
            raise ValueError("watchdog settings require the configure action")
        if action in ("on", "off"):
            settings = {"enabled": action == "on"}
        elif action not in ("status", "configure"):
            raise ValueError("watchdog action must be status, on, off or configure")
        if settings:
            self._validate_fields(settings)
            self._apply({**DEFAULTS, **self._project, **self._overrides, **settings})
            self._overrides.update(settings)
        return {
            **self._settings,
            "patterns": list(self._settings["patterns"]),
            "running": self.running,
            "tripped": self.tripped,
            "triggers": [dict(event) for event in reversed(self._history)],
        }

    def begin(self):
        self.running = True
        self.tripped = False
        self._tails.clear()
        self._last_beat = self._clock()

    def end(self):
        self.running = False
        self._tails.clear()

    def _trip(self, reason, **detail):
        if self.enabled and self.running and not self.tripped:
            self.tripped = True
            event = {"reason": reason, "timestamp": time.time(), **detail}
            self._history.append(event)
            self._on_trigger(event)

    def disconnected(self):
        try:
            self._trip("disconnect")
        finally:
            self.end()

    def output(self, stream, data):
        if not (self.enabled and self.running and not self.tripped and data):
            return
        tail = self._tails.get(stream, "")
        text = tail + data
        self._tails[stream] = text[-OUTPUT_OVERLAP:]
        for pattern in self._patterns:
            if any(match.end() > len(tail) for match in pattern.finditer(text)):
                self._trip("pattern", pattern=pattern.pattern)
                return
        if self._beat and any(match.end() > len(tail) for match in self._beat.finditer(text)):
            self._last_beat = self._clock()

    def tick(self, paused=False):
        if paused:
            self._last_beat = self._clock()
        elif self.running and self.enabled and not self.tripped and self._beat:
            timeout = self._settings["heartbeat_timeout"]
            if self._clock() - self._last_beat >= timeout:
                self._trip("heartbeat", timeout=timeout)
