"""End-to-end check against a real Thonny window, no board needed (uses Thonny's local
Python interpreter). Opens Thonny with a throwaway settings folder, drives it through the
MCP tools, then closes it.

    python tests/e2e_thonny.py "C:/path/to/thonny.exe"
"""

import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from thonny_ai_mcp import backends, server  # noqa: E402


def call(name, **arguments):
    result = server.handle({"method": "tools/call", "params": {"name": name, "arguments": arguments}})
    text = result["content"][0]["text"]
    if result["isError"]:
        raise AssertionError(f"{name} failed: {text}")
    return text


def main(thonny_exe):
    tmp = tempfile.mkdtemp(prefix="thonny-ai-e2e-")
    user_dir = os.path.join(tmp, "user")
    project = os.path.join(tmp, "project")
    remote = os.path.join(tmp, "remote").replace("\\", "/")
    os.makedirs(user_dir)
    os.makedirs(os.path.join(project, "src", "lib"))
    os.makedirs(remote)
    with open(os.path.join(user_dir, "configuration.ini"), "w") as f:
        f.write("[run]\nbackend_name = LocalCPython\n\n[general]\nlanguage = fr_FR\n")
    for rel, content in {"src/main.py": "print('main')\n", "src/lib/util.py": "X = 1\n"}.items():
        with open(os.path.join(project, rel), "w", newline="\n") as f:
            f.write(content)

    env = {**os.environ, "THONNY_USER_DIR": user_dir}
    os.environ["THONNY_AI_PROJECT"] = project
    thonny = subprocess.Popen([thonny_exe], env=env)
    try:
        deadline = time.time() + 60
        while not (backends.bridge_status() or {}).get("connected"):
            if time.time() > deadline:
                raise AssertionError("Thonny bridge did not come up")
            time.sleep(0.5)
        while backends.bridge_status()["state"] != backends.READY_STATE:
            time.sleep(0.3)

        steps = []

        def step(label, fn):
            fn()
            steps.append(label)
            print(f"  ok  {label}")

        step("status", lambda: _assert_in("Thonny is open", call("status")))
        step("exec + output", lambda: _assert_in("42", call("exec", code="print(6*7)")))
        step("code shown in Thonny's shell", lambda: _assert_in("print(6*7)", call("shell")))
        step(
            "multi-line exec",
            lambda: _assert_in("0\n1\n2", call("exec", code="for i in range(3):\n    print(i)")),
        )
        step("error reported", lambda: _expect_error("exec", "ZeroDivisionError", code="1/0"))
        step(
            "timeout interrupts",
            lambda: _expect_error("exec", "interrupted", code="while True: pass", timeout=2),
        )
        step("still usable after interrupt", lambda: _assert_in("ok", call("exec", code="print('ok')")))
        target = f"{remote}/sub/notes.txt"
        step("write (creates folders)", lambda: call("write", path=target, content="88.88 é✓\n"))
        step("read back", lambda: _assert_eq(call("read", path=target), "88.88 é✓\n"))
        step("sync uploads", lambda: _assert_in("2 uploaded", call("sync", remote_dir=remote, reset=False)))
        step("sync skips unchanged", lambda: _assert_in("0 uploaded, 2 unchanged", call("sync", remote_dir=remote)))
        step("rm", lambda: call("rm", path=f"{remote}/sub"))
        step("run_file", lambda: _assert_in("main", call("run_file", path="src/main.py")))
        step("info", lambda: _assert_in("firmware:", call("info")))
        loop = "import time\nfor i in range(4):\n    print('tick', i, flush=True)\n    time.sleep(0.5)"
        collected = []

        def start_then_monitor():
            collected.append(call("start", code=loop, wait=0.3))
            collected.append(call("monitor", seconds=6))
            joined = "".join(collected)
            for i in range(4):
                _assert_in(f"tick {i}", joined)
            _assert_in("[program ended]", collected[-1])

        step("start + monitor a finite program", start_then_monitor)
        forever = "import time\nwhile True:\n    print('sensor', time.time(), flush=True)\n    time.sleep(0.3)"
        step("start an endless program", lambda: _assert_in("[still running]", call("start", code=forever)))
        step("monitor it", lambda: _assert_in("sensor", call("monitor", seconds=1)))
        step("interrupt it", lambda: _assert_in("stopped", call("interrupt")))
        step("idle again", lambda: _assert_in("ok", call("exec", code="print('ok')")))
        step("open in editor", lambda: call("open", path="src/main.py"))
        step(
            "editor file reported",
            lambda: _assert_in("main.py", backends.bridge_status()["current_editor_file"] or ""),
        )
        print(f"\nE2E OK: {len(steps)} steps against a real Thonny")
    finally:
        thonny.terminate()
        try:
            thonny.wait(10)
        except subprocess.TimeoutExpired:
            thonny.kill()


def _assert_in(needle, haystack):
    assert needle in haystack, f"{needle!r} not in {haystack!r}"


def _assert_eq(a, b):
    assert a == b, f"{a!r} != {b!r}"


def _expect_error(tool, needle, **arguments):
    result = server.handle({"method": "tools/call", "params": {"name": tool, "arguments": arguments}})
    assert result["isError"], f"{tool} should have failed"
    _assert_in(needle, result["content"][0]["text"])


if __name__ == "__main__":
    main(sys.argv[1])
