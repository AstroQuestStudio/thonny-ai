"""MCP tools. Each tool call is one operation, shown as one row in Thonny's AI panel."""

import fnmatch
import hashlib
import os
import posixpath
import re
import time
import uuid

from . import config
from .backends import DeviceError, bridge_status, find_board_port, open_backend

MAX_TIMEOUT = 300.0
SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv", ".idea", ".vscode"}


def project_dir():
    return config.project_dir()


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


def _cap(text, limit=None):
    """Keeps the beginning and the end of a very long output so one call cannot flood the AI's
    context (a robot printing sensor values in a loop easily produces megabytes)."""
    limit = limit or config.max_output()
    if len(text) <= limit:
        return text
    head = limit // 3
    tail = limit - head
    omitted = len(text) - head - tail
    return f"{text[:head]}\n[… {omitted} characters omitted …]\n{text[-tail:]}"


def _safe_stop(backend):
    """Runs the project's safe-stop code (motors off…). Returns one line describing the outcome,
    never raises: it is called while something else already went wrong."""
    try:
        code = config.safe_stop_code()
    except config.ConfigError as e:
        return f"[safe stop NOT run: {e}]"
    if not code:
        return ""
    try:
        out, err, finished = backend.exec(code, 10.0, echo=True, quiet=False, echo_text="# ⛔ safe stop (thonny-ai)")
    except Exception as e:
        return f"[safe stop failed: {e}]"
    if err.strip():
        return f"[safe stop raised an error]\n{err.strip()}"
    if not finished:
        return "[safe stop did not finish within 10 s]"
    return "[safe stop code ran: hardware put in its safe state]"


def _run_quiet(backend, code, timeout=20.0):
    out, err, finished = backend.exec(code, timeout, echo=False, quiet=True)
    if err.strip():
        raise DeviceError(err.strip())
    if not finished:
        raise DeviceError(f"timed out after {timeout:.0f} s")
    return out


def _format_exec(backend, out, err, finished, timeout, note=""):
    text = _cap(out)
    if err:
        text += ("\n" if text else "") + "[stderr]\n" + _cap(err)
    if not finished:
        text += f"\n[interrupted after {timeout:.0f} s]"
    if note:
        text += "\n" + note
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
    try:
        st = bridge_status()
    except DeviceError as e:
        return f"{e}\nWait a moment, or check that no dialog is open in Thonny."
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
        note = "" if finished else _safe_stop(b)
        return _format_exec(b, out, err, finished, timeout, note)

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
        note = "" if finished else _safe_stop(b)
        return _format_exec(b, out, err, finished, timeout, note)

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


def _is_ignored(rel, patterns):
    """True when a path relative to the synced folder matches one of the glob patterns
    (matched against the whole path, the file name, and as a folder prefix)."""
    rel = rel.strip("/")
    name = posixpath.basename(rel)
    for pat in patterns:
        pat = pat.replace("\\", "/").strip("/")
        if fnmatch.fnmatchcase(rel, pat) or fnmatch.fnmatchcase(name, pat) or rel.startswith(pat + "/"):
            return True
    return False


def _collect_local(local_dir, ignore=()):
    files = []
    for root, dirs, names in os.walk(local_dir):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(names):
            if name.endswith(".pyc") or name.startswith("."):
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, local_dir).replace("\\", "/")
            if not _is_ignored(rel, ignore):
                files.append((full, rel))
    return files


def _remote_files(backend, root):
    """Every file under `root` on the board, as [(path, size)] (empty if the folder is missing)."""
    code = (
        "import os\n"
        "def _w(p):\n"
        "    try: names = os.listdir(p)\n"
        "    except OSError: return\n"
        "    for n in sorted(names):\n"
        "        f = p.rstrip('/') + '/' + n\n"
        "        s = os.stat(f)\n"
        "        if s[0] & 0x4000: _w(f)\n"
        "        else: print(s[6], f)\n"
        f"_w({root!r})\n"
    )
    files = []
    for line in _run_quiet(backend, code, 60).splitlines():
        size, _, path = line.strip().partition(" ")
        if path and size.isdigit():
            files.append((path, int(size)))
    return files


