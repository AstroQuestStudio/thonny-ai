"""Watchdog coverage with a fake clock, runner and board, plus the local HTTP bridge."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from thonny.common import InlineCommand, ToplevelCommand

from test_core import request
from test_panel import StopHost, WorkbenchStub
from thonny_ai_mcp import backends, server, tools
from thonnycontrib.thonny_ai import host as host_module, panel
from thonnycontrib.thonny_ai.core import BridgeCore, BridgeError, start_server
from thonnycontrib.thonny_ai.watchdog import Watchdog


class WatchdogTest(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.trigger = mock.Mock()
        self.watchdog = Watchdog(self.trigger, clock=lambda: self.now)

    def test_fragmented_error_trips_once_per_run(self):
        self.watchdog.begin()
        self.watchdog.output("stderr", "Trace")
        self.watchdog.output("stdout", "back")
        self.trigger.assert_not_called()
        self.watchdog.output("stderr", "back (most recent call last):\n")
        self.watchdog.output("stderr", "MemoryError")
        self.trigger.assert_called_once()
        self.watchdog.end()
        self.watchdog.output("stderr", "MemoryError")
        self.trigger.assert_called_once()
        self.watchdog.begin()
        self.watchdog.output("stderr", "OSError: [Errno 5]")
        self.assertEqual(self.trigger.call_count, 2)

    def test_heartbeat_times_out_and_old_output_does_not_reset_timer(self):
        self.watchdog.load_project({"heartbeat": r"\bHEARTBEAT\b", "heartbeat_timeout": 3})
        self.watchdog.begin()
        self.now = 2.0
        self.watchdog.output("stdout", "HEART")
        self.watchdog.output("stdout", "BEAT\n")
        self.now = 4.9
        self.watchdog.output("stdout", "sensor output\n")
        self.watchdog.tick()
        self.trigger.assert_not_called()
        self.now = 5.0
        self.watchdog.tick()
        self.watchdog.tick()
        self.trigger.assert_called_once_with({"reason": "heartbeat", "timestamp": mock.ANY, "timeout": 3})

    def test_heartbeat_expires_without_any_output(self):
        self.watchdog.load_project({"heartbeat": "ALIVE"})
        self.watchdog.begin()
        self.now = 5.0
        self.watchdog.tick()
        self.trigger.assert_called_once()

    def test_disabled_ignores_all_triggers(self):
        self.watchdog.load_project({"enabled": False, "heartbeat": "ALIVE"})
        self.watchdog.begin()
        self.watchdog.output("stderr", "Traceback")
        self.now = 100
        self.watchdog.tick()
        self.watchdog.disconnected()
        self.trigger.assert_not_called()

    def test_invalid_changes_leave_effective_settings_intact(self):
        self.watchdog.load_project({"patterns": ["MOTOR FAULT"]})
        for invalid in ({"patterns": ["["]}, {"heartbeat_timeout": float("inf")}, {"enabled": 1}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.watchdog.request("configure", {"heartbeat": "ALIVE", **invalid})
        status = self.watchdog.request("status", {})
        self.assertEqual(status["patterns"], ["MOTOR FAULT"])
        self.assertIsNone(status["heartbeat"])

    def test_runtime_overrides_and_reconfiguration_preserve_trip_latch(self):
        self.watchdog.begin()
        self.watchdog.output("stderr", "MemoryError")
        self.watchdog.request("off", {})
        self.watchdog.load_project({"enabled": True, "patterns": ["MOTOR FAULT"]})
        self.assertFalse(self.watchdog.enabled)
        self.watchdog.request("on", {})
        self.watchdog.output("stderr", "MOTOR FAULT")
        self.trigger.assert_called_once()


class HostWatchdogTest(unittest.TestCase):
    def setUp(self):
        workbench = mock.Mock()
        self.runner = mock.Mock()
        self.runner.is_running.return_value = True
        self.runner.get_backend_proxy.return_value.is_connected.return_value = True
        with mock.patch.object(host_module, "get_workbench", return_value=workbench):
            self.host = host_module.ThonnyHost()
        self.bindings = {call.args[0]: call.args[1] for call in workbench.bind.call_args_list}
        for name, value in (("get_workbench", workbench), ("get_runner", self.runner)):
            patcher = mock.patch.object(host_module, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dirs = [self.tmp.name]
        patcher = mock.patch.object(host_module, "project_dirs", return_value=self.dirs)
        patcher.start()
        self.addCleanup(patcher.stop)
        with mock.patch.object(panel, "get_workbench", return_value=WorkbenchStub()):
            self.controller = panel.Controller(BridgeCore(self.host), 0)
        self.controller.emergency_stop = mock.Mock()
        self.host.on_watchdog_trigger = self.controller.watchdog_triggered

    def accept(self, command):
        self.bindings["CommandAccepted"](SimpleNamespace(command=command))

    def output(self, data):
        self.bindings["ProgramOutput"](SimpleNamespace(data=data, stream_name="stderr"))

    def test_editor_run_pattern_calls_emergency_stop_once_with_start_dirs(self):
        self.accept(ToplevelCommand("Run", id="editor-run"))
        with mock.patch.object(host_module, "project_dirs", return_value=["other-project"]):
            self.output("MemoryError")
            self.output("Traceback")
        self.controller.emergency_stop.assert_called_once_with(
            self.dirs, expected_proxy=self.runner.get_backend_proxy.return_value
        )
        _, entry = self.controller._events.get_nowait()
        self.assertEqual(entry.status, "failed")
        self.assertIn("MemoryError", entry.summary)
        self.bindings["ToplevelResponse"]({"command_id": "editor-run"})
        self.assertFalse(self.host.watchdog.running)

    def test_disconnect_logs_without_attempting_stop(self):
        self.accept(ToplevelCommand("execute_source", id="mcp-run"))
        self.runner.get_backend_proxy.return_value.is_connected.return_value = False
        self.host._watchdog_tick()
        self.bindings["BackendTerminated"]()
        self.controller.emergency_stop.assert_not_called()
        _, entry = self.controller._events.get_nowait()
        self.assertIn("board disconnected", entry.summary)
        self.assertTrue(self.controller._events.empty())
        status = self.host.watchdog_request_now("status", {})
        self.assertEqual(status["triggers"][0]["reason"], "disconnect")
        self.assertFalse(status["running"])

    def test_inline_and_emergency_stop_commands_do_not_arm_watchdog(self):
        self.accept(InlineCommand("get_globals", id="inline"))
        self.host._exec = {"id": "safe", "watchdog": False, "out": [], "quiet": False}
        self.accept(ToplevelCommand("execute_source", id="safe"))
        self.output("Traceback")
        self.controller.emergency_stop.assert_not_called()

    def test_project_configuration_loaded_from_parent_on_command(self):
        root = Path(self.tmp.name)
        (root / "src").mkdir()
        (root / ".thonny-ai.json").write_text('{"watchdog": {"patterns": ["OVERCURRENT"]}}', encoding="utf-8")
        with mock.patch.object(host_module, "project_dirs", return_value=[str(root / "src")]):
            self.accept(ToplevelCommand("Run", id="project-run"))
            self.output("MemoryError")
            self.controller.emergency_stop.assert_not_called()
            self.output("OVERCURRENT")
        self.controller.emergency_stop.assert_called_once_with(
            [str(root / "src")], expected_proxy=self.runner.get_backend_proxy.return_value
        )

    def test_queued_watchdog_interrupt_cannot_target_a_replacement_connection(self):
        old_proxy = self.runner.get_backend_proxy.return_value
        self.runner.get_backend_proxy.return_value = mock.Mock()
        with mock.patch.object(self.host.main, "call", side_effect=lambda fn: fn()):
            with self.assertRaisesRegex(BridgeError, "connection changed"):
                self.host.interrupt(expected_proxy=old_proxy)
        self.runner.get_backend_proxy.return_value.interrupt.assert_not_called()


class BridgeWatchdogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.host = StopHost()
        cls.state = Watchdog(lambda event: None)
        cls.host.watchdog_request = lambda action, settings: cls.state.request(action, settings)
        cls.core = BridgeCore(cls.host, require_approval=lambda: cls.approval, approval_timeout=0)
        bridge = start_server(cls.core, port=0)
        cls.addClassCleanup(bridge.server_close)
        cls.addClassCleanup(bridge.shutdown)
        cls.port = bridge.server_address[1]

    def setUp(self):
        type(self).approval = False
        type(self).state = Watchdog(lambda event: None)

    @staticmethod
    def call_tool(**args):
        return server.handle({"method": "tools/call", "params": {"name": "watchdog", "arguments": args}})

    def test_http_status_skips_approval_and_writes_cannot_spoof_read_only(self):
        type(self).approval = True
        status, body = request(self.port, "/watchdog", {"action": "status"})
        self.assertEqual((status, body["enabled"]), (200, True))
        prior_read = self.core.operations()[-1]
        status, _ = request(
            self.port, "/watchdog", {"action": "off", "op": {"kind": "stop", "id": prior_read.id}}
        )
        self.assertEqual(status, 409)
        self.assertTrue(self.state.enabled)
        self.assertEqual(self.core.operations()[-1].kind, "watchdog")

    def test_mcp_configure_status_on_and_off_through_http(self):
        disconnected = {**self.host.status(), "connected": False}
        with (
            mock.patch.object(backends, "BRIDGE_URL", f"http://127.0.0.1:{self.port}"),
            mock.patch.object(self.host, "status", return_value=disconnected),
        ):
            configured = self.call_tool(action="configure", patterns=["FAULT"], heartbeat="ALIVE")
            self.assertFalse(configured["isError"])
            self.assertFalse(self.call_tool(action="off")["isError"])
            self.assertFalse(json.loads(self.call_tool()["content"][0]["text"])["enabled"])
            self.assertFalse(self.call_tool(action="on")["isError"])
            self.state.begin()
            self.state.output("stdout", "FAULT")
            status = json.loads(self.call_tool(action="status")["content"][0]["text"])
        self.assertTrue(status["enabled"])
        self.assertEqual(status["heartbeat"], "ALIVE")
        self.assertEqual(status["triggers"][0]["pattern"], "FAULT")

    def test_tool_with_thonny_closed_does_not_open_serial(self):
        with (
            mock.patch.object(backends, "_bridge_listening", return_value=False),
            mock.patch.object(tools, "open_backend", side_effect=AssertionError("serial fallback")),
            mock.patch.object(tools, "bridge_request") as bridge_request,
        ):
            result = self.call_tool(action="on")
        bridge_request.assert_not_called()
        self.assertTrue(result["isError"])
        self.assertIn("only available while Thonny is open", result["content"][0]["text"])
