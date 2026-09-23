"""MCP tools. Each tool call is one operation, shown as one row in Thonny's AI panel."""

import hashlib
import os
import posixpath
import uuid

from .backends import DeviceError, bridge_status, find_board_port, open_backend

MAX_TIMEOUT = 300.0
SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv", ".idea", ".vscode"}


def project_dir():
    return os.environ.get("THONNY_AI_PROJECT") or os.getcwd()


def local_path(path):
    return os.path.normpath(os.path.join(project_dir(), path))


def remote_path(path):
    path = path.replace("\\", "/")
    if ":" in path.split("/")[0]:  # Windows drive: local interpreter in Thonny
        return path
    return "/" + path.lstrip("/")


def _first_line(text, limit=80):
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    return line if len(line) <= limit else line[: limit - 1] + "…"


class Operation:
    """Runs one tool call on the board and reports the outcome to Thonny's AI panel."""

    def __init__(self, kind, summary, detail="", idle=True, passive=False):
        self.meta = {"id": uuid.uuid4().hex, "kind": kind, "summary": summary, "detail": detail}
        self.idle = idle
        self.passive = passive

    def run(self, fn):
        backend = open_backend(self.meta, passive=self.passive)
        try:
            if self.idle:
                backend.ensure_idle()
            result = fn(backend)
        except Exception as e:
            backend.finish(False, str(e))
            raise
        else:
            backend.finish(True, result)
            return result
        finally:
            backend.close()


def _run_quiet(backend, code, timeout=20.0):
    out, err, finished = backend.exec(code, timeout, echo=False, quiet=True)
    if err.strip():
        raise DeviceError(err.strip())
    if not finished:
        raise DeviceError(f"timed out after {timeout:.0f} s")
    return out


def _format_exec(backend, out, err, finished, timeout):
    text = out
    if err:
        text += ("\n" if text else "") + "[stderr]\n" + err
    if not finished:
        text += f"\n[interrupted after {timeout:.0f} s]"
    text = f"(via {backend.name})\n{text or '(no output)'}"
    if err.strip() or not finished:
        raise DeviceError(text)
    return text


def _make_dirs(backend, paths):
    dirs = set()
    for p in paths:
        parent = posixpath.dirname(p)
        while parent and parent not in ("/", ".") and not parent.endswith(":"):
            dirs.add(parent)
            parent = posixpath.dirname(parent)
    if not dirs:
        return
    code = (
        "import os\n"
        f"for _d in {sorted(dirs, key=len)!r}:\n"
        "    try: os.mkdir(_d)\n"
        "    except OSError: pass\n"
    )
    _run_quiet(backend, code)


def _remote_hashes(backend, paths):
    code = (
        "import hashlib, binascii\n"
        "def _h(p):\n"
        "    h=hashlib.sha256()\n"
        "    with open(p,'rb') as f:\n"
        "        while True:\n"
        "            c=f.read(512)\n"
        "            if not c: break\n"
        "            h.update(c)\n"
        "    return binascii.hexlify(h.digest()).decode()\n"
        f"for _p in {paths!r}:\n"
        "    try: print(_p, _h(_p))\n"
        "    except OSError: print(_p, '-')\n"
    )
    hashes = {}
    for line in _run_quiet(backend, code, 60).splitlines():
        path, _, digest = line.strip().rpartition(" ")
        if path:
            hashes[path] = digest
    return hashes


# ---------------------------------------------------------------- tools


def tool_status(args):
    st = bridge_status()
    lines = []
    if st:
        lines.append(
            f"Thonny is open (thonny-ai plugin active). Interpreter: {st.get('backend_description')}"
            f" | port: {st.get('port')} | connected: {st.get('connected')} | state: {st.get('state')}"
            f" | approval required: {st.get('require_approval')}"
        )
        if st.get("current_editor_file"):
            lines.append(f"File open in Thonny's editor: {st['current_editor_file']}")
    else:
        lines.append("Thonny is not running: tools will use the serial port directly.")
    try:
        port = find_board_port()
        lines.append(f"MicroPython board on USB: {port}" if port else "No MicroPython board on USB.")
    except Exception as e:
        lines.append(f"USB detection failed: {e}")
    lines.append(f"Project directory (relative paths): {project_dir()}")
    return "\n".join(lines)


