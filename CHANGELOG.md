# Changelog

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
