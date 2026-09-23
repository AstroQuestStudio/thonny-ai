import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import serial

from thonny_ai_mcp import backends, server, tools
from thonnycontrib.thonny_ai.core import BridgeCore, start_server

from fakes import FakeBackend, FakeBoard, FakeHost, FakeRawReplSerial

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def call(name, **arguments):
    return server.handle({"method": "tools/call", "params": {"name": name, "arguments": arguments}})


def text(result):
    return result["content"][0]["text"]


class ProtocolTest(unittest.TestCase):
    def test_initialize_negotiates_version(self):
        result = server.handle({"method": "initialize", "params": {"protocolVersion": "2025-03-26"}})
        self.assertEqual(result["protocolVersion"], "2025-03-26")
        self.assertEqual(result["serverInfo"]["name"], "thonny-ai")
        unknown = server.handle({"method": "initialize", "params": {"protocolVersion": "1999"}})
        self.assertEqual(unknown["protocolVersion"], server.SUPPORTED_PROTOCOLS[0])

    def test_tools_list_has_valid_schemas(self):
        listed = server.handle({"method": "tools/list"})["tools"]
        names = {t["name"] for t in listed}
        self.assertTrue({"exec", "sync", "read", "write", "ls", "rm", "status"} <= names)
        for t in listed:
            self.assertEqual(t["inputSchema"]["type"], "object")
            for req in t["inputSchema"]["required"]:
                self.assertIn(req, t["inputSchema"]["properties"])

    def test_unknown_tool_is_an_error_result(self):
        self.assertTrue(call("does_not_exist")["isError"])

    def test_stdio_loop(self):
        lines = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "nope"},
        ]
        stdin = "\n".join(json.dumps(line) for line in lines) + "\nnot json\n"
        proc = subprocess.run(
            [sys.executable, "-m", "thonny_ai_mcp"],
            input=stdin.encode(),
            capture_output=True,
            cwd=ROOT,
            timeout=30,
        )
        replies = [json.loads(line) for line in proc.stdout.decode().splitlines()]
        self.assertEqual([r.get("id") for r in replies], [1, 2, 3, None])
        self.assertEqual(replies[2]["error"]["code"], -32601)
        self.assertEqual(replies[3]["error"]["code"], -32700)


