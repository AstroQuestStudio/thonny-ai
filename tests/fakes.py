"""Test doubles: a fake Thonny host, a fake board (sandboxed filesystem) and a fake serial
port speaking MicroPython's raw REPL protocol."""

import builtins
import contextlib
import io
import os
import threading
import traceback


class SandboxOS:
    """The subset of MicroPython's `os` used by thonny-ai, rooted in a temp folder."""

    def __init__(self, root):
        self.root = root

    def _p(self, path):
        return os.path.join(self.root, path.lstrip("/"))

    def listdir(self, path="/"):
        return os.listdir(self._p(path))

    def stat(self, path):
        return tuple(os.stat(self._p(path)))

    def mkdir(self, path):
        os.mkdir(self._p(path))

    def remove(self, path):
        os.remove(self._p(path))

    def rename(self, source, destination):
        os.replace(self._p(source), self._p(destination))

    def rmdir(self, path):
        os.rmdir(self._p(path))

    def statvfs(self, path):
        return (4096, 4096, 212, 210, 210, 0, 0, 0, 0, 255)


class FakeBoard:
    """Runs code with CPython in a persistent namespace, like a MicroPython REPL."""

    def __init__(self, root):
        self.os = SandboxOS(root)
        real_import = builtins.__import__

        def _import(name, *args, **kw):
            return self.os if name == "os" else real_import(name, *args, **kw)

        def _open(path, mode="r", *args, **kw):
            return builtins.open(self.os._p(path), mode, *args, **kw)

        sandbox_builtins = dict(vars(builtins))
        sandbox_builtins["__import__"] = _import
        sandbox_builtins["open"] = _open
        self.namespace = {"__builtins__": sandbox_builtins, "__name__": "__main__"}

    def run(self, code):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                exec(compile(code, "<stdin>", "exec"), self.namespace)
            except BaseException:
                err.write(traceback.format_exc())
        return out.getvalue(), err.getvalue()

    def write(self, path, data):
        with builtins.open(self.os._p(path), "wb") as f:
            f.write(data)

    def read(self, path):
        with builtins.open(self.os._p(path), "rb") as f:
            return f.read()


class FakeBackend:
    """Stands in for ThonnyBackend/SerialBackend in tool tests."""

    name = "fake"

    def __init__(self, board):
        self.board = board
        self.calls = []
        self.finished = None

    def exec(self, code, timeout=10.0, echo=False, quiet=True, echo_text=None):
        self.calls.append(("exec", echo, echo_text))
        out, err = self.board.run(code)
        return out, err, True

    _started = None

    def start(self, code, echo_text=None):
        self.calls.append(("start", echo_text))
        self._started = self.board.run(code)

    def monitor(self, seconds):
        out, err = self._started or ("", "")
        self._started = None
        return out, err, False

    def write_file(self, path, data):
        self.calls.append(("write", path))
        self.board.write(path, data)

    def read_file(self, path):
        return self.board.read(path)

    def interrupt(self):
        self.calls.append(("interrupt",))

    def ensure_idle(self):
        return False

    def reset(self):
        self.calls.append(("reset",))

    def shell(self, max_chars):
        return ">>> "

    def open(self, path):
        self.calls.append(("open", path))

    def finish(self, ok, result):
        self.finished = (ok, result)

    def close(self):
        pass


class FakeHost:
    """Stands in for ThonnyHost (the Thonny side of the bridge)."""

    def __init__(self, board=None):
        self.board = board
        self.state = "waiting_toplevel_command"
        self.calls = []
        self.files = {}

    def status(self):
        return {
            "thonny": True,
            "backend": "FakePico",
            "backend_description": "Fake Pico",
            "port": "COM99",
            "connected": True,
            "state": self.state,
            "current_editor_file": None,
        }

    def exec_code(self, code, timeout, echo=False, quiet=False, echo_text=None):
        self.calls.append(("exec", code, echo, quiet, echo_text))
        out, err = self.board.run(code) if self.board else ("", "")
        return {"finished": True, "stdout": out, "stderr": err, "error": None}

    def start_code(self, code, echo=True, echo_text=None):
        self.calls.append(("start", code, echo_text))
        self._started = self.board.run(code) if self.board else ("", "")
        return {"cursor": 0}

    def monitor(self, seconds, since=None):
        out, err = getattr(self, "_started", None) or ("", "")
        self._started = None
        return {"stdout": out, "stderr": err, "running": False, "cursor": 1}

    def write_file(self, path, data):
        self.calls.append(("write", path))
        self.files[path] = data

    def read_file(self, path):
        return self.files[path]

    def interrupt(self):
        self.calls.append(("interrupt",))

    def restart(self):
        self.calls.append(("restart",))

    def shell_text(self, max_chars):
        return ">>> hello"[-max_chars:]

    def open_file(self, path):
        self.calls.append(("open", path))


class FakeRawReplSerial:
    """Emulates a MicroPython board's raw REPL over a serial port."""

    def __init__(self, board, hang_on=None):
        self.board = board
        self.hang_on = hang_on
        self._out = bytearray()
        self._code = bytearray()
        self._raw = False
        self._lock = threading.Lock()
        self.closed = False

    def write(self, data):
        with self._lock:
            for byte in data:
                self._on_byte(byte)
        return len(data)

    def _on_byte(self, byte):
        if byte == 0x01:
            self._raw = True
            self._code.clear()
            self._out += b"raw REPL; CTRL-B to exit\r\n>"
        elif byte == 0x02:
            self._raw = False
        elif byte == 0x03:
            if self._hanging:
                self._hanging = False
                self._out += b"\x04Traceback (most recent call last):\r\nKeyboardInterrupt: \r\n\x04>"
        elif byte == 0x04 and self._raw:
            code = self._code.decode()
            self._code.clear()
            self._out += b"OK"
            if self.hang_on and self.hang_on in code:
                self._hanging = True
                self._out += b"tick\r\n"
                return
            out, err = self.board.run(code)
            self._out += out.replace("\n", "\r\n").encode() + b"\x04"
            self._out += err.replace("\n", "\r\n").encode() + b"\x04>"
        elif self._raw:
            self._code.append(byte)

    _hanging = False

    def read(self, size=1):
        with self._lock:
            chunk = bytes(self._out[:size])
            del self._out[:size]
            return chunk

    def reset_input_buffer(self):
        with self._lock:
            self._out.clear()

    def close(self):
        self.closed = True
