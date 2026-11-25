"""Provision the board over direct USB without putting secrets in MCP arguments."""

import ipaddress
import os
import posixpath

from . import config
from .backends import DeviceError, bridge_status, open_backend

_SSID_ENV = "THONNY_AI_WIFI_SSID"
_WIFI_PASSWORD_ENV = "THONNY_AI_WIFI_PASSWORD"
_CONFIG = "/webrepl_cfg.py"
_HELPER = "/wifi_boot.py"
_BOOT = "/boot.py"
_BEGIN = b"# >>> thonny-ai Wi-Fi >>>"
_END = b"# <<< thonny-ai Wi-Fi <<<"
_BOOT_BLOCK = (
    _BEGIN,
    b"try:",
    b"    import wifi_boot as _thonny_ai_wifi_boot",
    b"    if not _thonny_ai_wifi_boot.start():",
    b"        print('thonny-ai: Wi-Fi connection timed out')",
    b"except Exception:",
    b"    print('thonny-ai: Wi-Fi startup failed')",
    _END,
)
_HELPER_HEADER = b"# Managed by thonny-ai wifi_setup\n"
_HELPER_CODE = _HELPER_HEADER + b"""\
def start(reconnect=False):
    import network
    import sys
    import time
    import webrepl

    sys.modules.pop('webrepl_cfg', None)
    import webrepl_cfg

    station = network.WLAN.IF_STA if hasattr(network.WLAN, 'IF_STA') else network.STA_IF
    wlan = network.WLAN(station)
    wlan.active(True)
    if reconnect:
        wlan.disconnect()
    if reconnect or not wlan.isconnected():
        wlan.connect(webrepl_cfg.SSID, webrepl_cfg.WIFI_PASSWORD)
        for _ in range(40):
            if wlan.isconnected():
                break
            time.sleep_ms(250)
    if not wlan.isconnected():
        return None
    webrepl.start(port=webrepl_cfg.PORT, password=webrepl_cfg.PASS)
    return wlan.ifconfig()[0]
"""


def _staging_path(path):
    return "/.thonny-ai-" + posixpath.basename(path) + ".tmp"


def is_wifi_credentials(path):
    """Keep Pico credentials out of ordinary MCP file reads and deployments."""
    return posixpath.basename(path.rstrip("/")).casefold() in (_CONFIG[1:], _staging_path(_CONFIG)[1:])


def _update_boot(original):
    lines = original.splitlines(keepends=True)
    markers = [
        (i, marker)
        for i, line in enumerate(lines)
        if (marker := line.rstrip(b"\r\n")) in (_BEGIN, _END)
    ]
    newline = b"\r\n" if b"\r\n" in original else b"\n"
    block = newline.join(_BOOT_BLOCK) + newline
    if markers:
        if [marker for _, marker in markers] != [_BEGIN, _END]:
            raise DeviceError("The thonny-ai Wi-Fi markers in boot.py are incomplete or duplicated.")
        return b"".join(lines[: markers[0][0]]) + block + b"".join(lines[markers[1][0] + 1 :])
    separator = newline if original and not original.endswith((b"\r", b"\n")) else b""
    return original + separator + block


def _run(backend, code, timeout=15.0):
    out, err, finished = backend.exec(code, timeout)
    if err.strip() or not finished:
        raise DeviceError("The Wi-Fi setup command failed on the board.")
    return out


def _replace(backend, path, data):
    # The Pico W's default LittleFS can atomically replace an existing file.
    temporary = _staging_path(path)
    try:
        backend.write_file(temporary, data)
        _run(backend, f"import os; os.rename({temporary!r}, {path!r})")
    except Exception:
        try:
            _run(backend, f"import os; os.remove({temporary!r})")
        except Exception:
            pass
        raise


def _provision(backend, credentials):
    try:
        backend.ensure_idle()
        files = _run(backend, "import network, webrepl, os\nfor _n in os.listdir('/'): print(_n)").splitlines()
        boot = backend.read_file(_BOOT) if _BOOT[1:] in files else b""
        helper = backend.read_file(_HELPER) if _HELPER[1:] in files else b""
    except Exception:
        raise DeviceError("Cannot inspect the board over USB. Check that its Pico W firmware includes WebREPL.") from None

    updated = _update_boot(boot)
    if _HELPER[1:] in files and not helper.startswith(_HELPER_HEADER):
        raise DeviceError("The board already has a custom wifi_boot.py. Rename it before running wifi_setup.")
    try:
        if helper != _HELPER_CODE:
            _replace(backend, _HELPER, _HELPER_CODE)
        _replace(backend, _CONFIG, credentials)
        if updated != boot:
            _replace(backend, _BOOT, updated)
    except Exception:
        raise DeviceError("Could not save the Wi-Fi setup on the board. Check its filesystem and rerun wifi_setup.") from None

    marker = "thonny_ai_wifi_ip="
    try:
        out = _run(
            backend,
            "import sys\n"
            "sys.modules.pop('wifi_boot', None)\n"
            "import wifi_boot\n"
            f"print({marker!r} + (wifi_boot.start(reconnect=True) or ''))\n",
            timeout=18.0,
        )
        ip = next(line[len(marker):] for line in out.splitlines() if line.startswith(marker))
        if ip:
            return str(ipaddress.IPv4Address(ip))
    except Exception:
        raise DeviceError("Setup was saved, but the board could not start Wi-Fi/WebREPL. Check the network and rerun wifi_setup.") from None
    raise DeviceError("Setup was saved, but Wi-Fi did not connect within 10 seconds. Check the network and rerun wifi_setup.")


def tool_wifi_setup(args):
    settings = config.webrepl_settings()
    ssid = os.environ.get(_SSID_ENV)
    wifi_password = os.environ.get(_WIFI_PASSWORD_ENV)
    if not ssid or wifi_password is None:
        raise DeviceError(f"Set {_SSID_ENV} and {_WIFI_PASSWORD_ENV} in the MCP server environment.")
    if not 1 <= len(ssid.encode("utf-8", "replace")) <= 32 or "\0" in ssid:
        raise DeviceError(f"{_SSID_ENV} must be a Wi-Fi network name of 1–32 UTF-8 bytes.")
    if bridge_status() is not None:
        raise DeviceError("Close Thonny before running wifi_setup so it can use the board directly over USB.")
    password = settings.password()
    try:
        credentials = (
            f"PASS = {password!r}\nSSID = {ssid!r}\nWIFI_PASSWORD = {wifi_password!r}\nPORT = {settings.port}\n"
        ).encode("utf-8")
    except UnicodeError:
        raise DeviceError("Wi-Fi setup environment values must be valid UTF-8.") from None
    backend = open_backend({}, transport="usb")
    try:
        ip = _provision(backend, credentials)
    finally:
        backend.close()
    return (
        f"Board joined Wi-Fi at {ip}. WebREPL is listening on port {settings.port}.\n"
        f"Set THONNY_AI_WEBREPL_HOST={ip} in the MCP server environment, or configure "
        f'"webrepl": {{"host": "{ip}", "port": {settings.port}, "password_env": "{settings.password_env}"}} '
        "in .thonny-ai.json.\n"
        f"Keep {settings.password_env} in the server environment. "
        "Set THONNY_AI_TRANSPORT=wifi to force Wi-Fi, or unplug USB and use auto."
    )
