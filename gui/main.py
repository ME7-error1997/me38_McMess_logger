"""McMESS logger window.

    py gui/main.py --port COM3
    py gui/main.py --demo          # simulated ECU and engine, no hardware

The window only draws. The sampling loop runs in mcmess.recorder on its own
thread -- the serial path is blocking and lock-step, and the ECU drops the session
after 2 s of silence, so it must never share a thread with the UI. The GUI reads a
snapshot on a timer; it never touches the serial port.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pyqtgraph as pg  # noqa: E402
from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402
from serial.tools import list_ports  # noqa: E402

from mcmess.channels import ChannelSet, load_channels  # noqa: E402
from mcmess.recorder import Recorder  # noqa: E402

# Absolute, not relative: this file is meant to run directly as
# `py gui/main.py`, where there is no parent package. The sys.path insert above
# makes the repo root importable, so `gui` resolves either way -- as a script or
# as `py -m gui.main`.
from gui import theme  # noqa: E402
from gui.picker import ChannelPicker  # noqa: E402
from gui import plots  # noqa: E402
from gui.plots import PlotStack  # noqa: E402

#: Redraw rate. Drawing is NOT independent of the sampling thread: measured on
#: the bench, painting six antialiased panels cost more than half the link's
#: throughput, because pyqtgraph's paint path holds the GIL while it works.
#: Antialiasing off plus OpenGL recovers most of it (see main()), after which
#: 20 FPS is affordable. Raise this if you need the last few Hz.
REFRESH_MS = 50

#: Points drawn per panel after decimation. Beyond this the extra detail is
#: sub-pixel and only costs time.
MAX_PLOT_POINTS = 4000

WINDOWS = (("10 s", 10.0), ("30 s", 30.0), ("60 s", 60.0), ("5 min", 300.0), ("All", None))


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, channels: ChannelSet, args: argparse.Namespace) -> None:
        super().__init__()
        self.channel_set = channels
        self.args = args
        self.recorder: Recorder | None = None
        self._selection = []
        self._paused = False
        self._reported_error = ""

        self.setWindowTitle("McMESS logger - Bosch M3.8 / 06A906018CG")
        self.resize(1480, 940)

        self._build_ui()
        self._build_timer()

        if args.demo:
            self.setWindowTitle(self.windowTitle() + "   [DEMO - simulated ECU]")
        if args.channels:
            wanted = [n.strip() for n in args.channels.split(",") if n.strip()]
            unknown = [n for n in wanted if n not in channels.by_name]
            if unknown:
                self._warn("Unknown channels", "Not in the database: " + ", ".join(unknown))
            self.picker.set_selection([n for n in wanted if n in channels.by_name])
        else:
            self.picker.select_starter_set()

    # -- construction ------------------------------------------------------

    def _build_ui(self) -> None:
        self.picker = ChannelPicker(self.channel_set)
        self.picker.selection_changed.connect(self._on_selection_changed)

        self.plots = PlotStack()

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        left = QtWidgets.QWidget()
        left_box = QtWidgets.QVBoxLayout(left)
        left_box.setContentsMargins(0, 0, 0, 0)
        left_box.addWidget(self.picker)
        splitter.addWidget(left)
        splitter.addWidget(self.plots)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([400, 1080])

        central = QtWidgets.QWidget()
        box = QtWidgets.QVBoxLayout(central)
        box.setContentsMargins(8, 8, 8, 8)
        box.setSpacing(8)
        box.addWidget(self._build_toolbar())
        box.addWidget(splitter, stretch=1)
        self.setCentralWidget(central)

        self._build_statusbar()

    def _build_toolbar(self) -> QtWidgets.QWidget:
        frame = QtWidgets.QFrame()
        frame.setObjectName("panel")
        row = QtWidgets.QHBoxLayout(frame)
        row.setContentsMargins(8, 6, 8, 6)
        row.setSpacing(8)

        row.addWidget(QtWidgets.QLabel("Port"))
        self.port_combo = QtWidgets.QComboBox()
        self.port_combo.setMinimumWidth(240)
        row.addWidget(self.port_combo)

        refresh = QtWidgets.QPushButton("Refresh")
        refresh.clicked.connect(self._refresh_ports)
        row.addWidget(refresh)

        row.addSpacing(12)
        row.addWidget(QtWidgets.QLabel("Speed"))
        self.speed_combo = QtWidgets.QComboBox()
        self.speed_combo.addItem("125000 baud (highspeed)", True)
        self.speed_combo.addItem("10417 baud (slow)", False)
        self.speed_combo.setToolTip(
            "Highspeed is worth asking for even from a PC: it shortens each frame\n"
            "from 1.056 ms to 0.088 ms, taking about 2 ms off every round trip.\n"
            "Drop to slow if the cable's transceiver cannot slew at 125 kBd."
        )
        row.addWidget(self.speed_combo)

        row.addSpacing(12)
        self.connect_button = QtWidgets.QPushButton("Connect")
        self.connect_button.setObjectName("primary")
        self.connect_button.clicked.connect(self._toggle_connection)
        row.addWidget(self.connect_button)

        self.record_button = QtWidgets.QPushButton("Record to CSV")
        self.record_button.setCheckable(True)
        self.record_button.setEnabled(False)
        self.record_button.clicked.connect(self._toggle_recording)
        row.addWidget(self.record_button)

        self.pause_button = QtWidgets.QPushButton("Freeze view")
        self.pause_button.setCheckable(True)
        self.pause_button.setToolTip(
            "Stops redrawing so a trace can be read. Sampling and recording continue."
        )
        self.pause_button.clicked.connect(self._toggle_pause)
        row.addWidget(self.pause_button)

        row.addStretch(1)
        if self.args.demo:
            demo = QtWidgets.QLabel("DEMO - simulated ECU, emulated rates")
            demo.setStyleSheet(f"color: {theme.STATUS_WARNING};")
            row.addWidget(demo)
            row.addSpacing(12)
        row.addWidget(QtWidgets.QLabel("Window"))
        self.window_combo = QtWidgets.QComboBox()
        for label, seconds in WINDOWS:
            self.window_combo.addItem(label, seconds)
        self.window_combo.setCurrentIndex(1)
        row.addWidget(self.window_combo)

        self._refresh_ports()
        return frame

    def _build_statusbar(self) -> None:
        bar = self.statusBar()
        self.state_label = QtWidgets.QLabel()
        self.rate_label = QtWidgets.QLabel()
        self.link_label = QtWidgets.QLabel()
        self.record_label = QtWidgets.QLabel()
        for widget in (self.state_label, self.rate_label, self.link_label, self.record_label):
            bar.addPermanentWidget(widget)
        self._set_state_text("idle")

        caveat = QtWidgets.QLabel(
            "conversions inferred from the A4 M3.8.2 damos - see docs"
        )
        caveat.setObjectName("caveat")
        caveat.setToolTip(
            "There is no DAMOS for the 018CG. Conversion formulas come from\n"
            "8D0907558E (Audi A4, M3.8.2, DAMOS 30N); the channel list and\n"
            "addresses are the 018CG's own. Names are shared across the M3.8\n"
            "family so the formulas are expected to apply, but this is an\n"
            "inference, not a measurement.\n\n"
            "See docs/mcmess-018cg.md section 7.1."
        )
        bar.addWidget(caveat)

    def _build_timer(self) -> None:
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(REFRESH_MS)
        self.timer.timeout.connect(self._refresh)
        self.timer.start()

    # -- ports -------------------------------------------------------------

    def _refresh_ports(self) -> None:
        current = self.port_combo.currentData()
        self.port_combo.clear()
        if self.args.demo:
            self.port_combo.addItem("SIM - simulated ECU (demo)", "SIM")
        for port in list_ports.comports():
            label = f"{port.device} - {port.description}"
            if port.vid == 0x0403:
                label += "  [FTDI]"
            elif port.vid == 0x1A86:
                label += "  [CH340: mark/space parity unreliable]"
            self.port_combo.addItem(label, port.device)
        if not self.port_combo.count():
            self.port_combo.addItem("no serial ports found", None)
        if current is not None:
            index = self.port_combo.findData(current)
            if index >= 0:
                self.port_combo.setCurrentIndex(index)

    # -- selection ---------------------------------------------------------

    def _on_selection_changed(self, channels: list) -> None:
        self._selection = list(channels)
        self.plots.rebuild(self._selection)
        if self.recorder is not None:
            self.recorder.set_channels(self._selection)

    # -- connection --------------------------------------------------------

    def _toggle_connection(self) -> None:
        if self.recorder is not None:
            self._disconnect()
        else:
            self._connect()

    def _connect(self) -> None:
        port = self.port_combo.currentData()
        if port is None:
            self._warn("No serial port", "No port is selected. Plug the cable in and press Refresh.")
            return
        if not self._selection:
            self._warn("No channels", "Tick at least one channel before connecting.")
            return

        factory = None
        if port == "SIM":
            from mcmess.simulator import make_demo_serial

            factory = lambda: make_demo_serial(self.channel_set)  # noqa: E731

        self._reported_error = ""
        self.recorder = Recorder(
            port=port,
            channels=self._selection,
            highspeed=bool(self.speed_combo.currentData()),
            serial_factory=factory,
        )
        self.recorder.start()
        self.connect_button.setText("Disconnect")
        self.record_button.setEnabled(True)
        self.port_combo.setEnabled(False)
        self.speed_combo.setEnabled(False)

    def _disconnect(self) -> None:
        if self.recorder is not None:
            self.recorder.stop()
            self.recorder = None
        self.connect_button.setText("Connect")
        self.record_button.setEnabled(False)
        self.record_button.setChecked(False)
        self.port_combo.setEnabled(True)
        self.speed_combo.setEnabled(True)
        self._set_state_text("idle")

    # -- recording ---------------------------------------------------------

    def _toggle_recording(self) -> None:
        if self.recorder is None:
            self.record_button.setChecked(False)
            return
        if self.record_button.isChecked():
            default = Path.cwd() / "logs" / "run.csv"
            path, _ = QtWidgets.QFileDialog.getSaveFileName(
                self, "Record log to", str(default), "CSV files (*.csv)"
            )
            if not path:
                self.record_button.setChecked(False)
                return
            self.recorder.start_recording(path)
            self.record_button.setText("Stop recording")
        else:
            self.recorder.stop_recording()
            self.record_button.setText("Record to CSV")

    def _toggle_pause(self) -> None:
        self._paused = self.pause_button.isChecked()

    # -- refresh -----------------------------------------------------------

    def _refresh(self) -> None:
        if self.recorder is None:
            return

        state = self.recorder.state
        self._set_state_text(state)
        stats = self.recorder.stats()

        if state == "error" and stats.last_error and stats.last_error != self._reported_error:
            self._reported_error = stats.last_error
            self._disconnect()
            self._warn("Link failed", stats.last_error)
            return

        self.rate_label.setText(
            f"{stats.sample_hz:5.1f} Hz   {stats.bytes_per_second:4.0f} B/s   "
            f"{stats.wire_bytes_per_sample} B/sample"
        )
        self.link_label.setText(
            f"{stats.baudrate} baud   rtt {stats.rtt_ms:.2f} ms   "
            f"timeouts {stats.timeouts}   reconnects {stats.reconnects}"
        )
        self.record_label.setText(
            f"recording {stats.recording_rows} rows" if stats.recording_path else ""
        )
        self.picker.set_measured_rate(stats.bytes_per_second)

        if self._paused:
            return

        seconds = self.window_combo.currentData()
        times, data, channels = self.recorder.snapshot(
            seconds=seconds, max_points=MAX_PLOT_POINTS
        )
        if not len(times) or data.shape[1] != len(self.plots.panels):
            return
        self.plots.update_data(times, data)
        self.plots.set_window(seconds, float(times[-1]))
        self.plots.update_readouts(times, data, self.recorder.last_raw())

    def _set_state_text(self, state: str) -> None:
        color = theme.state_color(state)
        self.state_label.setText(
            f'<span style="color:{color}">&#9679;</span> '
            f'<span style="color:{theme.TEXT_SECONDARY}">{state}</span>'
        )

    def _warn(self, title: str, message: str) -> None:
        QtWidgets.QMessageBox.warning(self, title, message)

    # -- shutdown ----------------------------------------------------------

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if self.recorder is not None:
            self.recorder.stop()
            self.recorder = None
        super().closeEvent(event)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="McMESS logger GUI")
    parser.add_argument("--port", help="preselect this serial port, e.g. COM3")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="offer a simulated ECU with a running engine model, so the GUI can be "
        "used with no hardware",
    )
    parser.add_argument("--database", help="override the channel/conversion JSON")
    parser.add_argument(
        "--channels",
        help="comma-separated channel names to preselect, e.g. N40,TL,FR "
        "(default: the page-0 starter set)",
    )
    parser.add_argument(
        "--screenshot", help="render the window to this PNG and exit (for checking)"
    )
    parser.add_argument(
        "--smooth",
        action="store_true",
        help="antialias the traces. Prettier, but roughly halves the sample "
        "rate -- painting competes with the sampling thread",
    )
    parser.add_argument(
        "--opengl",
        action="store_true",
        help="render plots through OpenGL. Measured a further ~1.5x sample "
        "rate on the bench, but it is driver-dependent -- check the traces "
        "actually draw on your machine before trusting a log taken with it",
    )
    args = parser.parse_args(argv)

    try:
        channels = load_channels(args.database) if args.database else load_channels()
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}")
        return 1

    # Painting is the logger's main throughput cost, not the serial link.
    # Measured on a real 018CG, six channels, window visible:
    #     antialias on                 219 B/s   27 Hz
    #     antialias off                451 B/s   56 Hz
    #     antialias off + OpenGL       537 B/s   67 Hz
    # against 735 B/s with the window hidden, so painting -- not the K-line --
    # is the limit. Antialiasing is therefore off by default. OpenGL is NOT:
    # it is faster again, but it is driver-dependent and a grab() of the window
    # came back with an empty plot area, so it is opt-in via --opengl until it
    # has been eyeballed on the machine in question.
    plots.ANTIALIAS = args.smooth
    pg.setConfigOptions(
        antialias=args.smooth,
        useOpenGL=args.opengl,
        background=theme.SURFACE,
        foreground=theme.TEXT_SECONDARY,
    )

    app = QtWidgets.QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    app.setStyleSheet(theme.STYLESHEET)

    window = MainWindow(channels, args)
    if args.port:
        index = window.port_combo.findData(args.port)
        if index >= 0:
            window.port_combo.setCurrentIndex(index)
    window.show()

    if args.screenshot:
        return _screenshot(app, window, Path(args.screenshot))
    return app.exec()


def _screenshot(app: QtWidgets.QApplication, window: MainWindow, path: Path) -> int:
    """Connect, let data accumulate, grab a PNG and quit. Used to check the layout."""
    path.parent.mkdir(parents=True, exist_ok=True)

    def shoot() -> None:
        window.grab().save(str(path))
        print(f"screenshot: {path.resolve()}")
        window.close()
        app.quit()

    QtCore.QTimer.singleShot(400, window._connect)
    QtCore.QTimer.singleShot(9000, shoot)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
