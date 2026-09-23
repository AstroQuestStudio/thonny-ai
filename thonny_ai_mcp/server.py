"""MCP server over stdio (JSON-RPC 2.0, one message per line). No third-party dependency
besides pyserial, so it runs with the Python bundled in Thonny."""

import json
import sys

from . import __version__
from .tools import TOOLS

SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")


class MethodNotFound(Exception):
    pass


def handle(request):
    method = request.get("method")
    params = request.get("params") or {}
    if method == "initialize":
        requested = params.get("protocolVersion")
        return {
            "protocolVersion": requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "thonny-ai", "version": __version__},
            "instructions": (
                "Tools to drive a MicroPython board (e.g. Raspberry Pi Pico W) through Thonny. "
                "Start with `status` or `info`. Use `exec` for short experiments, `start` + "
                "`monitor` + `interrupt` for programs that loop forever, and `sync` to deploy a "
                "project folder (then `monitor` to watch main.py). On a Pico W the onboard LED is "
                "machine.Pin('LED'). If an action is denied, the user refused it in Thonny: do not "
                "retry blindly, ask them."
            ),
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {
            "tools": [
                {"name": name, "description": desc, "inputSchema": schema}
                for name, (_, desc, schema) in TOOLS.items()
            ]
        }
    if method == "tools/call":
        name = params.get("name")
        if name not in TOOLS:
            return _tool_result(f"Unknown tool: {name}", True)
        try:
            return _tool_result(TOOLS[name][0](params.get("arguments") or {}), False)
        except Exception as e:
            return _tool_result(f"Error: {e}", True)
    raise MethodNotFound(method)


def _tool_result(text, is_error):
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _send(message):
    sys.stdout.buffer.write(json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def main():
    for raw in sys.stdin.buffer:
        raw = raw.strip()
        if not raw:
            continue
        try:
            request = json.loads(raw)
        except ValueError:
            _send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        if "id" not in request:
            continue  # notification
        reply = {"jsonrpc": "2.0", "id": request["id"]}
        try:
            reply["result"] = handle(request)
        except MethodNotFound as e:
            reply["error"] = {"code": -32601, "message": f"method not found: {e}"}
        except Exception as e:
            reply["error"] = {"code": -32603, "message": str(e)}
        _send(reply)


if __name__ == "__main__":
    main()