def tool_exec(args):
    code = args["code"]
    timeout = min(float(args.get("timeout", 10)), MAX_TIMEOUT)

    def fn(b):
        out, err, finished = b.exec(code, timeout, echo=True, quiet=False)
        return _format_exec(b, out, err, finished, timeout)

    return Operation("exec", _first_line(code), code).run(fn)


def tool_run_file(args):
    path = local_path(args["path"])
    with open(path, encoding="utf-8") as f:
        code = f.read()
    timeout = min(float(args.get("timeout", 30)), MAX_TIMEOUT)
    rel = os.path.relpath(path, project_dir())

    def fn(b):
        out, err, finished = b.exec(
            code, timeout, echo=True, quiet=False, echo_text=f"# ▶ {rel}  (thonny-ai)"
        )
        return _format_exec(b, out, err, finished, timeout)

    return Operation("run_file", rel, code).run(fn)


def tool_ls(args):
    path = remote_path(args.get("path", "/"))
    code = (
        "import os\n"
        "def _w(p):\n"
        "    for n in sorted(os.listdir(p)):\n"
        "        f=p.rstrip('/')+'/'+n\n"
        "        s=os.stat(f)\n"
        "        d=s[0]&0x4000\n"
        "        print('DIR ' if d else 'FILE', s[6], f)\n"
        "        if d: _w(f)\n"
        f"_w({path!r})\n"
        "try:\n"
        "    _s=os.statvfs('/')\n"
        "    print('FREE', _s[0]*_s[3], 'TOTAL', _s[0]*_s[2])\n"
        "except Exception: pass\n"
    )
    return Operation("ls", path).run(lambda b: _run_quiet(b, code).strip() or "(empty)")


def tool_read(args):
    path = remote_path(args["path"])
    data = Operation("read", path).run(lambda b: b.read_file(path))
    return data.decode("utf-8", "replace")


def tool_write(args):
    path = remote_path(args["path"])
    data = args["content"].encode("utf-8")

    def fn(b):
        _make_dirs(b, [path])
        b.write_file(path, data)
        return f"{path} written ({len(data)} bytes)"

    return Operation("write", f"{path} ({len(data)} B)", args["content"]).run(fn)


def tool_rm(args):
    path = remote_path(args["path"])
    code = (
        "import os\n"
        "def _rm(p):\n"
        "    if os.stat(p)[0]&0x4000:\n"
        "        for n in os.listdir(p): _rm(p.rstrip('/')+'/'+n)\n"
        "        os.rmdir(p)\n"
        "    else: os.remove(p)\n"
        f"_rm({path!r})\n"
    )
    return Operation("rm", path).run(lambda b: (_run_quiet(b, code), f"{path} deleted")[1])


def _collect_local(local_dir):
    files = []
    for root, dirs, names in os.walk(local_dir):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(names):
            if name.endswith(".pyc") or name.startswith("."):
                continue
            full = os.path.join(root, name)
            files.append((full, os.path.relpath(full, local_dir).replace("\\", "/")))
    return files


def tool_sync(args):
    local_dir = local_path(args.get("local_dir") or "src")
    if not os.path.isdir(local_dir):
        raise DeviceError(f"local folder not found: {local_dir} (pass local_dir)")
    remote_dir = remote_path(args.get("remote_dir") or "/")
    force = bool(args.get("force"))
    dry_run = bool(args.get("dry_run"))
    reset = args.get("reset", True)

    plan = []
    for full, rel in _collect_local(local_dir):
        with open(full, "rb") as f:
            data = f.read()
        target = posixpath.join(remote_dir, rel) if remote_dir != "/" else "/" + rel
        plan.append((target, data, hashlib.sha256(data).hexdigest()))
    if not plan:
        raise DeviceError(f"nothing to sync in {local_dir}")

    def fn(b):
        remote = {} if force else _remote_hashes(b, [p for p, _, _ in plan])
        changed = [(p, d) for p, d, h in plan if force or remote.get(p) != h]
        unchanged = len(plan) - len(changed)
        lines = [f"  {p} ({len(d)} B)" for p, d in changed]
        if dry_run:
            return (
                f"Dry run via {b.name}: {len(changed)} file(s) would be uploaded, "
                f"{unchanged} unchanged.\n" + "\n".join(lines)
            )
        _make_dirs(b, [p for p, _ in changed])
        for p, d in changed:
            b.write_file(p, d)
        report = f"Synced via {b.name} from {local_dir}: {len(changed)} uploaded, {unchanged} unchanged."
        if lines:
            report += "\n" + "\n".join(lines)
        if reset and changed:
            b.reset()
            report += (
                "\nSoft reset done: main.py restarts."
                if b.name == "serial"
                else "\nStop/Restart sent to Thonny (main.py runs on its own once Thonny is closed)."
            )
        return report

    detail = "\n".join(f"{p} ({len(d)} B)" for p, d, _ in plan)
    return Operation("sync", f"{os.path.basename(local_dir)} → {remote_dir}", detail).run(fn)


