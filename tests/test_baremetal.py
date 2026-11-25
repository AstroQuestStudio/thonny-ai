"""Offline coverage of the bare-metal MCP tools."""

import hashlib
import io
import os
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import serial

from thonny_ai_mcp import baremetal as bm
from thonny_ai_mcp.server import handle
from thonnycontrib.thonny_ai.core import BridgeCore, BridgeError


def uf2(family):
    header = struct.pack("<8I", 0x0A324655, 0x9E5D5157, 0x2000, 0x10000000, 256, 0, 1, family)
    return header + bytes(476) + struct.pack("<I", 0x0AB16F30)


class BareMetalTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.project = Path(tmp.name)
        self.root = self.project / "tools"
        self.activate(mock.patch.dict(os.environ, {"THONNY_AI_PROJECT": str(self.project), "THONNY_AI_TOOLCHAIN": str(self.root)}))
        self.activate(mock.patch.object(bm.shutil, "which", return_value=None))
        self.activate(mock.patch.object(bm.backends, "bridge_status", return_value=None))
        self.activate(mock.patch.object(bm, "_volume_roots", return_value=[]))
        self.serial_boards = self.activate(mock.patch.object(bm, "_serial_boards", return_value=[]))
        self.activate(mock.patch.object(bm, "_picotool_bootsel", return_value=[]))
        self.activate(mock.patch.dict(os.environ, {"THONNY_AI_SERIAL_FALLBACK": "1", "PICO_SDK_PATH": ""}))

    def activate(self, p):
        self.addCleanup(p.stop)
        return p.start()

    def fake_tools(self):
        suffix = ".exe" if os.name == "nt" else ""
        paths = {
            "gcc": f"bin/arm-none-eabi-gcc{suffix}",
            "cmake": f"cmake-3.31.12/bin/cmake{suffix}",
            "ninja": f"ninja/ninja{suffix}",
            "picotool": f"picotool/picotool{suffix}",
            "pioasm": f"pioasm/pioasm{suffix}",
        }
        for name in (*paths.values(), "picotool/picotoolConfig.cmake", "pioasm/pioasmConfig.cmake",
                     *(f"pico-sdk/{check}" for check in bm.SDK_CHECKS)):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# fake\n", encoding="utf-8")
        return {tool: self.root / file for tool, file in paths.items()}

    def volume(self, label):
        folder = self.project / label
        folder.mkdir()
        (folder / "INFO_UF2.TXT").write_text(f"UF2 Bootloader v3.0\nBoard-ID: {label}\n", encoding="ascii")
        return folder

    def test_detects_portable_tools_and_sdk(self):
        paths = self.fake_tools()
        chain = bm.discover_toolchain()
        self.assertEqual(chain.tools, paths)
        self.assertEqual(chain.missing(), [])
        self.assertEqual(chain.cmake_config("pioasm"), self.root / "pioasm")

    def test_config_and_environment_override(self):
        (self.project / ".thonny-ai.json").write_text('{"baremetal":{"toolchain_dir":"other"}}', encoding="utf-8")
        self.assertEqual(bm.toolchain_dir(), self.root)
        with mock.patch.dict(os.environ):
            os.environ.pop("THONNY_AI_TOOLCHAIN")
            self.assertEqual(bm.toolchain_dir(), self.project / "other")

    def test_new_project_with_sdk_import_and_lf(self):
        self.fake_tools()
        bm.new({"name": "blink", "board": "pico2_w"})
        project = self.project / "baremetal" / "blink"
        self.assertIn("set(PICO_BOARD pico2_w", (project / "CMakeLists.txt").read_text())
        self.assertIn(b'printf("blink %s\\n"', (project / "main.c").read_bytes())
        self.assertNotIn(b"\r", (project / "main.c").read_bytes())
        self.assertEqual((project / "pico_sdk_import.cmake").read_bytes(), b"# fake\n")
        with self.assertRaisesRegex(bm.DeviceError, "already exists"):
            bm.new({"name": "blink", "board": "pico2_w"})

    def test_new_rejects_outside_project(self):
        self.fake_tools()
        with self.assertRaisesRegex(bm.DeviceError, "inside the configured project"):
            bm.new({"name": "blink", "board": "pico_w", "directory": ".."})

    def test_build_returns_target_specific_uf2(self):
        self.fake_tools()
        bm.new({"name": "blink", "board": "pico_w"})
        output = self.project / "baremetal/blink/build/pico_w"
        output.mkdir(parents=True)
        image = output / "blink.uf2"
        image.write_bytes(uf2(0xE48BFF56))
        with mock.patch.object(bm, "_run", return_value=SimpleNamespace(returncode=0, stdout="")) as run:
            self.assertIn(str(image), bm.build({"path": "baremetal/blink"}))
        self.assertIn("-DPICO_BOARD=pico_w", run.call_args_list[0].args[0])
        self.assertIn("-G", run.call_args_list[0].args[0])
        self.assertIn("--build", run.call_args_list[1].args[0])

    def test_build_reports_first_compiler_diagnostic_with_output_cap(self):
        self.fake_tools()
        bm.new({"name": "blink", "board": "pico_w"})
        failure = "ninja progress\nFAILED: blink\n" + "command" * 5000
        failure += "\nmain.c:3:5: error: expected ';'\n" + "note\n" * 8000
        replies = [SimpleNamespace(returncode=0, stdout=""), SimpleNamespace(returncode=1, stdout=failure)]
        with mock.patch.object(bm, "_run", side_effect=replies):
            with self.assertRaises(bm.DeviceError) as error:
                bm.build({"path": "baremetal/blink"})
        self.assertIn("main.c:3:5: error: expected ';'", str(error.exception))
        self.assertNotIn("ninja progress", str(error.exception))
        self.assertLess(len(str(error.exception)), 12100)

    def test_missing_dependencies_have_install_instructions(self):
        text = bm.doctor({})
        self.assertIn("gcc: missing", text)
        self.assertIn("git clone", text)
        if os.name == "nt":
            self.assertIn("curl.exe", text)
        with self.assertRaisesRegex(bm.DeviceError, "Missing gcc"):
            bm.build({"path": "."})

    def test_detects_bootsel_from_info_file(self):
        drive = self.volume("RPI-RP2")
        other = self.volume("RP2350")
        decoy = self.volume("NOT-A-PICO")
        devices = bm.bootsel_drives([drive, other, decoy, drive])
        self.assertEqual([(d.chip, d.drive) for d in devices], [("RP2040", drive), ("RP2350", other)])
        with self.assertRaisesRegex(bm.DeviceError, "Multiple USB Pico"):
            bm._choose(devices)

    def test_copy_validates_chip_before_writing(self):
        target = self.volume("RP2350")
        data = uf2(0xE48BFF59)
        with self.assertRaisesRegex(bm.DeviceError, "RP2040 cannot be flashed on RP2350"):
            bm.copy_uf2(uf2(0xE48BFF56), target, "wrong.uf2")
        self.assertFalse((target / "wrong.uf2").exists())
        bm.copy_uf2(data, target, "blink.uf2")
        self.assertEqual((target / "blink.uf2").read_bytes(), data)

    def test_flash_waits_for_bootsel_detach_and_c_console(self):
        drive = self.volume("RPI-RP2")
        target = bm.Board("BOOTSEL", "RP2040", drive=drive)
        image = self.project / "blink.uf2"
        data = uf2(0xE48BFF56)
        image.write_bytes(data)
        self.serial_boards.return_value = [bm.Board("C program", "RP2040", port="COM7")]
        with mock.patch.object(bm, "detect_board", return_value=target), \
                mock.patch.object(bm, "_volume_chip", side_effect=["RP2040", ""]):
            result = bm.flash({"path": "blink.uf2"})
        self.assertIn("BOOTSEL drive detached", result)
        self.assertIn("C program connected on COM7", result)
        self.assertEqual((drive / "blink.uf2").read_bytes(), data)

    def test_vendor_only_flash_passes_verified_image_and_requests_reboot(self):
        paths = self.fake_tools()
        chain = bm.discover_toolchain()
        target = bm.Board("BOOTSEL", "RP2350")
        bm._picotool_bootsel.return_value = [target]
        image = self.project / "blink.uf2"
        data = uf2(0xE48BFF59)

        def run(command, timeout):
            self.assertEqual(command[:4], [paths["picotool"], "load", "-v", "-x"])
            self.assertEqual(command[4].read_bytes(), data)
            self.assertEqual(command[5:], ["--pid", "0x000f"])
            return SimpleNamespace(returncode=0, stdout="")

        with mock.patch.object(bm, "_run", side_effect=run) as runner:
            self.assertIn("written and verified", bm._write_image(chain, target, image, data, "auto"))
        runner.assert_called_once()

    def test_image_rejects_corruption_and_multiple_chips(self):
        incomplete = bytearray(uf2(0xE48BFF56))
        struct.pack_into("<I", incomplete, 24, 2)
        for data in (b"bad", uf2(0xE48BFF56)[:-1], uf2(0xE48BFF56) + uf2(0xE48BFF59), incomplete):
            with self.subTest(length=len(data)), self.assertRaises(bm.DeviceError):
                bm._uf2_chip(data)

    def test_firmware_choice_for_both_chips(self):
        for chip, board, filename in (
            ("RP2040", "pico_w", "RPI_PICO_W-"),
            ("RP2350", "pico2_w", "RPI_PICO2_W-"),
        ):
            selected, url, digest = bm.firmware_for(chip)
            self.assertEqual(selected, board)
            self.assertIn("https://micropython.org/resources/firmware/" + filename, url)
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
        with self.assertRaises(bm.DeviceError):
            bm.firmware_for("RP2040", "pico2_w")

    def test_firmware_rejects_bad_digest_and_uses_verified_cache(self):
        data = uf2(0xE48BFF56)
        digest = hashlib.sha256(data).hexdigest()
        chain = bm.discover_toolchain()
        spec = {"pico_w": ("test.uf2", digest)}
        with mock.patch.dict(bm.FIRMWARE, spec), mock.patch.object(bm.urllib.request, "urlopen") as url:
            url.return_value = io.BytesIO(b"invalid")
            with self.assertRaisesRegex(bm.DeviceError, "SHA-256"):
                bm._firmware(chain, "RP2040", None)
            self.assertFalse((self.root / "firmware" / "test.uf2").exists())
            url.return_value = io.BytesIO(data)
            _, path, cached = bm._firmware(chain, "RP2040", None)
            self.assertEqual(path.read_bytes(), data)
            self.assertEqual(cached, data)
            url.reset_mock()
            self.assertEqual(bm._firmware(chain, "RP2040", None)[2], data)
            url.assert_not_called()

    def test_console_stops_on_regex_and_never_transmits(self):
        self.serial_boards.return_value = [bm.Board("C program", "RP2040", port="COM3")]
        fake = mock.MagicMock()
        fake.listen.side_effect = ["blink o", "n\n"]
        with mock.patch.object(bm.backends, "SerialBackend", return_value=fake) as backend:
            text = bm.console({"seconds": 2, "until": "blink on"})
        self.assertIn("[pattern matched]", text)
        backend.assert_called_once_with(passive=True, port="COM3")
        fake.close.assert_called_once_with(force=True)
        fake.exec.assert_not_called()

    def test_software_bootsel_paths(self):
        self.fake_tools()
        chain = bm.discover_toolchain()
        target = bm.Board("BOOTSEL", "RP2040")
        fake = mock.MagicMock()
        fake.exec.return_value = ("THONNY_AI_BOARD=Raspberry Pi Pico W with RP2040\n", "", True)
        wifi = SimpleNamespace(name="wifi", close=mock.Mock())
        with mock.patch.object(bm.backends, "_session", wifi), \
                mock.patch.object(bm.backends, "SerialBackend", return_value=fake), \
                mock.patch.object(bm, "_await_bootsel", return_value=target):
            result = bm._enter_bootsel(chain, bm.Board("MicroPython", port="COM3"), "RP2040")
        self.assertIs(result, target)
        fake.reboot_to_bootsel.assert_called_once()
        wifi.close.assert_not_called()
        current = bm.Board("C program", "RP2040", port="COM3")
        self.serial_boards.return_value = [current]
        with mock.patch.object(bm, "_run") as run, mock.patch.object(bm, "_await_bootsel",
                side_effect=[None, target]), mock.patch.object(serial, "Serial") as port:
            self.assertIs(bm._enter_bootsel(chain, current, "RP2040"), target)
        self.assertEqual(run.call_args.args[0][1:4], ["reboot", "-u", "-f"])
        port.assert_called_once_with("COM3", 1200, timeout=0.1)

    def test_restore_requires_usb_in_wifi_mode(self):
        with mock.patch.dict(os.environ, {"THONNY_AI_TRANSPORT": "wifi"}):
            with self.assertRaisesRegex(bm.DeviceError, "BOOTSEL is unavailable over Wi-Fi"):
                bm.restore({})

    def test_restore_waits_for_raw_repl(self):
        target = bm.Board("BOOTSEL", "RP2350")
        mp = bm.Board("MicroPython", port="COM7")
        with mock.patch.object(bm, "detect_board", return_value=target), \
                mock.patch.object(bm, "_firmware", return_value=("pico2_w", Path("test.uf2"), b"image")), \
                mock.patch.object(bm, "_write_image", return_value="written"), \
                mock.patch.object(bm, "_probe_repl", return_value="pico2_w") as repl:
            self.serial_boards.return_value = [mp]
            self.assertIn("raw REPL verified on COM7", bm.restore({}))
        repl.assert_called_once_with(mp)

    def test_mcp_annotations(self):
        listed = {tool["name"]: tool for tool in handle({"method": "tools/list"})["tools"]}
        for name in ("bm_flash", "bm_restore_micropython"):
            self.assertTrue(listed[name]["annotations"]["destructiveHint"])
        self.assertTrue(listed["bm_console"]["annotations"]["readOnlyHint"])


class BareMetalBridgeTest(unittest.TestCase):
    def test_requires_approval_before_releasing_the_port(self):
        host = mock.Mock()
        core = BridgeCore(host, require_approval=lambda: True, approval_timeout=0)
        op = {"id": "flash", "kind": "bm_flash"}
        with self.assertRaisesRegex(BridgeError, "denied"):
            core.handle("POST", "/bm_claim", {"op": op, "port": "COM3"})
        host.claim_baremetal.assert_not_called()

    def test_restoration_excludes_concurrent_handoff_and_releases_claim(self):
        host = mock.Mock()
        core = BridgeCore(host)
        restoration = {"id": "restore", "kind": "bm_restore_micropython"}
        flash = {"id": "flash", "kind": "bm_flash"}
        core.handle("POST", "/bm_claim", {"op": restoration, "port": "COM3"})
        with self.assertRaisesRegex(BridgeError, "in progress"):
            core.handle("POST", "/bm_claim", {"op": flash, "port": "COM3"})
        core.handle("POST", "/bm_resume", {"op": restoration})
        host.resume_micropython.assert_called_once()
        core.finish(restoration, True, "restored")
        core.handle("POST", "/bm_claim", {"op": flash, "port": "COM3"})
