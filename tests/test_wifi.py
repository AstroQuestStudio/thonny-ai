"""WebREPL and provisioning tests with loopback sockets and a sandboxed board."""

import base64
import hashlib
import json
import os
import socket
import socketserver
import struct
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock
from uuid import uuid4

from fakes import FakeBoard, FakeRawReplSerial
from thonny_ai_mcp import backends, config, server, tools, webrepl, wifi_setup


def _test_password():
    return uuid4().hex[:8]


def _server_frame(data, opcode=1, final=True):
    first = (0x80 if final else 0) | opcode
    if len(data) <= 125:
        return bytes((first, len(data))) + data
    if len(data) <= 65535:
        return struct.pack("!BBH", first, 126, len(data)) + data
    return struct.pack("!BBQ", first, 127, len(data)) + data


class _WebReplHandler(socketserver.StreamRequestHandler):
    timeout = 2

    def _read_frame(self):
        header = self.rfile.read(2)
        if not header:
            return 8, b""
        opcode, size = header
        assert opcode & 0x80 and size & 0x80, "Client frames must be final and masked"
        size &= 0x7F
        if size in (126, 127):
            size = int.from_bytes(self.rfile.read(2 if size == 126 else 8), "big")
        mask = self.rfile.read(4)
        payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(self.rfile.read(size)))
        opcode &= 0x0F
        if opcode == 1:
            payload.decode("utf-8")
        return opcode, payload

    def handle(self):
        try:
            self._handle()
        except (ConnectionError, TimeoutError):
            pass
        except Exception as error:
            self.server.errors.append(error)

    def _handle(self):
        assert self.rfile.readline() == b"GET / HTTP/1.1\r\n"
        headers = {}
        for line in iter(self.rfile.readline, b"\r\n"):
            if not line:
                return
            header, value = line.split(b":", 1)
            headers[header.lower()] = value.strip()
        assert headers[b"sec-websocket-version"] == b"13"
        key = headers[b"sec-websocket-key"]
        assert len(base64.b64decode(key)) == 16
        accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
        self.request.sendall(
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: WebSocket\r\n"
            b"Connection: keep-alive, Upgrade\r\nSec-WebSocket-Accept: "
            + (b"invalid" if self.server.bad_accept else accept) + b"\r\n\r\n"
            + _server_frame(b"Pass", final=False) + _server_frame(b"word: ", opcode=0)
        )
        opcode, password = self._read_frame()
        if opcode == 8:
            return
        assert opcode == 1
        if password != self.server.password:
            self.request.sendall(_server_frame(b"\r\nAccess denied\r\n"))
            return
        self.request.sendall(
            _server_frame(b"\r\nWebREPL connected\r\n>>> ")
            + _server_frame(b"pico", opcode=9)
            + _server_frame(self.server.initial_output)
        )
        while True:
            opcode, data = self._read_frame()
            if opcode == 8:
                return
            if opcode == 10:
                assert data == b"pico"
                self.server.pong.set()
            elif opcode == 2:
                self.request.sendall(_server_frame(data, opcode=2))
            elif opcode == 1:
                self.server.commands.append(data)
                if data == b"\x03" and self.server.disconnect_on_interrupt:
                    self.request.sendall(_server_frame(b"", opcode=8))
                    self._read_frame()
                    return
                self.server.repl.write(data)
                output = self.server.repl.read(1 << 20)
                if output:
                    frame = _server_frame(output)
                    self.request.sendall(frame[:1])
                    self.request.sendall(frame[1:])


class WifiTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.env = {
            "THONNY_AI_PROJECT": tmp.name,
            config.WEBREPL_PASSWORD_ENV: _test_password(),
        }
        self.patch(mock.patch.dict(os.environ, self.env, clear=True))
        self.patch(mock.patch.object(backends, "_session", None))

    def patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def set_settings(self, **settings):
        (self.root / ".thonny-ai.json").write_text(json.dumps({"webrepl": settings}), encoding="utf-8")


