import csv
import io
import json
import unittest
from _tkinter import DONT_WAIT, WINDOW_EVENTS
from tkinter import ttk
from types import SimpleNamespace
from unittest import mock

from fakes import FakeHost
from test_panel import WorkbenchStub, make_root
from thonny_ai_mcp import backends, server, telemetry, tools
from thonnycontrib.thonny_ai import host, i18n, telemetry_view
from thonnycontrib.thonny_ai.core import BridgeCore


def call(**arguments):
    return server.handle(
        {"method": "tools/call", "params": {"name": "telemetry", "arguments": arguments}}
    )


class ParseTest(unittest.TestCase):
    def test_numeric_pairs_and_separators(self):
        self.assertEqual(
            telemetry.parse_line("dist=-23.4, vg:+.52; vd = 5e-1 cap.deg:-2.5E+2 wheel-4=2."),
            {"dist": -23.4, "vg": 0.52, "vd": 0.5, "cap.deg": -250, "wheel-4": 2},
        )

    def test_ignores_noise_and_partial_numbers(self):
        self.assertEqual(
            telemetry.parse_line("[Status] OK dist=bad wheel=1e cap=1.2.3 fake=2x huge=1e400 ok=0"),
            {"ok": 0},
        )
        self.assertEqual(telemetry.parse_line(""), {})

    def test_json_only_keeps_finite_numbers(self):
        self.assertEqual(
            telemetry.parse_line(
                '{"dist": 23.4, "cape": -1e2, "i": 2, "texte": "4", "active": true, '
                '"none": null, "list": [1], "sub": {"val": 2}, "nan": NaN, "inf": 1e400}'
            ),
            {"dist": 23.4, "cape": -100, "i": 2},
        )
        for line in ('{"dist": 23, bogus=3}', '{"dist":', '[{"dist": 23}]'):
            with self.subTest(line=line):
                self.assertEqual(telemetry.parse_line(line), {})

    def test_buffers_line_fragments_and_discards_overlong_lines(self):
        lines = telemetry.LineBuffer()
        self.assertEqual(list(lines.feed("dist=-2.5e")), [])
        self.assertEqual(list(lines.feed("-1\r\nnoise\ncap=4\n")), [{"dist": -0.25}, {"cap": 4}])
        self.assertEqual(list(lines.feed("X" * telemetry.MAX_LINE_LENGTH + "X")), [])
        self.assertEqual(list(lines.feed(" dist=9\ncap=2\n")), [{"cap": 2}])


class SeriesTest(unittest.TestCase):
    def test_stats_and_latest_points(self):
        stats = telemetry.SeriesStats(points=2)
        self.assertEqual(stats.summary(), {"count": 0})
        stats.add(10, -1)
        self.assertIsNone(stats.summary()["rate_hz"])
        stats.add(10.25, 2)
        stats.add(10.5, 5)
        result = stats.summary()
        self.assertEqual(result["points"], [(10.25, 2), (10.5, 5)])
        self.assertEqual({k: result[k] for k in ("last", "min", "max", "mean", "count", "rate_hz")},
                         {"last": 5, "min": -1, "max": 5, "mean": 2, "count": 3, "rate_hz": 4})
        self.assertAlmostEqual(result["stddev"], 6 ** 0.5)

    def test_extreme_values_stay_finite(self):
        stats = telemetry.SeriesStats()
        stats.add(1, 1e308)
        stats.add(1, -1e308)
        result = stats.summary()
        self.assertEqual(result["mean"], 0)
        self.assertEqual(result["stddev"], 1e308)
        self.assertIsNone(result["rate_hz"])
        self.assertNotIn("points", result)

    def test_window_retains_long_history_within_bounds(self):
        window = telemetry.SeriesWindow()
        window.add(0, {"old": 1})
        window.add(40, {"mid": 2})
        for value in range(telemetry.WINDOW_POINTS + 2):
            window.add(65, {"active": value})
        short = window.snapshot(65, 10)
        self.assertNotIn("old", short)
        self.assertEqual(short["mid"], [])
        self.assertEqual(len(short["active"]), telemetry.WINDOW_POINTS)
        self.assertEqual(short["active"][0], (65, 2))
        self.assertEqual(window.snapshot(65, 30)["mid"], [(40, 2)])
        window.clear()
        self.assertEqual(window.snapshot(65), {})

    def test_window_accepts_new_series_when_old_series_expire(self):
        window = telemetry.SeriesWindow()
        window.add(0, {f"s{i}": i for i in range(telemetry.MAX_SERIES + 1)})
        self.assertEqual(len(window.snapshot(0)), telemetry.MAX_SERIES)
        window.add(61, {"new": 1})
        self.assertEqual(window.snapshot(61), {"new": [(61, 1)]})


