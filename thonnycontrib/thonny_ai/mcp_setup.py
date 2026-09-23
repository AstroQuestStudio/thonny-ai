"""Registers the thonny-ai MCP server in Claude Code (`claude mcp add`)."""

import os
import shutil
import subprocess
import sys

from .i18n import t

SERVER_NAME = "thonny-ai"


def mcp_python():
    exe = sys.executable
    if os.path.basename(exe).lower() == "pythonw.exe":
        candidate = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.exists(candidate):
            return candidate
    return exe


def server_command():
    return [mcp_python(), "-m", "thonny_ai_mcp"]


def manual_command():
    parts = ["claude", "mcp", "add", SERVER_NAME, "-s", "user", "--", *server_command()]
    return " ".join(f'"{p}"' if " " in p else p for p in parts)


def find_claude():
    for name in ("claude", "claude.cmd", "claude.exe"):
        path = shutil.which(name)
        if path:
            return path
    return None


def register_with_claude_code():
    """Returns (success, message)."""
    claude = find_claude()
    if claude is None:
        return False, t("claude_missing", cmd=manual_command())
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    subprocess.run(
        [claude, "mcp", "remove", SERVER_NAME, "-s", "user"],
        capture_output=True, text=True, timeout=60, creationflags=flags,
    )
    proc = subprocess.run(
        [claude, "mcp", "add", SERVER_NAME, "-s", "user", "--", *server_command()],
        capture_output=True, text=True, timeout=60, creationflags=flags,
    )
    if proc.returncode != 0:
        return False, t("claude_failed", output=(proc.stdout + proc.stderr).strip())
    return True, t("claude_ok")
