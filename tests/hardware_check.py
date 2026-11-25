"""Real-board validation of every thonny-ai tool, in one command.

Plug the board in (Thonny open or closed, both work), then:

    python tests/hardware_check.py                     # uses THONNY_AI_PROJECT for the first stop
    python tests/hardware_check.py --i2c 4 5           # also scan I2C on GP4/GP5
    python tests/hardware_check.py --adc 26            # also read the ADC on GP26

ROBOTS: put the wheels in the air. The first step is an emergency `stop` with the project's
safe-stop code, so whatever main.py was doing is halted before anything else runs.
Everything the check writes lives in /_tai_check on the board and is deleted at the end.
"""

import argparse
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from thonny_ai_mcp import server  # noqa: E402

REMOTE = "/_tai_check"
RESULTS = []


def call(name, **arguments):
    result = server.handle({"method": "tools/call", "params": {"name": name, "arguments": arguments}})
    return result["isError"], result["content"][0]["text"]


def step(label, fn):
    started = time.time()
    try:
        detail = fn() or ""
        ok = True
    except AssertionError as e:
        ok, detail = False, str(e)
    except Exception as e:  # a crash in one step must not hide the others
        ok, detail = False, f"{type(e).__name__}: {e}"
    elapsed = time.time() - started
    RESULTS.append((label, ok))
    mark = "ok  " if ok else "FAIL"
    print(f"  {mark} {label}  ({elapsed:.1f} s){'  ' + detail if detail else ''}", flush=True)
    return ok


def expect_ok(name, *needles, **arguments):
    is_error, text = call(name, **arguments)
    assert not is_error, f"{name} returned an error: {text[:300]}"
    for needle in needles:
        assert needle in text, f"{name}: {needle!r} not in output: {text[:300]}"
    return text


