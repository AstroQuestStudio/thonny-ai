# Safety watchdog

The thonny-ai plugin watches stdout and stderr during programs launched from Thonny's editor or shell and through MCP commands such as `start`, `exec`, and `run_file`. It works with the AI Assistant panel closed. Use the **Watchdog** checkbox in the panel to turn it on or off.

The watchdog is on by default. An error pattern or a missed heartbeat requests the same emergency stop as the **STOP** button. Thonny interrupts the program and then runs the project's `safe_stop` or `safe_stop_file` code. If the board disconnects during a run, the watchdog records that it could not send an emergency stop. It triggers at most once per run and rearms on the next run.

## Project settings

In `.thonny-ai.json`, add a `watchdog` object alongside the safe-stop code.

~~~json
{
  "watchdog": {
    "enabled": true,
    "patterns": ["Traceback|MemoryError|OSError: \\[Errno"],
    "heartbeat": "(?m)^ROVER_ALIVE\\r?$",
    "heartbeat_timeout": 5
  }
}
~~~

- `enabled` defaults to `true`.
- `patterns` defaults to a single regex that matches `Traceback`, `MemoryError`, or `OSError: [Errno`. A custom list replaces the defaults. Use `[]` to turn off error-pattern detection.
- `heartbeat` is optional. The example expects the program to print `ROVER_ALIVE` on its own line. Use `null` or an empty string to turn off heartbeat detection.
- `heartbeat_timeout` is a positive number of seconds and defaults to 5. The timer starts when the program starts. Each new heartbeat renews it. Leave enough margin for program startup and normal output delays.

Thonny looks for the closest config with a `watchdog` entry, starting from the active editor's directory and then its working directory and their ancestors. It reloads the project settings before each run or when the watchdog is queried or changed while idle. Invalid watchdog settings leave the previous valid settings active and produce a warning.

## MCP

Use one of these `watchdog` actions.

- `{"action":"status"}` reads the effective settings, run state, and the last 10 triggers in the Thonny session, newest first.
- `{"action":"on"}` and `{"action":"off"}` toggle the watchdog.
- `{"action":"configure","heartbeat":"ROVER_ALIVE","heartbeat_timeout":8}` updates just the supplied settings.

The status includes `tripped` for the most recent run and `config_error` if the project settings were invalid. Changes made by MCP or the panel override the file for the current Thonny session. When manual approval is enabled, MCP changes need approval or existing session trust. Status reads and automatic stops work without approval.

## Operating limits

The watchdog requires Thonny and an execution it can observe. Direct serial MCP mode and autonomous programs running outside a Thonny command are outside its coverage. Keep regexes simple. Split matches can use up to 1,024 previous characters from the same output stream.

The original connection must remain available and responsive for Thonny to interrupt the program and run the project's stop code. Configure that code to put the motor outputs in a safe state. If the link drops, motor outputs can remain energised.
