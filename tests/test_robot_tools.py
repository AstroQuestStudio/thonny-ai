"""Tools added in 0.2.0: safe stop, project config, sync ignore/delete, pull, monitor `until`,
i2c_scan, gpio, output cap and MCP annotations."""

import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

from fakes import FakeBackend, FakeBoard
from thonny_ai_mcp import config, server, tools


def call(name, **arguments):
    return server.handle({"method": "tools/call", "params": {"name": name, "arguments": arguments}})


def text(result):
    return result["content"][0]["text"]


class FakeI2C:
    devices = [0x3C, 0x68]

    def __init__(self, bus, sda=None, scl=None, freq=400000):
        self.args = (bus, sda, scl, freq)

    def scan(self):
        return list(FakeI2C.devices)


class FakePin:
    IN, OUT, PULL_UP, PULL_DOWN = 0, 1, 2, 3
    levels = {}

    def __init__(self, n, mode=None, pull=None):
        self.n = n
        if mode == FakePin.OUT:
            FakePin.levels.setdefault(n, 0)

    def value(self, v=None):
        if v is None:
            return FakePin.levels.get(self.n, 1)
        FakePin.levels[self.n] = v


class FakeADC:
    def __init__(self, pin):
        self.pin = pin

    def read_u16(self):
        return 32768


class TimeoutBackend(FakeBackend):
    """The first exec (the user's endless loop) times out; later ones (safe stop) finish."""

    def exec(self, code, timeout=10.0, echo=False, quiet=True, echo_text=None):
        if "while True" in code:
            self.calls.append(("exec-timeout",))
            return "partial\n", "", False
        return super().exec(code, timeout, echo, quiet, echo_text)


class StreamBackend(FakeBackend):
    """monitor() hands out one chunk per call and the program keeps running."""

    def __init__(self, board, chunks):
        super().__init__(board)
        self.chunks = list(chunks)
        self.slices = []

    def monitor(self, seconds):
        self.slices.append(seconds)
        return (self.chunks.pop(0) if self.chunks else ""), "", True