INFO_CODE = """\
import sys, gc, os
try:
    import machine
except ImportError:
    machine = None
impl = sys.implementation
print('firmware:', impl.name, '.'.join(str(v) for v in impl.version[:3]), '|', getattr(impl, '_machine', sys.platform))
if machine:
    try: print('cpu_mhz:', machine.freq() // 1000000)
    except Exception: pass
    try:
        import binascii
        print('unique_id:', binascii.hexlify(machine.unique_id()).decode())
    except Exception: pass
gc.collect()
if hasattr(gc, 'mem_free'):
    print('ram_free:', gc.mem_free(), 'ram_used:', gc.mem_alloc())
try:
    s = os.statvfs('/')
    print('flash_free:', s[0] * s[3], 'flash_total:', s[0] * s[2])
except Exception: pass
try:
    import network
    w = network.WLAN(network.STA_IF)
    print('wifi:', ('connected ' + w.ifconfig()[0]) if w.isconnected() else 'available, not connected')
except Exception:
    print('wifi: none')
try: print('files:', sorted(os.listdir('/'))[:30])
except Exception: pass
"""


def tool_info(args):
    return Operation("info", "board info").run(lambda b: _run_quiet(b, INFO_CODE).strip())


def _format_stream(backend, out, err, running, prefix):
    text = f"{prefix} (via {backend.name})"
    if out:
        text += "\n" + out
    if err:
        text += "\n[stderr]\n" + err
    if running is None:
        text += "\n[console only: program state unknown]"
    else:
        text += "\n[still running]" if running else "\n[program ended]"
    return text


def tool_start(args):
    if bool(args.get("code")) == bool(args.get("path")):
        raise DeviceError("pass either `code` or `path`")
    echo_text = None
    if args.get("path"):
        path = local_path(args["path"])
        with open(path, encoding="utf-8") as f:
            code = f.read()
        label = os.path.relpath(path, project_dir())
        echo_text = f"# ▶ {label}  (thonny-ai, running in background)"
    else:
        code = args["code"]
        label = _first_line(code)
    wait = min(float(args.get("wait", 1.5)), 30.0)

    def fn(b):
        b.start(code, echo_text=echo_text)
        out, err, running = b.monitor(wait)
        return _format_stream(b, out, err, running, "Started")

    return Operation("start", label, code).run(fn)


def tool_monitor(args):
    seconds = min(float(args.get("seconds", 5)), MAX_TIMEOUT)

    def fn(b):
        if getattr(b, "passive", False):
            return _format_stream(b, b.listen(seconds), "", None, "Console output")
        out, err, running = b.monitor(seconds)
        return _format_stream(b, out, err, running, "Output")

    return Operation("monitor", f"{seconds:g} s", idle=False, passive=True).run(fn)


def tool_interrupt(args):
    def fn(b):
        stopped = b.ensure_idle()
        if not stopped:
            b.interrupt()  # a program started outside thonny-ai (e.g. main.py at boot)
        return f"Program stopped (via {b.name})." if stopped else f"Ctrl-C sent (via {b.name})."

    return Operation("interrupt", "stop program", idle=False).run(fn)


def tool_reset(args):
    return Operation("reset", "soft reset", idle=False).run(
        lambda b: (b.reset(), f"Soft reset sent (via {b.name}).")[1]
    )


def tool_shell(args):
    max_chars = int(args.get("max_chars", 4000))
    return Operation("shell", f"last {max_chars} chars", idle=False).run(
        lambda b: b.shell(max_chars) or "(empty shell)"
    )


