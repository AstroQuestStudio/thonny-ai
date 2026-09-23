"""UI translations (follows Thonny's language setting, English by default)."""

from thonny import get_workbench

_STRINGS = {
    "en": {
        "view_title": "AI Assistant",
        "menu_connect": "Connect Claude Code (thonny-ai)…",
        "menu_panel": "AI Assistant panel",
        "bridge_on": "● AI bridge listening on 127.0.0.1:{port}",
        "bridge_off": "○ AI bridge disabled (port {port} unavailable)",
        "connect_claude": "Connect Claude Code",
        "require_approval": "Approve every AI action",
        "auto_reconnect": "Auto-reconnect the board",
        "approval_title": "The AI asks for permission",
        "allow": "Allow",
        "deny": "Deny",
        "allow_session": "Allow all until Thonny closes",
        "more_pending": "  (+{n} waiting)",
        "col_time": "Time",
        "col_action": "Action",
        "col_detail": "Detail",
        "col_status": "Status",
        "result": "──── result ────",
        "copied": "(Command copied to the clipboard.)",
        "port_busy": "Port {port} unavailable: AI bridge disabled ({error})",
        "reconnecting": "Board detected on {port}: reconnecting automatically",
        "claude_missing": "Claude Code was not found in PATH.\n\n"
        "Install it, or run this command in a terminal:\n\n{cmd}",
        "claude_failed": "Registration failed:\n\n{output}",
        "claude_ok": "MCP server “thonny-ai” registered in Claude Code.\n\n"
        "Start a new Claude Code session (or type /mcp): the thonny-ai tools "
        "(exec, sync, read, write…) will be available.",
        "kind.exec": "Run code",
        "kind.start": "Start program",
        "kind.monitor": "Monitor",
        "kind.info": "Board info",
        "kind.run_file": "Run file",
        "kind.write": "Write",
        "kind.read": "Read",
        "kind.ls": "List",
        "kind.rm": "Delete",
        "kind.sync": "Sync",
        "kind.interrupt": "Interrupt",
        "kind.reset": "Reset",
        "kind.shell": "Shell",
        "kind.open": "Open",
        "kind.status": "Status",
        "kind.system": "System",
        "status.pending": "⏳ waiting",
        "status.approved": "▶ running",
        "status.denied": "⛔ denied",
        "status.done": "✔ ok",
        "status.failed": "✖ error",
        "status.info": "ℹ",
    },
    "fr": {
        "view_title": "Assistant IA",
        "menu_connect": "Connecter Claude Code (thonny-ai)…",
        "menu_panel": "Panneau Assistant IA",
        "bridge_on": "● Pont IA à l'écoute sur 127.0.0.1:{port}",
        "bridge_off": "○ Pont IA désactivé (port {port} indisponible)",
        "connect_claude": "Connecter Claude Code",
        "require_approval": "Valider chaque action de l'IA",
        "auto_reconnect": "Reconnexion auto de la carte",
        "approval_title": "L'IA demande l'autorisation",
        "allow": "Autoriser",
        "deny": "Refuser",
        "allow_session": "Tout autoriser jusqu'à la fermeture",
        "more_pending": "  (+{n} en attente)",
        "col_time": "Heure",
        "col_action": "Action",
        "col_detail": "Détail",
        "col_status": "État",
        "result": "──── résultat ────",
        "copied": "(Commande copiée dans le presse-papiers.)",
        "port_busy": "Port {port} indisponible : pont IA désactivé ({error})",
        "reconnecting": "Carte détectée sur {port} : reconnexion automatique",
        "claude_missing": "Claude Code est introuvable dans le PATH.\n\n"
        "Installe-le, ou lance cette commande dans un terminal :\n\n{cmd}",
        "claude_failed": "Échec de l'enregistrement :\n\n{output}",
        "claude_ok": "Serveur MCP « thonny-ai » enregistré dans Claude Code.\n\n"
        "Ouvre une nouvelle session Claude Code (ou tape /mcp) : les outils "
        "thonny-ai (exec, sync, read, write…) seront disponibles.",
        "kind.exec": "Exécution",
        "kind.start": "Lancement",
        "kind.monitor": "Écoute",
        "kind.info": "Infos carte",
        "kind.run_file": "Script",
        "kind.write": "Écriture",
        "kind.read": "Lecture",
        "kind.ls": "Liste",
        "kind.rm": "Suppression",
        "kind.sync": "Synchro",
        "kind.interrupt": "Interruption",
        "kind.reset": "Redémarrage",
        "kind.shell": "Console",
        "kind.open": "Ouverture",
        "kind.status": "État",
        "kind.system": "Système",
        "status.pending": "⏳ en attente",
        "status.approved": "▶ en cours",
        "status.denied": "⛔ refusé",
        "status.done": "✔ ok",
        "status.failed": "✖ erreur",
        "status.info": "ℹ",
    },
}


def _lang():
    try:
        code = (get_workbench().get_option("general.language") or "en").lower()
    except Exception:
        code = "en"
    return code[:2] if code[:2] in _STRINGS else "en"


def t(key, **kw):
    text = _STRINGS[_lang()].get(key) or _STRINGS["en"].get(key, key)
    return text.format(**kw) if kw else text