class ToolsTest(unittest.TestCase):
    """Tools against a fake board whose filesystem lives in a temp folder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.board_root = os.path.join(self.tmp.name, "board")
        self.project = os.path.join(self.tmp.name, "project")
        os.makedirs(self.board_root)
        os.makedirs(os.path.join(self.project, "src", "lib"))
        self._write_local("src/main.py", "import lib.motors\nprint('go')\n")
        self._write_local("src/lib/motors.py", "SPEED = 1\n")
        self.backend = FakeBackend(FakeBoard(self.board_root))
        patches = [
            mock.patch.object(tools, "open_backend", lambda op, passive=False: self.backend),
            mock.patch.dict(os.environ, {"THONNY_AI_PROJECT": self.project}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_local(self, rel, content):
        with open(os.path.join(self.project, rel), "w", encoding="utf-8", newline="\n") as f:
            f.write(content)

    def test_exec_returns_output_and_echoes(self):
        result = call("exec", code="for i in range(3): print(i)")
        self.assertFalse(result["isError"])
        self.assertIn("0\n1\n2", text(result))
        self.assertEqual(self.backend.calls[0], ("exec", True, None))
        self.assertEqual(self.backend.finished[0], True)

    def test_exec_error_is_reported(self):
        result = call("exec", code="1/0")
        self.assertTrue(result["isError"])
        self.assertIn("ZeroDivisionError", text(result))
        self.assertEqual(self.backend.finished[0], False)

    def test_write_read_ls_rm(self):
        self.assertFalse(call("write", path="data/notes.txt", content="88.88 é✓\n")["isError"])
        self.assertEqual(text(call("read", path="/data/notes.txt")), "88.88 é✓\n")
        listing = text(call("ls"))
        self.assertIn("DIR", listing)
        self.assertIn("/data/notes.txt", listing)
        self.assertIn("FREE", listing)
        self.assertFalse(call("rm", path="data")["isError"])
        self.assertNotIn("notes.txt", text(call("ls")))

    def test_sync_uploads_only_changes(self):
        first = text(call("sync"))
        self.assertIn("2 uploaded, 0 unchanged", first)
        self.assertIn(("reset",), self.backend.calls)
        self.assertEqual(self.backend.board.read("/lib/motors.py"), b"SPEED = 1\n")

        self.backend.calls.clear()
        self.assertIn("0 uploaded, 2 unchanged", text(call("sync")))
        self.assertNotIn(("reset",), self.backend.calls)

        self._write_local("src/lib/motors.py", "SPEED = 2\n")
        third = text(call("sync", reset=False))
        self.assertIn("1 uploaded, 1 unchanged", third)
        self.assertIn("/lib/motors.py", third)
        self.assertEqual(self.backend.board.read("/lib/motors.py"), b"SPEED = 2\n")

    def test_sync_dry_run_and_force(self):
        dry = text(call("sync", dry_run=True))
        self.assertIn("2 file(s) would be uploaded", dry)
        self.assertEqual(os.listdir(self.board_root), [])
        call("sync", reset=False)
        self.assertIn("2 uploaded", text(call("sync", force=True, reset=False)))

    def test_sync_missing_folder(self):
        result = call("sync", local_dir="nope")
        self.assertTrue(result["isError"])
        self.assertIn("not found", text(result))

    def test_start_code_returns_first_output(self):
        result = call("start", code="print('rover ready')")
        self.assertFalse(result["isError"])
        self.assertIn("Started", text(result))
        self.assertIn("rover ready", text(result))
        self.assertIn("[program ended]", text(result))

    def test_start_file_uses_short_label(self):
        call("start", path="src/lib/motors.py")
        kind, echo_text = self.backend.calls[0]
        self.assertEqual(kind, "start")
        self.assertIn("motors.py", echo_text)

    def test_start_requires_code_or_path(self):
        self.assertTrue(call("start")["isError"])
        self.assertTrue(call("start", code="x=1", path="src/main.py")["isError"])

    def test_monitor(self):
        self.backend.start("print('distance', 42)")
        self.assertIn("distance 42", text(call("monitor", seconds=1)))

    def test_info(self):
        info = text(call("info"))
        self.assertIn("firmware:", info)
        self.assertIn("wifi:", info)

    def test_run_file_shows_short_label(self):
        result = call("run_file", path="src/lib/motors.py")
        self.assertFalse(result["isError"])
        _, echo, echo_text = self.backend.calls[0]
        self.assertTrue(echo)
        self.assertIn("motors.py", echo_text)


class SerialBackendTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.board = FakeBoard(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _backend(self, **kw):
        fake = FakeRawReplSerial(self.board, **kw)
        with mock.patch.object(serial, "Serial", lambda *a, **k: fake), mock.patch.object(
            backends, "find_board_port", lambda: "COM42"
        ):
            return backends.SerialBackend(), fake

    def test_exec_and_error(self):
        b, _ = self._backend()
        out, err, finished = b.exec("print('hi')")
        self.assertEqual((out, err, finished), ("hi\r\n", "", True))
        _, err, _ = b.exec("1/0")
        self.assertIn("ZeroDivisionError", err)

    def test_timeout_interrupts(self):
        b, _ = self._backend(hang_on="while True")
        out, err, finished = b.exec("while True: pass", timeout=0.3)
        self.assertFalse(finished)
        self.assertIn("KeyboardInterrupt", err)

    def test_file_roundtrip_in_chunks(self):
        b, _ = self._backend()
        data = ("ligne é✓ " * 400).encode()
        b.write_file("/big.txt", data)
        self.assertEqual(b.read_file("/big.txt"), data)

    def test_close_leaves_raw_repl(self):
        b, fake = self._backend()
        b.close()
        self.assertTrue(fake.closed)

    def test_start_monitor_interrupt(self):
        b, fake = self._backend(hang_on="while True")
        b.start("while True: pass")
        out, err, running = b.monitor(0.3)
        self.assertEqual((out, running), ("tick\r\n", True))
        self.assertTrue(b.ensure_idle())
        self.assertFalse(b.running)
        self.assertEqual(b.exec("print(1)")[0], "1\r\n")

    def test_running_program_keeps_the_session(self):
        b, fake = self._backend(hang_on="while True")
        b.start("while True: pass")
        b.close()
        self.assertFalse(fake.closed)
        with mock.patch.object(backends, "bridge_status", lambda: None):
            self.assertIs(backends.open_backend({}), b)
        b.ensure_idle()
        b.close()
        self.assertTrue(fake.closed)
        self.assertIsNone(backends._session)

    def test_opening_thonny_releases_the_session(self):
        b, fake = self._backend(hang_on="while True")
        b.start("while True: pass")
        b.close()
        with mock.patch.object(backends, "bridge_status", lambda: {"connected": True}):
            backend = backends.open_backend({"id": "x"})
        self.assertIsInstance(backend, backends.ThonnyBackend)
        self.assertTrue(fake.closed)

    def test_passive_listen_does_not_interrupt(self):
        fake = FakeRawReplSerial(self.board)
        fake._out += b"distance 12\r\n"
        with mock.patch.object(serial, "Serial", lambda *a, **k: fake), mock.patch.object(
            backends, "find_board_port", lambda: "COM42"
        ):
            b = backends.SerialBackend(passive=True)
        self.assertEqual(b.listen(0.2), "distance 12\r\n")
        b.close()
        self.assertTrue(fake.closed)


class ThonnyBackendTest(unittest.TestCase):
    """MCP tools -> HTTP -> BridgeCore -> host, end to end (Thonny replaced by FakeHost)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.host = FakeHost(FakeBoard(self.tmp.name))
        self.approval = False
        self.core = BridgeCore(self.host, require_approval=lambda: self.approval, approval_timeout=2)
        self.server = start_server(self.core, port=0)
        url = f"http://127.0.0.1:{self.server.server_address[1]}"
        p = mock.patch.object(backends, "BRIDGE_URL", url)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def test_status_sees_thonny(self):
        self.assertIn("Thonny is open", text(call("status")))

    def test_busy_thonny_is_not_mistaken_for_closed(self):
        real_status = self.host.status

        def slow_status():
            import time

            time.sleep(1.5)
            return real_status()

        self.host.status = slow_status
        self.assertIsInstance(backends.open_backend({"id": "slow"}), backends.ThonnyBackend)

    def test_thonny_object_links_are_stripped(self):
        self.host.exec_code = lambda *a, **k: {
            "finished": True,
            "stdout": "[object_link_for_thonny=1690299465936]42[/object_link_for_thonny]\n",
            "stderr": "",
            "error": None,
        }
        self.assertEqual(text(call("exec", code="6*7")), "(via thonny)\n42\n")

    def test_closed_thonny_means_serial(self):
        with mock.patch.object(backends, "BRIDGE_URL", "http://127.0.0.1:9"):
            self.assertIsNone(backends.bridge_status())

    def test_exec_goes_through_thonny_and_is_logged(self):
        result = call("exec", code="print(6*7)")
        self.assertIn("(via thonny)", text(result))
        self.assertIn("42", text(result))
        op = self.core.operations()[-1]
        self.assertEqual((op.kind, op.status), ("exec", "done"))

    def test_start_and_monitor_through_thonny(self):
        started = text(call("start", code="print('hello from the loop')"))
        self.assertIn("hello from the loop", started)
        self.assertEqual(self.host.calls[0][0], "start")
        op = [o for o in self.core.operations() if o.kind == "start"][-1]
        self.assertEqual(op.status, "done")

    def test_monitor_is_read_only(self):
        self.approval = True
        self.assertFalse(call("monitor", seconds=0.1)["isError"])

    def test_denied_operation(self):
        self.approval = True

        def deny():
            import time

            for _ in range(100):
                pending = [o for o in self.core.operations() if o.allowed is None]
                if pending:
                    self.core.decide(pending[0].id, False)
                    return
                time.sleep(0.02)

        threading.Thread(target=deny).start()
        result = call("exec", code="print('nope')")
        self.assertTrue(result["isError"])
        self.assertIn("denied", text(result))


if __name__ == "__main__":
    unittest.main()
