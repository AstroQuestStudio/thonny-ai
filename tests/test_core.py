import json
import threading
import time
import unittest
import urllib.error
import urllib.request

from thonnycontrib.thonny_ai.core import BridgeCore, start_server

from fakes import FakeHost


def request(port, path, payload=None, headers=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        method="GET" if payload is None else "POST",
        headers={"X-Thonny-AI": "1", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        with e:
            return e.code, json.loads(e.read())


class BridgeServerTest(unittest.TestCase):
    def setUp(self):
        self.host = FakeHost()
        self.approval = False
        self.core = BridgeCore(self.host, require_approval=lambda: self.approval, approval_timeout=3)
        self.events = []
        self.core.add_listener(lambda event, op: self.events.append((event, op.id)))
        self.server = start_server(self.core, port=0)
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_rejects_requests_without_header(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/status")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        ctx.exception.close()
        self.assertEqual(ctx.exception.code, 403)

    def test_rejects_browser_requests(self):
        status, body = request(self.port, "/status", headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self.assertFalse(body["ok"])

    def test_status(self):
        status, body = request(self.port, "/status")
        self.assertEqual(status, 200)
        self.assertEqual(body["port"], "COM99")
        self.assertFalse(body["require_approval"])

    def test_exec_is_routed_with_display_options(self):
        op = {"id": "op1", "kind": "exec", "summary": "print(1)"}
        status, body = request(
            self.port, "/exec", {"code": "print(1)", "echo": True, "echo_text": "# x", "op": op}
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.host.calls, [("exec", "print(1)", True, False, "# x")])

    def test_write_then_read_roundtrip(self):
        import base64

        op = {"id": "op2", "kind": "write"}
        payload = base64.b64encode("é✓".encode()).decode()
        request(self.port, "/write_file", {"path": "/a.txt", "content_b64": payload, "op": op})
        _, body = request(self.port, "/read_file", {"path": "/a.txt", "op": {"id": "op3", "kind": "read"}})
        self.assertEqual(base64.b64decode(body["content_b64"]).decode(), "é✓")

    def test_unknown_endpoint_and_missing_argument(self):
        self.assertEqual(request(self.port, "/nope", {})[0], 404)
        self.assertEqual(request(self.port, "/exec", {"op": {"id": "x"}})[0], 500)

    def test_approval_allowed(self):
        self.approval = True
        op = {"id": "needs-ok", "kind": "exec", "summary": "led on"}
        threading.Thread(target=self._decide_later, args=("needs-ok", True)).start()
        status, _ = request(self.port, "/exec", {"code": "x=1", "op": op})
        self.assertEqual(status, 200)
        self.assertIn(("approval_needed", "needs-ok"), self.events)

    def test_approval_denied(self):
        self.approval = True
        threading.Thread(target=self._decide_later, args=("nope", False)).start()
        status, body = request(self.port, "/exec", {"code": "x=1", "op": {"id": "nope", "kind": "exec"}})
        self.assertEqual(status, 409)
        self.assertIn("denied", body["error"])
        self.assertEqual(self.host.calls, [])

    def test_approval_timeout_denies(self):
        self.approval = True
        self.core.approval_timeout = 0.3
        status, _ = request(self.port, "/exec", {"code": "x=1", "op": {"id": "slow", "kind": "exec"}})
        self.assertEqual(status, 409)

    def test_one_approval_per_operation(self):
        self.approval = True
        op = {"id": "batch", "kind": "sync"}
        threading.Thread(target=self._decide_later, args=("batch", True)).start()
        request(self.port, "/exec", {"code": "a=1", "op": op})
        request(self.port, "/exec", {"code": "b=2", "op": op})
        asked = [e for e in self.events if e == ("approval_needed", "batch")]
        self.assertEqual(len(asked), 1)
        self.assertEqual(len(self.host.calls), 2)

    def test_read_only_operations_skip_approval(self):
        self.approval = True
        status, _ = request(self.port, "/shell", {"op": {"id": "r", "kind": "shell"}})
        self.assertEqual(status, 200)

    def test_trust_session(self):
        self.approval = True
        threading.Thread(target=self._decide_later, args=("first", True, True)).start()
        request(self.port, "/exec", {"code": "a=1", "op": {"id": "first", "kind": "exec"}})
        status, _ = request(self.port, "/exec", {"code": "a=2", "op": {"id": "second", "kind": "exec"}})
        self.assertEqual(status, 200)
        self.assertNotIn(("approval_needed", "second"), self.events)

    def test_finish_updates_operation(self):
        op = {"id": "f", "kind": "exec", "summary": "s"}
        request(self.port, "/exec", {"code": "a=1", "op": op})
        request(self.port, "/finish", {"op": op, "ok": True, "result": "done!"})
        final = next(o for o in self.core.operations() if o.id == "f")
        self.assertEqual((final.status, final.result), ("done", "done!"))

    def _decide_later(self, op_id, allowed, trust=False):
        for _ in range(100):
            if ("approval_needed", op_id) in self.events:
                self.core.decide(op_id, allowed, trust)
                return
            time.sleep(0.02)


if __name__ == "__main__":
    unittest.main()
