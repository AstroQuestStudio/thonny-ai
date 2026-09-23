# Contributing to thonny-ai

Thanks for helping! thonny-ai is small on purpose: standard library + pyserial, no framework.
Please keep it that way.

## Good first contributions

- **A new UI language**: copy the `"en"` block in `thonnycontrib/thonny_ai/i18n.py`, translate,
  open a PR. Thonny picks it from its language setting.
- **A hardware report**: run [docs/hardware-testing.md](docs/hardware-testing.md) on your board
  (ESP32, Pico 2, micro:bit…) and open an issue with the *Hardware report* template.
- **A new MCP tool**: add a function and a `TOOLS` entry in `thonny_ai_mcp/tools.py`, plus a test
  in `tests/test_mcp.py` using the fake board.

Issues labeled `good first issue` are a good place to start.

## Setup

```bash
git clone https://github.com/AstroQuestStudio/thonny-ai
cd thonny-ai
python -m pip install -e ".[test]"
```

To try your changes inside Thonny, run `pip install -e .` from Thonny's *Tools → Open system
shell…*, then restart Thonny.

## Tests

```bash
PYTHONPATH=tests python -m unittest discover -s tests -v   # unit tests, no board needed
python tests/e2e_thonny.py /path/to/thonny.exe             # drives a real Thonny window
```

- Unit tests use a fake board (sandboxed filesystem) and a fake serial port speaking MicroPython's
  raw REPL, so everything except real USB timing is covered.
- If you touch `reconnect.py`, `host.py` or `backends.py`, please also run the relevant parts of
  the [hardware checklist](docs/hardware-testing.md).

## Code layout

| Path | Role |
|---|---|
| `thonnycontrib/thonny_ai/core.py` | Bridge core: operations, approval, HTTP API. No Tk, fully unit-tested |
| `thonnycontrib/thonny_ai/host.py` | Adapter to Thonny internals (runs everything on Tk's main thread) |
| `thonnycontrib/thonny_ai/panel.py` | *AI Assistant* panel |
| `thonnycontrib/thonny_ai/reconnect.py` | Auto-reconnect watcher |
| `thonny_ai_mcp/server.py` | MCP JSON-RPC over stdio |
| `thonny_ai_mcp/tools.py` | MCP tools |
| `thonny_ai_mcp/backends.py` | Thonny bridge client and serial raw REPL fallback |

## Principles

1. **The human stays in control.** Anything the AI does must be visible in Thonny and deniable
   when approval is on. Never add a way around the approval gate.
2. **Never fight the user.** For example, auto-reconnect does not undo a deliberate *Disconnect*.
3. **No new runtime dependency** without discussing it in an issue first: the MCP server must keep
   running with the Python bundled in Thonny.
4. Keep Thonny responsive: never block Tk's main thread; use `MainThread.call()` from other threads.

## Pull requests

- One topic per PR, with a test when behaviour changes.
- Describe how you tested it (unit, e2e, which board).
- Be kind in reviews. See the [Code of Conduct](CODE_OF_CONDUCT.md).
