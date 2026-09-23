"""Adapter between the bridge and Thonny. Tk is not thread-safe: every access to
Thonny goes through the main loop via MainThread.call()."""

import queue
import threading
import time
import uuid

from thonny import get_runner, get_shell, get_workbench
from thonny.common import InlineCommand, ToplevelCommand

from .core import BridgeError

AI_TAG = "thonny_ai_command"
LOG_MAX_ENTRIES = 5000


class MainThread:
    def __init__(self, workbench):
        self._wb = workbench
        self._jobs = queue.Queue()
        self._wb.after(40, self._poll)

    def _poll(self):
        try:
            while True:
                fn, box = self._jobs.get_nowait()
                try:
                    box["result"] = fn()
                except Exception as e:
                    box["error"] = e
                box["done"].set()
        except queue.Empty:
            pass
        self._wb.after(25, self._poll)

    def call(self, fn, timeout=15.0):
        box = {"done": threading.Event()}
        self._jobs.put((fn, box))
        if not box["done"].wait(timeout):
            raise BridgeError("Thonny is not responding (is a dialog blocking the window?)")
        if "error" in box:
            err = box["error"]
            raise err if isinstance(err, BridgeError) else BridgeError(f"{type(err).__name__}: {err}")
        return box.get("result")


def _new_id():
    return "thonny_ai_" + uuid.uuid4().hex[:12]


