"""Automatic reconnection to the board.

Two triggers, never against the user's intent:
- the board *reappears* on USB (unplugged then plugged back, or plugged after startup);
- Thonny prints "Connection lost" while the board is still present (USB glitch).
A deliberate disconnect (Disconnect menu) keeps the port present without "Connection lost",
so it is never undone.
"""

import logging
import threading
import time

from thonny import get_runner, get_workbench

from .i18n import t

logger = logging.getLogger(__name__)

POLL_INTERVAL = 1.5
MIN_RETRY_DELAY = 3.0
MAX_RETRIES_WHILE_PRESENT = 4


def _list_ports():
    from serial.tools import list_ports

    return list(list_ports.comports())


class ReconnectWatcher:
    def __init__(self, on_event=lambda text: None):
        self._on_event = on_event
        self._previous = None
        self._lost = False
        self._retries = 0
        self._last_attempt = 0.0
        self._ports = None
        wb = get_workbench()
        wb.bind("ProgramOutput", self._on_output, True)
        threading.Thread(target=self._scan_loop, name="thonny-ai-ports", daemon=True).start()
        wb.after(1000, self._tick)

    def _enabled(self):
        return get_workbench().get_option("thonny_ai.auto_reconnect", True)

    def _on_output(self, msg):
        if "Connection lost" in (getattr(msg, "data", "") or ""):
            self._lost = True
            self._retries = 0

    def _scan_loop(self):
        while True:
            try:
                self._ports = _list_ports()
            except Exception:
                logger.exception("thonny-ai: port scan failed")
            time.sleep(POLL_INTERVAL)

    def _candidates(self, ports):
        wb = get_workbench()
        backend = wb.get_option("run.backend_name")
        configured = wb.get_option(f"{backend}.port")
        if configured is None:  # local interpreter, no board
            return None
        if configured not in ("auto", "webrepl"):
            return {p.device for p in ports if p.device == configured}
        spec = wb.get_backends().get(backend)
        is_potential = getattr(spec.proxy_class, "_is_potential_port", None) if spec else None
        if is_potential is None:
            return None
        found = set()
        for p in ports:
            try:
                if is_potential(p):
                    found.add(p.device)
            except Exception:
                pass
        return found

    def _tick(self):
        try:
            self._check()
        except Exception:
            logger.exception("thonny-ai: reconnect error")
        get_workbench().after(int(POLL_INTERVAL * 1000), self._tick)

    def _check(self):
        ports = self._ports
        if ports is None or not self._enabled():
            return
        present = self._candidates(ports)
        if present is None:
            self._previous = None
            return
        appeared = bool(self._previous is not None and present - self._previous)
        self._previous = present

        runner = get_runner()
        if runner.get_backend_proxy() is not None:
            if runner.is_waiting_toplevel_command():
                self._lost = False
                self._retries = 0
            return
        if not present:
            return

        now = time.time()
        if appeared:
            # the USB port may appear before MicroPython is ready: allow a few attempts
            self._lost = True
            self._retries = 0
        elif not self._lost or self._retries >= MAX_RETRIES_WHILE_PRESENT:
            return
        if now - self._last_attempt < MIN_RETRY_DELAY:
            return

        self._last_attempt = now
        self._retries += 1
        port = sorted(present)[0]
        self._on_event(t("reconnecting", port=port))
        logger.info("thonny-ai: auto-reconnect (%s)", port)
        runner.restart_backend(clean=True, automatic=True)
