"""Build Pico SDK projects and switch a single USB Pico between C and MicroPython."""

import contextlib
import ctypes
import hashlib
import math
import os
from pathlib import Path
import re
import shutil
import string
import struct
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid
from dataclasses import dataclass

from . import backends, config
from .backends import DeviceError


BOARDS = {"pico_w": "RP2040", "pico2_w": "RP2350"}
BOARD_FOR_CHIP = {chip: board for board, chip in BOARDS.items()}
C_PIDS = {0x000A: "RP2040", 0x0009: "RP2350"}
MP_PID = 0x0005
PICO_VID = 0x2E8A
BOOTSEL_HELP = "Hold BOOTSEL while plugging the Pico into USB, then retry."
NO_USB_PICO = f"No Pico was detected over USB. BOOTSEL is unavailable over Wi-Fi. {BOOTSEL_HELP}"
SDK_TAG = "2.3.1"
SDK_CHECKS = (
    "pico_sdk_init.cmake",
    "external/pico_sdk_import.cmake",
    "lib/cyw43-driver/src/cyw43.h",
    "lib/tinyusb/src/tusb.h",
)
FIRMWARE = {
    "pico_w": ("RPI_PICO_W-20260824-v1.29.0.uf2",
               "f918c0a082c6daf53e27998b7f990ba5f065db2e298b21a34024532a20786794"),
    "pico2_w": ("RPI_PICO2_W-20260824-v1.29.0.uf2",
                "c49c7ed93eb7eeaaa78436b17bd4329e5d4ccbaebc1e79503ad75e0a29c4530e"),
}
UF2_FAMILIES = {
    0xE48BFF56: "RP2040", 0xE48BFF59: "RP2350",
    0xE48BFF5A: "RP2350", 0xE48BFF5B: "RP2350",
}
MAX_FIRMWARE = 16 * 1024 * 1024
REPL_QUERY = "import os; print('THONNY_AI_BOARD=' + os.uname().machine)"
INSTALL_ASSETS = {
    "gcc": ("arm.zip", "https://developer.arm.com/-/media/Files/downloads/gnu/14.3.rel1/binrel/"
                        "arm-gnu-toolchain-14.3.rel1-mingw-w64-x86_64-arm-none-eabi.zip", ""),
    "cmake": ("cmake.zip", "https://cmake.org/files/v3.31/cmake-3.31.12-windows-x86_64.zip", ""),
    "ninja": ("ninja.zip", "https://github.com/ninja-build/ninja/releases/download/v1.13.2/ninja-win.zip", "ninja"),
    "picotool": ("picotool.zip", "https://github.com/raspberrypi/pico-sdk-tools/releases/download/"
                                "v2.3.1-0/picotool-2.3.1-x64-win.zip", ""),
    "pioasm": ("pico-sdk-tools.zip", "https://github.com/raspberrypi/pico-sdk-tools/releases/download/"
                                   "v2.3.1-0/pico-sdk-tools-2.3.1-x64-win.zip", ""),
}

CMAKE = """cmake_minimum_required(VERSION 3.13)
set(PICO_BOARD {board} CACHE STRING "Pico SDK board")
include(pico_sdk_import.cmake)
project({name} LANGUAGES C CXX ASM)
set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)
pico_sdk_init()

add_executable({name} main.c)
target_compile_definitions({name} PRIVATE USBD_PRODUCT="{board}")
target_link_libraries({name} PRIVATE pico_stdlib pico_cyw43_arch_none)
pico_enable_stdio_usb({name} 1)
pico_enable_stdio_uart({name} 0)
pico_add_extra_outputs({name})
"""

BLINK = r"""#include <stdbool.h>
#include <stdio.h>

#include "pico/cyw43_arch.h"
#include "pico/stdlib.h"

int main(void) {
    stdio_init_all();
    if (cyw43_arch_init()) {
        for (;;) {
            puts("CYW43 init failed");
            sleep_ms(1000);
        }
    }

    bool on = false;
    for (;;) {
        on = !on;
        cyw43_arch_gpio_put(CYW43_WL_GPIO_LED_PIN, on);
        printf("blink %s\n", on ? "on" : "off");
        sleep_ms(500);
    }
}
"""


