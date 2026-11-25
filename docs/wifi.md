# Control a Pico W over Wi-Fi

thonny-ai can use MicroPython WebREPL to run code, stream output, transfer files,
and interrupt a program while the Pico W is powered independently of USB.

## Set up the board over USB

1. Use the official MicroPython **RPI_PICO_W** firmware. The v1.26.1 and v1.29.0
   Pico W manifests include the networking bundle, which freezes WebREPL into
   these builds. The rp2 port also enables the native WebREPL module. These builds
   need no `mip.install`. On a custom image, check `import network, webrepl` over
   USB. If the native `_webrepl` module is missing, use the official firmware or
   enable it in the build. [Official firmware downloads](https://micropython.org/download/RPI_PICO_W/),
   [v1.26.1 Pico W manifest](https://github.com/micropython/micropython/blob/v1.26.1/ports/rp2/boards/RPI_PICO_W/manifest.py),
   [v1.26.1 networking bundle](https://github.com/micropython/micropython-lib/blob/34c4ee1647ac4b177ae40adf0ec514660e433dc0/micropython/bundles/bundle-networking/manifest.py),
   [v1.29.0 Pico W manifest](https://github.com/micropython/micropython/blob/v1.29.0/ports/rp2/boards/RPI_PICO_W/manifest.py),
   [v1.29.0 networking bundle](https://github.com/micropython/micropython-lib/blob/ee4bb8ff139e24c42b739935fbd8ec7c4d061e02/micropython/bundles/bundle-networking/manifest.py),
   [rp2 firmware options](https://github.com/micropython/micropython/blob/v1.29.0/ports/rp2/mpconfigport.h#L226-L243).
2. Close Thonny and connect the Pico W by USB. Supply the values below in the
   MCP server process environment using your local launcher or secret manager.
   Restart the MCP server so it inherits the environment. Use a trusted local
   network with 2.4 GHz Wi-Fi and allow the PC to reach the board.

   | Variable | Value |
   | --- | --- |
   | `THONNY_AI_WIFI_SSID` | Name of the network the Pico W should join |
   | `THONNY_AI_WIFI_PASSWORD` | Wi-Fi network password |
   | `THONNY_AI_WEBREPL_PASSWORD` | Separate WebREPL password of 4–9 UTF-8 bytes, without NUL or line breaks |

   These PowerShell prompts set the values without placing passwords on command
   lines. Start your MCP client from the same shell.

   ```powershell
   $env:THONNY_AI_WIFI_SSID = Read-Host 'Wi-Fi network name'
   $env:THONNY_AI_WIFI_PASSWORD = [System.Net.NetworkCredential]::new('', (Read-Host 'Wi-Fi password' -AsSecureString)).Password
   $env:THONNY_AI_WEBREPL_PASSWORD = [System.Net.NetworkCredential]::new('', (Read-Host 'WebREPL password' -AsSecureString)).Password
   ```

3. Call `wifi_setup` with an empty argument object. The tool stops the current
   program, saves `/webrepl_cfg.py` and `/wifi_boot.py` on the board, and adds or
   updates a marked block in `/boot.py`. It preserves the rest of `boot.py`.
   The board gets up to ten seconds to join the network and returns its IP if
   WebREPL starts. Rerun `wifi_setup` over USB to change the network or passwords.
   A custom `wifi_boot.py` must be renamed before setup. If the board already
   starts WebREPL elsewhere in `boot.py`, consolidate that startup into the
   managed block so the listener starts once.
4. Configure the returned IP in `THONNY_AI_WEBREPL_HOST` or in `.thonny-ai.json`
   in the project directory. `THONNY_AI_WEBREPL_HOST` overrides `webrepl.host`.
   `webrepl.port` defaults to 8266. `webrepl.password_env` overrides the default
   name of the password variable for both setup and login.

   ```json
   {
     "webrepl": {
       "host": "192.168.1.42",
       "port": 8266,
       "password_env": "THONNY_AI_WEBREPL_PASSWORD"
     }
   }
   ```

   For root deployments that use `sync` with deletion, include `boot.py` and
   `wifi_boot.py` in the project's `ignore` list to preserve the bootstrap on the
   board. Keep the Wi-Fi password variables for provisioning and the WebREPL
   password variable for every subsequent Wi-Fi session.

## Select a transport

| `THONNY_AI_TRANSPORT` | Behavior |
| --- | --- |
| `auto` (default) | Uses Thonny when its bridge is open. Otherwise uses USB when detected, then Wi-Fi when configured. |
| `usb` | Uses direct USB serial. Close Thonny to free the port. `THONNY_AI_SERIAL_PORT` can specify the port. |
| `wifi` | Uses WebREPL directly, including while USB is plugged in. |

`THONNY_AI_SERIAL_FALLBACK=0` disables automatic USB selection. A direct session
that owns a running program stays open between `start`, `monitor`, and `interrupt`
so that the next tool continues using the same REPL. `status` reports the mode,
USB detection, and whether it can authenticate to the configured Wi-Fi endpoint.
Wi-Fi status uses the active WebREPL session when available. A WebREPL listener
allows one active client. End an active session before switching transports or
connecting a second client. [MicroPython WebREPL documentation](https://github.com/micropython/webrepl#webrepl-file-transfer).

WebREPL uses unencrypted `ws://` and gives an authenticated client full access to
the board. Keep the WebREPL port on a trusted LAN, with no Internet forwarding.
The board stores both passwords in `/webrepl_cfg.py`. The `read`, `pull`, and `sync`
MCP file tools exclude the credential file, while arbitrary `exec` code still has
the board's full filesystem access. A dropped Wi-Fi connection can leave a
program running. Keep the rover's local failsafe or physical stop available.
[WebREPL transport notes](https://github.com/micropython/webrepl#technical-details).

## Validate on hardware

1. With the rover stationary and the wheels clear, run `wifi_setup` over USB and
   check the returned IP and the preserved contents of `boot.py`. Run it again and
   check that there is still one managed block.
2. Select `wifi`, then call `status` and `exec` with `print(2 + 2)`. Unplug the
   data cable while keeping the board powered and repeat both calls. Check
   `auto` selection with Thonny closed and USB unplugged.
3. Use `write` and `read` to round-trip a test file. Run a harmless loop with
   `start`, observe its output with `monitor`, and check that `interrupt` stops
   it and that a new `exec` succeeds. Repeat with a short `exec` timeout.
4. Call `reset`, wait for the Pico W to boot, then reconnect and run `exec` again.
   Power-cycle the board and repeat. Confirm that `main.py` starts as expected
   and that `status` reaches the new IP if DHCP changes it.
5. With the wheels clear, test the rover's local response to a Wi-Fi loss before
   driving it untethered. The software tests use a simulated board and loopback
   sockets. Interrupt delivery during active code depends on the firmware.
   [MicroPython's rp2 WebREPL interrupt change](https://github.com/micropython/micropython/pull/13200).

## Protocol and reuse

The client opens TCP, sends an HTTP/1.1 WebSocket upgrade for `/` with a random
`Sec-WebSocket-Key`, and verifies the 101 response and `Sec-WebSocket-Accept`.
Client frames are masked. Incoming REPL data is read as a stream across
WebSocket frames and TCP reads, and ping frames receive pong replies.
WebREPL sends a `Password: ` prompt, accepts the password followed by CR or LF,
and signals success or denial. Raw REPL commands go in text frames. Ctrl-A
enters raw mode, Ctrl-D submits code, and the reply carries `OK`, stdout and
stderr separated by Ctrl-D, then the `>` prompt. [RFC 6455](https://www.rfc-editor.org/rfc/rfc6455),
[WebREPL firmware handshake](https://github.com/micropython/micropython-lib/blob/ee4bb8ff139e24c42b739935fbd8ec7c4d061e02/micropython/net/webrepl/webrepl.py#L19-L69),
[Firmware login](https://github.com/micropython/micropython/blob/v1.29.0/extmod/modwebrepl.c#L182-L205),
[MicroPython raw REPL](https://docs.micropython.org/en/v1.29.0/reference/repl.html#raw-mode-and-raw-paste-mode).

The optional native file protocol uses binary frames and an 82-byte
little-endian `WA` header (`<2sBBQLH64s`) with an opcode, flags, offset, size,
filename length, and a 64-byte filename field. PUT (1) and GET (2) receive
`WB` status responses. PUT sends data blocks, and GET requests each chunk with a
zero byte and receives a 16-bit length followed by data. thonny-ai's `read`
and `write` use the same raw REPL filesystem operations over USB and Wi-Fi.
[Official WebREPL reference client](https://github.com/micropython/webrepl/blob/master/webrepl_cli.py#L85-L215).

`WebSocket` holds the wire protocol and authentication, and `RawReplBackend`
holds the board operations. A future phone interface can call through the PC
that already owns the WebREPL session. This keeps one client connected to the
Pico W.
