"""Emergency stop from Thonny's own window (STOP button, Tools menu).

The MCP server reads `.thonny-ai.json` from THONNY_AI_PROJECT; Thonny does not know that
variable, so the plugin looks for the file next to the file open in the editor, then in the
folders above it and above Thonny's working directory (a portable Thonny usually lives inside
the project)."""

import json
import os

CONFIG_NAME = ".thonny-ai.json"


def project_dirs(workbench):
    dirs = []
    try:
        editor = workbench.get_editor_notebook().get_current_editor()
        filename = editor.get_filename() if editor else None
        if filename:
            dirs.append(os.path.dirname(filename))
    except Exception:
        pass
    dirs.append(os.getcwd())
    return dirs


def _candidates(start_dirs):
    seen = set()
    for start in start_dirs:
        if not start:
            continue
        path = os.path.abspath(start)
        while True:
            if path not in seen:
                seen.add(path)
                yield path
            parent = os.path.dirname(path)
            if parent == path:
                break
            path = parent


def _project_configs(start_dirs):
    for folder in _candidates(start_dirs):
        path = os.path.join(folder, CONFIG_NAME)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                yield path, data
        except (OSError, ValueError):
            continue


def find_watchdog_config(start_dirs):
    for _, data in _project_configs(start_dirs):
        if "watchdog" in data:
            return data["watchdog"]
    return {}


def find_safe_stop(start_dirs):
    """Returns (code, source_path) of the nearest project config with a safe-stop code,
    or (None, None). A broken file is skipped: a stop must never fail on a typo."""
    env_code = os.environ.get("THONNY_AI_SAFE_CODE")
    if env_code:
        return env_code, "THONNY_AI_SAFE_CODE"
    for path, data in _project_configs(start_dirs):
        try:
            if data.get("safe_stop"):
                return str(data["safe_stop"]), path
            if data.get("safe_stop_file"):
                with open(os.path.join(os.path.dirname(path), data["safe_stop_file"]), encoding="utf-8") as f:
                    return f.read(), path
        except (OSError, ValueError):
            continue
    return None, None