class McpTest(unittest.TestCase):
    def test_thonny_collects_fresh_stats_and_leaves_monitor_cursor_alone(self):
        elapsed = 0.0
        reads = []
        chunks = iter([("dist=-1\nwheel=0.5\ndist=", True), ("2\n", True), ('{"dist": 5}\n', False)])

        class Host(FakeHost):
            def monitor(self, seconds, since=None):
                nonlocal elapsed
                elapsed += seconds
                reads.append(since)
                out, running = next(chunks)
                return {"stdout": out, "stderr": "dist=999\n", "running": running, "cursor": len(reads)}

        bridge = BridgeCore(Host(), require_approval=lambda: True, approval_timeout=0)

        def request(path, payload=None, timeout=None):
            return bridge.handle("GET" if payload is None else "POST", path, payload or {})

        with (
            mock.patch.object(backends, "bridge_status", return_value={"connected": True}),
            mock.patch.object(backends, "bridge_request", side_effect=request),
            mock.patch.object(backends.ThonnyBackend, "cursor", 99),
            mock.patch.object(tools.time, "monotonic", side_effect=lambda: elapsed),
        ):
            response = call(seconds=10, series=["dist"], points=2)
            self.assertEqual(backends.ThonnyBackend.cursor, 99)
        self.assertFalse(response["isError"], response)
        data = json.loads(response["content"][0]["text"])
        self.assertEqual(reads, [None, 1, 2])
        self.assertEqual(data["backend"], "thonny")
        self.assertFalse(data["running"])
        self.assertAlmostEqual(data["elapsed_seconds"], 0.6)
        self.assertEqual(set(data["series"]), {"dist"})
        stats = data["series"]["dist"]
        self.assertEqual(stats["count"], 3)
        self.assertEqual(stats["last"], 5)
        self.assertAlmostEqual(stats["mean"], 2)
        self.assertAlmostEqual(stats["rate_hz"], 5)
        self.assertEqual([point[1] for point in stats["points"]], [2, 5])
        self.assertEqual(bridge.host.calls, [])
        op, = bridge.operations()
        self.assertTrue(op.read_only)
        self.assertTrue(op.auto_approved)
        self.assertEqual(op.status, "done")

    def test_passive_serial_uses_listen_and_no_board_commands(self):
        elapsed = 0.0
        reads = iter([b'{"cap\xc3', b'\xa9": 1}\n{"cap\xc3\xa9": 3}\n'])

        def read(_size):
            nonlocal elapsed
            elapsed += 0.2
            return next(reads)

        fake = mock.Mock(name="serial device")
        fake.read.side_effect = read
        with (
            mock.patch.object(backends, "bridge_status", return_value=None),
            mock.patch.object(backends, "_session", None),
            mock.patch("serial.Serial", return_value=fake) as serial,
            mock.patch.dict(backends.os.environ, {
                "THONNY_AI_SERIAL_FALLBACK": "1",
                "THONNY_AI_SERIAL_PORT": "COM42",
            }),
            mock.patch.object(backends.time, "time", side_effect=lambda: elapsed),
            mock.patch.object(tools.time, "monotonic", side_effect=lambda: elapsed),
        ):
            response = call(seconds=0.4)
        self.assertFalse(response["isError"], response)
        data = json.loads(response["content"][0]["text"])
        self.assertIsNone(data["running"])
        self.assertEqual(data["series"]["capé"]["count"], 2)
        self.assertIsNone(data["series"]["capé"]["rate_hz"])
        self.assertNotIn("points", data["series"]["capé"])
        serial.assert_called_once_with("COM42", 115200, timeout=0.05)
        self.assertEqual(fake.read.call_count, 2)
        fake.write.assert_not_called()
        fake.close.assert_called_once_with()

    def test_disconnected_thonny_is_left_alone(self):
        with (
            mock.patch.object(backends, "bridge_status", return_value={"connected": False}),
            mock.patch.object(backends, "bridge_request") as request,
            mock.patch.object(backends, "SerialBackend") as serial,
        ):
            response = call(seconds=1)
        self.assertTrue(response["isError"])
        self.assertIn("disconnected", response["content"][0]["text"])
        request.assert_not_called()
        serial.assert_not_called()

    def test_tool_schema_and_invalid_input(self):
        schema = next(entry for entry in server.handle({"method": "tools/list"})["tools"]
                      if entry["name"] == "telemetry")
        self.assertTrue(schema["annotations"]["readOnlyHint"])
        self.assertEqual(set(schema["inputSchema"]["properties"]), {"seconds", "series", "points"})
        with mock.patch.object(tools, "open_backend") as backend:
            for args in ({"seconds": 0}, {"seconds": float("nan")}, {"series": "dist"}, {"points": 51}):
                with self.subTest(args=args):
                    self.assertTrue(call(**args)["isError"])
        backend.assert_not_called()