class RobotToolsBase(unittest.TestCase):
    backend_class = FakeBackend

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.board_root = os.path.join(self.tmp.name, "board")
        self.project = os.path.join(self.tmp.name, "project")
        os.makedirs(self.board_root)
        os.makedirs(os.path.join(self.project, "src", "lib"))
        self.write_local("src/main.py", "print('go')\n")
        self.write_local("src/lib/motors.py", "SPEED = 1\n")
        self.write_local("src/notes.md", "# notes\n")
        self.board = FakeBoard(self.board_root)
        self.backend = self.make_backend()
        machine = types.ModuleType("machine")
        machine.I2C, machine.Pin, machine.ADC = FakeI2C, FakePin, FakeADC
        FakePin.levels = {}
        env = {"THONNY_AI_PROJECT": self.project}
        patches = [
            mock.patch.object(tools, "open_backend", lambda op, passive=False: self.backend),
            mock.patch.dict(os.environ, env),
            mock.patch.dict(sys.modules, {"machine": machine}),
            mock.patch.object(tools.time, "sleep", lambda s: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("THONNY_AI_SAFE_CODE", None)
        os.environ.pop("THONNY_AI_MAX_OUTPUT", None)

    def make_backend(self):
        return self.backend_class(self.board)

    def tearDown(self):
        self.tmp.cleanup()

    def write_local(self, rel, content):
        full = os.path.join(self.project, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)

    def set_config(self, **values):
        self.write_local(config.CONFIG_NAME, json.dumps(values))


class OutputCapTest(RobotToolsBase):
    def test_short_text_is_untouched(self):
        self.assertEqual(tools._cap("abc", 100), "abc")

    def test_long_text_keeps_head_and_tail(self):
        capped = tools._cap("A" * 5000 + "Z" * 5000, 1000)
        self.assertTrue(capped.startswith("A" * 300))
        self.assertTrue(capped.endswith("Z" * 600))
        self.assertIn("characters omitted", capped)
        self.assertLess(len(capped), 1100)

    def test_exec_output_is_capped_by_config(self):
        self.set_config(max_output=500)
        result = call("exec", code="print('x' * 5000)")
        self.assertFalse(result["isError"])
        self.assertIn("characters omitted", text(result))
        self.assertLess(len(text(result)), 800)

    def test_env_overrides_config(self):
        self.set_config(max_output=500)
        with mock.patch.dict(os.environ, {"THONNY_AI_MAX_OUTPUT": "3000"}):
            self.assertEqual(config.max_output(), 3000)


class ConfigTest(RobotToolsBase):
    def test_no_file_means_defaults(self):
        self.assertEqual(config.load(), {})
        self.assertIsNone(config.safe_stop_code())

    def test_broken_json_is_a_clear_error(self):
        self.write_local(config.CONFIG_NAME, "{ nope")
        with self.assertRaises(config.ConfigError):
            config.load()

    def test_safe_stop_from_file_and_env(self):
        self.write_local("safe.py", "x = 1\n")
        self.set_config(safe_stop_file="safe.py")
        self.assertEqual(config.safe_stop_code(), "x = 1\n")
        with mock.patch.dict(os.environ, {"THONNY_AI_SAFE_CODE": "y = 2"}):
            self.assertEqual(config.safe_stop_code(), "y = 2")

    def test_sync_uses_config_defaults(self):
        self.write_local("firmware/main.py", "print('fw')\n")
        self.set_config(local_dir="firmware")
        self.assertIn("1 uploaded", text(call("sync", reset=False)))
        self.assertEqual(self.board.read("/main.py"), b"print('fw')\n")


class SafeStopTest(RobotToolsBase):
    def test_stop_runs_the_safe_code(self):
        self.set_config(safe_stop="MOTORS_OFF = True")
        result = call("stop")
        self.assertFalse(result["isError"])
        self.assertIn("safe stop code ran", text(result))
        self.assertTrue(self.board.namespace["MOTORS_OFF"])
        self.assertNotIn("WARNING", text(result))

    def test_stop_warns_without_safe_code(self):
        result = call("stop")
        self.assertIn("WARNING", text(result))
        self.assertIn("safe_stop", text(result))

    def test_interrupt_also_runs_the_safe_code(self):
        self.set_config(safe_stop="MOTORS_OFF = True")
        self.assertIn("safe stop code ran", text(call("interrupt")))
        self.assertTrue(self.board.namespace["MOTORS_OFF"])

    def test_safe_code_error_is_reported_not_raised(self):
        self.set_config(safe_stop="1/0")
        result = call("stop")
        self.assertFalse(result["isError"])
        self.assertIn("safe stop raised an error", text(result))
        self.assertIn("ZeroDivisionError", text(result))

    def test_broken_config_does_not_hide_the_stop(self):
        self.write_local(config.CONFIG_NAME, "{ nope")
        result = call("stop")
        self.assertFalse(result["isError"])
        self.assertIn("safe stop NOT run", text(result))

    def test_env_var_safe_code(self):
        with mock.patch.dict(os.environ, {"THONNY_AI_SAFE_CODE": "FROM_ENV = 1"}):
            self.assertIn("safe stop code ran", text(call("stop")))
        self.assertEqual(self.board.namespace["FROM_ENV"], 1)


class ExecTimeoutSafeStopTest(RobotToolsBase):
    backend_class = TimeoutBackend

    def test_timeout_triggers_safe_stop(self):
        self.set_config(safe_stop="MOTORS_OFF = True")
        result = call("exec", code="while True: pass", timeout=1)
        self.assertTrue(result["isError"])
        self.assertIn("interrupted after", text(result))
        self.assertIn("safe stop code ran", text(result))
        self.assertTrue(self.board.namespace["MOTORS_OFF"])

    def test_timeout_without_safe_code_just_reports(self):
        result = call("exec", code="while True: pass", timeout=1)
        self.assertTrue(result["isError"])
        self.assertNotIn("safe stop", text(result))


class SyncOptionsTest(RobotToolsBase):
    def test_ignore_argument(self):
        out = text(call("sync", ignore=["*.md"], reset=False))
        self.assertIn("2 uploaded", out)
        self.assertFalse(os.path.exists(os.path.join(self.board_root, "notes.md")))

    def test_ignore_from_config_and_folder_prefix(self):
        self.set_config(ignore=["lib"])
        out = text(call("sync", dry_run=True))
        self.assertIn("/main.py", out)
        self.assertNotIn("motors.py", out)

    def test_delete_removes_orphans_after_dry_run(self):
        call("sync", reset=False)
        os.remove(os.path.join(self.project, "src", "notes.md"))
        dry = text(call("sync", delete=True, dry_run=True))
        self.assertIn("1 would be deleted", dry)
        self.assertIn("DELETE /notes.md", dry)
        self.assertTrue(os.path.exists(os.path.join(self.board_root, "notes.md")))
        done = text(call("sync", delete=True))
        self.assertIn("1 deleted", done)
        self.assertFalse(os.path.exists(os.path.join(self.board_root, "notes.md")))
        self.assertIn(("reset",), self.backend.calls)

    def test_delete_spares_ignored_files(self):
        self.board.write("/secrets.py", b"WIFI = 'x'\n")
        out = text(call("sync", delete=True, ignore=["secrets.py"], dry_run=True))
        self.assertNotIn("secrets.py", out)

    def test_without_delete_orphans_stay(self):
        self.board.write("/old.py", b"")
        call("sync", reset=False)
        self.assertTrue(os.path.exists(os.path.join(self.board_root, "old.py")))


class PullTest(RobotToolsBase):
    def setUp(self):
        super().setUp()
        call("sync", reset=False)

    def test_pull_new_files(self):
        out = text(call("pull", local_dir="backup"))
        self.assertIn("3 new", out)
        with open(os.path.join(self.project, "backup", "lib", "motors.py"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "SPEED = 1\n")

    def test_pull_keeps_different_local_files_by_default(self):
        self.write_local("backup/main.py", "print('my edit')\n")
        out = text(call("pull", local_dir="backup"))
        self.assertIn("1 different (kept)", out)
        with open(os.path.join(self.project, "backup", "main.py"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "print('my edit')\n")

    def test_pull_overwrite(self):
        self.write_local("backup/main.py", "print('my edit')\n")
        out = text(call("pull", local_dir="backup", overwrite=True))
        self.assertIn("overwritten", out)
        with open(os.path.join(self.project, "backup", "main.py"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "print('go')\n")

    def test_pull_dry_run_writes_nothing(self):
        out = text(call("pull", local_dir="backup", dry_run=True))
        self.assertIn("Dry run", out)
        self.assertFalse(os.path.exists(os.path.join(self.project, "backup")))

    def test_pull_identical_files_are_counted(self):
        call("pull", local_dir="backup")
        self.assertIn("3 identical", text(call("pull", local_dir="backup")))

    def test_pull_refuses_paths_escaping_the_folder(self):
        with mock.patch.object(tools, "_remote_files", lambda b, root: [("/../evil.py", 3)]):
            result = call("pull", local_dir="backup")
        self.assertTrue(result["isError"])
        self.assertIn("escapes", text(result))

    def test_pull_empty_board(self):
        out = text(call("pull", remote_dir="/nothing-here"))
        self.assertIn("No file found", out)


class MonitorUntilTest(RobotToolsBase):
    def make_backend(self):
        return StreamBackend(self.board, ["booting\n", "calibrating\n", "READY\n", "later\n"])

    def test_returns_as_soon_as_the_pattern_appears(self):
        out = text(call("monitor", seconds=30, until="READY"))
        self.assertIn("READY", out)
        self.assertNotIn("later", out)
        self.assertIn("[pattern matched]", out)
        self.assertEqual(len(self.backend.slices), 3)

    def test_reports_when_the_pattern_never_shows(self):
        with mock.patch.object(tools.time, "time", side_effect=[0, 0, 0.5, 0.6, 5, 5, 99, 99]):
            out = text(call("monitor", seconds=2, until="NEVER"))
        self.assertIn("not seen", out)

    def test_invalid_regex_is_an_error(self):
        result = call("monitor", until="(")
        self.assertTrue(result["isError"])
        self.assertIn("regular expression", text(result))

    def test_without_until_nothing_changes(self):
        out = text(call("monitor", seconds=2))
        self.assertNotIn("pattern", out)
        self.assertEqual(len(self.backend.slices), 1)


class HardwareToolsTest(RobotToolsBase):
    def test_i2c_scan_names_known_devices(self):
        out = text(call("i2c_scan", sda=4, scl=5))
        self.assertIn("0x3C", out)
        self.assertIn("SSD1306", out)
        self.assertIn("0x68", out)
        self.assertIn("IMU", out)

    def test_i2c_scan_default_pins_and_unknown_device(self):
        FakeI2C.devices = [0x10]
        self.addCleanup(setattr, FakeI2C, "devices", [0x3C, 0x68])
        out = text(call("i2c_scan"))
        self.assertIn("default pins", out)
        self.assertIn("unknown device", out)

    def test_i2c_scan_nothing_found_gives_advice(self):
        FakeI2C.devices = []
        self.addCleanup(setattr, FakeI2C, "devices", [0x3C, 0x68])
        self.assertIn("no device answered", text(call("i2c_scan", sda=4, scl=5)))

    def test_i2c_scan_needs_both_pins(self):
        result = call("i2c_scan", sda=4)
        self.assertTrue(result["isError"])
        self.assertIn("both", text(result))

    def test_gpio_read_write_adc(self):
        self.assertIn("= 0", text(call("gpio", pin=15, mode="out", value=0)))
        self.assertIn("= 1", text(call("gpio", pin=15, mode="out", value=1)))
        self.assertIn("GP27 (in_pullup) = 1", text(call("gpio", pin=27, mode="in_pullup")))
        self.assertIn("ADC = 32768", text(call("gpio", pin=26, mode="adc")))

    def test_gpio_validation(self):
        self.assertTrue(call("gpio", mode="in")["isError"])
        self.assertTrue(call("gpio", pin=99)["isError"])
        self.assertTrue(call("gpio", pin=3, mode="out")["isError"])
        self.assertTrue(call("gpio", pin=3, mode="in", value=1)["isError"])
        self.assertTrue(call("gpio", pin=3, mode="flying")["isError"])


class AnnotationsTest(unittest.TestCase):
    def test_every_tool_is_annotated(self):
        listed = server.handle({"method": "tools/list"})["tools"]
        self.assertGreaterEqual(len(listed), 19)
        for tool in listed:
            self.assertIn(tool["name"], tools.ANNOTATIONS, tool["name"])
            self.assertTrue(tool["title"])
            self.assertIn("readOnlyHint", tool["annotations"])

    def test_read_only_tools_cannot_be_destructive(self):
        for name in ("status", "monitor", "ls", "read", "shell"):
            self.assertTrue(tools.annotations_for(name)["readOnlyHint"])
            self.assertNotIn("destructiveHint", tools.annotations_for(name))
        for name in ("rm", "write", "sync", "exec"):
            self.assertTrue(tools.annotations_for(name)["destructiveHint"])

    def test_new_tools_are_listed(self):
        names = {t["name"] for t in server.handle({"method": "tools/list"})["tools"]}
        self.assertTrue({"stop", "pull", "i2c_scan", "gpio"} <= names)


if __name__ == "__main__":
    unittest.main()
