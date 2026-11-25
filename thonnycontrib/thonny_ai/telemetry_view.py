"""Live telemetry from Thonny's program output."""

import csv
import math
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from thonny_ai_mcp.telemetry import LineBuffer, SeriesWindow

from .i18n import t
from .panel import DARK, LIGHT, _button, _is_dark

VIEW_ID = "TelemetryView"
REFRESH_MS = 100
WINDOWS = ("10 s", "30 s", "60 s")
ACCENTS = ("approved", "ok", "warn", "failed")


def nice_axis(minimum, maximum, target=5):
    """Round axis bounds and ticks on a 1 / 2 / 2.5 / 5 × 10^n step, e.g. 0, 0.25, 0.5…
    Returns (low, high, ticks, step)."""
    if minimum == maximum:
        pad = abs(minimum) * 0.1 or 1.0
        minimum, maximum = minimum - pad, maximum + pad
    raw = (maximum - minimum) / (target - 1)
    magnitude = 10 ** math.floor(math.log10(raw))
    step = 10 * magnitude
    for factor in (1, 2, 2.5, 5):
        if factor * magnitude >= raw:
            step = factor * magnitude
            break
    low = math.floor(minimum / step + 1e-9) * step
    high = math.ceil(maximum / step - 1e-9) * step
    count = int(round((high - low) / step))
    ticks = [low + index * step for index in range(count + 1)]
    return low, high, ticks, step


def format_tick(value, step):
    """As few decimals as the step needs (0.25 → 2 decimals, 5 → none); never prints -0."""
    if abs(value) < step * 1e-6:
        value = 0.0
    decimals = 0
    while decimals < 6 and abs(round(step, decimals) - step) > step * 1e-9:
        decimals += 1
    return f"{value:.{decimals}f}"