def _delete_remote(backend, paths):
    code = "import os\n" f"for _p in {paths!r}:\n    os.remove(_p)\n"
    _run_quiet(backend, code, 60)


def _remote_target(remote_dir, rel):
    return posixpath.join(remote_dir, rel) if remote_dir != "/" else "/" + rel


def tool_sync(args):
    local_dir = local_path(args.get("local_dir") or config.get("local_dir") or "src")
    if not os.path.isdir(local_dir):
        raise DeviceError(f"local folder not found: {local_dir} (pass local_dir)")
    remote_dir = remote_path(args.get("remote_dir") or config.get("remote_dir") or "/")
    force = bool(args.get("force"))
    dry_run = bool(args.get("dry_run"))
    delete = bool(args.get("delete"))
    reset = args.get("reset", True)
    ignore = list(config.ignore_patterns()) + list(args.get("ignore") or [])

    plan = []
    for full, rel in _collect_local(local_dir, ignore):
        with open(full, "rb") as f:
            data = f.read()
        plan.append((_remote_target(remote_dir, rel), data, hashlib.sha256(data).hexdigest()))
    if not plan:
        raise DeviceError(f"nothing to sync in {local_dir}")

    def fn(b):
        remote = {} if force else _remote_hashes(b, [p for p, _, _ in plan])
        changed = [(p, d) for p, d, h in plan if force or remote.get(p) != h]
        unchanged = len(plan) - len(changed)
        lines = [f"  {p} ({len(d)} B)" for p, d in changed]
        extra = []
        if delete:
            wanted = {p for p, _, _ in plan}
            prefix = remote_dir.rstrip("/") + "/"
            for path, _ in _remote_files(b, remote_dir):
                rel = path[len(prefix):] if path.startswith(prefix) else path.lstrip("/")
                if path not in wanted and not _is_ignored(rel, ignore):
                    extra.append(path)
            lines += [f"  DELETE {p}" for p in extra]
        if dry_run:
            return (
                f"Dry run via {b.name}: {len(changed)} file(s) would be uploaded, "
                f"{unchanged} unchanged"
                + (f", {len(extra)} would be deleted" if delete else "")
                + ".\n"
                + "\n".join(lines)
            )
        _make_dirs(b, [p for p, _ in changed])
        for p, d in changed:
            b.write_file(p, d)
        if extra:
            _delete_remote(b, extra)
        report = f"Synced via {b.name} from {local_dir}: {len(changed)} uploaded, {unchanged} unchanged"
        report += f", {len(extra)} deleted." if delete else "."
        if lines:
            report += "\n" + "\n".join(lines)
        if reset and (changed or extra):
            b.reset()
            report += (
                "\nSoft reset done: main.py restarts."
                if b.name == "serial"
                else "\nStop/Restart sent to Thonny (main.py runs on its own once Thonny is closed)."
            )
        return report

    detail = "\n".join(f"{p} ({len(d)} B)" for p, d, _ in plan)
    return Operation("sync", f"{os.path.basename(local_dir)} → {remote_dir}", detail).run(fn)


