"""Reach MicroPython through Thonny, USB serial, or Wi-Fi WebREPL."""

import base64
import codecs
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config

BRIDGE_PORT = int(os.environ.get("THONNY_AI_PORT", "47821"))
BRIDGE_URL = f"http://127.0.0.1:{BRIDGE_PORT}"
FILE_CHUNK = 1024
READY_STATE = "waiting_toplevel_command"


class DeviceError(Exception):
    pass


# ---------------------------------------------------------------- Thonny bridge


def bridge_request(path, payload=None, timeout=5.0):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        BRIDGE_URL + path,
        data=data,
        method="GET" if payload is None else "POST",
        headers={"X-Thonny-AI": "1", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        with e:
            try:
                body = json.loads(e.read() or b"{}")
            except ValueError:
                body = {"error": f"HTTP {e.code}"}
    if not body.get("ok"):
        raise DeviceError(body.get("error", "unknown error from the Thonny bridge"))
    return body


def _bridge_listening():
    port = urllib.parse.urlparse(BRIDGE_URL).port
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def bridge_status():
    """Thonny status dict, or None when Thonny (with the plugin) is not running.

    A Thonny that is running but momentarily busy must not be mistaken for a closed one
    (that would silently switch to the serial port), hence the separate listening check."""
    if not _bridge_listening():
        return None
    try:
        return bridge_request("/status", timeout=15)
    except (urllib.error.URLError, OSError, ValueError) as e:
        if not _bridge_listening():
            return None
        raise DeviceError(f"Thonny is open but not responding ({e})")


_OBJECT_LINK = re.compile(r"\[object_link_for_thonny=[^\]]*\](.*?)\[/object_link_for_thonny\]", re.S)


def _clean(text):
    """Thonny wraps REPL values in object-inspector links: keep only the value."""
    return _OBJECT_LINK.sub(r"\1", text)


class ThonnyBackend:
    name = "thonny"
    cursor = None  # position in Thonny's output log, shared by start/monitor across calls

    def __init__(self, op):
        self.op = op
        self._telemetry_cursor = None

    def _post(self, path, payload=None, timeout=15.0):
        return bridge_request(path, {**(payload or {}), "op": self.op}, timeout)

    def exec(self, code, timeout=10.0, echo=False, quiet=True, echo_text=None):
        r = self._post(
            "/exec",
            {"code": code, "timeout": timeout, "echo": echo, "quiet": quiet, "echo_text": echo_text},
            timeout + 15,
        )
        if r.get("error"):
            raise DeviceError(r["error"])
        if not r["finished"]:
            self.ensure_idle()
        return _clean(r["stdout"]), _clean(r["stderr"]), r["finished"]

    def start(self, code, echo_text=None):
        ThonnyBackend.cursor = self._post(
            "/start", {"code": code, "echo": True, "echo_text": echo_text}
        )["cursor"]

    def monitor(self, seconds):
        # Telemetry starts at the current log head and leaves the monitor cursor alone.
        telemetry = self.op.get("kind") == "telemetry"
        since = self._telemetry_cursor if telemetry else ThonnyBackend.cursor
        r = self._post(
            "/monitor", {"seconds": seconds, "since": since}, timeout=seconds + 15
        )
        if telemetry:
            self._telemetry_cursor = r["cursor"]
        else:
            ThonnyBackend.cursor = r["cursor"]
        return _clean(r["stdout"]), _clean(r["stderr"]), r["running"]

    def write_file(self, path, data):
        self._post(
            "/write_file",
            {"path": path, "content_b64": base64.b64encode(data).decode("ascii")},
            timeout=60 + len(data) / 2000,
        )

    def read_file(self, path):
        r = self._post("/read_file", {"path": path}, timeout=90)
        return base64.b64decode(r["content_b64"])

    def interrupt(self):
        self._post("/interrupt")

    def _wait_ready(self, seconds):
        deadline = time.time() + seconds
        while time.time() < deadline:
            st = bridge_request("/status")
            if st.get("connected") and st["state"] == READY_STATE:
                return True
            time.sleep(0.2)
        return False

    def ensure_idle(self):
        """Stops whatever runs. Returns True if something had to be stopped."""
        if bridge_request("/status")["state"] == READY_STATE:
            return False
        self.interrupt()
        if self._wait_ready(3):
            return True
        # Ctrl-C was not enough (e.g. local Python started without a console): Stop/Restart
        self.reset()
        if not self._wait_ready(15):
            raise DeviceError("the interpreter is still busy after Ctrl-C and Stop/Restart")
        return True

    def reset(self):
        self._post("/restart")

    def shell(self, max_chars):
        return self._post("/shell", {"max_chars": max_chars})["text"]

    def open(self, path):
        self._post("/open", {"path": path})

    def finish(self, ok, result):
        try:
            bridge_request("/finish", {"op": self.op, "ok": ok, "result": result}, timeout=3)
        except Exception:
            pass

    def close(self):
        pass


# ---------------------------------------------------------------- raw REPL

KNOWN_VIDS = {0x2E8A, 0x1209, 0x239A, 0x10C4, 0x1A86, 0x0403, 0x303A, 0x0D28}
_session = None  # Direct backend kept open while a started program runs


def find_board_port():
    from serial.tools import list_ports

    ports = list(list_ports.comports())
    for p in ports:
        if p.vid == 0x2E8A:  # Raspberry Pi (Pico, Pico W, Pico 2…)
            return p.device
    for p in ports:
        if p.vid in KNOWN_VIDS:
            return p.device
    return None


class RawReplBackend:
    """MicroPython raw REPL. Output of a running program is framed as
    `OK <stdout> \\x04 <stderr> \\x04 >`, which lets `monitor` stream it."""

    def __init__(self, stream, passive=False):
        """passive=True only listens to the board's console: the running program is not
        interrupted (used by `monitor` to watch main.py running on its own)."""
        self._stream = stream
        self.passive = passive
        self.running = False
        self._raw = False
        self._phase = None
        self._pending = b""
        self._console_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            if not passive:
                self._enter_raw_repl()
        except BaseException:
            self.close(force=True)
            raise

    def listen(self, seconds):
        deadline = time.monotonic() + seconds
        data = bytearray()
        while time.monotonic() < deadline:
            data.extend(self._stream.read(512))
        return self._console_decoder.decode(data)

    def _read_until(self, marker, timeout):
        buf = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            buf.extend(self._stream.read(512))
            if marker in buf:
                return bytes(buf)
        raise TimeoutError

    def _enter_raw_repl(self):
        self._stream.write(b"\r\x03\x03")
        time.sleep(0.2)
        self._stream.reset_input_buffer()
        self._stream.write(b"\r\x01")
        try:
            self._read_until(b"raw REPL; CTRL-B to exit\r\n>", 3)
        except TimeoutError:
            raise DeviceError("the board does not answer the raw REPL (is MicroPython flashed?)") from None
        self._raw = True
        self.running = False

    def start(self, code, echo_text=None):
        data = code.encode("utf-8")
        start = 0
        while start < len(data):
            end = min(start + 256, len(data))
            # WebSocket text frames must end at a UTF-8 code point boundary.
            while end < len(data) and data[end] & 0xC0 == 0x80:
                end -= 1
            self._stream.write(data[start:end])
            time.sleep(0.01)
            start = end
        self._stream.write(b"\x04")
        try:
            buf = self._read_until(b"OK", 3)
        except TimeoutError:
            raise DeviceError("the board did not accept the code") from None
        self.running = True
        self._phase = "out"
        self._pending = buf[buf.index(b"OK") + 2 :]
        self._decoders = {phase: codecs.getincrementaldecoder("utf-8")("replace") for phase in ("out", "err")}

    def monitor(self, seconds):
        output = {"out": [], "err": []}
        deadline = time.monotonic() + seconds
        while self.running:
            if self._pending:
                chunk, self._pending = self._pending, b""
            elif time.monotonic() < deadline:
                chunk = self._stream.read(512)
                if not chunk:
                    continue
            else:
                break
            while chunk and self.running:
                if self._phase in ("out", "err"):
                    head, sep, chunk = chunk.partition(b"\x04")
                    output[self._phase].append(self._decoders[self._phase].decode(head, final=bool(sep)))
                    if sep:
                        self._phase = "err" if self._phase == "out" else "prompt"
                elif self._phase == "prompt":
                    if b">" in chunk:
                        self.running = False
                    chunk = b""
        return "".join(output["out"]), "".join(output["err"]), self.running

    def exec(self, code, timeout=10.0, echo=False, quiet=True, echo_text=None):
        self.start(code)
        out, err, running = self.monitor(timeout)
        finished = not running
        if running:
            self.interrupt()
            more_out, more_err, running = self.monitor(3)
            out, err = out + more_out, err + more_err
            if running:
                self._enter_raw_repl()
        return out, err, finished

    def _run(self, code, timeout=15.0):
        out, err, finished = self.exec(code, timeout)
        if err.strip() or not finished:
            raise DeviceError(err.strip() or "timeout")
        return out

    def write_file(self, path, data):
        self._run(f"_f=open({path!r},'wb')")
        try:
            for i in range(0, len(data), FILE_CHUNK):
                self._run(f"_f.write({data[i : i + FILE_CHUNK]!r})")
        finally:
            self._run("_f.close()")

    def read_file(self, path):
        code = (
            "import binascii\n"
            f"with open({path!r},'rb') as _f:\n"
            "    while True:\n"
            "        _c=_f.read(512)\n"
            "        if not _c: break\n"
            "        print(binascii.hexlify(_c).decode())\n"
        )
        return bytes.fromhex("".join(self._run(code, 90).split()))

    def interrupt(self):
        self._stream.write(b"\x03")

    def ensure_idle(self):
        """Stops a program started with `start`. Returns True if one was running."""
        if not self.running:
            return False
        self.interrupt()
        self.monitor(3)
        if self.running:
            self._enter_raw_repl()
        return True

    def reset(self):
        self.ensure_idle()
        self._stream.write(b"\x02")
        self._raw = False
        time.sleep(0.1)
        self._stream.write(b"\x04")

    def shell(self, max_chars):
        raise DeviceError("Thonny is not running: its shell is unavailable")

    def open(self, path):
        raise DeviceError("Thonny is not running: cannot open a file in its editor")

    def finish(self, ok, result):
        pass

    def close(self, force=False):
        """Release the transport unless a started program is still running."""
        global _session
        if self.running and not force:
            _session = self
            return
        if _session is self:
            _session = None
        try:
            if self._raw:
                self._stream.write(b"\x02")
        except Exception:
            pass
        finally:
            self._raw = False
            try:
                self._stream.close()
            except Exception:
                pass


class SerialBackend(RawReplBackend):
    name = "serial"

    def __init__(self, passive=False, port=None):
        import serial

        self.port = port or os.environ.get("THONNY_AI_SERIAL_PORT") or find_board_port()
        if not self.port:
            raise DeviceError("no MicroPython board found on USB. Is it plugged in?")
        try:
            self.ser = serial.Serial(self.port, 115200, timeout=0.05)
        except serial.SerialException as e:
            raise DeviceError(f"{self.port} is busy ({e}). Close the program using it, or open Thonny.")
        super().__init__(self.ser, passive=passive)


# ---------------------------------------------------------------- selection


def transport_mode():
    mode = os.environ.get("THONNY_AI_TRANSPORT", "auto")
    if mode not in ("auto", "usb", "wifi"):
        raise DeviceError("THONNY_AI_TRANSPORT must be auto, usb, or wifi.")
    return mode


def _open_thonny(op, st):
    if op.get("kind") == "telemetry":  # read-only: never restart Thonny just to listen
        if not st.get("connected"):
            raise DeviceError("Thonny is open but its interpreter is disconnected")
        return ThonnyBackend(op)
    if st.get("connected"):
        return ThonnyBackend(op)
    backend = ThonnyBackend(op)
    backend.reset()  # Stop/Restart: Thonny reconnects to the board
    deadline = time.time() + 10
    while time.time() < deadline:
        time.sleep(0.5)
        st = bridge_status()
        if st and st.get("connected") and st.get("state") == READY_STATE:
            return backend
    raise DeviceError(
        "Thonny is open but cannot reconnect to the board. Check the cable, "
        "then click Stop/Restart in Thonny."
    )


def probe_webrepl(settings):
    """Check WebREPL login without entering raw REPL or interrupting a program."""
    if getattr(_session, "endpoint", None) == (settings.host, settings.port):
        return
    from .webrepl import connect

    connect(settings, timeout=1.0).close()


def open_backend(op, passive=False, *, transport=None):
    """Auto selects Thonny, USB, then WebREPL. Reuse any running direct session."""
    mode = transport if transport is not None else transport_mode()
    st = bridge_status() if mode == "auto" else None
    usb_enabled = mode == "usb" or os.environ.get("THONNY_AI_SERIAL_FALLBACK", "1") != "0"

    if _session is not None:
        if st is None and (
            (_session.name == "serial" and usb_enabled and mode != "wifi")
            or (_session.name == "wifi" and mode != "usb")
        ):
            return _session
        _session.close(force=True)
    if st is not None:
        return _open_thonny(op, st)
    if mode != "wifi" and usb_enabled:
        port = os.environ.get("THONNY_AI_SERIAL_PORT") or find_board_port()
        if port:
            return SerialBackend(passive=passive, port=port)
        if mode == "usb":
            raise DeviceError("no MicroPython board found on USB. Is it plugged in?")

    settings = config.webrepl_settings()
    if settings.host:
        from .webrepl import WebReplBackend

        return WebReplBackend(settings, passive=passive)
    if mode == "wifi":
        raise DeviceError("Set THONNY_AI_WEBREPL_HOST or webrepl.host to use Wi-Fi.")
    if not usb_enabled:
        raise DeviceError("Thonny is not running, the serial fallback is disabled, and Wi-Fi is not configured.")
    raise DeviceError("no MicroPython board found on USB and Wi-Fi is not configured")
