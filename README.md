# thonny-ai

[![tests](https://github.com/AstroQuestStudio/thonny-ai/actions/workflows/tests.yml/badge.svg)](https://github.com/AstroQuestStudio/thonny-ai/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![MCP](https://img.shields.io/badge/MCP-server-8A2BE2)
![MicroPython](https://img.shields.io/badge/MicroPython-Pico%20%7C%20ESP32-2b2728)

**Let an AI assistant drive Thonny and your MicroPython board: visibly, safely, hands-free.**

thonny-ai is a [Thonny](https://thonny.org) plugin plus an [MCP](https://modelcontextprotocol.io)
server. Claude Code (or any MCP client) can run code on your Raspberry Pi Pico / ESP32, watch a
running robot, read and write the board's files and deploy your project. Everything happens
**inside Thonny's shell, in front of you**, and you can require your approval for every action.

![AI code in Thonny's shell](docs/img/shell-echo.png)

![AI Assistant panel with an approval request](docs/img/approval-panel.png)

## Why

Classroom and hobby MicroPython work happens in Thonny, and AI assistants are good at writing
MicroPython. What's missing is the link: copy-pasting code into the shell, clicking Stop/Restart
after every USB hiccup, dragging files onto the board by hand, copying sensor output back to the
chat. thonny-ai removes that friction without taking the human out of the loop.

## Features

| | |
|---|---|
| **Visible AI** | Code sent by the AI appears after `>>>` in Thonny's shell, syntax-highlighted and tinted, exactly as if it had been typed. Teachers and students see every command. |
| **Human in the loop** | *AI Assistant* panel: log of every action (run, write, delete, sync…). Tick **Approve every AI action** and nothing touches the board until you click *Allow*. |
| **Long-running programs** | `start` a robot loop or a sensor stream, `monitor` what it prints while it runs, `interrupt` it. The AI can finally debug code that never returns. |
| **Auto-reconnect** | Replug the board, or survive a USB glitch: Thonny reconnects by itself. No more “Connection lost — use Stop/Restart”. A deliberate *Disconnect* is never undone. |
| **Smart sync** | `sync` uploads a project folder and only sends files whose SHA-256 changed, then restarts `main.py`. |
| **Works without Thonny** | When Thonny is closed, the MCP server talks to the board directly over the serial port (MicroPython raw REPL), and can even listen to `main.py` without stopping it. |
| **One-click setup** | *Tools → Connect Claude Code* registers the MCP server for you. |
| **Zero extra dependency** | Pure standard library + pyserial (already bundled with Thonny). |
| **English / Français** | The UI follows Thonny's language. More languages welcome. |

## Quick start

1. **Install the plugin into Thonny.** In Thonny, open *Tools → Open system shell…* and run:
   ```
   pip install git+https://github.com/AstroQuestStudio/thonny-ai
   ```
   Or download the ready-made **Thonny-AI portable** (Windows) from
   [Releases](https://github.com/AstroQuestStudio/thonny-ai/releases): unzip, run `thonny.exe`.
2. **Restart Thonny.** The *AI Assistant* panel is under *View* (or *Tools → AI Assistant panel*).
3. **Connect your AI.** Click *Connect Claude Code* in the panel. Other MCP clients: see below.
4. Talk to your board:
   > *“Blink the Pico's LED.”*
   > *“Start a loop that prints the distance from the ultrasonic sensor on GP14/GP15, watch it for
   > 10 seconds and tell me if the readings are noisy.”*
   > *“Deploy src/ to the board and check main.py starts without errors.”*

### Other MCP clients

The server runs with Thonny's Python:

```json
{
  "mcpServers": {
    "thonny-ai": {
      "command": "C:/path/to/Thonny/python.exe",
      "args": ["-m", "thonny_ai_mcp"],
      "env": { "THONNY_AI_PROJECT": "C:/path/to/your/project" }
    }
  }
}
```

The same block works for Claude Desktop (`claude_desktop_config.json`), Cursor (`.cursor/mcp.json`)
and any client that speaks MCP over stdio.

## Tools

| Tool | What it does |
|---|---|
| `status` · `info` | How the board is reached (Thonny or serial, port, approval mode) · firmware, CPU, RAM, flash, Wi-Fi |
| `exec` | Run MicroPython code, return prints and tracebacks (a timeout interrupts runaway loops) |
| `start` · `monitor` · `interrupt` | Launch a program that runs forever · read its output while it runs · stop it |
| `run_file` | Run a local `.py` file on the board without saving it there |
| `ls` · `read` · `write` · `rm` | Manage the board's filesystem |
| `sync` | Upload a folder (default `src/`), only changed files, then soft-reset. `dry_run`, `force` |
| `reset` | Soft reset (`boot.py` then `main.py`) |
| `shell` · `open` | Read Thonny's shell (what the user sees) · open a local file in Thonny's editor |

## How it works

```
Claude Code ──MCP/stdio──► thonny_ai_mcp ──┬── Thonny running? ──► HTTP 127.0.0.1:47821 ──► thonny-ai plugin ──► Thonny ──► board
                                           └── Thonny closed   ──► serial raw REPL ─────────────────────────────────────► board
```

- A serial port can be opened by one program only. While Thonny is connected it owns the port,
  so the MCP server goes through Thonny. That is also why everything shows up in its shell.
- File transfers use Thonny's own backend commands (`write_file` / `read_file`), so they don't
  clutter the shell.
- Each MCP tool call is one **operation**: one approval and one row in the panel, even when it
  needs many round-trips (for example a `sync` of 20 files).

## Security model

- The bridge listens on `127.0.0.1` only.
- Every request must carry the `X-Thonny-AI: 1` header, and any request with an `Origin` header is
  rejected, so web pages open in your browser cannot reach it.
- With *Approve every AI action* on, code execution and file changes wait for your click (and are
  denied after 2 minutes without an answer). Read-only actions (`status`, `info`, `ls`, `read`,
  `monitor`, `shell`) are never blocked.
- If the user denies an action, the AI gets an explicit “denied by the user” error.

Found a problem? See [SECURITY.md](SECURITY.md).

## Configuration

| Setting | Where | Default |
|---|---|---|
| Approve every AI action | AI Assistant panel | off |
| Auto-reconnect the board | AI Assistant panel | on |
| Bridge port | `thonny_ai.port` in Thonny's configuration / `THONNY_AI_PORT` for the MCP server | 47821 |
| Project folder for relative paths | `THONNY_AI_PROJECT` | MCP server working directory |
| Disable serial fallback | `THONNY_AI_SERIAL_FALLBACK=0` | enabled |
| Force a serial port | `THONNY_AI_SERIAL_PORT=COM3` | auto-detect |

## Development

```bash
python -m pip install -e ".[test]"
PYTHONPATH=tests python -m unittest discover -s tests -v   # 51 tests, no board needed
python tests/e2e_thonny.py /path/to/thonny.exe             # 21 steps in a real Thonny window
python bundle/build_portable.py thonny-X.Y.Z-windows-portable.zip
```

The unit tests use a fake board (sandboxed filesystem) and a fake serial port that speaks the
MicroPython raw REPL protocol. The end-to-end script opens a real Thonny with its local Python
interpreter and drives it through the MCP tools. Real-board behaviour is checked with the
[hardware checklist](docs/hardware-testing.md).

## Contributing

Contributions are very welcome, from a translation to a new tool. Start with
[CONTRIBUTING.md](CONTRIBUTING.md). Hardware reports for boards other than the Pico W are
especially useful.

## Roadmap

- [ ] PyPI release and listing in Thonny's plugin manager
- [ ] Wi-Fi (WebREPL) connection, for robots that can't stay on a USB cable
- [ ] Plotter and variables snapshot tools for the AI
- [ ] More UI languages
- [ ] Demo GIF

## Français

thonny-ai permet à un assistant IA (Claude Code ou tout client MCP) de piloter Thonny et ta carte
MicroPython : le code de l'IA s'affiche dans la console Thonny comme s'il était tapé, chaque action
peut être soumise à ta validation, l'IA peut lancer un programme qui tourne en boucle et lire ce
qu'il affiche, et Thonny se reconnecte tout seul quand on rebranche la carte. L'interface est en
français si Thonny est en français.

## License

MIT © Mathis Cottereau. Not affiliated with the Thonny project; thonny-ai is a third-party plugin
built on Thonny's public plugin interface.
