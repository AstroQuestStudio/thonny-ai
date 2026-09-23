"""Two ways to reach the board:

- ThonnyBackend: through the thonny-ai plugin running inside Thonny (preferred: everything is
  visible in Thonny's shell and can require the user's approval);
- SerialBackend: straight to the serial port with MicroPython's raw REPL, used only when
  Thonny is not running (a serial port can be opened by one program at a time).
"""

import base64
import json
import os
import time
import urllib.error
import urllib.request

BRIDGE_PORT = int(os.environ.get("THONNY_AI_PORT", "47821"))
BRIDGE_URL = f"http://127.0.0.1:{BRIDGE_PORT}"
SERIAL_CHUNK = 1024
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


def bridge_status():
    """Thonny status dict, or None when Thonny (with the plugin) is not running."""
    try:
        return bridge_request("/status", timeout=0.8)
    except (urllib.error.URLError, OSError, DeviceError, ValueError):
        return None


class ThonnyBackend:
    name = "thonny"
    cursor = None  # position in Thonny's output log, shared by start/monitor across calls

    def __init__(self, op):
        self.op = op

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
        return r["stdout"], r["stderr"], r["finished"]

    def start(self, code, echo_text=None):
        ThonnyBackend.cursor = self._post(
            "/start", {"code": code, "echo": True, "echo_text": echo_text}
        )["cursor"]

    def monitor(self, seconds):
        r = self._post(
            "/monitor", {"seconds": seconds, "since": ThonnyBackend.cursor}, timeout=seconds + 15
        )
        ThonnyBackend.cursor = r["cursor"]
        return r["stdout"], r["stderr"], r["running"]

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


# ---------------------------------------------------------------- direct serial

KNOWN_VIDS = {0x2E8A, 0x1209, 0x239A, 0x10C4, 0x1A86, 0x0403, 0x303A, 0x0D28}
_session = None  # SerialBackend kept open while a started program runs


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


class SerialBackend:
    """MicroPython raw REPL. Output of a running program is framed as
    `OK <stdout> \\x04 <stderr> \\x04 >`, which lets `monitor` stream it."""

    name = "serial"

    def __init__(self, passive=False):
        """passive=True only listens to the board's console: the running program is not
        interrupted (used by `monitor` to watch main.py running on its own)."""
        import serial

        port = os.environ.get("THONNY_AI_SERIAL_PORT") or find_board_port()
        if not port:
            raise DeviceError("no MicroPython board found on USB. Is it plugged in?")
        try:
            self.ser = serial.Serial(port, 115200, timeout=0.05)
        except serial.SerialException as e:
            raise DeviceError(f"{port} is busy ({e}). Close the program using it, or open Thonny.")
        self.port = port
        self.passive = passive
        self.running = False
        self._phase = None
        self._pending = b""
        if not passive:
            self._enter_raw_repl()

    def listen(self, seconds):
        deadline = time.time() + seconds
        data = b""
        while time.time() < deadline:
            data += self.ser.read(512)
        return data.decode("utf-8", "replace")

    def _read_until(self, marker, timeout):
        buf = b""
        deadline = time.time() + timeout
        while time.time() < deadline:
            buf += self.ser.read(512)
            if marker in buf:
                return buf
        raise TimeoutError(buf)

    def _enter_raw_repl(self):
        self.ser.write(b"\r\x03\x03")
        time.sleep(0.2)
        self.ser.reset_input_buffer()
        self.ser.write(b"\r\x01")
        try:
            self._read_until(b"raw REPL; CTRL-B to exit\r\n>", 3)
        except TimeoutError:
            raise DeviceError("the board does not answer the raw REPL (is MicroPython flashed?)")
        self.running = False

    def start(self, code, echo_text=None):
        data = code.encode("utf-8")
        for i in range(0, len(data), 256):
            self.ser.write(data[i : i + 256])
            time.sleep(0.01)
        self.ser.write(b"\x04")
        try:
            buf = self._read_until(b"OK", 3)
        except TimeoutError:
            raise DeviceError("the board did not accept the code")
        self.running = True
        self._phase = "out"
        self._pending = buf[buf.index(b"OK") + 2 :]

    def monitor(self, seconds):
        out, err = b"", b""
        deadline = time.time() + seconds
        while self.running:
            if self._pending:
                chunk, self._pending = self._pending, b""
            elif time.time() < deadline:
                chunk = self.ser.read(512)
                if not chunk:
                    continue
            else:
                break
            while chunk and self.running:
                if self._phase in ("out", "err"):
                    head, sep, chunk = chunk.partition(b"\x04")
                    if self._phase == "out":
                        out += head
                    else:
                        err += head
                    if sep:
                        self._phase = "err" if self._phase == "out" else "prompt"
                elif self._phase == "prompt":
                    if b">" in chunk:
                        self.running = False
                    chunk = b""
        return out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), self.running

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
            for i in range(0, len(data), SERIAL_CHUNK):
                self._run(f"_f.write({data[i : i + SERIAL_CHUNK]!r})")
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
        self.ser.write(b"\x03")

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
        self.running = False
        self.ser.write(b"\x02")
        time.sleep(0.1)
        self.ser.write(b"\x04")

    def shell(self, max_chars):
        raise DeviceError("Thonny is not running: its shell is unavailable")

    def open(self, path):
        raise DeviceError("Thonny is not running: cannot open a file in its editor")

    def finish(self, ok, result):
        pass

    def close(self, force=False):
        """Releases the port, unless a started program is still running (kept for monitor)."""
        global _session
        if self.running and not force:
            _session = self
            return
        if _session is self:
            _session = None
        try:
            if not self.passive:
                self.ser.write(b"\x02")
            self.ser.close()
        except Exception:
            pass


# ---------------------------------------------------------------- selection


def open_backend(op, passive=False):
    """Thonny when it is running (reconnecting it if it lost the board), serial otherwise."""
    global _session
    st = bridge_status()
    if st is None:
        if os.environ.get("THONNY_AI_SERIAL_FALLBACK", "1") == "0":
            raise DeviceError("Thonny is not running and the serial fallback is disabled")
        if _session is not None:
            return _session
        return SerialBackend(passive=passive)
    if _session is not None:  # Thonny was opened: hand the port over to it
        _session.close(force=True)
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