def expect_error(name, needle, **arguments):
    is_error, text = call(name, **arguments)
    assert is_error, f"{name} should have failed: {text[:300]}"
    assert needle in text, f"{name}: {needle!r} not in error: {text[:300]}"
    return text


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--i2c", nargs=2, type=int, metavar=("SDA", "SCL"))
    parser.add_argument("--adc", type=int, metavar="GPIO")
    parser.add_argument("--keep", action="store_true", help="leave /_tai_check on the board")
    args = parser.parse_args()

    real_project = os.environ.get("THONNY_AI_PROJECT") or os.getcwd()
    print(f"thonny-ai hardware check — project for the first stop: {real_project}")

    # 1. Safety first, with the real project's safe-stop code.
    os.environ["THONNY_AI_PROJECT"] = real_project
    step("status: board reachable", lambda: _status())
    step("stop: emergency stop first (wheels in the air!)", lambda: expect_ok("stop").splitlines()[-1])

    # 2. Everything else in a throwaway project.
    tmp = tempfile.mkdtemp(prefix="thonny-ai-hw-")
    project = os.path.join(tmp, "project")
    os.makedirs(os.path.join(project, "src", "lib"))
    files = {"src/main_check.py": "print('check main')\n", "src/lib/util_check.py": "X = 42\n"}
    for rel, content in files.items():
        with open(os.path.join(project, rel), "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
    os.environ["THONNY_AI_PROJECT"] = project

    step("info", lambda: expect_ok("info", "ram_free").splitlines()[0])
    step("exec: output", lambda: expect_ok("exec", "4", code="print(2 + 2)") and "")
    step(
        "exec: onboard LED blinks",
        lambda: expect_ok(
            "exec",
            "blinked",
            code="from machine import Pin\nimport time\nl = Pin('LED', Pin.OUT)\n"
            "for _ in range(6):\n    l.toggle(); time.sleep(0.1)\nl.off()\nprint('blinked')",
        )
        and "",
    )
    step("exec: traceback reported", lambda: expect_error("exec", "ZeroDivisionError", code="1/0") and "")
    step(
        "exec: timeout interrupts an endless loop",
        lambda: expect_error("exec", "interrupted", code="while True:\n    pass", timeout=2) and "",
    )
    step("exec: board usable after the interrupt", lambda: expect_ok("exec", "alive", code="print('alive')") and "")
    step("exec: huge output is capped", lambda: expect_ok("exec", "omitted", code="print('x' * 60000)") and "")

    step("write", lambda: expect_ok("write", "written", path=f"{REMOTE}/a.txt", content="héllo\n") and "")
    step("read back (UTF-8)", lambda: _assert_equal(expect_ok("read", path=f"{REMOTE}/a.txt"), "héllo\n"))
    step("ls shows it", lambda: expect_ok("ls", f"{REMOTE}/a.txt", path=REMOTE) and "")

    sync_args = {"local_dir": "src", "remote_dir": f"{REMOTE}/sync", "reset": False}
    step("sync: uploads", lambda: expect_ok("sync", "2 uploaded", **sync_args) and "")
    step("sync: skips unchanged", lambda: expect_ok("sync", "0 uploaded", **sync_args) and "")
    os.remove(os.path.join(project, "src", "lib", "util_check.py"))
    step(
        "sync: delete dry run",
        lambda: expect_ok("sync", "1 would be deleted", delete=True, dry_run=True, **sync_args) and "",
    )
    step("sync: delete", lambda: expect_ok("sync", "1 deleted", delete=True, **sync_args) and "")

    step("pull", lambda: _pull_check(project))

    endless = (
        "import time\nprint('READY')\nn = 0\nwhile True:\n"
        "    n += 1\n    print('tick', n)\n    time.sleep(0.2)\n"
    )
    step("start an endless program", lambda: expect_ok("start", "READY", code=endless, wait=1) and "")
    step("monitor until 'tick 8'", lambda: expect_ok("monitor", "[pattern matched]", seconds=15, until=r"tick 8\b") and "")
    step("interrupt it", lambda: expect_ok("interrupt") and "")
    step("board idle again", lambda: expect_ok("exec", "idle", code="print('idle')") and "")

    os.environ["THONNY_AI_SAFE_CODE"] = "print('SAFE STOP OK')"
    step("stop runs the safe-stop code", lambda: expect_ok("stop", "safe stop code ran") and "")
    os.environ.pop("THONNY_AI_SAFE_CODE")

    if args.i2c:
        sda, scl = args.i2c
        step(f"i2c_scan GP{sda}/GP{scl}", lambda: expect_ok("i2c_scan", "I2C", sda=sda, scl=scl).splitlines()[0])
    else:
        step("i2c_scan default pins", lambda: expect_ok("i2c_scan", "I2C").splitlines()[0])
    if args.adc is not None:
        step(f"gpio adc GP{args.adc}", lambda: expect_ok("gpio", "ADC", pin=args.adc, mode="adc"))

    if not args.keep:
        step("rm the check folder", lambda: expect_ok("rm", "deleted", path=REMOTE) and "")
    step("reset", lambda: expect_ok("reset") and "")
    time.sleep(2)
    step("board answers after reset", lambda: expect_ok("exec", "back", code="print('back')") and "")

    shutil.rmtree(tmp, ignore_errors=True)
    failed = [label for label, ok in RESULTS if not ok]
    print()
    if failed:
        print(f"HARDWARE CHECK: {len(failed)} FAILED / {len(RESULTS)}: " + "; ".join(failed))
        return 1
    print(f"HARDWARE CHECK OK: {len(RESULTS)} steps on the real board")
    return 0


def _status():
    text = expect_ok("status")
    assert "connected: True" in text or "MicroPython board on USB: " in text, text
    return text.splitlines()[0]


def _assert_equal(actual, expected):
    assert actual == expected, f"got {actual!r}, expected {expected!r}"
    return ""


def _pull_check(project):
    text = expect_ok("pull", "new", remote_dir=REMOTE, local_dir="pulled")
    with open(os.path.join(project, "pulled", "a.txt"), encoding="utf-8") as f:
        _assert_equal(f.read(), "héllo\n")
    with open(os.path.join(project, "pulled", "sync", "main_check.py"), encoding="utf-8") as f:
        _assert_equal(f.read(), "print('check main')\n")
    return text.splitlines()[0]


if __name__ == "__main__":
    sys.exit(main())
