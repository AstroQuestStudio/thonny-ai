"""AI Assistant panel: activity log of every AI action, with optional manual approval."""

import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from thonny import get_workbench

from . import mcp_setup
from .i18n import t

VIEW_ID = "ThonnyAIView"
MAX_ROWS = 500


def _button(parent, text, command):
    # Thonny's theme gives buttons a fixed width: size them to their label instead
    return ttk.Button(parent, text=text, command=command, width=len(text) + 2)


class SystemEntry:
    def __init__(self, text):
        self.id = f"sys-{time.time_ns()}"
        self.kind = "system"
        self.summary = text
        self.detail = text
        self.created = time.time()
        self.status = "info"
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

    def system_event(self, text):
        self._events.put(("new", SystemEntry(text)))

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


class ThonnyAIView(ttk.Frame):
    controller = None  # injected by load_plugin

    def __init__(self, master):
        super().__init__(master)
        ctrl = ThonnyAIView.controller
        ctrl.view = self
        self._ctrl = ctrl
        wb = get_workbench()

        top = ttk.Frame(self)
        top.pack(fill="x", padx=6, pady=(6, 2))
        status = t("bridge_on" if ctrl.bridge_running else "bridge_off", port=ctrl.port)
        ttk.Label(top, text=status).pack(side="left")
        _button(top, t("connect_claude"), self.connect_claude).pack(side="right")

        self._options_row = ttk.Frame(self)
        self._options_row.pack(fill="x", padx=6)
        ttk.Checkbutton(
            self._options_row,
            text=t("require_approval"),
            variable=wb.get_variable("thonny_ai.require_approval"),
        ).pack(side="left")
        ttk.Checkbutton(
            self._options_row,
            text=t("auto_reconnect"),
            variable=wb.get_variable("thonny_ai.auto_reconnect"),
        ).pack(side="left", padx=(12, 0))

        self._approval = ttk.LabelFrame(self, text=t("approval_title"))
        self._approval_label = ttk.Label(self._approval, wraplength=520, justify="left")
        self._approval_label.pack(fill="x", padx=6, pady=(4, 2))
        self._approval_detail = tk.Text(self._approval, height=7, wrap="none", relief="flat")
        self._approval_detail.pack(fill="both", expand=True, padx=6)
        buttons = ttk.Frame(self._approval)
        buttons.pack(fill="x", padx=6, pady=4)
        _button(buttons, t("allow"), lambda: self._decide(True)).pack(side="left")
        _button(buttons, t("deny"), lambda: self._decide(False)).pack(side="left", padx=6)
        _button(buttons, t("allow_session"), lambda: self._decide(True, trust_session=True)).pack(
            side="right"
        )

        columns = ("time", "action", "detail", "status")
        self._tree = ttk.Treeview(self, columns=columns, show="headings", height=8)
        for col, width, stretch in (
            ("time", 70, False),
            ("action", 100, False),
            ("detail", 320, True),
            ("status", 100, False),
        ):
            self._tree.heading(col, text=t(f"col_{col}"))
            self._tree.column(col, width=width, stretch=stretch)
        self._tree.pack(fill="both", expand=True, padx=6, pady=(4, 6))
        self._tree.bind("<Double-1>", self._show_details)

        for op in ctrl.entries:
            self._upsert(op)
        self._render_approval()

    def refresh(self, op):
        self._upsert(op)
        self._render_approval()

    def _upsert(self, op):
        values = (
            time.strftime("%H:%M:%S", time.localtime(op.created)),
            t(f"kind.{op.kind}"),
            op.summary,
            t(f"status.{op.status}"),
        )
        if self._tree.exists(op.id):
            self._tree.item(op.id, values=values)
        else:
            self._tree.insert("", 0, iid=op.id, values=values)
            children = self._tree.get_children()
            if len(children) > MAX_ROWS:
                self._tree.delete(*children[MAX_ROWS:])

    def _render_approval(self):
        pending = self._ctrl.pending
        if not pending:
            self._approval.pack_forget()
            return
        op = pending[0]
        more = t("more_pending", n=len(pending) - 1) if len(pending) > 1 else ""
        self._approval_label.configure(text=f"{t('kind.' + op.kind)} : {op.summary}{more}")
        self._approval_detail.configure(state="normal")
        self._approval_detail.delete("1.0", "end")
        self._approval_detail.insert("1.0", op.detail or op.summary)
        self._approval_detail.configure(state="disabled")
        self._approval.pack(fill="x", padx=6, pady=4, after=self._options_row)

    def _decide(self, allowed, trust_session=False):
        if self._ctrl.pending:
            self._ctrl.decide(self._ctrl.pending[0], allowed, trust_session)

    def _show_details(self, event=None):
        selection = self._tree.selection()
        if not selection:
            return
        op = next((e for e in self._ctrl.entries if e.id == selection[0]), None)
        if op is None:
            return
        win = tk.Toplevel(self)
        win.title(f"{t('kind.' + op.kind)} — {op.summary}")
        text = tk.Text(win, wrap="none", width=90, height=24)
        text.pack(fill="both", expand=True)
        body = op.detail or op.summary
        if op.result:
            body += f"\n\n{t('result')}\n{op.result}"
        text.insert("1.0", body)
        text.configure(state="disabled")

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
