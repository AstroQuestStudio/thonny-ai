"""AI Assistant panel: live status, emergency stop, activity log of every AI action, and optional
manual approval."""

import os
import queue
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox, ttk

from thonny import get_workbench

from . import mcp_setup
from .i18n import t
from .safety import find_safe_stop, project_dirs

VIEW_ID = "ThonnyAIView"
MAX_ROWS = 500
BOARD_POLL_MS = 1500

# Actions that change the board (or its pins): the approval card warns about them.
WRITE_KINDS = frozenset({
    "exec", "start", "run_file", "write", "rm", "sync", "gpio", "reset",
    "bm_flash", "bm_restore_micropython",
})

# Colours readable on both light and dark Thonny themes (used for text only, never as backgrounds).
LIGHT = {
    "done": "#1f8a4c",
    "failed": "#c62f2f",
    "denied": "#c26a12",
    "pending": "#a77c00",
    "approved": "#1f6fc2",
    "info": "#707070",
    "stop": "#c62f2f",
    "ok": "#1f8a4c",
    "warn": "#c26a12",
    "bad": "#c62f2f",
    "idle": "#9a9a9a",
}
DARK = {
    "done": "#4cc07a",
    "failed": "#ff6b6b",
    "denied": "#ffa94d",
    "pending": "#f2c94c",
    "approved": "#5aa9ff",
    "info": "#a0a0a0",
    "stop": "#ff6b6b",
    "ok": "#4cc07a",
    "warn": "#ffa94d",
    "bad": "#ff6b6b",
    "idle": "#808080",
}


def _is_dark(widget):
    try:
        bg = ttk.Style().lookup("TFrame", "background") or "white"
        r, g, b = widget.winfo_rgb(bg)
        return (0.299 * r + 0.587 * g + 0.114 * b) / 65535 < 0.5
    except Exception:
        return False


def _button(parent, text, command):
    # Thonny's theme gives buttons a fixed width: size them to their label instead
    return ttk.Button(parent, text=text, command=command, width=len(text) + 2)


def _color_button(parent, text, command, color):
    """Flat coloured button (ttk buttons cannot be tinted on every theme)."""
    return tk.Button(
        parent,
        text=text,
        command=command,
        bg=color,
        fg="white",
        activebackground=color,
        activeforeground="white",
        relief="flat",
        borderwidth=0,
        padx=10,
        pady=3,
        cursor="hand2",
    )


