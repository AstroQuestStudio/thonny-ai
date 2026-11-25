# Bare-metal C/C++ on Pico W and Pico 2 W

The bare-metal MCP tools create and build a Pico SDK C/C++ project, replace the firmware over USB, read the application's USB serial output, and restore MicroPython. Connect one target Pico with a USB data cable. Use the standard MicroPython or Pico SDK USB interfaces for software reboot. Disconnect other Picos before flashing.

| Board | Chip | Pico SDK `PICO_BOARD` | BOOTSEL volume |
| --- | --- | --- | --- |
| Raspberry Pi Pico W | RP2040 | `pico_w` | `RPI-RP2` |
| Raspberry Pi Pico 2 W | RP2350 (Arm) | `pico2_w` | `RP2350` |

[Pico board and BOOTSEL documentation](https://www.raspberrypi.com/documentation/microcontrollers/pico-series.html) · [Pico SDK 2.3.1](https://github.com/raspberrypi/pico-sdk/releases/tag/2.3.1) · [Raspberry Pi USB product allocations](https://github.com/raspberrypi/usb-pid)

## Portable Windows setup

`bm_doctor` prints tool paths, versions, SDK status, detected USB mode and PowerShell commands for missing components. The default directory is `%USERPROFILE%\Documents\pico-toolchain`. Set `THONNY_AI_TOOLCHAIN` for the MCP server, or configure the project in `.thonny-ai.json`.

```json
{
  "baremetal": { "toolchain_dir": "C:/Users/you/Documents/pico-toolchain" }
}
```

The environment variable takes precedence. Relative paths in the configuration resolve from the project directory. Builds add the tools to the CMake subprocess's `PATH` and leave the system `PATH` alone. Install the x64 portable archives and extract them with the layout below. The Pico SDK's checked-out submodules provide TinyUSB and the CYW43 driver. The SDK uses `picotool` to produce UF2s and `pioasm` to assemble the CYW43 PIO program. Keep their CMake package files alongside the executables.

| Component | Tested version | Path within `pico-toolchain` |
| --- | --- | --- |
| Arm GNU Toolchain | 14.3.Rel1 (GCC 14.3.1) | `bin/arm-none-eabi-gcc.exe` |
| CMake | 3.31.12 | `cmake-3.31.12-windows-x86_64/bin/cmake.exe` |
| Ninja | 1.13.2 | `ninja/ninja.exe` |
| Pico SDK | 2.3.1 | `pico-sdk/` |
| picotool | 2.3.1 | `picotool/picotool.exe` |
| pioasm | 2.3.1 | `pioasm/pioasm.exe` |

[Arm GNU Toolchain downloads](https://developer.arm.com/downloads/-/arm-gnu-toolchain-downloads) · [CMake downloads](https://cmake.org/download/) · [Ninja release](https://github.com/ninja-build/ninja/releases/tag/v1.13.2) · [Portable Raspberry Pi tools](https://github.com/raspberrypi/pico-sdk-tools/releases/tag/v2.3.1-0)

## MCP workflow

1. Call `bm_doctor`. The tool uses USB descriptors and BOOTSEL drive metadata. If the MicroPython descriptor is generic, supply the model to `bm_new`. Flashing from MicroPython confirms the exact model through `os.uname().machine` before rebooting.
2. Call `bm_new` with `{"name":"blink","board":"pico_w"}` or use `pico2_w`. The generated `baremetal/blink/` contains `CMakeLists.txt`, `main.c`, and the SDK's `pico_sdk_import.cmake`. The blink uses the CYW43 onboard LED and prints `blink on` / `blink off` over USB CDC.
3. Call `bm_build` with `{"path":"baremetal/blink"}`. This configures CMake with Ninja and returns the UF2 path under `build/<board>/`. To build another compatible CMake project, pass `board` if its `CMakeLists.txt` does not declare it. Build projects whose CMake code you trust.
4. Back up MicroPython project files with `pull` before changing firmware. For a rover, isolate motor power during firmware changes. Call `bm_flash` with `{"path":"baremetal/blink/build/pico_w/blink.uf2"}`. The tool checks the UF2 family and reports the reboot and any USB CDC interface it sees.
5. Call `bm_console` with `{"seconds":10,"until":"blink on"}`. It passively reads the Pico SDK CDC interface at 115200 baud. `until` is optional and uses Python regular expressions. The supplied C program uses the SDK's default reset interfaces.
6. Call `bm_restore_micropython` with `{}` or pass `{"board":"pico_w"}` or `{"board":"pico2_w"}`. The tool checks the chip, flashes the matching official UF2, and verifies the board name over the raw MicroPython REPL. Use `sync` to redeploy saved project files when needed.

With the matching Thonny plugin running, flash and restore follow the panel's approval setting. Thonny releases the Pico so the MCP server can own USB during C mode. The plugin suspends automatic reconnection until it verifies the restored MicroPython REPL and requests a Thonny restart. Standalone MCP also works with the serial fallback enabled. For a manual recovery after an interrupted operation, use the BOOTSEL button and retry the appropriate bare-metal tool. Use Thonny's Stop/Restart once a known MicroPython firmware is running.

## BOOTSEL and flash confirmation

- MicroPython enters BOOTSEL with `machine.bootloader()`. The USB REPL disconnects immediately, so the tool waits for BOOTSEL instead of waiting for a final REPL prompt. [MicroPython RP2 deployment](https://github.com/micropython/micropython/blob/v1.29.0/ports/rp2/README.md)
- For a running C program, `picotool reboot -u -f` first tries the SDK's USB vendor reset interface. If the Pico stays in application mode, opening and closing its USB CDC port at 1200 baud can trigger the SDK reset hook. Those paths depend on what the running program enabled. If neither path works, hold BOOTSEL while plugging in the Pico. [picotool reboot documentation](https://github.com/raspberrypi/picotool/tree/2.3.1#reboot) · [Pico SDK USB reset configuration](https://github.com/raspberrypi/pico-sdk/blob/2.3.1/src/rp2_common/pico_usb_reset/include/pico/usb_reset_config.h)
- In `auto` mode, the tool prefers copying the UF2 to the mounted BOOTSEL volume. It checks `INFO_UF2.TXT` and waits for the drive to detach after the copy. With only the vendor interface available, it uses `picotool load -v -x` to verify flash and request a reboot. Set `method` to `copy` or `picotool` to select a path. A Windows USB driver may be needed for direct `picotool` device access. The mass-storage path works with the OS's standard storage driver. [Pico UF2 flashing](https://www.raspberrypi.com/documentation/microcontrollers/pico-series.html) · [picotool load and Windows drivers](https://github.com/raspberrypi/picotool/tree/2.3.1)
- A C program with USB CDC disabled can boot without exposing a COM port. In that case, `bm_flash` reports the available write/reboot evidence and leaves application startup unverified. Use `bm_console` with a program that enables USB CDC.

## MicroPython recovery image

The tool pins the official Arm release `v1.29.0` and one SHA-256 per board. Every download and cache hit is checked against that digest and the expected UF2 chip family. The cache is `firmware/` under `toolchain_dir`. The initial download requires HTTPS access to MicroPython's servers. The versioned URLs and digests are in `thonny_ai_mcp/baremetal.py`. BOOTSEL identifies the chip family, so restoration assumes its supported wireless Pico model when the running firmware cannot report a board name.

[Official Pico W firmware](https://micropython.org/download/RPI_PICO_W/) · [Official Pico 2 W Arm firmware](https://micropython.org/download/RPI_PICO2_W/)

## Optional SWD

Connect the Raspberry Pi Debug Probe's SC, GND and SD to the Pico's SWCLK, GND and SWDIO and power the Pico separately. Use Raspberry Pi's OpenOCD build with CMSIS-DAP. From the build directory, upload the ELF for the chosen target.

```powershell
openocd -f interface/cmsis-dap.cfg -f target/rp2040.cfg -c "adapter speed 5000" -c "program blink.elf verify reset exit"
openocd -f interface/cmsis-dap.cfg -f target/rp2350.cfg -c "adapter speed 5000" -c "program blink.elf verify reset exit"
```

Use the first line for Pico W and the second for Pico 2 W. For source debugging, start OpenOCD without the `program` command and attach `arm-none-eabi-gdb` to its local GDB server. The MCP tools use USB, and SWD is a separate recovery and debugging route. [Raspberry Pi Debug Probe](https://www.raspberrypi.com/documentation/microcontrollers/debug-probe.html) · [Getting started guide](https://pip-assets.raspberrypi.com/categories/609-microcontroller-boards/documents/RP-008276-DS-5-getting-started-with-pico.pdf)