class ThonnyHost:
    def __init__(self):
        self.main = MainThread(get_workbench())
        self._exec_lock = threading.Lock()
        self._exec = None  # running execution: {"id", "out", "done", "error", "quiet"}
        self._waiters = {}  # inline command id -> {"done", "msg"}
        # every program output (AI or user), numbered, for `monitor`
        self._log = []
        self._seq = 0
        self._log_lock = threading.Lock()
        wb = get_workbench()
        wb.bind("ProgramOutput", self._on_output, True)
        wb.bind("ToplevelResponse", self._on_toplevel_response, True)
        wb.bind("InlineResponse", self._on_inline_response, True)
        wb.bind("BackendTerminated", self._on_terminated, True)

    # ------------------------------------------------------------ Thonny events

    def _append_log(self, stream, data):
        with self._log_lock:
            self._seq += 1
            self._log.append((self._seq, stream, data))
            if len(self._log) > LOG_MAX_ENTRIES:
                del self._log[: len(self._log) - LOG_MAX_ENTRIES]

    def _on_output(self, msg):
        stream = getattr(msg, "stream_name", "stdout")
        current = self._exec
        if current is not None:
            current["out"].append((stream, msg.data))
        if current is None or not current["quiet"]:
            self._append_log(stream, msg.data)

    def _on_toplevel_response(self, msg):
        # the local CPython backend reports tracebacks here instead of on stderr
        trace = ""
        user_exception = msg.get("user_exception")
        if user_exception:
            trace = "".join(item[0] for item in user_exception.get("items", []))
            trace = trace or str(user_exception.get("message", ""))
            self._append_log("stderr", trace)
        current = self._exec
        if current is not None and msg.get("command_id") in (current["id"], None):
            current["error"] = msg.get("error")
            if trace:
                current["out"].append(("stderr", trace))
            current["done"].set()

    def _on_inline_response(self, msg):
        waiter = self._waiters.pop(msg.get("command_id"), None)
        if waiter is not None:
            waiter["msg"] = msg
            waiter["done"].set()

    def _on_terminated(self, msg=None):
        reason = "connection to the board lost (unplugged or rebooted)"
        current = self._exec
        if current is not None:
            current["error"] = reason
            current["done"].set()
        for waiter in list(self._waiters.values()):
            waiter["msg"] = {"error": reason}
            waiter["done"].set()
        self._waiters.clear()

    # ------------------------------------------------------------ helpers (main thread)

    @staticmethod
    def _require_ready():
        runner = get_runner()
        if runner.get_backend_proxy() is None:
            raise BridgeError("Thonny is not connected to any interpreter (board unplugged?)")
        if not runner.is_waiting_toplevel_command():
            raise BridgeError(
                f"interpreter busy (state={runner.get_state()}): a program is running, "
                "interrupt it first"
            )
        return runner

    @staticmethod
    def _echo_in_shell(display):
        """Shows the code after >>> as if it had been typed, highlighted as coming from the AI."""
        text = get_shell().text
        if text.has_pending_input():
            return  # never overwrite what the user is typing
        display = display if display.endswith("\n") else display + "\n"
        start = text.index("input_start")
        text.insert("input_start", display, ("toplevel", "command", AI_TAG))
        end = text.index(f"{start}+{len(display)}c")
        text.mark_set("input_start", end)
        text.mark_set("output_insert", end)
        text.mark_set("command_io_start", "output_insert")
        text.mark_gravity("command_io_start", "left")
        if hasattr(text, "_applied_io_events"):
            text._applied_io_events = []
            text._queued_io_events = []
        text.see("end")

    @staticmethod
    def configure_ai_tag():
        text = get_shell().text
        r, g, b = (c // 256 for c in text.winfo_rgb(text.cget("background")))
        dark = (0.299 * r + 0.587 * g + 0.114 * b) < 128
        text.tag_configure(AI_TAG, background="#2e2745" if dark else "#f1ecfb")
        text.tag_lower(AI_TAG)

    # ------------------------------------------------------------ API used by BridgeCore

    def status(self):
        def fn():
            wb = get_workbench()
            runner = get_runner()
            proxy = runner.get_backend_proxy()
            backend = wb.get_option("run.backend_name")
            spec = wb.get_backends().get(backend)
            connected = proxy is not None and (
                proxy.is_connected() if hasattr(proxy, "is_connected") else True
            )
            editor = wb.get_editor_notebook().get_current_editor()
            return {
                "thonny": True,
                "backend": backend,
                "backend_description": getattr(spec, "description", backend),
                "port": getattr(proxy, "_port", None) or wb.get_option(f"{backend}.port"),
                "connected": connected,
                "state": runner.get_state(),
                "current_editor_file": editor.get_filename() if editor else None,
            }

        return self.main.call(fn)

    def exec_code(self, code, timeout, echo=False, quiet=False, echo_text=None):
        if not self._exec_lock.acquire(timeout=2):
            raise BridgeError("another AI execution is already in progress")
        try:
            current = {
                "id": _new_id(),
                "out": [],
                "done": threading.Event(),
                "error": None,
                "quiet": quiet,
            }

            def start():
                runner = self._require_ready()
                self._exec = current
                if quiet:
                    get_shell().set_ignore_program_output(True)
                elif echo:
                    try:
                        self._echo_in_shell(echo_text or code)
                    except Exception:
                        pass  # echo is cosmetic, execution must go on
                runner.send_command(
                    ToplevelCommand(
                        "execute_source", source=code, tty_mode=False, id=current["id"]
                    )
                )

            try:
                self.main.call(start)
            except BridgeError:
                self._exec = None
                raise
            finished = current["done"].wait(timeout)
            self._exec = None
            if quiet:
                self.main.call(lambda: get_shell().set_ignore_program_output(False))
            return {
                "finished": finished,
                "stdout": "".join(d for s, d in current["out"] if s != "stderr"),
                "stderr": "".join(d for s, d in current["out"] if s == "stderr"),
                "error": current["error"],
            }
        finally:
            self._exec_lock.release()

    def start_code(self, code, echo=True, echo_text=None):
        """Launches code without waiting for it to end (robot loops, sensor streams)."""
        cursor = self._seq

        def start():
            runner = self._require_ready()
            if echo:
                try:
                    self._echo_in_shell(echo_text or code)
                except Exception:
                    pass
            runner.send_command(
                ToplevelCommand("execute_source", source=code, tty_mode=False, id=_new_id())
            )

        self.main.call(start)
        return {"cursor": cursor}

    def monitor(self, seconds, since=None):
        """Output of the running program: everything after `since` (or after now),
        collected for up to `seconds`, returning early once the program has ended."""
        if since is None:
            since = self._seq
        deadline = time.time() + max(0.0, seconds)
        running = self.main.call(lambda: get_runner().is_running())
        while running and time.time() < deadline:
            time.sleep(0.2)
            running = self.main.call(lambda: get_runner().is_running())
        with self._log_lock:
            entries = [e for e in self._log if e[0] > since]
            cursor = self._seq
        return {
            "stdout": "".join(d for _, s, d in entries if s != "stderr"),
            "stderr": "".join(d for _, s, d in entries if s == "stderr"),
            "running": running,
            "cursor": cursor,
        }

    def _inline(self, name, timeout, **kw):
        cmd_id = _new_id()
        waiter = {"done": threading.Event(), "msg": None}

        def start():
            runner = self._require_ready()
            self._waiters[cmd_id] = waiter
            runner.send_command(InlineCommand(name, id=cmd_id, **kw))

        self.main.call(start)
        if not waiter["done"].wait(timeout):
            self._waiters.pop(cmd_id, None)
            raise BridgeError(f"{name}: no answer from the board after {timeout:.0f} s")
        msg = waiter["msg"]
        error = msg.get("error") or msg.get("errors")
        if error:
            raise BridgeError(f"{name}: {error}")
        return msg

    def _uses_local_filesystem(self):
        def fn():
            proxy = self._require_ready().get_backend_proxy()
            return proxy.uses_local_filesystem()

        return self.main.call(fn)

    def write_file(self, path, data):
        if self._uses_local_filesystem():  # local Python: the "device" is this computer
            with open(path, "wb") as f:
                f.write(data)
            return
        self._inline(
            "write_file",
            timeout=30 + len(data) / 2000,
            path=path,
            content_bytes=data,
            make_shebang_scripts_executable=False,
        )

    def read_file(self, path):
        if self._uses_local_filesystem():
            with open(path, "rb") as f:
                return f.read()
        return self._inline("read_file", timeout=60, path=path)["content_bytes"]

    def interrupt(self):
        def fn():
            proxy = get_runner().get_backend_proxy()
            if proxy is None:
                raise BridgeError("no interpreter connected")
            proxy.interrupt()

        self.main.call(fn)

    def restart(self):
        self.main.call(lambda: get_runner().cmd_stop_restart())

    def shell_text(self, max_chars):
        return self.main.call(lambda: get_shell().text.get("1.0", "end-1c")[-max_chars:])

    def open_file(self, path):
        self.main.call(lambda: get_workbench().get_editor_notebook().show_file(path))