class Tooltip:
    def __init__(self, widget, text):
        self.widget, self.text, self.tip = widget, text, None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, _event=None):
        if self.tip:
            return
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{self.widget.winfo_rootx()}+{self.widget.winfo_rooty() + self.widget.winfo_height() + 4}")
        tk.Label(
            self.tip, text=self.text, background="#2b2b2b", foreground="white", padx=6, pady=3, wraplength=320
        ).pack()

    def _hide(self, _event=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


class SystemEntry:
    def __init__(self, text, status="info"):
        self.id = f"sys-{time.time_ns()}"
        self.kind = "system"
        self.summary = text
        self.detail = text
        self.created = time.time()
        self.status = status
        self.result = ""


class Controller:
    """Receives bridge events from any thread and applies them in the Tk thread."""

    def __init__(self, core, port):
        self.core = core
        self.port = port
        self.bridge_running = False
        self.entries = []
        self.pending = []
        self.view = None
        self._events = queue.Queue()
        core.add_listener(lambda event, op: self._events.put((event, op)))
        get_workbench().after(100, self._poll)

    def system_event(self, text, status="info"):
        self._events.put(("new", SystemEntry(text, status)))

    def call_soon(self, fn):
        self._events.put(("call", fn))

    def _poll(self):
        try:
            while True:
                event, payload = self._events.get_nowait()
                self._apply(event, payload)
        except queue.Empty:
            pass
        get_workbench().after(100, self._poll)

    def _apply(self, event, op):
        if event == "call":
            op()
            return
        if event == "new" and op not in self.entries:
            self.entries.append(op)
            del self.entries[:-MAX_ROWS]
        if event == "approval_needed" and op not in self.pending:
            self.pending.append(op)
            wb = get_workbench()
            wb.show_view(VIEW_ID, set_focus=False)
            wb.bell()
            wb.become_active_window(False)
        if op in self.pending and op.status != "pending":
            self.pending.remove(op)
        if self.view is not None:
            self.view.refresh(op)

    def decide(self, op, allowed, trust_session=False):
        self.core.decide(op.id, allowed, trust_session)

    def clear(self):
        self.entries = [e for e in self.entries if e in self.pending]

    # ------------------------------------------------------------ emergency stop

    def _project_dirs(self):
        return project_dirs(get_workbench())

    def watchdog_triggered(self, event, dirs, proxy):
        reason = event["reason"]
        self.system_event(t(f"watchdog_{reason}", **event), "failed")
        if reason != "disconnect":
            self.emergency_stop(dirs, expected_proxy=proxy)

    def emergency_stop(self, dirs=None, expected_proxy=None):
        """Interrupts the program, then runs the project's safe-stop code (motors off)."""
        dirs = self._project_dirs() if dirs is None else dirs
        self.system_event(t("stop_running"), "failed")
        host = self.core.host

        def work():
            try:
                host.interrupt(expected_proxy=expected_proxy)
            except Exception as e:
                self.system_event(t("stop_failed", error=e), "failed")
                return
            code, source = find_safe_stop(dirs)
            if not code:
                self.system_event(t("stop_done") + " " + t("stop_safe_none"), "denied")
                return
            time.sleep(0.4)
            try:
                result = host.exec_code(
                    code, 10.0, echo=True, quiet=False, echo_text="# ⛔ safe stop (thonny-ai)",
                    watchdog=False, expected_proxy=expected_proxy,
                )
            except Exception as e:
                self.system_event(t("stop_done") + " " + t("stop_safe_failed", error=e), "failed")
                return
            if result.get("stderr", "").strip() or result.get("error"):
                error = (result.get("stderr", "").strip() or str(result.get("error"))).splitlines()[-1]
                self.system_event(t("stop_done") + " " + t("stop_safe_failed", error=error), "failed")
            elif not result["finished"]:
                self.system_event(t("stop_done") + " " + t("stop_safe_timed_out"), "failed")
            else:
                self.system_event(t("stop_done") + " " + t("stop_safe_ran", source=os.path.basename(source)), "done")

        threading.Thread(target=work, daemon=True).start()


class DetailsWindow(tk.Toplevel):
    """Code and result of one action, monospaced, with a Copy button."""

    def __init__(self, master, op):
        super().__init__(master)
        self.title(f"{t('kind.' + op.kind)} — {op.summary}")
        self.geometry("780x480")
        self.minsize(420, 240)
        self.bind("<Escape>", lambda _e: self.destroy())
        mono = tkfont.nametofont("TkFixedFont")
        self._notebook = ttk.Notebook(self)
        self._notebook.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        self._texts = []
        for label, body in (
            (t("tab_code"), op.detail or op.summary),
            (t("tab_result"), op.result or t("no_result")),
        ):
            page = ttk.Frame(self._notebook)
            text = tk.Text(page, wrap="word", font=mono, relief="flat", padx=8, pady=6)
            bar = ttk.Scrollbar(page, orient="vertical", command=text.yview)
            text.configure(yscrollcommand=bar.set)
            bar.pack(side="right", fill="y")
            text.pack(side="left", fill="both", expand=True)
            text.insert("1.0", body)
            text.configure(state="disabled")
            self._notebook.add(page, text=label)
            self._texts.append(text)
        if op.result and op.status in ("done", "failed"):
            self._notebook.select(1)
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=(0, 8))
        self._copy = _button(bar, t("copy"), self._copy_current)
        self._copy.pack(side="left")
        _button(bar, "✕", self.destroy).pack(side="right")

    def _copy_current(self):
        text = self._texts[self._notebook.index(self._notebook.select())]
        self.clipboard_clear()
        self.clipboard_append(text.get("1.0", "end-1c"))
        self._copy.configure(text=t("copy_done"))
        self.after(1200, lambda: self._copy.winfo_exists() and self._copy.configure(text=t("copy")))


