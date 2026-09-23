# Hardware validation checklist

The automated tests cover everything that can run without a board (51 unit tests, plus 21
end-to-end steps against a real Thonny using its local Python). This checklist covers what needs
real hardware. Run it before each release, and when you touch `reconnect.py`, `host.py` or
`backends.py`.

Takes about 15 minutes. You need a MicroPython board (tested target: Raspberry Pi Pico W,
MicroPython 1.2x) and Claude Code (or any MCP client) connected to thonny-ai.

Report results in the release PR, or in an issue using the *Hardware report* template.

## 0. Setup

- [ ] Thonny open, interpreter *MicroPython (Raspberry Pi Pico)*, port *auto*
- [ ] *View → AI Assistant* panel visible, shows “AI bridge listening on 127.0.0.1:47821”
- [ ] Ask the AI: “call status then info” → port, firmware version, free RAM/flash are correct

## 1. Visible AI

- [ ] “Blink the onboard LED 5 times” → the code shows after `>>>` with the purple tint, the LED blinks
- [ ] “Run 1/0” → the traceback is in Thonny's shell **and** the AI reports the error
- [ ] Type something in the shell without pressing Enter, then ask the AI to run code → your
      typed text is not overwritten (the AI code runs without echo)

## 2. Approval

- [ ] Tick *Approve every AI action*, ask for `exec` → the panel pops up with the code; *Deny* →
      the AI receives “denied by the user” and nothing runs
- [ ] Ask again, *Allow* → it runs
- [ ] `ls` / `read` / `monitor` never ask for approval
- [ ] *Allow all until Thonny closes* → following actions no longer ask
- [ ] Leave a request unanswered for 2 minutes → denied automatically

## 3. Long-running programs

- [ ] “Start a loop printing the internal temperature every 0.5 s” (`start`) → returns first readings, `[still running]`
- [ ] `monitor` for 5 s → new readings, program still running
- [ ] `interrupt` → “Program stopped”, then `exec print(1)` works

## 4. Files and sync

- [ ] `write` `/test/hello.txt` with accents (é✓), `read` it back identical, `rm` `/test`
- [ ] `sync` a project folder → N uploaded; `sync` again → 0 uploaded, N unchanged
- [ ] Change one file, `sync` → exactly 1 uploaded; `main.py` restarts
- [ ] `sync dry_run=true` → lists changes, board untouched

## 5. Auto-reconnect

- [ ] Unplug the board while Thonny is connected → “Connection lost”; plug it back → within
      ~3 s Thonny reconnects by itself, the panel logs “Board detected on COMx”
- [ ] Start Thonny with the board unplugged, then plug it → connects automatically
- [ ] *Run → Disconnect* while plugged → Thonny stays disconnected (no auto-reconnect)
- [ ] Untick *Auto-reconnect the board*, unplug/replug → no reconnection

## 6. Without Thonny (serial fallback)

- [ ] Close Thonny. `status` → “Thonny is not running”; `exec print(2+2)` → `4 (via serial)`
- [ ] `sync` → soft reset, `main.py` runs on its own
- [ ] `monitor` → shows `main.py`'s prints **without** stopping it
- [ ] `start` an endless loop, `monitor` twice, `interrupt` → stops
- [ ] Open Thonny while a `start`ed program runs → Thonny connects (the port is released)

## Results log

| Date | Board / firmware | OS | Thonny | Result | Notes |
|---|---|---|---|---|---|
| | | | | | |
