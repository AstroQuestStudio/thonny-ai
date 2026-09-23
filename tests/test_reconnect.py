import types
import unittest
from unittest import mock

from thonnycontrib.thonny_ai import reconnect


def port(device, vid=0x2E8A):
    return types.SimpleNamespace(device=device, vid=vid, pid=5, description="", interface=None)


class PicoProxy:
    @classmethod
    def _is_potential_port(cls, p):
        return p.vid == 0x2E8A


class FakeWorkbench:
    def __init__(self):
        self.options = {
            "run.backend_name": "RPiPico",
            "RPiPico.port": "auto",
            "thonny_ai.auto_reconnect": True,
        }
        self.handlers = {}

    def get_option(self, name, default=None):
        return self.options.get(name, default)

    def get_backends(self):
        return {"RPiPico": types.SimpleNamespace(proxy_class=PicoProxy)}

    def bind(self, event, handler, add=None):
        self.handlers[event] = handler

    def after(self, ms, fn):
        pass


class FakeRunner:
    def __init__(self):
        self.proxy = object()
        self.state = "waiting_toplevel_command"
        self.restarts = 0

    def get_backend_proxy(self):
        return self.proxy

    def is_waiting_toplevel_command(self):
        return self.state == "waiting_toplevel_command"

    def restart_backend(self, clean, first=False, automatic=False):
        self.restarts += 1


class ReconnectTest(unittest.TestCase):
    def setUp(self):
        self.wb = FakeWorkbench()
        self.runner = FakeRunner()
        self.events = []
        patches = [
            mock.patch.object(reconnect, "get_workbench", lambda: self.wb),
            mock.patch.object(reconnect, "get_runner", lambda: self.runner),
            mock.patch.object(reconnect.threading, "Thread", mock.MagicMock()),
            mock.patch.object(reconnect, "t", lambda key, **kw: f"{key}:{kw.get('port')}"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.clock = [1000.0]
        clock = mock.patch.object(reconnect.time, "time", lambda: self.clock[0])
        clock.start()
        self.addCleanup(clock.stop)
        self.watcher = reconnect.ReconnectWatcher(on_event=self.events.append)

    def tick(self, ports, advance=5.0):
        self.clock[0] += advance
        self.watcher._ports = ports
        self.watcher._check()

    def lose_connection(self):
        self.wb.handlers["ProgramOutput"](types.SimpleNamespace(data="\nConnection lost -- EOF\n"))
        self.runner.proxy = None

    def test_replug_reconnects_once(self):
        self.tick([port("COM3")])
        self.lose_connection()
        self.tick([])  # unplugged
        self.assertEqual(self.runner.restarts, 0)
        self.tick([port("COM3")])  # plugged back
        self.assertEqual(self.runner.restarts, 1)
        self.assertEqual(self.events, ["reconnecting:COM3"])
        self.runner.proxy = object()
        self.tick([port("COM3")])
        self.assertEqual(self.runner.restarts, 1)

    def test_glitch_while_present_retries_with_limit(self):
        self.tick([port("COM3")])
        self.lose_connection()
        for _ in range(10):
            self.tick([port("COM3")])
        self.assertEqual(self.runner.restarts, reconnect.MAX_RETRIES_WHILE_PRESENT)

    def test_retry_delay(self):
        self.tick([port("COM3")])
        self.lose_connection()
        self.tick([port("COM3")], advance=5)
        self.tick([port("COM3")], advance=0.5)
        self.assertEqual(self.runner.restarts, 1)

    def test_board_plugged_after_startup(self):
        self.runner.proxy = None  # Thonny started without a board
        self.tick([])
        self.tick([port("COM7")])
        self.assertEqual(self.runner.restarts, 1)

    def test_deliberate_disconnect_is_respected(self):
        self.tick([port("COM3")])
        self.runner.proxy = None  # Disconnect menu: no "Connection lost", port still there
        for _ in range(5):
            self.tick([port("COM3")])
        self.assertEqual(self.runner.restarts, 0)

    def test_ignores_other_usb_serial_devices(self):
        self.runner.proxy = None
        self.tick([])
        self.tick([port("COM9", vid=0x1234)])
        self.assertEqual(self.runner.restarts, 0)

    def test_explicit_port_setting(self):
        self.wb.options["RPiPico.port"] = "COM5"
        self.runner.proxy = None
        self.tick([])
        self.tick([port("COM3")])
        self.assertEqual(self.runner.restarts, 0)
        self.tick([port("COM3"), port("COM5")])
        self.assertEqual(self.runner.restarts, 1)

    def test_local_interpreter_never_reconnects(self):
        self.wb.options["run.backend_name"] = "LocalCPython"
        self.runner.proxy = None
        self.tick([])
        self.tick([port("COM3")])
        self.assertEqual(self.runner.restarts, 0)

    def test_can_be_disabled(self):
        self.wb.options["thonny_ai.auto_reconnect"] = False
        self.runner.proxy = None
        self.tick([])
        self.tick([port("COM3")])
        self.assertEqual(self.runner.restarts, 0)


if __name__ == "__main__":
    unittest.main()