class TelemetryView(ttk.Frame):
    host = None  # installed by load_plugin, which owns the ThonnyHost

    def __init__(self, master):
        super().__init__(master, padding=(8, 5))
        self._lines = LineBuffer()
        self._series = SeriesWindow()
        self._started = time.monotonic()
        self._paused_at = None
        self._timer = None
        self._legend_items = {}
        self._legend_width = None

        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", pady=(0, 4))
        ttk.Label(toolbar, text=t("telemetry_window")).pack(side="left")
        self._window = ttk.Combobox(toolbar, values=WINDOWS, state="readonly", width=5)
        self._window.current(1)
        self._window.pack(side="left", padx=(4, 8))
        self._pause = _button(toolbar, t("telemetry_pause"), self._toggle_pause)
        self._pause.configure(width=max(len(t("telemetry_pause")), len(t("telemetry_resume"))) + 2)
        self._pause.pack(side="left", padx=(0, 4))
        _button(toolbar, t("telemetry_clear"), self._clear).pack(side="left", padx=(0, 4))
        _button(toolbar, t("telemetry_export"), self._export).pack(side="left")

        self._legend = ttk.Frame(self)
        self._legend.pack(fill="x", side="bottom")
        self._canvas = tk.Canvas(self, width=440, height=170, highlightthickness=0)
        self._canvas.pack(fill="both", expand=True)

        self._unsubscribe = self.host.subscribe_output(self._on_output)
        self.bind("<Map>", self._schedule)
        self.bind("<Unmap>", self._cancel)
        self.bind("<Destroy>", self._on_destroy)

    def _on_output(self, stream, chunk):
        if stream == "stderr":
            return
        now = time.monotonic()
        for fields in self._lines.feed(chunk):
            if self._paused_at is None:
                self._series.add(now, fields)

    def _toggle_pause(self):
        if self._paused_at is None:
            self._paused_at = time.monotonic()
            self._pause.configure(text=t("telemetry_resume"))
        else:
            self._paused_at = None
            self._pause.configure(text=t("telemetry_pause"))

    def _clear(self):
        self._series.clear()
        self._lines = LineBuffer()
        for item, _, _, _ in self._legend_items.values():
            item.destroy()
        self._legend_items.clear()

    def _schedule(self, _event=None):
        if self._timer is None:
            self._timer = self.after(REFRESH_MS, self._tick)

    def _cancel(self, _event=None):
        if self._timer is not None:
            self.after_cancel(self._timer)
            self._timer = None

    def _on_destroy(self, event):
        if event.widget is self:
            self._cancel()
            self._unsubscribe()

    def _tick(self):
        self._timer = None
        self._draw()
        self._schedule()

    def _window_data(self):
        now = time.monotonic() if self._paused_at is None else self._paused_at
        seconds = int(self._window.get().split()[0])
        return now, seconds, self._series.snapshot(now, seconds)

    def _export(self):
        _, _, data = self._window_data()
        path = filedialog.asksaveasfilename(
            parent=self, title=t("telemetry_export"), defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
        )
        if not path:
            return
        rows = ((stamp, name, value) for name, points in data.items() for stamp, value in points)
        try:
            with open(path, "w", encoding="utf-8", newline="") as file:
                writer = csv.writer(file)
                writer.writerow(("elapsed_seconds", "series", "value"))
                for stamp, name, value in sorted(rows, key=lambda row: row[0]):
                    if name.lstrip().startswith(("=", "+", "-", "@", "＝", "＋", "－", "＠")):
                        name = "'" + name
                    writer.writerow((round(stamp - self._started, 6), name, value))
        except OSError as exc:
            messagebox.showerror(t("telemetry_export"), str(exc), parent=self)

    def _update_legend(self, names, dark):
        changed = tuple(names) != tuple(self._legend_items)
        for name in list(self._legend_items):
            if name not in names:
                item, _, _, _ = self._legend_items.pop(name)
                item.destroy()
        used = {slot for _, _, _, slot in self._legend_items.values()}
        for name in names:
            if name not in self._legend_items:
                slot = next(index for index in range(len(used) + 1) if index not in used)
                used.add(slot)
                item = ttk.Frame(self._legend)
                swatch = tk.Frame(item, width=10, height=3)
                swatch.pack(side="left", padx=(0, 2))
                visible = tk.BooleanVar(item, value=True)
                label = name if len(name) <= 18 else name[:17] + "…"
                ttk.Checkbutton(item, text=label, variable=visible).pack(side="left")
                self._legend_items[name] = (item, swatch, visible, slot)

        palette = DARK if dark else LIGHT
        colors = tuple(palette[key] for key in ACCENTS) + (
            ("#c7a4ff", "#54d0c9", "#ed9bd1", "#e9cd74") if dark
            else ("#7553b5", "#007d7c", "#ae3c82", "#866000")
        )
        width = self._canvas.winfo_width()
        if self._legend_items:
            column_width = max(item.winfo_reqwidth() for item, _, _, _ in self._legend_items.values()) + 8
            columns = max(1, width // column_width)
            if changed or self._legend_width != columns:
                for index, (item, _, _, _) in enumerate(self._legend_items.values()):
                    item.grid(row=index // columns, column=index % columns, sticky="w", padx=(0, 8))
                self._legend_width = columns
        visible_colors = {}
        for name, (_, swatch, visible, slot) in self._legend_items.items():
            color = colors[slot % len(colors)]
            swatch.configure(background=color if visible.get() else palette["idle"])
            if visible.get():
                visible_colors[name] = (color, (4, 3) if slot >= len(colors) else ())
        return visible_colors

    def _draw(self):
        canvas = self._canvas
        width, height = canvas.winfo_width(), canvas.winfo_height()
        style = ttk.Style(self)
        dark = _is_dark(self)
        palette = DARK if dark else LIGHT
        canvas.configure(background=style.lookup("TFrame", "background") or ("#252525" if dark else "white"))
        canvas.delete("all")
        now, seconds, data = self._window_data()
        colors = self._update_legend(data, dark)
        if width < 150 or height < 55:
            return
        visible = {name: points for name, points in data.items() if name in colors and points}
        if not visible:
            key = "telemetry_hidden" if any(data.values()) else "telemetry_waiting"
            canvas.create_text(width / 2, height / 2, text=t(key), fill=palette["info"], width=width - 12)
            return

        values = [value for points in visible.values() for _, value in points]
        low, high, ticks, step = nice_axis(min(values), max(values))
        left, right, top, bottom = 80, width - 20, 8, height - 24
        axis = style.lookup("TLabel", "foreground") or palette["info"]
        grid = palette["idle"]

        def y_coordinate(value):
            fraction = (value - low) / (high - low)
            return bottom - fraction * (bottom - top)

        def x_coordinate(offset):
            fraction = max(0.0, min(1.0, 1 + offset / seconds))
            return left + fraction * (right - left)

        for value in ticks:
            y = y_coordinate(value)
            canvas.create_line(left, y, right, y, fill=grid, dash=(2, 4))
            canvas.create_text(left - 6, y, anchor="e", text=format_tick(value, step), fill=axis)
        divisions = 2 if width < 420 else 5
        for index in range(divisions + 1):
            offset = -seconds + seconds * index / divisions
            x = x_coordinate(offset)
            canvas.create_line(x, top, x, bottom, fill=grid, dash=(2, 4))
            canvas.create_text(x, bottom + 6, anchor="n", text=f"{offset:g} s", fill=axis)

        for name, points in visible.items():
            color, dash = colors[name]
            coords = [coordinate for stamp, value in points for coordinate in
                      (x_coordinate(stamp - now), y_coordinate(value))]
            if len(points) > 1:
                canvas.create_line(*coords, fill=color, width=2, dash=dash, tags="curve")
            else:
                x, y = coords
                canvas.create_oval(x - 2, y - 2, x + 2, y + 2, fill=color, outline="", tags="curve")
