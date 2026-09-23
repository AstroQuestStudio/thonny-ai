"""Bridge core, independent from Tk/Thonny (unit-testable).

Every MCP tool call is one *operation* (id, kind, summary, detail). All HTTP
requests of an operation share its id: one approval per operation, and one row
in the activity log.
"""

import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_PORT = 47821
PROTOCOL_VERSION = 1
READ_ONLY_KINDS = frozenset({"status", "info", "ls", "read", "shell", "open", "monitor"})
APPROVAL_TIMEOUT = 120.0
MAX_KEPT_OPS = 300
OP_PATHS = frozenset(
    {
        "/exec",
        "/start",
        "/monitor",
        "/write_file",
        "/read_file",
        "/interrupt",
        "/restart",
        "/shell",
        "/open",
    }
)


class BridgeError(Exception):
    pass


class UnknownEndpoint(Exception):
    pass


class Operation:
    def __init__(self, op_id, kind, summary, detail):
        self.id = op_id
        self.kind = kind
        self.summary = summary
        self.detail = detail
        self.created = time.time()
        self.status = "pending"  # pending -> approved | denied -> done | failed
        self.result = ""
        self.auto_approved = False
        self._decided = threading.Event()
        self.allowed = None

    @property
    def read_only(self):
        return self.kind in READ_ONLY_KINDS

    def decide(self, allowed, reason=""):
        if self._decided.is_set():
            return
        self.allowed = allowed
        self.status = "approved" if allowed else "denied"
        if reason:
            self.result = reason
        self._decided.set()

    def wait_decision(self, timeout):
        return self._decided.wait(timeout)


class BridgeCore:
    """Operation approval, activity log and request routing to the host (Thonny)."""

    def __init__(self, host, require_approval=lambda: False, approval_timeout=APPROVAL_TIMEOUT):
        self.host = host
        self.require_approval = require_approval
        self.approval_timeout = approval_timeout
        self.session_trust = False
        self._ops = {}
        self._order = []
        self._lock = threading.Lock()
        self._listeners = []

    # ------------------------------------------------------------ activity log

    def add_listener(self, fn):
        """fn(event, op) is called from any thread."""
        self._listeners.append(fn)

    def _emit(self, event, op):
        for fn in list(self._listeners):
            try:
                fn(event, op)
            except Exception:
                pass

    def operations(self):
        with self._lock:
            return [self._ops[i] for i in self._order]

    def _get_or_create_op(self, meta):
        meta = meta or {}
        op_id = str(meta.get("id") or f"anon-{time.time_ns()}")
        with self._lock:
            op = self._ops.get(op_id)
            if op is not None:
                return op
            op = Operation(
                op_id,
                str(meta.get("kind") or "exec"),
                str(meta.get("summary") or ""),
                str(meta.get("detail") or ""),
            )
            self._ops[op_id] = op
            self._order.append(op_id)
            while len(self._order) > MAX_KEPT_OPS:
                self._ops.pop(self._order.pop(0), None)
        self._emit("new", op)
        return op

    # ------------------------------------------------------------ validation

    def authorize(self, op):
        if op.allowed is None:
            if op.read_only or self.session_trust or not self.require_approval():
                op.auto_approved = True
                op.decide(True)
                self._emit("update", op)
            else:
                self._emit("approval_needed", op)
                if not op.wait_decision(self.approval_timeout):
                    op.decide(False, "denied: no answer in Thonny (timeout)")
                self._emit("update", op)
        if not op.allowed:
            raise BridgeError(
                f"Operation denied by the user in Thonny ({op.result or op.summary})"
            )

    def decide(self, op_id, allowed, trust_session=False):
        with self._lock:
            op = self._ops.get(op_id)
        if op is None:
            return
        if trust_session and allowed:
            self.session_trust = True
        op.decide(allowed, "" if allowed else "denied in Thonny")
        self._emit("update", op)

    def finish(self, meta, ok, result):
        op = self._get_or_create_op(meta)
        op.status = "done" if ok else "failed"
        op.result = str(result or "")[:2000]
        self._emit("update", op)

    # ------------------------------------------------------------ routing

    def handle(self, method, path, args):
        if method == "GET" and path == "/status":
            return {
                "protocol": PROTOCOL_VERSION,
                "require_approval": bool(self.require_approval()),
                **self.host.status(),
            }
        if method != "POST":
            raise UnknownEndpoint(path)

        if path == "/finish":
            self.finish(args.get("op"), bool(args.get("ok")), args.get("result"))
            return {}

        if path not in OP_PATHS:
            raise UnknownEndpoint(path)
        op = self._get_or_create_op(args.get("op"))
        self.authorize(op)
        host = self.host

        if path == "/exec":
            return host.exec_code(
                args["code"],
                float(args.get("timeout", 10)),
                echo=bool(args.get("echo")),
                quiet=bool(args.get("quiet")),
                echo_text=args.get("echo_text"),
            )
        if path == "/start":
            return host.start_code(
                args["code"], echo=bool(args.get("echo", True)), echo_text=args.get("echo_text")
            )
        if path == "/monitor":
            since = args.get("since")
            return host.monitor(float(args.get("seconds", 5)), None if since is None else int(since))
        if path == "/write_file":
            host.write_file(args["path"], base64.b64decode(args["content_b64"]))
            return {}
        if path == "/read_file":
            data = host.read_file(args["path"])
            return {"content_b64": base64.b64encode(data).decode("ascii")}
        if path == "/interrupt":
            host.interrupt()
            return {}
        if path == "/restart":
            host.restart()
            return {}
        if path == "/shell":
            return {"text": host.shell_text(int(args.get("max_chars", 4000)))}
        if path == "/open":
            host.open_file(args["path"])
            return {}
        raise UnknownEndpoint(path)


def make_handler(core):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _reply(self, code, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # Custom header required and any Origin refused: a web page open in a browser
        # cannot reach the bridge (no CORS, and a "simple" request cannot carry the header).
        def _allowed(self):
            if self.headers.get("Origin") or self.headers.get("X-Thonny-AI") != "1":
                self._reply(403, {"ok": False, "error": "forbidden"})
                return False
            return True

        def _dispatch(self, method):
            if not self._allowed():
                return
            try:
                args = {}
                if method == "POST":
                    length = int(self.headers.get("Content-Length") or 0)
                    args = json.loads(self.rfile.read(length) or b"{}")
                result = core.handle(method, self.path, args)
                self._reply(200, {"ok": True, **result})
            except UnknownEndpoint:
                self._reply(404, {"ok": False, "error": f"unknown endpoint: {self.path}"})
            except BridgeError as e:
                self._reply(409, {"ok": False, "error": str(e)})
            except Exception as e:
                self._reply(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

    return Handler


def start_server(core, port=DEFAULT_PORT):
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(core))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="thonny-ai-bridge", daemon=True).start()
    return server