@dataclass
class Toolchain:
    root: Path
    sdk: Path
    tools: dict

    @property
    def sdk_ready(self):
        return all((self.sdk / name).is_file() for name in SDK_CHECKS)

    def cmake_config(self, name):
        tool = self.tools[name]
        if tool:
            parent = tool.parent
            for path in (parent, parent.parent / "lib" / "cmake" / name, self.root / name):
                if (path / f"{name}Config.cmake").is_file():
                    return path
        return None

    def missing(self):
        missing = [name for name, path in self.tools.items() if path is None]
        if not self.sdk_ready:
            missing.append("sdk")
        for name in ("picotool", "pioasm"):
            if self.tools[name] and not self.cmake_config(name):
                missing.append(f"{name} CMake package")
        return missing


def toolchain_dir():
    directory = os.environ.get("THONNY_AI_TOOLCHAIN")
    if directory is None:
        settings = config.get("baremetal", {})
        if not isinstance(settings, dict):
            raise DeviceError('.thonny-ai.json: "baremetal" must be an object')
        directory = settings.get("toolchain_dir")
        if directory is not None and (not isinstance(directory, str) or not directory.strip()):
            raise DeviceError('.thonny-ai.json: "baremetal.toolchain_dir" must be a nonempty path')
    if not directory:
        return Path.home() / "Documents" / "pico-toolchain"
    return (Path(config.project_dir()) / Path(directory).expanduser()).resolve()


def discover_toolchain(root=None):
    root = Path(root) if root is not None else toolchain_dir()
    layouts = {
        "gcc": ("arm-none-eabi-gcc", ("bin", "arm-gnu-toolchain*/bin")),
        "cmake": ("cmake", ("bin", "cmake*/bin")),
        "ninja": ("ninja", ("bin", "ninja", "ninja*/bin")),
        "picotool": ("picotool", ("picotool", "bin", "picotool*/picotool", "picotool*/bin")),
        "pioasm": ("pioasm", ("pioasm", "bin", "pico-sdk-tools*/pioasm")),
    }
    tools = {}
    for key, (name, dirs) in layouts.items():
        executable = name + (".exe" if os.name == "nt" else "")
        locations = [root]
        for pattern in dirs:
            locations.extend(sorted(root.glob(pattern), reverse=True))
        local = next((p / executable for p in locations if (p / executable).is_file()), None)
        fallback = shutil.which(name) if local is None else None
        tools[key] = local or (Path(fallback) if fallback else None)
    sdk = root / "pico-sdk"
    if not (sdk / "pico_sdk_init.cmake").is_file() and os.environ.get("PICO_SDK_PATH"):
        sdk = Path(os.environ["PICO_SDK_PATH"]).expanduser().resolve()
    return Toolchain(root, sdk, tools)


def _install_commands(chain, missing):
    def quote(path):
        return "'" + str(path).replace("'", "''") + "'"

    if os.name != "nt":
        return ("Install Arm GNU Toolchain, CMake, Ninja and the matching picotool and pioasm releases "
                "from the sources in docs/bare-metal.md. Clone the SDK with:\n"
                f"git clone --branch {SDK_TAG} --recurse-submodules "
                f"https://github.com/raspberrypi/pico-sdk.git {quote(chain.sdk)}")

    root = quote(chain.root)
    lines = [
        f"$picoTools = {root}",
        "New-Item -ItemType Directory -Force -Path $picoTools | Out-Null",
    ]
    for item in missing:
        if item == "sdk":
            if (chain.sdk / "pico_sdk_init.cmake").is_file():
                lines.append(f"git -C {quote(chain.sdk)} submodule update --init --recursive")
            else:
                lines.append(f"git clone --branch {SDK_TAG} --recurse-submodules "
                             f"https://github.com/raspberrypi/pico-sdk.git {quote(chain.sdk)}")
            continue
        key = item.split(" ", 1)[0]
        filename, url, subdir = INSTALL_ASSETS[key]
        dest = '$picoTools'
        if subdir:
            dest = f'(Join-Path $picoTools "{subdir}")'
            lines.append(f"New-Item -ItemType Directory -Force -Path {dest} | Out-Null")
        archive = f'(Join-Path $picoTools "{filename}")'
        lines.extend((f'curl.exe -fL -o {archive} "{url}"', f"tar.exe -xf {archive} -C {dest}"))
    return "\n".join(lines)


