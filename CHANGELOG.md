# Changelog

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