def tool_pull(args):
    """Board → computer: download a folder (default: everything) into a local folder."""
    remote_dir = remote_path(args.get("remote_dir") or "/")
    local_dir = local_path(args.get("local_dir") or "board_pull")
    overwrite = bool(args.get("overwrite"))
    dry_run = bool(args.get("dry_run"))
    ignore = list(config.ignore_patterns()) + list(args.get("ignore") or [])
    base = os.path.abspath(local_dir)

    def fn(b):
        files = _remote_files(b, remote_dir)
        if not files:
            return f"No file found under {remote_dir} on the board."
        prefix = remote_dir.rstrip("/") + "/"
        todo = []
        for path, _ in files:
            rel = path[len(prefix):] if path.startswith(prefix) else path.lstrip("/")
            if _is_ignored(rel, ignore):
                continue
            full = os.path.abspath(os.path.join(base, *rel.split("/")))
            try:
                inside = os.path.commonpath([base, full]) == base
            except ValueError:
                inside = False
            if not inside:
                raise DeviceError(f"refusing a path that escapes the local folder: {path}")
            todo.append((path, full, rel))
        hashes = _remote_hashes(b, [p for p, _, _ in todo])
        new, same, changed = [], [], []
        for path, full, rel in todo:
            if not os.path.exists(full):
                new.append((path, full, rel))
                continue
            with open(full, "rb") as f:
                local_hash = hashlib.sha256(f.read()).hexdigest()
            (same if local_hash == hashes.get(path) else changed).append((path, full, rel))
        to_write = new + (changed if overwrite else [])
        lines = [f"  NEW      {rel}" for _, _, rel in new]
        lines += [
            f"  {'UPDATE  ' if overwrite else 'DIFFERS '} {rel}" + ("" if overwrite else " (kept: pass overwrite)")
            for _, _, rel in changed
        ]
        summary = (
            f"{len(new)} new, {len(changed)} different "
            f"({'overwritten' if overwrite else 'kept'}), {len(same)} identical"
        )
        if dry_run:
            return f"Dry run via {b.name}: {summary}.\n" + "\n".join(lines)
        for path, full, _ in to_write:
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as f:
                f.write(b.read_file(path))
        return f"Pulled via {b.name} into {local_dir}: {summary}.\n" + "\n".join(lines)

    return Operation("pull", f"{remote_dir} → {os.path.basename(base)}").run(fn)


I2C_KNOWN = {
    0x0C: "AK8963 magnetometer",
    0x1E: "HMC5883L / LIS3MDL magnetometer",
    0x1D: "ADXL345 accelerometer",
    0x29: "VL53L0X / VL53L1X distance sensor, TCS34725 colour",
    0x3C: "SSD1306 OLED display",
    0x3D: "SSD1306 OLED display",
    0x40: "PCA9685 PWM driver, INA219 power monitor",
    0x48: "ADS1115 ADC",
    0x53: "ADXL345 accelerometer",
    0x68: "MPU-6050 / MPU-6500 / MPU-9250 / ICM-20948 IMU, DS3231 RTC",
    0x69: "MPU-6050 / ICM-20948 IMU (AD0 high)",
    0x6A: "LSM6DS3 IMU",
    0x6B: "LSM6DS3 IMU",
    0x70: "PCA9548 I2C mux, PCA9685 all-call",
    0x76: "BME280 / BMP280",
    0x77: "BME280 / BMP280 / BMP180",
}


def _int_arg(args, key, default=None, low=0, high=48):
    value = args.get(key, default)
    if value is None:
        return None
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise DeviceError(f"`{key}` must be an integer")
    if not low <= value <= high:
        raise DeviceError(f"`{key}` must be between {low} and {high}")
    return value


def tool_i2c_scan(args):
    bus = _int_arg(args, "id", 0, 0, 9)
    sda = _int_arg(args, "sda")
    scl = _int_arg(args, "scl")
    freq = _int_arg(args, "freq", 400000, 10000, 3400000)
    if (sda is None) != (scl is None):
        raise DeviceError("pass both `sda` and `scl`, or neither (board default pins)")
    pins = f", sda=Pin({sda}), scl=Pin({scl})" if sda is not None else ""
    code = (
        "from machine import I2C, Pin\n"
        f"_i = I2C({bus}{pins}, freq={freq})\n"
        "_a = _i.scan()\n"
        "print(' '.join(str(x) for x in _a))\n"
    )

    def fn(b):
        out = _run_quiet(b, code).strip()
        found = [int(x) for x in out.split()] if out else []
        where = f"sda=GP{sda} scl=GP{scl}" if sda is not None else "default pins"
        if not found:
            return (
                f"I2C{bus} ({where}): no device answered. Check wiring, pull-ups and power "
                "(sda/scl swapped is the usual culprit)."
            )
        rows = [f"  0x{a:02X}  {I2C_KNOWN.get(a, 'unknown device')}" for a in found]
        return f"I2C{bus} ({where}): {len(found)} device(s)\n" + "\n".join(rows)

    return Operation("i2c_scan", f"I2C{bus} {sda if sda is not None else ''}".strip()).run(fn)


GPIO_MODES = {"in": "Pin.IN", "in_pullup": "Pin.IN, Pin.PULL_UP", "in_pulldown": "Pin.IN, Pin.PULL_DOWN"}


