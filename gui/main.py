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
from mcmess.derived import DERIVED_COLUMNS, expand_selection  # noqa: E402
from mcmess.recorder import Recorder, auto_log_path  # noqa: E402
from mcmess.wideband import (  # noqa: E402
    add_arguments as add_wideband_args,
    from_args as make_wideband,
    make_channel,
)

# Absolute, not relative: this file is meant to run directly as
# `py gui/main.py`, where there is no parent package. The sys.path insert above
# makes the repo root importable, so `gui` resolves either way -- as a script or
# as `py -m gui.main`.
from gui import theme  # noqa: E402
from gui.picker import ChannelPicker  # noqa: E402
from gui import plots  # noqa: E402
from gui.live import LivePlot  # noqa: E402
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
        self._log_path: Path | None = None
        self._selection = []
        self._paused = False
        self._reported_error = ""
        self._viewers: list[QtWidgets.QMainWindow] = []

        # A wideband (or any second input) is not in the channel database and never
        # appears in the picker: it is not an ECU channel and cannot be ticked off
        # without also closing its port. It is appended to whatever is selected.
        self.derived_channels, self.derivations = [], []
        #: ECU channels a derivation needs that were not asked for by name. These
        #: MUST end up ticked in the picker: a derived column is computed from the
        #: sample, so if its inputs are never read it silently stays blank. That is
        #: exactly what happened to KR_CYL1..4 on the first car pull -- the expanded
        #: list was computed here and then thrown away.
        self.derivation_inputs: list[str] = []
        if getattr(args, "channels", None):
            asked = [n.strip() for n in args.channels.split(",") if n.strip()]
            expanded, self.derivations = expand_selection(
                channels, asked, knock_base=args.knock_base
            )
            self.derivation_inputs = [
                c.name for c in expanded if not c.is_virtual and c.name not in asked
            ]
            self.derived_channels = [
                c for d in self.derivations for c in d.channels()
                if c.name in asked
            ]
        self.aux_source = make_wideband(args)
        self.aux_channels = [make_channel(self.aux_source)] if self.aux_source else []
        self._extra_columns = self.derived_channels + self.aux_channels
        self._selection = list(self._extra_columns)

        part = channels.part_number or "unknown ECU"
        self.setWindowTitle(f"McMESS logger - Bosch M3.8 / {part}")
        self.resize(1480, 940)

        self._build_ui()
        self._build_timer()
        if getattr(args, "stacked", False):
            self.view_combo.setCurrentIndex(1)

        if args.demo:
            self.setWindowTitle(self.windowTitle() + "   [DEMO - simulated ECU]")
        if args.channels:
            wanted = [n.strip() for n in args.channels.split(",") if n.strip()]
            # Derived columns are legitimate names that are deliberately not in the
            # channel database -- they are computed, not read. Without this they
            # tripped the unknown-channel warning, and since it is modal the window
            # hung on a dialog nobody was there to click.
            unknown = [
                n for n in wanted
                if n not in channels.by_name and n not in DERIVED_COLUMNS
            ]
            if unknown:
                self._warn("Unknown channels", "Not in the database: " + ", ".join(unknown))
            selected = [n for n in wanted if n in channels.by_name]
            selected += [n for n in self.derivation_inputs if n not in selected]
            self.picker.set_selection(selected)
        else:
            self.picker.select_starter_set()

    # -- construction ------------------------------------------------------

    def _build_ui(self) -> None:
        self.picker = ChannelPicker(self.channel_set)
        self.picker.selection_changed.connect(self._on_selection_changed)

        # Two views of the same data. Overlay is the default: channels that
        # share a unit share a scale, which is what lets you see that TL never
        # reaches LDSHK rather than comparing two numbers across a gap. Stacked
        # stays one click away -- twenty traces on one plot is a lot, and small
        # multiples are still the better read when you have that many.
        self.plots = PlotStack()
        self.live = LivePlot(antialias=plots.ANTIALIAS)
        self.live.readout_changed.connect(self._on_readout_changed)
        self.view_stack = QtWidgets.QStackedWidget()
        self.view_stack.addWidget(self.live)
        self.view_stack.addWidget(self.plots)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        left = QtWidgets.QWidget()
        left_box = QtWidgets.QVBoxLayout(left)
        left_box.setContentsMargins(0, 0, 0, 0)
        left_box.addWidget(self.picker)
        splitter.addWidget(left)
        splitter.addWidget(self.view_stack)
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

        self.view_button = QtWidgets.QPushButton("View log...")
        self.view_button.setToolTip(
            "Open a recorded log in the viewer: traces against time, exact\n"
            "values at a cursor, min/max/mean over a zoomed pull. Defaults to\n"
            "the log this session is writing."
        )
        self.view_button.clicked.connect(self._open_viewer)
        row.addWidget(self.view_button)

        row.addStretch(1)
        if self.args.demo:
            demo = QtWidgets.QLabel("DEMO - simulated ECU, emulated rates")
            demo.setStyleSheet(f"color: {theme.STATUS_WARNING};")
            row.addWidget(demo)
            row.addSpacing(12)
        row.addWidget(QtWidgets.QLabel("View"))
        self.view_combo = QtWidgets.QComboBox()
        self.view_combo.addItem("overlay", True)
        self.view_combo.addItem("stacked", False)
        self.view_combo.setToolTip(
            "Overlay: one plot, one Y scale per unit, a cursor you can place.\n"
            "Stacked: one panel per channel, which reads better past a dozen."
        )
        self.view_combo.currentIndexChanged.connect(self._on_view_changed)
        row.addWidget(self.view_combo)

        self.auto_y_button = QtWidgets.QPushButton("Auto Y")
        self.auto_y_button.setEnabled(False)
        self.auto_y_button.setToolTip(
            "Drop every fixed Y range back to auto-fit. Right-click a channel\n"
            "on the chart or in the value table to fix one."
        )
        self.auto_y_button.clicked.connect(self._auto_ranges)
        row.addWidget(self.auto_y_button)

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
        # Which sample the value table is showing: live, your cursor, or the
        # mouse. A number on screen should never be ambiguous about that.
        self.cursor_label = QtWidgets.QLabel()
        # Wide enough that "reading last sample" is not elided to "reading la..."
        self.cursor_label.setMinimumWidth(150)
        for widget in (self.state_label, self.cursor_label, self.rate_label,
                       self.link_label, self.record_label):
            bar.addPermanentWidget(widget)
        self._set_state_text("idle")

        # Which ECU's map is loaded, always on screen. Addresses are not shared across
        # the M3.8 family, so this is not decoration: with the wrong database every
        # reading above page 0 is a different RAM cell, and still looks plausible.
        part = self.channel_set.part_number or "unknown ECU"
        caveat = QtWidgets.QLabel(
            f"{part}  -  conversions inferred from the A4 M3.8.2 damos - see docs"
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
        # Aux columns ride along with every selection, always last, so the panel
        # order does not shuffle when ECU channels are ticked on and off.
        self._selection = list(channels) + self._extra_columns
        self.plots.rebuild(self._selection)
        self.live.rebuild(self._selection)
        self._update_following()
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
        if not any(not c.is_aux for c in self._selection):
            self._warn(
                "No channels",
                "Tick at least one ECU channel before connecting. Aux inputs are "
                "sampled on the ECU's clock, so they cannot be logged on their own.",
            )
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
            channel_set=self.channel_set,
            aux_sources=[self.aux_source] if self.aux_source else [],
            derivations=self.derivations,
        )
        self.recorder.start()
        self.connect_button.setText("Disconnect")
        self.record_button.setEnabled(True)
        self.port_combo.setEnabled(False)
        self.speed_combo.setEnabled(False)

        # Record from the moment the link is up, unless told not to. A pull you
        # did not press Record for is a pull you have to drive again, and the
        # only cost of recording one you did not want is a small file: the log
        # names itself from the date and time, so nothing is overwritten.
        if self.args.autorecord and not self.record_button.isChecked():
            self.record_button.setChecked(True)
            self._toggle_recording()
        self._update_following()

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
        # Disconnected: the data has stopped moving, so the cursor is the
        # reader's. Click anywhere on the chart to read the values there.
        self._update_following()

    # -- recording ---------------------------------------------------------

    def _toggle_recording(self) -> None:
        if self.recorder is None:
            self.record_button.setChecked(False)
            return
        if self.record_button.isChecked():
            path = auto_log_path(
                self.args.log_dir,
                part_number=self.channel_set.part_number,
                tag=self.args.tag,
            )
            if self.args.ask_filename:
                chosen, _ = QtWidgets.QFileDialog.getSaveFileName(
                    self, "Record log to", str(path.resolve()), "CSV files (*.csv)"
                )
                if not chosen:
                    self.record_button.setChecked(False)
                    return
                path = Path(chosen)
            # One press, recording, no dialog: in a car there is rarely a free hand,
            # and the old default of logs/run.csv quietly overwrote the previous pull.
            self.recorder.start_recording(path)
            self._log_path = path
            self.record_button.setText("Stop recording")
        else:
            self.recorder.stop_recording()
            self.record_button.setText("Record to CSV")

    def _toggle_pause(self) -> None:
        self._paused = self.pause_button.isChecked()
        # Freezing is the same bargain as disconnecting: the view stops
        # advancing, so the cursor stops following and becomes yours. A frozen
        # chart you cannot read a value off would be a frozen chart for nothing.
        self._update_following()

    def _on_readout_changed(self, source: str) -> None:
        self.cursor_label.setText(
            f'<span style="color:{theme.TEXT_MUTED}">reading</span> '
            f'<span style="color:{theme.TEXT_SECONDARY}">{source}</span>'
            if self._overlay else ""
        )

    def _on_view_changed(self) -> None:
        self.view_stack.setCurrentIndex(0 if self._overlay else 1)
        self._on_readout_changed(self.live.source())
        self._update_following()
        self.auto_y_button.setEnabled(self._overlay and self.live.has_fixed_ranges)

    @property
    def _overlay(self) -> bool:
        return bool(self.view_combo.currentData())

    def _auto_ranges(self) -> None:
        self.live.auto_ranges()
        self.auto_y_button.setEnabled(False)

    def _update_following(self) -> None:
        """The cursor follows the newest sample only while data is arriving."""
        running = self.recorder is not None and self.recorder.state == "running"
        following = running and not self._paused
        if following != self.live.following:
            self.live.set_following(following)

    # -- the viewer --------------------------------------------------------

    def _open_viewer(self) -> None:
        """Open a recorded log in the viewer window.

        The viewer is a separate window, not a tab: in a car you want the live
        traces still on screen while you read the last pull, and on a bench you
        want them side by side.
        """
        from gui.viewer import LogViewerWindow

        window = LogViewerWindow()
        # Qt owns a window only while something references it; without this the
        # viewer is garbage-collected the moment this method returns.
        self._viewers.append(window)
        window.destroyed.connect(
            lambda: self._viewers.remove(window) if window in self._viewers else None
        )
        window.show()
        if self._log_path and self._log_path.exists():
            window.open_path(self._log_path)
        else:
            window._open_dialog()

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
        name = Path(stats.recording_path).name if stats.recording_path else ""
        self.record_label.setText(
            f"recording {stats.recording_rows} rows -> {name}" if name else ""
        )
        self.picker.set_measured_rate(stats.bytes_per_second)
        self._update_following()
        self.auto_y_button.setEnabled(self._overlay and self.live.has_fixed_ranges)

        if self._paused:
            return

        seconds = self.window_combo.currentData()
        times, data, channels = self.recorder.snapshot(
            seconds=seconds, max_points=MAX_PLOT_POINTS
        )
        if not len(times):
            return
        if self._overlay:
            self.live.update_data(times, data)
            self.live.set_window(seconds, float(times[-1]))
        elif data.shape[1] == len(self.plots.panels):
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
        "--connect",
        action="store_true",
        help="connect as soon as the window opens, instead of waiting for the button",
    )
    parser.add_argument(
        "--knock-base",
        default="auto",
        help=(
            "whether KLINFO counts cylinders 0..3 or 1..4 (0, 1, or auto). "
            "auto waits for a 0 or a 4, which settles it without guessing"
        ),
    )
    parser.add_argument(
        "--log-dir",
        default="logs",
        help="where Record to CSV writes (default: logs/)",
    )
    parser.add_argument(
        "--tag",
        default="",
        help="appended to the auto-generated log filename, e.g. --tag pull2",
    )
    parser.add_argument(
        "--ask-filename",
        action="store_true",
        help="open a save dialog instead of naming the log automatically",
    )
    parser.add_argument(
        "--stacked",
        action="store_true",
        help="open in the small-multiples view (one panel per channel) instead "
        "of the overlay. Easier to read past a dozen channels, and about 8%% "
        "cheaper in sample rate",
    )
    parser.add_argument(
        "--no-autorecord",
        dest="autorecord",
        action="store_false",
        help="do not start recording on connect; wait for the Record button "
        "(by default a CSV is written from the moment the link comes up)",
    )
    add_wideband_args(parser)
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
        elif args.connect:
            print(
                f"warning: {args.port} is not in the port list, so --connect has "
                "nothing to connect to. Press Refresh once the cable is plugged in."
            )
    window.show()

    if args.screenshot:
        return _screenshot(app, window, Path(args.screenshot))
    if args.connect:
        # Shortly after the window is up, not before: the handshake blocks its
        # thread, and starting it inside main() would show an unpainted window for
        # the second or two the init pulse and ack take.
        QtCore.QTimer.singleShot(400, window._connect)
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
