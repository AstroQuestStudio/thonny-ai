# Changelog

## 0.2.0 — unreleased

Robot-oriented release.

- **`stop`** — emergency stop: interrupts the program and runs the project's safe-stop code
  (`safe_stop` in `.thonny-ai.json`, or `THONNY_AI_SAFE_CODE`) so motors and actuators end up off.
  Ctrl-C alone leaves PWM outputs running on a Pico. `interrupt` and a timed-out `exec` /
  `run_file` run the safe-stop code too. `stop` and `interrupt` are never held up by the approval
  panel.
- **`.thonny-ai.json`** — per-project settings: `local_dir`, `remote_dir`, `ignore`, `safe_stop`,
  `safe_stop_file`, `max_output`.
- **`pull`** — download board files into a local folder (backup, recover code that only exists on
  the board); never overwrites a different local file unless `overwrite` is set; refuses board
  paths that would escape the local folder.
- **`sync`** — `ignore` patterns and `delete` (remove board files that no longer exist locally,
  respecting `ignore`; use `dry_run` first).
- **`monitor`** — `until` (regular expression): returns as soon as the text appears, instead of
  waiting for the whole duration.
- **`i2c_scan`** and **`gpio`** — check wiring without writing code: devices found on an I2C bus
  (named: IMU, OLED, distance sensors…), and digital / pull-up / ADC reads or an output write.
- Output of one call is capped (default 20000 characters, head and tail kept) so a program that
  prints in a loop cannot flood the AI's context.
- MCP tool `title` and `annotations` (`readOnlyHint`, `destructiveHint`) so clients can
  auto-approve read-only tools and warn on destructive ones.
- **New panel**: live board status line (interpreter, port, connected, ready/running) with a status
  dot; red **STOP** button and *Tools → Emergency stop robot* (interrupts, then runs the project's
  safe-stop code found in `.thonny-ai.json`); approval card that tells whether the action changes the
  board; colour-coded log (readable on light and dark themes) with text filter, *Errors only*, *Clear*,
  and a footer with counts; details window with Code / Result tabs, monospaced, with Copy; right-click
  menu to copy code or result.
- 116 tests (was 54).

## 0.1.1 — 2026-09-23

- Fix: a Thonny that is open but briefly busy is no longer mistaken for a closed one (which made
  the MCP server fall back to the serial port).
- Fix: values echoed by Thonny's local Python no longer contain Thonny's internal object-link markup.
- Fix: silent operations (file listing, sync…) no longer leave empty `>>>` lines in the shell.
- Fix: the *Status* column of the AI Assistant panel stays visible when the panel is narrow.
- Docs: demo GIF, FAQ, compatibility table, `llms.txt` for AI assistants.

## 0.1.0 — 2026-09-23

First release.

- Thonny plugin with a local bridge (127.0.0.1 only, browser requests refused).
- AI code shows up in Thonny's shell after `>>>`, highlighted, as if typed.
- **AI Assistant** panel: log of every AI action, optional approval of each action
  (allow / deny / allow until Thonny closes).
- Auto-reconnect when the board is plugged back in or after a USB glitch.
- MCP server (`python -m thonny_ai_mcp`), no dependency besides pyserial: `status`, `info`,
  `exec`, `start`, `monitor`, `interrupt`, `run_file`, `ls`, `read`, `write`, `rm`,
  `sync` (SHA-256 diff), `reset`, `shell`, `open`.
- Long-running programs: `start` + `monitor` + `interrupt`, through Thonny or directly over serial;
  `monitor` can listen to `main.py` without stopping it.
- Direct serial fallback (MicroPython raw REPL) when Thonny is closed.
- One-click “Connect Claude Code”.
- English and French UI.
