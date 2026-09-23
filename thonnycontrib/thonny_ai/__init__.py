"""thonny-ai: lets an AI assistant (Claude Code or any MCP client) drive Thonny and your
MicroPython board, visibly and under the user's control."""

import logging

from thonny import get_shell, get_workbench

from .core import DEFAULT_PORT, BridgeCore, start_server
from .i18n import t

__version__ = "0.1.0"
logger = logging.getLogger(__name__)


def _connect_claude_code():
    from .panel import VIEW_ID

    view = get_workbench().show_view(VIEW_ID)
    if view:
        view.connect_claude()


def _configure_shell_tag():
    from .host import ThonnyHost

    try:
        get_shell()
        ThonnyHost.configure_ai_tag()
    except Exception:
        get_workbench().after(500, _configure_shell_tag)


def load_plugin():
    from .host import ThonnyHost
    from .panel import VIEW_ID, Controller, ThonnyAIView
    from .reconnect import ReconnectWatcher

    wb = get_workbench()
    wb.set_default("thonny_ai.require_approval", False)
    wb.set_default("thonny_ai.auto_reconnect", True)
    wb.set_default("thonny_ai.port", DEFAULT_PORT)
    port = int(wb.get_option("thonny_ai.port"))

    core = BridgeCore(ThonnyHost(), require_approval=lambda: wb.get_option("thonny_ai.require_approval"))
    controller = Controller(core, port)
    ThonnyAIView.controller = controller
    wb.add_view(ThonnyAIView, t("view_title"), "se")
    wb.add_command(
        "thonny_ai_connect_claude",
        "tools",
        t("menu_connect"),
        _connect_claude_code,
        group=180,
    )
    wb.add_command(
        "thonny_ai_show_panel",
        "tools",
        t("menu_panel"),
        lambda: wb.show_view(VIEW_ID),
        group=180,
    )

    try:
        start_server(core, port)
        controller.bridge_running = True
        logger.info("thonny-ai: bridge listening on 127.0.0.1:%s", port)
    except OSError as e:
        logger.warning("thonny-ai: port %s unavailable (%s)", port, e)
        controller.system_event(t("port_busy", port=port, error=e))

    ReconnectWatcher(on_event=controller.system_event)
    wb.after(300, _configure_shell_tag)