class ViewTest(unittest.TestCase):
    def setUp(self):
        self.root = make_root()
        self.addCleanup(self.root.destroy)
        stub = WorkbenchStub(self.root)
        stub.bind = mock.Mock()
        self.addCleanup(stub.cancel_all)
        for module in (host, i18n):
            patch = mock.patch.object(module, "get_workbench", return_value=stub)
            patch.start()
            self.addCleanup(patch.stop)
        self.host = host.ThonnyHost()
        with mock.patch.object(telemetry_view.TelemetryView, "host", self.host):
            self.view = telemetry_view.TelemetryView(self.root)
        self.view.pack(fill="both", expand=True)
        self.root.geometry("600x300")
        self.root.deiconify()
        self.update_layout()

    def update_layout(self):
        self.root.update_idletasks()
        while self.root.tk.dooneevent(DONT_WAIT | WINDOW_EVENTS):
            self.root.update_idletasks()

    def emit(self, data, offset, stream="stdout"):
        with mock.patch.object(telemetry_view.time, "monotonic", return_value=self.view._started + offset):
            self.host._on_output(SimpleNamespace(data=data, stream_name=stream))

    def test_live_graph_legend_pause_clear_and_timers(self):
        self.assertIsNotNone(self.view._timer)
        self.emit("dist=", 0)
        self.emit("2 vg=0.5\n", 1)
        self.emit("dist=4 vg=0.6\n", 2)
        self.emit("ignored=8\n", 2, stream="stderr")
        with mock.patch.object(telemetry_view.time, "monotonic", return_value=self.view._started + 2):
            self.view._draw()
            canvas = self.view._canvas
            self.assertEqual(len(canvas.find_withtag("curve")), 2)
            self.assertEqual(set(self.view._legend_items), {"dist", "vg"})
            swatch = self.view._legend_items["dist"][1]
            self.assertEqual(swatch.cget("background"), telemetry_view.LIGHT["approved"])
            with mock.patch.object(telemetry_view, "_is_dark", return_value=True):
                self.view._draw()
                self.assertEqual(swatch.cget("background"), telemetry_view.DARK["approved"])
            item = self.view._legend_items["dist"][0]
            checkbutton = next(child for child in item.winfo_children() if isinstance(child, ttk.Checkbutton))
            checkbutton.invoke()
            self.view._draw()
            self.assertEqual(len(canvas.find_withtag("curve")), 1)
            self.view._pause.invoke()
        self.emit("vg=0.9\n", 3)
        self.assertEqual([v for _, v in self.view._window_data()[2]["vg"]], [0.5, 0.6])
        self.view._clear()
        self.view._draw()
        self.assertEqual(canvas.find_withtag("curve"), ())
        self.view.pack_forget()
        self.update_layout()
        self.assertIsNone(self.view._timer)
        self.view.destroy()
        self.assertEqual(self.host._output_listeners, set())

    def test_csv_uses_current_window_and_includes_hidden_series(self):
        self.emit("dist=2\n", 0)
        self.emit("dist=3 vg=0.42\n", 15)
        self.view._window.current(0)
        output = io.StringIO()
        with (
            mock.patch.object(telemetry_view.time, "monotonic", return_value=self.view._started + 20),
            mock.patch.object(telemetry_view.filedialog, "asksaveasfilename", return_value="samples.csv"),
            mock.patch("builtins.open", return_value=mock.MagicMock()) as opened,
        ):
            opened.return_value.__enter__.return_value = output
            self.view._draw()
            self.view._legend_items["dist"][2].set(False)
            self.view._export()
        rows = list(csv.reader(io.StringIO(output.getvalue())))
        self.assertEqual(rows, [["elapsed_seconds", "series", "value"], ["15.0", "dist", "3.0"],
                                ["15.0", "vg", "0.42"]])


class NiceAxisTest(unittest.TestCase):
    def test_round_steps(self):
        from thonnycontrib.thonny_ai.telemetry_view import format_tick, nice_axis

        low, high, ticks, step = nice_axis(-0.024, 1.315)
        self.assertEqual(step, 0.5)
        self.assertEqual([format_tick(v, step) for v in ticks], ["-0.5", "0.0", "0.5", "1.0", "1.5"])
        low, high, ticks, step = nice_axis(12, 87)
        self.assertEqual((low, high, step), (0, 100, 20))
        self.assertEqual([format_tick(v, step) for v in ticks], ["0", "20", "40", "60", "80", "100"])
        low, high, ticks, step = nice_axis(0.61, 0.98)
        self.assertEqual([format_tick(v, step) for v in ticks], ["0.6", "0.7", "0.8", "0.9", "1.0"])

    def test_flat_series_and_negative_zero(self):
        from thonnycontrib.thonny_ai.telemetry_view import format_tick, nice_axis

        low, high, ticks, step = nice_axis(5, 5)
        self.assertLess(low, 5)
        self.assertGreater(high, 5)
        self.assertEqual(format_tick(-1e-12, 0.1), "0.0")