def _need(chain, *missing):
    raise DeviceError(f"Missing {', '.join(missing)}. Install in the portable toolchain:\n"
                      f"{_install_commands(chain, missing)}")


def _run(command, timeout=15, env=None):
    try:
        return subprocess.run(
            [str(arg) for arg in command], env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except OSError as e:
        raise DeviceError(f"Cannot run {command[0]}: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise DeviceError(f"{Path(command[0]).name} timed out after {timeout:g} s") from e


def _diagnostics(output, limit):
    lines = [line for line in output.strip().splitlines() if not re.match(r"^\[\d+/\d+\] ", line)]
    for i, line in enumerate(lines):
        if re.search(r"(?:\berror:|CMake Error|undefined reference)", line, re.I):
            important = "\n".join(lines[i:])
            if len(important) > limit:
                half = limit // 2 - 20
                return important[:half] + "\n[… output omitted …]\n" + important[-half:]
            return important
    return "\n".join(lines)[-limit:] or "(no output)"


def _board(value):
    if value not in BOARDS:
        raise DeviceError("Choose board pico_w (RP2040) or pico2_w (RP2350).")
    return value


def _project_path(path):
    root = Path(config.project_dir()).resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        raise DeviceError("Path must be inside the configured project directory.")
    return target


def new(args):
    name = args.get("name", "")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", name):
        raise DeviceError("Use a project name starting with a letter, with up to 64 letters, digits, _ or -.")
    chain = discover_toolchain()
    board = args.get("board")
    if board is None:
        detected = detect_board(chain)
        board = detected.board or BOARD_FOR_CHIP.get(detected.chip)
    board = _board(board)
    source = chain.sdk / "external" / "pico_sdk_import.cmake"
    if not source.is_file():
        _need(chain, "sdk")
    parent = _project_path(args.get("directory", "baremetal"))
    project = parent / name
    try:
        parent.mkdir(parents=True, exist_ok=True)
        if project.exists():
            raise DeviceError(f"Project directory already exists: {project}")
        with tempfile.TemporaryDirectory(prefix=".bm-new-", dir=parent) as scratch:
            temp = Path(scratch) / name
            temp.mkdir()
            (temp / "CMakeLists.txt").write_text(CMAKE.format(board=board, name=name), encoding="utf-8", newline="\n")
            (temp / "main.c").write_text(BLINK, encoding="utf-8", newline="\n")
            (temp / source.name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
            temp.rename(project)
    except OSError as e:
        raise DeviceError(f"Cannot create {project}: {e}") from e
    return f"Created {board} project at {project}. Pass this directory to bm_build."


def build(args):
    chain = discover_toolchain()
    if missing := chain.missing():
        _need(chain, *missing)
    source = _project_path(args["path"])
    cmake_file = source / "CMakeLists.txt"
    if not cmake_file.is_file():
        raise DeviceError(f"No CMakeLists.txt in {source}. Pass a project directory.")
    board = args.get("board")
    if board is None:
        match = re.search(r"(?m)^\s*set\s*\(\s*PICO_BOARD\s+[\"']?(pico_w|pico2_w)\b",
                          cmake_file.read_text(encoding="utf-8"))
        board = match[1] if match else None
    board = _board(board)
    output = _project_path(source / "build" / board)
    cmake, gcc, ninja = (chain.tools[k] for k in ("cmake", "gcc", "ninja"))
    env = os.environ.copy()
    tools = [str(p.parent) for p in chain.tools.values()]
    env["PATH"] = os.pathsep.join(dict.fromkeys([*tools, env.get("PATH", "")]))
    env.pop("PICOTOOL_FORCE_FETCH_FROM_GIT", None)
    command = [
        cmake, "-S", source, "-B", output, "-G", "Ninja",
        f"-DCMAKE_MAKE_PROGRAM={ninja.as_posix()}",
        "-DCMAKE_BUILD_TYPE=Release", f"-DPICO_BOARD={board}",
        f"-DPICO_SDK_PATH={chain.sdk.as_posix()}", f"-DPICO_TOOLCHAIN_PATH={gcc.parent.as_posix()}",
        f"-Dpicotool_DIR={chain.cmake_config('picotool').as_posix()}",
        f"-Dpioasm_DIR={chain.cmake_config('pioasm').as_posix()}",
    ]
    for stage, cmd in (("configure", command), ("compile", [cmake, "--build", output])):
        result = _run(cmd, timeout=240, env=env)
        if result.returncode:
            raise DeviceError(f"{stage} failed (exit {result.returncode}):\n"
                              + _diagnostics(result.stdout, min(config.max_output(), 12000)))
    artifacts = sorted(output.rglob("*.uf2"))
    if not artifacts:
        raise DeviceError(f"Build completed without a UF2 under {output}. Use pico_add_extra_outputs in CMake.")
    for artifact in artifacts:
        _check_chip(_uf2_chip(_read_uf2(artifact)), BOARDS[board])
    return f"Built {board} ({BOARDS[board]}):\n" + "\n".join(str(p) for p in artifacts)


@dataclass(frozen=True)
class Board:
    mode: str
    chip: str = ""
    board: str = ""
    port: str = ""
    drive: Path = None


def _volume_roots():
    if os.name == "nt":
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        # Skip network shares and empty CD drives. BOOTSEL reports as removable or fixed.
        for i, letter in enumerate(string.ascii_uppercase):
            root = letter + ":\\"
            if mask & (1 << i) and ctypes.windll.kernel32.GetDriveTypeW(root) in (2, 3):
                yield Path(root)
    else:
        for parent in (Path("/Volumes"), Path("/media"), Path("/run/media"), Path("/mnt")):
            for name in ("RPI-RP2", "RP2350"):
                yield parent / name
                if parent.name == "media":
                    yield from parent.glob(f"*/{name}")


def _volume_chip(root):
    try:
        with (root / "INFO_UF2.TXT").open(encoding="ascii", errors="replace") as info:
            data = info.read(2048)
    except OSError:
        return ""
    match = re.search(r"^Board-ID:\s*(RPI-RP2|RP2350)\s*$", data, re.M | re.I)
    if not match or not data.startswith("UF2 Bootloader"):
        return ""
    return "RP2040" if match[1].upper() == "RPI-RP2" else "RP2350"


def bootsel_drives(roots=None):
    return [Board("BOOTSEL", chip=chip, drive=root) for root in
            dict.fromkeys(Path(p) for p in (_volume_roots() if roots is None else roots))
            if (chip := _volume_chip(root))]


def _board_name(text):
    if re.search(r"\b(?:pico\s*2\s*w|pico2_w)\b", text, re.I):
        return "pico2_w"
    if re.search(r"\b(?:pico\s+w|pico_w)\b", text, re.I):
        return "pico_w"
    return ""


def _serial_boards():
    from serial.tools import list_ports

    results = []
    for p in list_ports.comports():
        if p.vid != PICO_VID or p.pid not in {MP_PID, *C_PIDS}:
            continue
        board = _board_name(p.product or "")
        if p.pid == MP_PID:
            results.append(Board("MicroPython", BOARDS.get(board, ""), board, p.device))
        else:
            results.append(Board("C program", C_PIDS[p.pid], board, p.device))
    return results


def _picotool_bootsel(tool):
    if not tool:
        return []
    result = _run([tool, "info", "-d"])
    if result.returncode:
        return []
    chips = re.findall(r"(?mi)^\s*type:\s*(RP2040|RP2350)\b[^\r\n]*$", result.stdout)
    return [Board("BOOTSEL", chip.upper()) for chip in chips]


def _choose(boards):
    if len(boards) > 1:
        raise DeviceError("Multiple USB Pico devices found. Disconnect the other Picos and retry.")
    return boards[0] if boards else Board("none")


def detect_board(chain):
    mounted = bootsel_drives()
    serial = _serial_boards()
    # picotool can also see the mounted devices. Query it when the operating system
    # did not mount any BOOTSEL volumes (for instance a vendor-only reboot).
    return _choose((mounted or _picotool_bootsel(chain.tools["picotool"])) + serial)


def doctor(args):
    chain = discover_toolchain()
    lines = [f"Toolchain directory: {chain.root}"]
    for name, path in chain.tools.items():
        if path is None:
            lines.append(f"{name}: missing")
            continue
        try:
            result = _run([path, "version" if name == "picotool" else "--version"], 10)
            version = next((line for line in result.stdout.splitlines() if line.strip()), "")
            if result.returncode or not version:
                version = f"could not get version (exit {result.returncode})"
        except DeviceError as e:
            version = str(e)
        lines.append(f"{name}: {path} — {version[:240]}")
    version_file = chain.sdk / "pico_sdk_version.cmake"
    version = ""
    if version_file.is_file():
        version = ".".join(re.findall(r"(?m)^\s*set\(PICO_SDK_VERSION_(?:MAJOR|MINOR|REVISION) (\d+)\)",
                                      version_file.read_text(encoding="utf-8")))
    lines.append(f"Pico SDK: {chain.sdk} — " + (f"ready {version}".rstrip() if chain.sdk_ready else "missing or incomplete"))
    try:
        board = detect_board(chain)
        if board.mode == "none":
            lines.append("Board: none detected over USB.")
        else:
            detail = ", ".join(str(part) for part in (board.chip, board.board, board.port, board.drive) if part)
            lines.append(f"Board: {board.mode}" + (f" ({detail})" if detail else ""))
            if not board.board:
                suggested = BOARD_FOR_CHIP.get(board.chip)
                if suggested:
                    lines.append(f"SDK board for this chip's wireless Pico: {suggested}.")
                else:
                    lines.append("USB metadata does not identify the Pico model. Specify board for bm_new.")
    except DeviceError as e:
        lines.append(f"Board: {e}")
    if missing := chain.missing():
        lines.append(f"Missing: {', '.join(missing)}\n{_install_commands(chain, missing)}")
    return "\n".join(lines)


def _read_uf2(path):
    if path.suffix.lower() != ".uf2" or not path.is_file():
        raise DeviceError(f"Expected an existing .uf2 file: {path}")
    try:
        with path.open("rb") as source:
            data = source.read(MAX_FIRMWARE + 1)
    except OSError as e:
        raise DeviceError(f"Cannot read UF2 {path}: {e}") from e
    return data


def _uf2_chip(data):
    if not data or len(data) > MAX_FIRMWARE or len(data) % 512:
        raise DeviceError("Invalid or oversized UF2 image.")
    families = set()
    blocks = {}
    for offset in range(0, len(data), 512):
        start1, start2, flags, _address, size, number, count, family = struct.unpack_from("<8I", data, offset)
        end, = struct.unpack_from("<I", data, offset + 508)
        if ((start1, start2, end) != (0x0A324655, 0x9E5D5157, 0x0AB16F30)
                or not 0 < size <= 476 or not number < count or not flags & 0x2000):
            raise DeviceError(f"Invalid or family-less UF2 block at byte {offset}.")
        families.add(family)
        if family in UF2_FAMILIES:
            total, seen = blocks.setdefault(family, (count, set()))
            if count != total or number in seen:
                raise DeviceError("UF2 contains inconsistent or duplicate program blocks.")
            seen.add(number)
    chips = {UF2_FAMILIES[family] for family in families if family in UF2_FAMILIES}
    if len(chips) != 1 or families - {*UF2_FAMILIES, 0xE48BFF57, 0xE48BFF58}:
        raise DeviceError("UF2 must contain a single supported Pico chip family.")
    if any(len(seen) != total for total, seen in blocks.values()):
        raise DeviceError("UF2 is missing program blocks.")
    return chips.pop()


def _check_chip(image_chip, target_chip):
    if image_chip != target_chip:
        raise DeviceError(f"UF2 for {image_chip} cannot be flashed on {target_chip}.")


def copy_uf2(data, drive, filename):
    _check_chip(_uf2_chip(data), _volume_chip(drive) or "an unrecognized BOOTSEL volume")
    # The boot ROM watches incoming sectors; a temporary filename or rename is not
    # an atomic write to this virtual disk. Send the complete image and let it reboot.
    try:
        with (drive / Path(filename).name).open("wb") as dest:
            dest.write(data)
    except OSError as e:
        raise DeviceError(f"Writing the UF2 to {drive} failed: {e}. Re-enter BOOTSEL before retrying.") from e


def firmware_for(chip, board=None):
    if board is None:
        board = BOARD_FOR_CHIP.get(chip)
    board = _board(board)
    _check_chip(BOARDS[board], chip)
    name, sha256 = FIRMWARE[board]
    return board, f"https://micropython.org/resources/firmware/{name}", sha256


def _firmware(chain, chip, board):
    board, url, digest = firmware_for(chip, board)
    path = chain.root / "firmware" / url.rsplit("/", 1)[-1]
    try:
        if path.is_file():
            data = _read_uf2(path)
            if hashlib.sha256(data).hexdigest() == digest:
                _check_chip(_uf2_chip(data), chip)
                return board, path, data
        with urllib.request.urlopen(url, timeout=20) as response:
            data = response.read(MAX_FIRMWARE + 1)
        if hashlib.sha256(data).hexdigest() != digest:
            raise DeviceError(f"SHA-256 does not match the pinned MicroPython firmware: {url}")
        _check_chip(_uf2_chip(data), chip)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".download-", dir=path.parent) as scratch:
            temporary = Path(scratch) / path.name
            temporary.write_bytes(data)
            temporary.replace(path)
    except OSError as e:
        raise DeviceError(f"Could not download or cache MicroPython firmware ({url}): {e}") from e
    return board, path, data


@contextlib.contextmanager
def _serial(port, passive=False):
    from serial import SerialException

    session = backends._session
    if getattr(session, "name", None) == "serial" and session.port == port:
        if passive:
            raise DeviceError("The port is in use by a running MicroPython session.")
        session.close(force=True)
    try:
        backend = backends.SerialBackend(passive=passive, port=port)
        try:
            yield backend
        finally:
            backend.close(force=True)
    except SerialException as e:
        raise DeviceError(f"USB serial operation on {port} failed: {e}") from e


def _repl_board(backend):
    out, err, finished = backend.exec(REPL_QUERY, timeout=3)
    found = re.search(r"(?m)^THONNY_AI_BOARD=([^\r\n]+)", out)
    if not finished or err or not found:
        raise DeviceError("MicroPython did not answer the board identification query.")
    board = _board_name(found[1])
    if not board:
        raise DeviceError(f"Expected a Pico W or Pico 2 W, detected {found[1]}.")
    return board


def _probe_repl(current):
    with _serial(current.port) as backend:
        return _repl_board(backend)


def _poll(check, seconds):
    deadline = time.monotonic() + seconds
    wait = threading.Event().wait
    for _ in range(math.ceil(seconds / 0.25) + 1):
        if result := check():
            return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        wait(min(0.25, remaining))
    return None


def _await_bootsel(tool, seconds):
    def mounted():
        drives = bootsel_drives()
        return _choose(drives) if drives else None

    # Try the OS first. Vendor-only BOOTSEL is useful when the mass-storage
    # interface is disabled or the OS will not mount it.
    target = _poll(mounted, seconds) or _choose(_picotool_bootsel(tool))
    return target if target.mode == "BOOTSEL" else None


def _enter_bootsel(chain, current, expected_chip):
    if current.mode == "BOOTSEL":
        _check_chip(expected_chip, current.chip)
        return current
    if not current.port:
        raise DeviceError(NO_USB_PICO)
    picotool = chain.tools["picotool"]
    if current.mode == "MicroPython":
        with _serial(current.port) as backend:
            _check_chip(expected_chip, BOARDS[_repl_board(backend)])
            backend.reboot_to_bootsel()
        target = _await_bootsel(picotool, 8)
    else:
        _check_chip(expected_chip, current.chip)
        if picotool:
            pid = next(pid for pid, chip in C_PIDS.items() if chip == current.chip)
            try:
                _run([picotool, "reboot", "-u", "-f", "--vid", "0x2e8a", "--pid", hex(pid)])
            except DeviceError:
                pass  # Try the CDC reset if the installed picotool cannot open the device.
            if target := _await_bootsel(picotool, 3):
                _check_chip(expected_chip, target.chip)
                return target
        from serial import Serial, SerialException

        ports = _serial_boards()
        if current in ports:
            try:
                with Serial(current.port, 1200, timeout=0.1):
                    pass
            except SerialException:
                pass  # The reset can disconnect the USB connection during open/close.
        target = _await_bootsel(picotool, 8)
    if not target:
        raise DeviceError(f"The Pico did not enter BOOTSEL automatically. {BOOTSEL_HELP}")
    _check_chip(expected_chip, target.chip)
    return target


def _write_image(chain, target, path, data, method):
    _check_chip(_uf2_chip(data), target.chip)
    if target.drive and method in ("auto", "copy"):
        copy_uf2(data, target.drive, path.name)
        if not _poll(lambda: not _volume_chip(target.drive), 12):
            raise DeviceError(f"UF2 copied, but {target.drive} stayed in BOOTSEL. Check the image and USB cable.")
        return f"copied to {target.drive}; BOOTSEL drive detached"
    if method == "copy":
        raise DeviceError(f"No BOOTSEL drive is mounted. {BOOTSEL_HELP}")
    picotool = chain.tools["picotool"]
    if picotool is None:
        _need(chain, "picotool")
    accessible = _choose(_picotool_bootsel(picotool))
    if accessible.mode != "BOOTSEL":
        raise DeviceError("picotool could not access the Pico in BOOTSEL. Use the mounted drive if available.")
    _check_chip(target.chip, accessible.chip)
    # Feed the exact bytes we validated, even if the original was edited concurrently.
    with tempfile.TemporaryDirectory(prefix=".bm-flash-", dir=path.parent) as scratch:
        image = Path(scratch) / path.name
        image.write_bytes(data)
        pid = "0x0003" if target.chip == "RP2040" else "0x000f"
        result = _run([picotool, "load", "-v", "-x", image, "--pid", pid], 45)
    if result.returncode:
        raise DeviceError("picotool load failed:\n" + _diagnostics(result.stdout, 2000))
    if target.drive and not _poll(lambda: not _volume_chip(target.drive), 12):
        raise DeviceError("picotool wrote the UF2, but the drive stayed in BOOTSEL.")
    return "written and verified by picotool; reboot requested"


@contextlib.contextmanager
def _hardware_operation(kind, current, summary):
    meta = {"id": uuid.uuid4().hex, "kind": kind, "summary": summary, "detail": summary}
    status = backends.bridge_status()
    if status is None:
        if os.environ.get("THONNY_AI_SERIAL_FALLBACK") == "0":
            raise DeviceError("Thonny is not running and the serial fallback is disabled.")
        yield None
        return
    backends.bridge_request("/bm_claim", {"op": meta, "port": current.port}, timeout=135)
    backend = backends.ThonnyBackend(meta)
    try:
        yield backend
    except Exception as e:
        backend.finish(False, str(e))
        raise


def flash(args):
    path = _project_path(args["path"])
    data = _read_uf2(path)
    chip = _uf2_chip(data)
    chain = discover_toolchain()
    current = detect_board(chain)
    if current.mode == "none":
        raise DeviceError(NO_USB_PICO)
    method = args.get("method", "auto")
    if method not in ("auto", "copy", "picotool"):
        raise DeviceError("Choose flash method auto, copy, or picotool.")
    if method == "picotool" and not chain.tools["picotool"]:
        _need(chain, "picotool")
    if current.chip:
        _check_chip(chip, current.chip)
    with _hardware_operation("bm_flash", current, f"Flash {path.name} ({chip})") as panel:
        target = _enter_bootsel(chain, current, chip)
        result = _write_image(chain, target, path, data, method)

        def reconnected():
            board = _choose(_serial_boards())
            return board if board.mode != "none" and board.chip in ("", chip) else None

        board = _poll(reconnected, 8)
        detail = f"{board.mode} connected on {board.port}" if board else "no USB CDC port appeared; application startup unverified"
        message = f"{path.name}: {result}; {detail}."
        if panel:
            panel.finish(True, message)
        return message


def restore(args):
    chain = discover_toolchain()
    current = detect_board(chain)
    if current.mode == "none":
        raise DeviceError(NO_USB_PICO)
    requested = _board(args["board"]) if "board" in args else None
    if current.board and requested and requested != current.board:
        raise DeviceError(f"Requested {requested}, but the board reports {current.board}.")
    summary = f"Restore official MicroPython ({requested or current.board or current.chip or current.port})"
    with _hardware_operation("bm_restore_micropython", current, summary) as panel:
        identified = _probe_repl(current) if current.mode == "MicroPython" else current.board
        if requested and identified and requested != identified:
            raise DeviceError(f"Requested {requested}, but the board reports {identified}.")
        chip = current.chip or BOARDS[identified]
        board, path, data = _firmware(chain, chip, requested or identified or None)
        target = _enter_bootsel(chain, current, chip)
        result = _write_image(chain, target, path, data, "auto")

        def verify():
            candidate = _choose(_serial_boards())
            if candidate.mode != "MicroPython":
                return None
            try:
                actual = _probe_repl(candidate)
            except DeviceError:
                return None
            if actual != board:
                raise DeviceError(f"Restored MicroPython booted on {actual}, expected {board}.")
            return candidate

        port = _poll(verify, 15)
        if not port:
            raise DeviceError(f"UF2 {result}, but MicroPython did not answer its raw REPL.")
        message = f"Restored MicroPython to {board}; raw REPL verified on {port.port}."
        if panel:
            try:
                backends.bridge_request("/bm_resume", {"op": panel.op})
            except (OSError, DeviceError) as e:
                message += f" Thonny could not reconnect automatically: {e}"
            panel.finish(True, message)
        return message


def console(args):
    from .tools import MAX_TIMEOUT, _cap, _watch

    seconds = float(args.get("seconds", 5))
    if not math.isfinite(seconds) or seconds <= 0:
        raise DeviceError("seconds must be a positive finite number.")
    seconds = min(seconds, MAX_TIMEOUT)
    try:
        pattern = re.compile(args["until"]) if args.get("until") else None
    except re.error as e:
        raise DeviceError(f"until is not a valid regular expression: {e}") from e
    current = _choose(_serial_boards())
    if current.mode != "C program":
        raise DeviceError("No Pico SDK USB CDC console found. Enable USB stdio in your CMake project.")
    with _hardware_operation("bm_console", current, f"Listen on {current.port} ({seconds:g} s)") as panel:
        with _serial(current.port, passive=True) as backend:
            output, _, _, matched = _watch(lambda s: (backend.listen(s), "", None), seconds, pattern)
        message = f"C console ({current.port}):\n{_cap(output) if output else '(no output)'}"
        if matched is not None:
            message += "\n[pattern matched]" if matched else "\n[pattern not seen]"
        if panel:
            panel.finish(True, message)
        return message