class ThonnyAIView(ttk.Frame):
    controller = None  # injected by load_plugin

    def __init__(self, master):
        super().__init__(master)
        ctrl = ThonnyAIView.controller
        ctrl.view = self
        self._ctrl = ctrl
        self._wb = get_workbench()
        self._colors = DARK if _is_dark(self) else LIGHT
        self._board_after = None

        self._build_header()
        self._build_options()
        self._build_approval()
        self._build_filter()
        self._build_log()
        self._build_footer()

        self._rebuild()
        self._render_approval()
        self._poll_board()
        self.bind("<Destroy>", self._on_destroy, add="+")

    # ------------------------------------------------------------ layout

    def _build_header(self):
        c = self._colors
        head = ttk.Frame(self)
        head.pack(fill="x", padx=8, pady=(8, 0))
        self._dot = tk.Label(head, text="●", fg=c["idle"], font=("TkDefaultFont", 14), bd=0)
        self._dot.pack(side="left")
        titles = ttk.Frame(head)
        titles.pack(side="left", padx=(6, 0))
        bold = tkfont.Font(font="TkDefaultFont")
        bold.configure(weight="bold", size=bold.cget("size") + 1)
        ttk.Label(titles, text=t("view_title"), font=bold).pack(anchor="w")
        small = tkfont.Font(font="TkDefaultFont")
        small.configure(size=max(8, small.cget("size") - 1))
        self._board_label = ttk.Label(titles, text="…", font=small)
        self._board_label.pack(anchor="w")
        self._bridge_label = ttk.Label(
            titles,
            text=t("bridge_on" if self._ctrl.bridge_running else "bridge_off", port=self._ctrl.port),
            font=small,
        )
        self._bridge_label.pack(anchor="w")

        actions = ttk.Frame(head)
        actions.pack(side="right")
        stop = _color_button(actions, t("stop_button"), self._ctrl.emergency_stop, c["bad"])
        stop.configure(font=bold)
        stop.pack(side="top", anchor="e")
        Tooltip(stop, t("stop_tip"))
        link = _button(actions, t("connect_claude"), self.connect_claude)
        link.pack(side="top", anchor="e", pady=(4, 0))

    def _build_options(self):
        self._options_row = ttk.Frame(self)
        self._options_row.pack(fill="x", padx=8, pady=(8, 0))
        ttk.Checkbutton(
            self._options_row,
            text=t("require_approval"),
            variable=self._wb.get_variable("thonny_ai.require_approval"),
        ).pack(side="left")
        ttk.Checkbutton(
            self._options_row,
            text=t("auto_reconnect"),
            variable=self._wb.get_variable("thonny_ai.auto_reconnect"),
        ).pack(side="left", padx=(12, 0))
        watchdog = self._ctrl.core.host.watchdog_request_now("status", {})
        self._watchdog_enabled = tk.BooleanVar(self, value=watchdog["enabled"])
        ttk.Checkbutton(
            self._options_row,
            text=t("watchdog"),
            variable=self._watchdog_enabled,
            command=self._toggle_watchdog,
        ).pack(side="left", padx=(12, 0))

    def _toggle_watchdog(self):
        action = "on" if self._watchdog_enabled.get() else "off"
        self._ctrl.core.host.watchdog_request_now(action, {})

    def _build_approval(self):
        self._approval = tk.Frame(self, highlightthickness=2, highlightbackground=self._colors["warn"])
        inner = ttk.Frame(self._approval)
        inner.pack(fill="both", expand=True, padx=2, pady=2)
        bold = tkfont.Font(font="TkDefaultFont")
        bold.configure(weight="bold")
        ttk.Label(inner, text=t("approval_title"), font=bold).pack(anchor="w", padx=6, pady=(4, 0))
        self._approval_label = ttk.Label(inner, wraplength=520, justify="left")
        self._approval_label.pack(fill="x", padx=6, pady=(2, 0))
        self._approval_risk = tk.Label(inner, anchor="w", justify="left", bd=0)
        self._approval_risk.pack(fill="x", padx=6)
        self._approval_detail = tk.Text(
            inner, height=7, wrap="none", relief="flat", font=tkfont.nametofont("TkFixedFont"), padx=6, pady=4
        )
        self._approval_detail.pack(fill="both", expand=True, padx=6, pady=(2, 0))
        buttons = ttk.Frame(inner)
        buttons.pack(fill="x", padx=6, pady=6)
        _color_button(buttons, t("allow"), lambda: self._decide(True), self._colors["ok"]).pack(side="left")
        _button(buttons, t("deny"), lambda: self._decide(False)).pack(side="left", padx=6)
        _button(buttons, t("allow_session"), lambda: self._decide(True, trust_session=True)).pack(side="right")

    def _build_filter(self):
        row = ttk.Frame(self)
        row.pack(fill="x", padx=8, pady=(8, 0))
        ttk.Label(row, text=t("filter_label")).pack(side="left")
        self._filter = tk.StringVar()
        self._filter.trace_add("write", lambda *_: self._rebuild())
        ttk.Entry(row, textvariable=self._filter, width=16).pack(side="left", padx=(6, 8))
        self._errors_only = tk.BooleanVar(value=False)
        self._errors_only.trace_add("write", lambda *_: self._rebuild())
        ttk.Checkbutton(row, text=t("only_errors"), variable=self._errors_only).pack(side="left")
        _button(row, t("clear"), self._clear).pack(side="right")

    def _build_log(self):
        c = self._colors
        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=8, pady=(6, 0))
        columns = ("time", "action", "detail", "status")
        self._tree = ttk.Treeview(frame, columns=columns, show="headings", height=8)
        # small base widths: "detail" stretches, so the status column stays visible in a narrow panel
        for col, width, stretch in (
            ("time", 64, False),
            ("action", 110, False),
            ("detail", 140, True),
            ("status", 96, False),
        ):
            self._tree.heading(col, text=t(f"col_{col}"))
            self._tree.column(col, width=width, minwidth=width if not stretch else 60, stretch=stretch)
        bar = ttk.Scrollbar(frame, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self._tree.pack(side="left", fill="both", expand=True)
        for status in ("done", "failed", "denied", "pending", "approved", "info", "stop"):
            self._tree.tag_configure(status, foreground=c[status])
        self._tree.bind("<Double-1>", self._show_details)
        self._tree.bind("<Return>", self._show_details)
        self._menu = tk.Menu(self, tearoff=False)
        self._menu.add_command(label=t("tab_code") + " / " + t("tab_result"), command=self._show_details)
        self._menu.add_command(label=t("copy") + " (" + t("tab_code") + ")", command=lambda: self._copy_selected("detail"))
        self._menu.add_command(label=t("copy") + " (" + t("tab_result") + ")", command=lambda: self._copy_selected("result"))
        self._tree.bind("<Button-3>", self._popup)

    def _build_footer(self):
        self._summary = ttk.Label(self, anchor="w")
        self._summary.pack(fill="x", padx=8, pady=(4, 8))

    # ------------------------------------------------------------ live board status

    def _poll_board(self):
        if not self.winfo_exists():
            return
        c = self._colors
        text, color = t("board_none"), c["idle"]
        try:
            st = self._ctrl.core.host.status_now()
            self._watchdog_enabled.set(st["watchdog_enabled"])
            name = st.get("backend_description") or st.get("backend") or "?"
            port = st.get("port") or ""
            if st.get("connected"):
                state = t("board_ready") if st.get("state") == "waiting_toplevel_command" else t("board_busy")
                text = f"{name} · {port} · {t('board_connected')} · {state}"
                color = c["ok"]
            else:
                text = f"{name} · {port} · {t('board_disconnected')}"
                color = c["warn"]
        except Exception:
            pass
        if not self._ctrl.bridge_running:
            color = c["bad"]
        self._board_label.configure(text=text)
        self._dot.configure(fg=color)
        self._board_after = self.after(BOARD_POLL_MS, self._poll_board)

    def _on_destroy(self, event):
        if event.widget is self and self._board_after:
            try:
                self.after_cancel(self._board_after)
            except Exception:
                pass
        if event.widget is self and self._ctrl.view is self:
            self._ctrl.view = None

    # ------------------------------------------------------------ log

    def _matches(self, op):
        if self._errors_only.get() and op.status not in ("failed", "denied"):
            return False
        needle = self._filter.get().strip().lower()
        if not needle:
            return True
        haystack = f"{t('kind.' + op.kind)} {op.summary} {t('status.' + op.status)}".lower()
        return needle in haystack

    def _values(self, op):
        return (
            time.strftime("%H:%M:%S", time.localtime(op.created)),
            t(f"kind.{op.kind}"),
            op.summary.replace("\n", " "),
            t(f"status.{op.status}"),
        )

    def _tags(self, op):
        return ("stop",) if op.kind == "stop" else (op.status,)

    def _rebuild(self):
        self._tree.delete(*self._tree.get_children())
        for op in self._ctrl.entries:  # oldest first: each insert goes on top
            if self._matches(op):
                self._tree.insert("", 0, iid=op.id, values=self._values(op), tags=self._tags(op))
        self._update_summary()

    def refresh(self, op):
        if self._tree.exists(op.id):
            if self._matches(op):
                self._tree.item(op.id, values=self._values(op), tags=self._tags(op))
            else:
                self._tree.delete(op.id)
        elif self._matches(op):
            self._tree.insert("", 0, iid=op.id, values=self._values(op), tags=self._tags(op))
            children = self._tree.get_children()
            if len(children) > MAX_ROWS:
                self._tree.delete(*children[MAX_ROWS:])
        self._render_approval()
        self._update_summary()

    def _update_summary(self):
        entries = self._ctrl.entries
        failed = sum(1 for e in entries if e.status == "failed")
        self._summary.configure(
            text=t("summary", n=len(entries), failed=failed, pending=len(self._ctrl.pending))
        )

    def _clear(self):
        self._ctrl.clear()
        self._rebuild()

    # ------------------------------------------------------------ approval card

    def _render_approval(self):
        pending = self._ctrl.pending
        if not pending:
            self._approval.pack_forget()
            return
        op = pending[0]
        more = t("more_pending", n=len(pending) - 1) if len(pending) > 1 else ""
        risk = "risk_read"
        if op.kind == "watchdog":
            risk = "risk_watchdog"
        elif op.kind in WRITE_KINDS:
            risk = "risk_write"
        color = self._colors["approved"] if risk == "risk_read" else self._colors["bad"]
        self._approval.configure(highlightbackground=color)
        self._approval_label.configure(text=f"{t('kind.' + op.kind)} : {op.summary}{more}")
        self._approval_risk.configure(text=t(risk), fg=color)
        self._approval_detail.configure(state="normal")
        self._approval_detail.delete("1.0", "end")
        self._approval_detail.insert("1.0", op.detail or op.summary)
        self._approval_detail.configure(state="disabled")
        self._approval.pack(fill="x", padx=8, pady=(8, 0), after=self._options_row)

    def _decide(self, allowed, trust_session=False):
        if self._ctrl.pending:
            self._ctrl.decide(self._ctrl.pending[0], allowed, trust_session)

    # ------------------------------------------------------------ details, copy, menu

    def _selected_op(self):
        selection = self._tree.selection()
        if not selection:
            return None
        return next((e for e in self._ctrl.entries if e.id == selection[0]), None)

    def _show_details(self, _event=None):
        op = self._selected_op()
        if op is not None:
            DetailsWindow(self, op)

    def _copy_selected(self, attr):
        op = self._selected_op()
        if op is not None:
            self.clipboard_clear()
            self.clipboard_append(getattr(op, attr, "") or "")

    def _popup(self, event):
        row = self._tree.identify_row(event.y)
        if row:
            self._tree.selection_set(row)
            self._menu.tk_popup(event.x_root, event.y_root)

    # ------------------------------------------------------------ Claude Code setup

    def connect_claude(self):
        def work():
            ok, message = mcp_setup.register_with_claude_code()
            self._ctrl.call_soon(lambda: self._show_setup_result(ok, message))

        threading.Thread(target=work, daemon=True).start()

    def _show_setup_result(self, ok, message):
        if ok:
            messagebox.showinfo("thonny-ai", message, master=self)
            return
        self.clipboard_clear()
        self.clipboard_append(mcp_setup.manual_command())
        messagebox.showwarning("thonny-ai", f"{message}\n\n{t('copied')}", master=self)