def tool_gpio(args):
    pin = _int_arg(args, "pin", None, 0, 48)
    if pin is None:
        raise DeviceError("`pin` is required")
    mode = args.get("mode", "in")
    value = _int_arg(args, "value", None, 0, 1)
    if mode == "out":
        if value is None:
            raise DeviceError("mode `out` needs `value` (0 or 1)")
        code = f"from machine import Pin\n_p = Pin({pin}, Pin.OUT)\n_p.value({value})\nprint(_p.value())\n"
    elif mode == "adc":
        code = f"from machine import ADC, Pin\nprint(ADC(Pin({pin})).read_u16())\n"
    elif mode in GPIO_MODES:
        if value is not None:
            raise DeviceError("`value` only applies to mode `out`")
        code = f"from machine import Pin\nprint(Pin({pin}, {GPIO_MODES[mode]}).value())\n"
    else:
        raise DeviceError("`mode` must be one of: in, in_pullup, in_pulldown, out, adc")

    def fn(b):
        out = _run_quiet(b, code).strip()
        if mode == "adc":
            raw = int(out)
            return f"GP{pin} ADC = {raw} (0-65535, ≈ {raw / 65535 * 3.3:.2f} V at 3.3 V ref)"
        return f"GP{pin} ({mode}) = {out}"

    return Operation("gpio", f"GP{pin} {mode}" + (f"={value}" if value is not None else "")).run(fn)


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
_ = gc.collect()
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
        text += "\n" + _cap(out)
    if err:
        text += "\n[stderr]\n" + _cap(err)
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


def _watch(read, seconds, pattern):
    """Collects output for up to `seconds`. With a pattern, reads in 1 s slices and stops as soon
    as it appears (e.g. waiting for a "READY" line) or when the program ends.
    Returns (out, err, running, matched); matched is None when no pattern was given."""
    if pattern is None:
        out, err, running = read(seconds)
        return out, err, running, None
    deadline = time.time() + seconds
    out = err = ""
    running = None
    matched = False
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        o, e, running = read(min(1.0, remaining))
        out, err = out + o, err + e
        if pattern.search(out) or pattern.search(err):
            matched = True
            break
        if running is False:
            break
    return out, err, running, matched


def tool_monitor(args):
    seconds = min(float(args.get("seconds", 5)), MAX_TIMEOUT)
    pattern = None
    if args.get("until"):
        try:
            pattern = re.compile(args["until"])
        except re.error as e:
            raise DeviceError(f"`until` is not a valid regular expression: {e}")

    def fn(b):
        if getattr(b, "passive", False):
            out, err, running, matched = _watch(lambda s: (b.listen(s), "", None), seconds, pattern)
            text = _format_stream(b, out, err, None, "Console output")
        else:
            out, err, running, matched = _watch(b.monitor, seconds, pattern)
            text = _format_stream(b, out, err, running, "Output")
        if matched is not None:
            text += "\n[pattern matched]" if matched else f"\n[pattern /{pattern.pattern}/ not seen]"
        return text

    return Operation("monitor", f"{seconds:g} s", idle=False, passive=True).run(fn)


def _stop_program(b):
    """Ctrl-C the running program, then put the hardware in its safe state."""
    stopped = b.ensure_idle()
    if not stopped:
        b.interrupt()  # a program started outside thonny-ai (e.g. main.py at boot)
        time.sleep(0.3)
    text = f"Program stopped (via {b.name})." if stopped else f"Ctrl-C sent (via {b.name})."
    note = _safe_stop(b)
    return text + ("\n" + note if note else "")


def tool_interrupt(args):
    return Operation("interrupt", "stop program", idle=False).run(_stop_program)


