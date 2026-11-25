"""Per-project settings, read from `.thonny-ai.json` in the project directory.

Every key is optional::

    {
      "local_dir": "src",                 # default folder for `sync` / `pull`
      "remote_dir": "/",                  # default destination on the board
      "ignore": ["*.md", "tests/*"],      # glob patterns skipped by `sync` (and protected from `delete`)
      "safe_stop": "import motors; motors.stop()",   # MicroPython run after anything that stops a program
      "safe_stop_file": "safe_stop.py",   # alternative: a local file with that code
      "max_output": 20000                 # characters returned to the AI by one call
    }

`THONNY_AI_SAFE_CODE` and `THONNY_AI_MAX_OUTPUT` override the file.
"""

import ipaddress
import json
import os
import re
from dataclasses import dataclass

CONFIG_NAME = ".thonny-ai.json"
DEFAULT_MAX_OUTPUT = 20000
WEBREPL_PASSWORD_ENV = "THONNY_AI_WEBREPL_PASSWORD"

_cache = {}  # path -> (mtime, parsed)


class ConfigError(Exception):
    pass


def project_dir():
    return os.environ.get("THONNY_AI_PROJECT") or os.getcwd()


def load():
    """Parsed settings dict ({} when there is no file). A broken file is a clear error."""
    path = os.path.join(project_dir(), CONFIG_NAME)
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return {}
    cached = _cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except ValueError as e:
        raise ConfigError(f"{CONFIG_NAME} is not valid JSON: {e}")
    if not isinstance(data, dict):
        raise ConfigError(f"{CONFIG_NAME} must contain a JSON object")
    _cache[path] = (mtime, data)
    return data


def get(key, default=None):
    return load().get(key, default)


@dataclass(frozen=True)
class WebReplSettings:
    host: str | None
    port: int
    password_env: str

    def password(self):
        value = os.environ.get(self.password_env)
        if not value:
            raise ConfigError(f"Set {self.password_env} in the MCP server environment for WebREPL.")
        try:
            length = len(value.encode("utf-8"))
        except UnicodeError:
            length = 0
        if not 4 <= length <= 9 or any(c in value for c in "\0\r\n"):
            raise ConfigError(f"{self.password_env} must contain a WebREPL password of 4–9 UTF-8 bytes.")
        return value


def _valid_webrepl_host(host):
    if not isinstance(host, str) or not host or re.search(r"[\s\x00-\x1f\x7f/\\@?#\[\]]", host):
        return False
    try:
        if ":" in host:
            ipaddress.IPv6Address(host)
        else:
            host.encode("idna")
    except (ValueError, UnicodeError):
        return False
    return True


def webrepl_settings():
    value = get("webrepl", {})
    if not isinstance(value, dict):
        raise ConfigError(f'{CONFIG_NAME}: "webrepl" must contain an object')
    host = os.environ.get("THONNY_AI_WEBREPL_HOST") or value.get("host")
    if host is not None and not _valid_webrepl_host(host):
        raise ConfigError("WebREPL host must be a hostname or IP address, without a URL or port.")
    port = value.get("port", 8266)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ConfigError("WebREPL port must be an integer between 1 and 65535.")
    password_env = value.get("password_env", WEBREPL_PASSWORD_ENV)
    if not isinstance(password_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", password_env):
        raise ConfigError("WebREPL password_env must be an environment variable name.")
    return WebReplSettings(host, port, password_env)


def ignore_patterns():
    value = get("ignore", [])
    if not isinstance(value, list) or not all(isinstance(p, str) for p in value):
        raise ConfigError(f'{CONFIG_NAME}: "ignore" must be a list of glob patterns')
    return value


def max_output():
    raw = os.environ.get("THONNY_AI_MAX_OUTPUT") or get("max_output", DEFAULT_MAX_OUTPUT)
    try:
        return max(200, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_MAX_OUTPUT


def safe_stop_code():
    """MicroPython code that puts the hardware in a safe state (motors off…), or None."""
    code = os.environ.get("THONNY_AI_SAFE_CODE")
    if code:
        return code
    code = get("safe_stop")
    if code:
        return str(code)
    name = get("safe_stop_file")
    if name:
        full = os.path.normpath(os.path.join(project_dir(), name))
        try:
            with open(full, encoding="utf-8") as f:
                return f.read()
        except OSError as e:
            raise ConfigError(f'{CONFIG_NAME}: cannot read safe_stop_file "{name}" ({e})')
    return None
