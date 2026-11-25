"""Panel, emergency stop button and project safe-stop discovery.

The Controller and safety tests are headless. The view tests need a display and are skipped
when Tk cannot open one (headless CI)."""

import json
import os
import tempfile
import time
import tkinter as tk
import unittest
from unittest import mock

from fakes import FakeHost
from thonnycontrib.thonny_ai import i18n, panel, safety
from thonnycontrib.thonny_ai.core import BridgeCore, Operation


class WorkbenchStub:
    """The few Workbench methods the panel uses."""

    def __init__(self, root=None):
        self.root = root
        self.vars = {}
        self._after_ids = []

    def get_variable(self, key):
        return self.vars.setdefault(key, tk.BooleanVar(self.root, value=False))

    def get_option(self, key, *args):
        return "en"

    def after(self, ms, fn=None):
        if not self.root:
            return None
        after_id = self.root.after(ms, fn)
        self._after_ids.append(after_id)
        return after_id

    def cancel_all(self):
        for after_id in self._after_ids:
            try:
                self.root.after_cancel(after_id)
            except tk.TclError:
                pass
        self._after_ids.clear()

    def show_view(self, *args, **kwargs):
        pass

    def bell(self):
        pass

    def become_active_window(self, *args):
        pass

    def get_editor_notebook(self):
        raise RuntimeError("no editor in tests")


class StopHost(FakeHost):
    def __init__(self):
        super().__init__()
        self.interrupted = 0
        self.executed = []
        self.exec_result = {"finished": True, "stdout": "", "stderr": "", "error": None}
        self.interrupt_error = None

    def interrupt(self):
        if self.interrupt_error:
            raise self.interrupt_error
        self.interrupted += 1

    def exec_code(self, code, timeout, echo=False, quiet=False, echo_text=None):
        self.executed.append(code)
        return self.exec_result

    def status_now(self):
        return {
            "backend": "MicroPython.RPI_PICO",
            "backend_description": "MicroPython (Raspberry Pi Pico W)",
            "port": "COM3",
            "connected": True,
            "state": "waiting_toplevel_command",
        }


def write_config(folder, **values):
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, safety.CONFIG_NAME), "w", encoding="utf-8") as f:
        json.dump(values, f)


class SafetyDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = os.path.join(self.tmp.name, "project")
        self.deep = os.path.join(self.project, "tools", "thonny")
        os.makedirs(self.deep)
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("THONNY_AI_SAFE_CODE", None)

    def test_finds_config_in_a_parent_folder(self):
        write_config(self.project, safe_stop="motors.stop()")
        code, source = safety.find_safe_stop([self.deep])
        self.assertEqual(code, "motors.stop()")
        self.assertTrue(source.endswith(safety.CONFIG_NAME))

    def test_nothing_configured(self):
        self.assertEqual(safety.find_safe_stop([self.deep]), (None, None))

    def test_env_wins(self):
        write_config(self.project, safe_stop="from_file()")
        with mock.patch.dict(os.environ, {"THONNY_AI_SAFE_CODE": "from_env()"}):
            self.assertEqual(safety.find_safe_stop([self.deep])[0], "from_env()")

    def test_safe_stop_file(self):
        with open(os.path.join(self.project, "stop.py"), "w", encoding="utf-8") as f:
            f.write("pwm.deinit()\n")
        write_config(self.project, safe_stop_file="stop.py")
        self.assertEqual(safety.find_safe_stop([self.deep])[0], "pwm.deinit()\n")

    def test_broken_file_never_blocks_a_stop(self):
        with open(os.path.join(self.deep, safety.CONFIG_NAME), "w", encoding="utf-8") as f:
            f.write("{ not json")
        write_config(self.project, safe_stop="outer()")
        self.assertEqual(safety.find_safe_stop([self.deep])[0], "outer()")

    def test_none_and_empty_start_dirs_are_ignored(self):
        self.assertEqual(safety.find_safe_stop([None, ""]), (None, None))


class EmergencyStopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.host = StopHost()
        stub = WorkbenchStub()
        for target in (panel, i18n):
            patcher = mock.patch.object(target, "get_workbench", lambda stub=stub: stub)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.ctrl = panel.Controller(BridgeCore(self.host), 47821)
        patcher = mock.patch.object(self.ctrl, "_project_dirs", lambda: [self.tmp.name])
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("THONNY_AI_SAFE_CODE", None)
        patcher = mock.patch.object(panel.time, "sleep", lambda s: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_stop(self):
        self.ctrl.emergency_stop()
        deadline = time.time() + 3
        while time.time() < deadline:
            self.ctrl._poll()
            if len(self.ctrl.entries) >= 2:
                break
            time.sleep(0.02)
        return [e.summary for e in self.ctrl.entries], [e.status for e in self.ctrl.entries]

    def test_interrupts_then_runs_safe_code(self):
        write_config(self.tmp.name, safe_stop="motors.stop()")
        summaries, statuses = self.run_stop()
        self.assertEqual(self.host.interrupted, 1)
        self.assertEqual(self.host.executed, ["motors.stop()"])
        self.assertIn("Safe-stop code ran", summaries[-1])
        self.assertEqual(statuses[-1], "done")

    def test_warns_when_nothing_is_configured(self):
        summaries, statuses = self.run_stop()
        self.assertEqual(self.host.interrupted, 1)
        self.assertEqual(self.host.executed, [])
        self.assertIn("No safe_stop configured", summaries[-1])
        self.assertEqual(statuses[-1], "denied")

    def test_safe_code_error_is_shown(self):
        write_config(self.tmp.name, safe_stop="boom()")
        self.host.exec_result = {"finished": True, "stdout": "", "stderr": "Traceback\nNameError: boom", "error": None}
        summaries, statuses = self.run_stop()
        self.assertIn("Safe-stop code failed", summaries[-1])
        self.assertIn("NameError: boom", summaries[-1])
        self.assertEqual(statuses[-1], "failed")

    def test_interrupt_failure_is_shown(self):
        self.host.interrupt_error = RuntimeError("no interpreter connected")
        summaries, statuses = self.run_stop()
        self.assertIn("Emergency stop failed", summaries[-1])
        self.assertEqual(self.host.executed, [])


def make_root():
    try:
        root = tk.Tk()
    except tk.TclError:
        raise unittest.SkipTest("no display available for Tk")
    root.withdraw()
    return root


class ViewTest(unittest.TestCase):
    def setUp(self):
        self.root = make_root()
        self.addCleanup(self.root.destroy)
        stub = WorkbenchStub(self.root)
        self.addCleanup(stub.cancel_all)  # runs before root.destroy
        for target in (panel, i18n):
            patcher = mock.patch.object(target, "get_workbench", lambda stub=stub: stub)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.host = StopHost()
        self.core = BridgeCore(self.host, require_approval=lambda: True)
        self.ctrl = panel.Controller(self.core, 47821)
        self.ctrl.bridge_running = True
        panel.ThonnyAIView.controller = self.ctrl
        self.view = panel.ThonnyAIView(self.root)
        self.view.pack(fill="both", expand=True)
        self.root.update()

    def add(self, kind, summary, status="done", result=""):
        op = Operation(f"op{len(self.ctrl.entries)}", kind, summary, "code")
        op.status = status
        op.result = result
        self.ctrl.entries.append(op)
        self.view.refresh(op)
        return op

    def rows(self):
        return [self.view._tree.item(i, "values")[2] for i in self.view._tree.get_children()]

    def test_rows_are_listed_newest_first_with_status_tags(self):
        self.add("exec", "first")
        self.add("exec", "second", "failed")
        self.assertEqual(self.rows(), ["second", "first"])
        top = self.view._tree.get_children()[0]
        self.assertEqual(self.view._tree.item(top, "tags"), ("failed",))

    def test_stop_rows_get_the_stop_tag(self):
        self.add("stop", "EMERGENCY STOP")
        top = self.view._tree.get_children()[0]
        self.assertEqual(self.view._tree.item(top, "tags"), ("stop",))

    def test_text_filter_and_errors_only(self):
        self.add("exec", "led.on()")
        self.add("write", "/main.py", "failed")
        self.add("rm", "/old.py", "denied")
        self.view._filter.set("main")
        self.assertEqual(self.rows(), ["/main.py"])
        self.view._filter.set("")
        self.view._errors_only.set(True)
        self.assertEqual(sorted(self.rows()), ["/main.py", "/old.py"])
        self.view._errors_only.set(False)
        self.assertEqual(len(self.rows()), 3)

    def test_clear_keeps_pending_requests(self):
        self.add("exec", "done one")
        pending = self.add("write", "/main.py", "pending")
        self.ctrl.pending.append(pending)
        self.view._clear()
        self.assertEqual(self.rows(), ["/main.py"])

    def test_summary_counts(self):
        self.add("exec", "a")
        self.add("exec", "b", "failed")
        self.assertIn("2 actions · 1 failed · 0 waiting", self.view._summary.cget("text"))

    def test_approval_card_warns_about_writes_only(self):
        op = self.add("write", "/main.py", "pending")
        self.ctrl.pending.append(op)
        self.view.refresh(op)
        self.assertTrue(self.view._approval.winfo_manager())
        self.assertIn("changes the board", self.view._approval_risk.cget("text"))
        read = self.add("i2c_scan", "I2C0", "pending")
        self.ctrl.pending[:] = [read]
        self.view.refresh(read)
        self.assertIn("only reads", self.view._approval_risk.cget("text"))
        self.ctrl.pending.clear()
        self.view.refresh(read)
        self.assertFalse(self.view._approval.winfo_manager())

    def test_board_line_shows_live_state(self):
        self.view._poll_board()
        text = self.view._board_label.cget("text")
        self.assertIn("COM3", text)
        self.assertIn("connected", text)
        self.assertIn("ready", text)

    def test_board_line_survives_a_failing_host(self):
        with mock.patch.object(self.host, "status_now", side_effect=RuntimeError("boom")):
            self.view._poll_board()
        self.assertIn("No interpreter", self.view._board_label.cget("text"))

    def test_details_window_shows_code_and_result(self):
        op = self.add("exec", "x = 1", "done", result="42")
        window = panel.DetailsWindow(self.view, op)
        self.root.update()
        pages = [t.get("1.0", "end-1c") for t in window._texts]
        self.assertEqual(pages, ["code", "42"])
        window.destroy()

    def test_destroying_the_view_detaches_it(self):
        self.view.destroy()
        self.root.update()
        self.assertIsNone(self.ctrl.view)


if __name__ == "__main__":
    unittest.main()