class WebReplTest(WifiTestCase):
    def setUp(self):
        super().setUp()
        self.board = FakeBoard(self.root)
        self.webrepl = socketserver.TCPServer(("127.0.0.1", 0), _WebReplHandler)
        self.webrepl.password = self.env[config.WEBREPL_PASSWORD_ENV].encode("ascii") + b"\r"
        self.webrepl.bad_accept = False
        self.webrepl.disconnect_on_interrupt = False
        self.webrepl.initial_output = b""
        self.webrepl.repl = FakeRawReplSerial(self.board, hang_on="while True: pass")
        self.webrepl.pong = threading.Event()
        self.webrepl.errors = []
        self.webrepl.commands = []
        self.port = self.webrepl.server_address[1]
        thread = threading.Thread(target=self.webrepl.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()

        def close():
            self.webrepl.shutdown()
            self.webrepl.server_close()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertFalse(self.webrepl.errors, "Fake WebREPL failed")

        self.addCleanup(close)
        self.set_settings(host="127.0.0.1", port=self.port)

    def backend(self, passive=False):
        backend = webrepl.WebReplBackend(config.webrepl_settings(), passive=passive)
        self.addCleanup(backend.close, force=True)
        return backend

    def test_exec_and_python_error(self):
        b = self.backend()
        self.assertEqual(b.exec("print(6*7)"), ("42\r\n", "", True))
        self.assertIn("ZeroDivisionError", b.exec("1/0")[1])
        # The first REPL chunk would bisect a UTF-8 code point with byte slicing alone.
        self.assertEqual(b.exec("#" + "a" * 254 + "é\nprint('é')")[0], "é\r\n")
        self.assertTrue(self.webrepl.pong.wait(1))

    def test_timeout_interrupts_and_restores_raw_repl(self):
        b = self.backend()
        out, err, finished = b.exec("while True: pass", timeout=0.15)
        self.assertEqual(out, "tick\r\n")
        self.assertFalse(finished)
        self.assertIn("KeyboardInterrupt", err)
        self.assertEqual(b.exec("print('ok')")[0], "ok\r\n")

    def test_binary_file_roundtrip_in_chunks(self):
        b = self.backend()
        data = bytes(range(256)) * 9
        b.write_file("/sample.bin", data)
        self.assertEqual(b.read_file("/sample.bin"), data)

    def test_mcp_start_monitor_and_interrupt_share_socket(self):
        os.environ["THONNY_AI_TRANSPORT"] = "wifi"
        self.addCleanup(lambda: backends._session and backends._session.close(force=True))
        started = tools.tool_start({"code": "while True: pass", "wait": 0.15})
        self.assertIn("tick", started)
        self.assertIn("still running", started)
        saved = backends._session
        with mock.patch.object(tools, "bridge_status", return_value=None), mock.patch.object(
            tools, "find_board_port", return_value=None,
        ):
            self.assertIn("reachable and authenticated", tools.tool_status({}))
        self.assertIn("still running", tools.tool_monitor({"seconds": 0.1}))
        self.assertIs(backends._session, saved)
        self.assertIn("Program stopped (via wifi)", tools.tool_interrupt({}))
        self.assertIsNone(backends._session)

    def test_passive_console_does_not_send_repl_commands(self):
        self.webrepl.initial_output = b"distance 12\r\n"
        b = self.backend(passive=True)
        self.assertEqual(b.listen(0.15), "distance 12\r\n")
        self.assertEqual(self.webrepl.commands, [])

    def test_reset_leaves_raw_repl_before_ctrl_d(self):
        b = self.backend()
        b.reset()
        b.close()
        self.assertTrue(self.webrepl.pong.wait(1))
        self.webrepl.shutdown()
        self.assertEqual(self.webrepl.commands[-2:], [b"\x02", b"\x04"])

    def test_wrong_password_is_a_sanitized_error(self):
        os.environ[config.WEBREPL_PASSWORD_ENV] = _test_password()
        with self.assertRaisesRegex(backends.DeviceError, "rejected the password"):
            self.backend()

    def test_invalid_websocket_accept_is_rejected_before_login(self):
        self.webrepl.bad_accept = True
        with self.assertRaisesRegex(backends.DeviceError, "Invalid WebREPL WebSocket handshake"):
            self.backend()
        self.assertEqual(self.webrepl.commands, [])

    def test_unreachable_server(self):
        # Bound but not listening, so no other process can claim this port.
        with socket.socket() as closed_server:
            closed_server.bind(("127.0.0.1", 0))
            settings = config.WebReplSettings(
                "127.0.0.1", closed_server.getsockname()[1], config.WEBREPL_PASSWORD_ENV,
            )
            with self.assertRaisesRegex(backends.DeviceError, "WebREPL is unreachable"):
                webrepl.WebReplBackend(settings)

    def test_binary_websocket_frames_and_extended_lengths(self):
        client = webrepl.connect(config.webrepl_settings())
        self.addCleanup(client.close)
        data = bytes(range(256)) * 257
        client.write(data, binary=True)
        self.assertEqual(client.read(len(data), timeout=1.0), data)
        self.assertTrue(self.webrepl.pong.wait(1))

    def test_disconnect_drops_kept_session(self):
        b = self.backend()
        b.start("while True: pass")
        b.close()
        self.assertIs(backends._session, b)
        self.webrepl.disconnect_on_interrupt = True
        b.interrupt()
        with self.assertRaisesRegex(backends.DeviceError, "closed the connection"):
            b.monitor(1.0)
        b.close()
        self.assertIsNone(backends._session)

    def test_status_authenticates_without_repl_commands(self):
        with mock.patch.object(tools, "bridge_status", return_value=None), mock.patch.object(
            tools, "find_board_port", return_value=None,
        ):
            self.assertIn("reachable and authenticated", tools.tool_status({}))
        self.assertEqual(self.webrepl.commands, [])


class TransportSelectionTest(WifiTestCase):
    def setUp(self):
        super().setUp()
        self.set_settings(host="127.0.0.1")
        self.bridge = self.patch(mock.patch.object(backends, "bridge_status", return_value=None))
        self.find_port = self.patch(mock.patch.object(backends, "find_board_port", return_value=None))
        self.serial = self.patch(mock.patch.object(backends, "SerialBackend"))
        self.wifi = self.patch(mock.patch.object(webrepl, "WebReplBackend"))

    def test_auto_prefers_thonny_then_usb_then_wifi(self):
        self.bridge.return_value = {"connected": True}
        self.assertIsInstance(backends.open_backend({}), backends.ThonnyBackend)
        self.find_port.assert_not_called()
        self.bridge.return_value = None
        self.find_port.return_value = "COM42"
        self.assertIs(backends.open_backend({}), self.serial.return_value)
        self.wifi.assert_not_called()
        self.find_port.return_value = None
        self.assertIs(backends.open_backend({}, passive=True), self.wifi.return_value)
        self.wifi.assert_called_once_with(config.webrepl_settings(), passive=True)

    def test_forced_wifi_bypasses_thonny_and_usb(self):
        os.environ["THONNY_AI_TRANSPORT"] = "wifi"
        self.assertIs(backends.open_backend({}), self.wifi.return_value)
        self.bridge.assert_not_called()
        self.find_port.assert_not_called()

    def test_forced_usb_does_not_fallback_to_wifi(self):
        os.environ["THONNY_AI_TRANSPORT"] = "usb"
        with self.assertRaisesRegex(backends.DeviceError, "no MicroPython board found on USB"):
            backends.open_backend({})
        self.wifi.assert_not_called()
        os.environ["THONNY_AI_SERIAL_PORT"] = "COM71"
        self.assertIs(backends.open_backend({}), self.serial.return_value)
        self.serial.assert_called_once_with(passive=False, port="COM71")

    def test_auto_serial_fallback_flag_still_disables_serial(self):
        os.environ["THONNY_AI_SERIAL_FALLBACK"] = "0"
        self.assertIs(backends.open_backend({}), self.wifi.return_value)
        self.find_port.assert_not_called()

    def test_config_overrides_and_validation(self):
        self.set_settings(host="board.local", port=9271, password_env="ROVER_WEBREPL")
        os.environ["THONNY_AI_WEBREPL_HOST"] = "127.0.0.1"
        settings = config.webrepl_settings()
        self.assertEqual((settings.host, settings.port, settings.password_env), ("127.0.0.1", 9271, "ROVER_WEBREPL"))
        with self.assertRaisesRegex(config.ConfigError, "Set ROVER_WEBREPL"):
            settings.password()
        os.environ["THONNY_AI_WEBREPL_HOST"] = "127.0.0.1:8266"
        with self.assertRaisesRegex(config.ConfigError, "without a URL or port"):
            config.webrepl_settings()


class WifiSetupTest(WifiTestCase):
    def setUp(self):
        super().setUp()
        self.board = FakeBoard(self.root)
        self.patch(mock.patch.object(backends, "find_board_port", return_value="COM42"))
        self.open_usb = self.patch(mock.patch("serial.Serial", side_effect=lambda *a, **kw: FakeRawReplSerial(self.board)))
        self.bridge = self.patch(mock.patch.object(wifi_setup, "bridge_status", return_value=None))
        self.wlan = mock.Mock()
        self.wlan.ifconfig.return_value = ("192.0.2.42", "", "", "")
        self.webrepl_start = mock.Mock()
        self.modules = {
            "sys": types.SimpleNamespace(modules={}),
            "network": types.SimpleNamespace(WLAN=lambda interface: self.wlan, STA_IF=0),
            "time": types.SimpleNamespace(sleep_ms=lambda ms: None),
            "webrepl": types.SimpleNamespace(start=self.webrepl_start),
        }
        builtins = self.board.namespace["__builtins__"]
        original_import = builtins["__import__"]

        def board_import(name, *args, **kwargs):
            if name in self.modules:
                return self.modules[name]
            if name in ("wifi_boot", "webrepl_cfg"):
                module = types.ModuleType(name)
                module.__dict__["__builtins__"] = builtins
                exec(self.board.read(f"/{name}.py"), module.__dict__)
                return module
            return original_import(name, *args, **kwargs)

        builtins["__import__"] = board_import
        os.environ["THONNY_AI_WIFI_SSID"] = "simulated access point"
        os.environ["THONNY_AI_WIFI_PASSWORD"] = uuid4().hex
        os.environ["THONNY_AI_TRANSPORT"] = "wifi"  # Setup always uses USB.

    def call_setup(self):
        result = server.handle({"method": "tools/call", "params": {"name": "wifi_setup"}})
        return result["isError"], result["content"][0]["text"]

    def test_provision_preserves_boot_and_is_idempotent(self):
        original = b"# Existing initialization\r\nprint('existing boot')"
        self.board.write("/boot.py", original)
        for _ in range(2):
            failed, result = self.call_setup()
            self.assertFalse(failed, result)
            self.assertIn("THONNY_AI_WEBREPL_HOST=192.0.2.42", result)
            self.assertNotIn(os.environ["THONNY_AI_WIFI_PASSWORD"], result)
            self.assertNotIn(self.env[config.WEBREPL_PASSWORD_ENV], result)
        boot = self.board.read("/boot.py")
        self.assertTrue(boot.startswith(original + b"\r\n"))
        self.assertEqual(boot.count(b"# >>> thonny-ai Wi-Fi >>>"), 1)
        self.assertEqual(self.webrepl_start.call_count, 2)
        self.assertEqual(self.open_usb.call_args.args, ("COM42", 115200))
        self.assertFalse(list(self.root.glob("*.tmp")))
        # Run boot.py as on startup, with Wi-Fi already connected.
        out, err = self.board.run(boot.decode())
        self.assertIn("existing boot", out)
        self.assertEqual(err, "")
        self.assertEqual(self.webrepl_start.call_count, 3)

    def test_existing_custom_helper_is_preserved(self):
        self.board.write("/wifi_boot.py", b"# User's script\n")
        failed, result = self.call_setup()
        self.assertTrue(failed)
        self.assertIn("custom wifi_boot.py", result)
        self.assertEqual(self.board.read("/wifi_boot.py"), b"# User's script\n")
        self.assertFalse((self.root / "webrepl_cfg.py").exists())

    def test_failed_boot_replace_preserves_original(self):
        original = b"# User's boot script\n"
        self.board.write("/boot.py", original)
        rename = self.board.os.rename

        def fail_boot_replace(source, target):
            if target == "/boot.py":
                raise OSError("simulated filesystem failure")
            rename(source, target)

        with mock.patch.object(self.board.os, "rename", side_effect=fail_boot_replace):
            failed, result = self.call_setup()
        self.assertTrue(failed)
        self.assertIn("Could not save", result)
        self.assertEqual(self.board.read("/boot.py"), original)
        self.assertFalse(list(self.root.glob("*.tmp")))

    def test_timeout_without_board_output_or_passwords(self):
        self.wlan.isconnected.return_value = False
        failed, result = self.call_setup()
        self.assertTrue(failed)
        self.assertIn("did not connect within 10 seconds", result)
        self.assertTrue((self.root / "boot.py").exists())
        self.webrepl_start.assert_not_called()

    def test_unexpected_board_error_does_not_expose_passwords(self):
        self.webrepl_start.side_effect = ValueError(os.environ[config.WEBREPL_PASSWORD_ENV])
        failed, result = self.call_setup()
        self.assertTrue(failed)
        self.assertIn("could not start Wi-Fi/WebREPL", result)
        self.assertNotIn(os.environ[config.WEBREPL_PASSWORD_ENV], result)

    def test_setup_refuses_open_thonny(self):
        self.bridge.return_value = {"connected": True}
        failed, result = self.call_setup()
        self.assertTrue(failed)
        self.assertIn("Close Thonny", result)
        self.open_usb.assert_not_called()

    def test_credentials_are_excluded_from_read_and_pull(self):
        self.board.write("/webrepl_cfg.py", self.env[config.WEBREPL_PASSWORD_ENV].encode())
        self.board.write("/.thonny-ai-webrepl_cfg.py.tmp", self.env[config.WEBREPL_PASSWORD_ENV].encode())
        self.board.write("/main.py", b"print(42)")
        with mock.patch.object(tools, "open_backend", return_value=backends.SerialBackend()):
            with self.assertRaisesRegex(backends.DeviceError, "excluded from MCP file reads"):
                tools.tool_read({"path": "/webrepl_cfg.py"})
            tools.tool_pull({})
        self.assertEqual(list((self.root / "board_pull").iterdir()), [self.root / "board_pull" / "main.py"])