def tool_stop(args):
    """Emergency stop: interrupt, then the safe-stop code; warns loudly when none is configured."""

    def fn(b):
        text = _stop_program(b)
        if "safe stop" not in text:
            text += (
                "\nWARNING: no safe-stop code configured, so motors driven by PWM may still be "
                'running. Add "safe_stop" to .thonny-ai.json (e.g. "import motors; motors.stop()"), '
                "or call `reset`."
            )
        return text

    return Operation("stop", "EMERGENCY STOP", idle=False).run(fn)


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
        "so it also works for main.py running on its own after boot. Pass `until` (a regular "
        "expression) to return as soon as that text appears, e.g. until='READY|Traceback'.",
        _schema({"seconds": _NUM, "until": _STR}),
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
        "reset (default true), ignore (glob patterns to skip, e.g. ['*.md','tests']), delete "
        "(also remove board files that no longer exist locally: use dry_run first). Defaults for "
        "local_dir, remote_dir and ignore can live in .thonny-ai.json.",
        _schema(
            {
                "local_dir": _STR,
                "remote_dir": _STR,
                "force": _BOOL,
                "dry_run": _BOOL,
                "reset": _BOOL,
                "delete": _BOOL,
                "ignore": {"type": "array", "items": _STR},
            }
        ),
    ),
    "pull": (
        tool_pull,
        "Download files from the board into a local folder (default 'board_pull'): backup, or "
        "recover code that only exists on the board. Existing local files that differ are kept "
        "unless overwrite is true. Options: remote_dir (default '/'), local_dir, overwrite, "
        "dry_run, ignore.",
        _schema(
            {
                "remote_dir": _STR,
                "local_dir": _STR,
                "overwrite": _BOOL,
                "dry_run": _BOOL,
                "ignore": {"type": "array", "items": _STR},
            }
        ),
    ),
    "interrupt": (
        tool_interrupt,
        "Stop the program running on the board (Ctrl-C, escalating to Stop/Restart in Thonny "
        "if needed), then run the project's safe-stop code if one is configured.",
        _schema(),
    ),
    "stop": (
        tool_stop,
        "EMERGENCY STOP for robots: interrupt the program and run the safe-stop code from "
        ".thonny-ai.json (\"safe_stop\") so motors and actuators end up off, even PWM outputs "
        "that Ctrl-C leaves running. Warns when no safe-stop code is configured.",
        _schema(),
    ),
    "i2c_scan": (
        tool_i2c_scan,
        "Scan an I2C bus and name the devices found (IMU, OLED, distance sensor, PWM driver…). "
        "Pass sda and scl (GPIO numbers) or none for the board's default pins; id (default 0) "
        "and freq (default 400000) are optional. Stops the program currently running first.",
        _schema({"sda": _NUM, "scl": _NUM, "id": _NUM, "freq": _NUM}),
    ),
    "gpio": (
        tool_gpio,
        "Read or drive one pin. mode: in (default), in_pullup, in_pulldown, out (needs value 0/1) "
        "or adc (analog read, 0-65535). Stops the program currently running first; remember an "
        "output pin keeps its level until reset.",
        _schema(
            {"pin": _NUM, "mode": {"type": "string", "enum": ["in", "in_pullup", "in_pulldown", "out", "adc"]}, "value": _NUM},
            ["pin"],
        ),
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


# MCP tool annotations (hints for clients: what may be auto-approved, what deserves a warning).
# (title, readOnly, destructive). "destructive" = can overwrite or delete something.
ANNOTATIONS = {
    "status": ("Connection status", True, False),
    "info": ("Board information", False, False),
    "exec": ("Run code on the board", False, True),
    "start": ("Start a background program", False, True),
    "monitor": ("Watch program output", True, False),
    "run_file": ("Run a local file on the board", False, True),
    "ls": ("List board files", True, False),
    "read": ("Read a board file", True, False),
    "write": ("Write a board file", False, True),
    "rm": ("Delete board files", False, True),
    "sync": ("Deploy a project folder", False, True),
    "pull": ("Download board files", False, False),
    "interrupt": ("Stop the program", False, False),
    "stop": ("Emergency stop", False, False),
    "reset": ("Soft reset", False, False),
    "shell": ("Read Thonny's shell", True, False),
    "open": ("Open a file in Thonny", False, False),
    "i2c_scan": ("Scan the I2C bus", False, False),
    "gpio": ("Read or drive a pin", False, True),
}


def annotations_for(name):
    title, read_only, destructive = ANNOTATIONS.get(name, (name, False, True))
    note = {"title": title, "readOnlyHint": read_only}
    if not read_only:
        note["destructiveHint"] = destructive
    return note