def tool_open(args):
    path = local_path(args["path"])
    return Operation("open", path, idle=False).run(
        lambda b: (b.open(path), f"Opened in Thonny: {path}")[1]
    )


def _schema(props=None, required=()):
    return {"type": "object", "properties": props or {}, "required": list(required)}


_STR = {"type": "string"}
_NUM = {"type": "number"}
_BOOL = {"type": "boolean"}

TOOLS = {
    "status": (
        tool_status,
        "How the board is reached right now: Thonny open/connected (interpreter, port, state, "
        "whether the user must approve actions) or direct serial. Call it first when unsure.",
        _schema(),
    ),
    "info": (
        tool_info,
        "One-call board fact sheet: firmware and board name, CPU MHz, free/used RAM, free flash, "
        "unique id, Wi-Fi state, root files. Stops the program currently running.",
        _schema(),
    ),
    "exec": (
        tool_exec,
        "Run MicroPython code on the board and return its output (prints and tracebacks). "
        "When Thonny is open, the code appears in its shell highlighted as coming from the AI, "
        "exactly like a student typing it. Stops the program currently running first. "
        "timeout in seconds (default 10, max 300); on timeout the code is interrupted. "
        "For programs that loop forever (robots, sensor streams) use `start` + `monitor`.",
        _schema({"code": _STR, "timeout": _NUM}, ["code"]),
    ),
    "start": (
        tool_start,
        "Launch a long-running program WITHOUT waiting for it to end (robot main loop, sensor "
        "logging…): pass `code`, or `path` of a local .py file. Returns what it printed during the "
        "first `wait` seconds (default 1.5). Then use `monitor` to keep reading its output and "
        "`interrupt` to stop it. Stops the program currently running first.",
        _schema({"code": _STR, "path": _STR, "wait": _NUM}),
    ),
    "monitor": (
        tool_monitor,
        "Read what the running program prints, for up to `seconds` (default 5, returns early if "
        "it ends). Never interrupts it: with Thonny closed it just listens to the board's console, "
        "so it also works for main.py running on its own after boot.",
        _schema({"seconds": _NUM}),
    ),
    "run_file": (
        tool_run_file,
        "Run a .py file from the computer on the board without saving it there "
        "(path relative to the project directory). Default timeout 30 s.",
        _schema({"path": _STR, "timeout": _NUM}, ["path"]),
    ),
    "ls": (
        tool_ls,
        "List files on the board recursively, with sizes and free space.",
        _schema({"path": {"type": "string", "default": "/"}}),
    ),
    "read": (
        tool_read,
        "Read a file stored on the board.",
        _schema({"path": _STR}, ["path"]),
    ),
    "write": (
        tool_write,
        "Write a text file on the board (parent folders are created).",
        _schema({"path": _STR, "content": _STR}, ["path", "content"]),
    ),
    "rm": (
        tool_rm,
        "Delete a file or folder (recursively) on the board.",
        _schema({"path": _STR}, ["path"]),
    ),
    "sync": (
        tool_sync,
        "Upload a project folder (default 'src') to the board, sending only files whose "
        "SHA-256 differs from the board copy, then soft-reset so main.py restarts. "
        "Options: remote_dir (default '/'), force (upload everything), dry_run (only report), "
        "reset (default true).",
        _schema(
            {"local_dir": _STR, "remote_dir": _STR, "force": _BOOL, "dry_run": _BOOL, "reset": _BOOL}
        ),
    ),
    "interrupt": (
        tool_interrupt,
        "Stop the program running on the board (Ctrl-C, escalating to Stop/Restart in Thonny "
        "if needed).",
        _schema(),
    ),
    "reset": (
        tool_reset,
        "Soft-reset the board (boot.py then main.py run again).",
        _schema(),
    ),
    "shell": (
        tool_shell,
        "Read the end of Thonny's shell, i.e. what the user sees (Thonny must be open).",
        _schema({"max_chars": {"type": "integer", "default": 4000}}),
    ),
    "open": (
        tool_open,
        "Open a file from the computer in Thonny's editor (Thonny must be open).",
        _schema({"path": _STR}, ["path"]),
    ),
}
