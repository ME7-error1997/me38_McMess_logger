"""The window: one for making a log and for reading one.

It used to be two. The logger drew live traces and the viewer opened CSVs, and
the moment a pull ended you were looking at the wrong one. They are the same
job -- channels down the left with their values, a chart, a cursor -- and the
only real difference is whether the data is still arriving. So this is one
window with one rule:

**While data is arriving, the chart belongs to the data.** The cursor sits on
the newest sample and the values down the left are the live ones. The mouse can
not move the cursor, zoom, pan or hover: every one of those would put old
numbers on screen in the place where the live ones are read.

**When it stops, the chart belongs to the reader.** Disconnect -- or lose the
link -- and the session is opened as the log it has just become: the CSV that
was being written, re-read from disk, so what you inspect is what you saved.
Click to place the cursor, drag to zoom, read any channel at any sample.

What a session logs and what it draws are decided before connecting, in the
channel window (gui/picker.py), and kept in a profile (mcmess/profile.py). The
two lists differ on purpose: every logged channel has a row and a live value
here, but only the ticked ones get a trace.

The sampling loop runs in mcmess.recorder on its own thread -- the serial path
is blocking and lock-step, and the ECU drops the session after 2 s of silence,
so it must never share a thread with the UI. The window reads a snapshot on a
timer; it never touches the serial port.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets
from serial.tools import list_ports

from mcmess.channels import PAGE0_STARTER, ChannelSet, load_channels
from mcmess.derived import DERIVED_COLUMNS, expand_selection
from mcmess.logenums import describe_value, enum_for
from mcmess.logparse import (
    ParsedLog,
    decode_log_bytes,
    format_value,
    looks_binary,
    parse_log,
    scale_group_key,
)
from mcmess.profile import (
    DEFAULT_PROFILE,
    Profile,
    ProfileError,
    belongs_on_graph,
    load_profile,
    split_known,
)
from mcmess.recorder import Recorder, auto_log_path
from mcmess.replay import FINISHED, ReplayError, ReplayRecorder
from mcmess.wideband import from_args as make_wideband
from mcmess.wideband import make_channel

from . import theme
from .logchart import Hover, LogChart, Trace
from .picker import ChannelDialog
from .sources import Entry, LiveSource, LogSource, describer

#: Redraw rate while live. Drawing is NOT independent of the sampling thread:
#: measured on the bench, painting cost more than half the link's throughput,
#: because pyqtgraph's paint path holds the GIL while it works. 20 FPS is what
#: is affordable with antialiasing off.
REFRESH_MS = 50

#: Points handed to the chart per trace while live. Beyond this the extra
#: detail is sub-pixel and only costs time.
MAX_PLOT_POINTS = 4000

#: How often the value column and the stats repaint while data is arriving.
#: Measured on the simulator, repainting them at the full redraw rate cost about
#: a fifth of the sample rate, and numbers changing twenty times a second are
#: unreadable anyway.
VALUES_INTERVAL_S = 0.2

WINDOWS = (("10 s", 10.0), ("30 s", 30.0), ("60 s", 60.0), ("5 min", 300.0), ("All", None))

#: Common fixed Y ranges, offered on the right-click menu. A starting point --
#: custom min / max is always there too.
PRESETS: tuple[tuple[str, float, float], ...] = (
    ("Engine speed 0 - 8000 rpm", 0, 8000),
    ("Load 0 - 10 ms/Umdr.", 0, 10),
    ("Boost 0 - 3000 mbar", 0, 3000),
    ("Boost 0 - 5 bar", 0, 5),
    ("Boost 0 - 300 kPa", 0, 300),
    ("Boost -1 - 2 bar (relative)", -1, 2),
    ("Air mass 0 - 1500 mg/stk", 0, 1500),
    ("Air flow 0 - 1000 kg/h", 0, 1000),
    ("Air flow 0 - 300 g/s", 0, 300),
    ("Lambda 0.6 - 1.4", 0.6, 1.4),
    ("AFR 8 - 20", 8, 20),
    ("Temperature 0 - 150 °C", 0, 150),
    ("EGT 0 - 1000 °C", 0, 1000),
    ("Timing -10 - 50 °", -10, 50),
    ("Knock retard -15 - 0 °", -15, 0),
    ("Percent 0 - 100 %", 0, 100),
    ("Volts 0 - 16 V", 0, 16),
)

#: Opening a log bigger than this is likely a mistake -- a few hundred MB of CSV
#: is not a pull, it is a stuck logger. Asked about rather than refused.
LARGE_FILE_MB = 64

FILE_FILTER = "Logs (*.csv *.txt *.log *.tsv *.CSV);;All files (*)"

SIM_PORT = "SIM"
REPLAY_PORT = "REPLAY"

PLACEHOLDER = (
    "Connect to make a log, or open one.\n\n"
    "While data is arriving the cursor rides on the newest sample.\n"
    "Once it stops, click to place it and drag to zoom."
)


def default_args() -> argparse.Namespace:
    """What the window assumes when it is built without a command line."""
    return argparse.Namespace(
        port=None, demo=False, database=None, channels=None, profile=None,
        connect=False, select=False, knock_base="auto", log_dir="logs", tag="",
        ask_filename=False, autorecord=True, smooth=False, opengl=False,
        replay=None, replay_speed=1.0, wb_port=None, log=None, screenshot=None,
    )


def pick_color(taken: set[str], n: int) -> str:
    """A colour nobody else on the chart is using."""
    for color in theme.SERIES_EXTENDED:
        if color not in taken:
            return color
    return theme.viewer_color(n)


@dataclass
class Carry:
    """What survives the moment a live session becomes a log.

    Everything is keyed by channel *name*: live, a channel's key is its name;
    in a parsed file it is a column number. The picture should not change when
    the data stops -- same traces, same colours, same scales, same stretch of
    time on screen -- only the cursor comes free.
    """

    selected: list[str] = field(default_factory=list)
    colors: dict[str, str] = field(default_factory=dict)
    axis: str | None = None
    ranges: dict[str, tuple[float, float]] = field(default_factory=dict)
    view: tuple[float, float] | None = None


class GridModel(QtCore.QAbstractTableModel):
    """The raw grid, virtualised. Sorting reorders the whole log, not a page."""

    def __init__(self, log: ParsedLog) -> None:
        super().__init__()
        self.log = log
        self.order = list(range(len(log.rows)))

    def rowCount(self, parent=None) -> int:  # noqa: N802
        return 0 if parent and parent.isValid() else len(self.order)

    def columnCount(self, parent=None) -> int:  # noqa: N802
        return 0 if parent and parent.isValid() else len(self.log.columns)

    def log_row(self, view_row: int) -> int:
        return self.order[view_row] if 0 <= view_row < len(self.order) else 0

    def data(self, index, role=QtCore.Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        if role == QtCore.Qt.ItemDataRole.DisplayRole:
            v = self.log.rows[self.order[index.row()]][index.column()]
            return "" if v is None else format_value(v)
        if role == QtCore.Qt.ItemDataRole.TextAlignmentRole:
            return int(
                QtCore.Qt.AlignmentFlag.AlignRight
                | QtCore.Qt.AlignmentFlag.AlignVCenter
            )
        return None

    def headerData(self, section, orientation, role=QtCore.Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if role != QtCore.Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == QtCore.Qt.Orientation.Horizontal:
            column = self.log.columns[section]
            return f"{column.label} [{column.unit}]" if column.unit else column.label
        # The original log row, so a sorted view still says where a row came from.
        return str(self.order[section] + 1)

    def sort(self, column: int, order=QtCore.Qt.SortOrder.AscendingOrder) -> None:
        self.layoutAboutToBeChanged.emit()
        rows = self.log.rows
        missing = float("inf")  # empty cells sort last either way

        def key(i: int) -> float:
            v = rows[i][column]
            return missing if v is None else v

        self.order.sort(key=key, reverse=order == QtCore.Qt.SortOrder.DescendingOrder)
        self.layoutChanged.emit()


class MainWindow(QtWidgets.QMainWindow):
    """Logger and viewer. See the module docstring for the one rule."""

    COL_TICK, COL_NAME, COL_VALUE, COL_UNIT, COL_ABOUT = range(5)

    def __init__(
        self,
        channels: ChannelSet | None = None,
        args: argparse.Namespace | None = None,
        path: str | Path | None = None,
    ) -> None:
        super().__init__()
        # Before anything is built: a splitter laid out at Qt's default 640x480
        # and resized afterwards gives every extra pixel to the chart, and the
        # description column is squeezed out of the panel.
        self.resize(1560, 980)
        self.channel_set = channels if channels is not None else load_channels()
        self.args = args or default_args()
        self._describe = describer(self.channel_set)

        self.source: LogSource | LiveSource | None = None
        self.path: Path | None = None
        self.selected: list[str] = []
        self.colors: dict[str, str] = {}
        self.axis_key: str | None = None
        self.cursor_row: int | None = None
        self.hover: Hover | None = None
        self._rows: dict[str, int] = {}          # entry key -> panel row
        self._x_col: int | None | str = "own"
        self._loading = False

        self.recorder: Recorder | ReplayRecorder | None = None
        #: A seam for the tests: given the port, return something that quacks
        #: like a recorder. None means build the real one.
        self.recorder_factory = None
        #: Data is arriving and the view is tracking it. The one rule.
        self.following = False
        self._paused = False
        self._log_path: Path | None = None       # the CSV being written
        self._reported_error = ""
        self._measured_rate = 0.0
        self._values_at = 0.0
        self._windows: list[QtWidgets.QMainWindow] = []
        self.settings = QtCore.QSettings("McMESS", "logviewer")

        # A wideband (or any second input) is not in the channel database and
        # is never in a profile: it is not an ECU channel, and it cannot be
        # dropped without also closing its port. It rides along with whatever
        # is logged.
        self.aux_source = make_wideband(self.args)
        self.aux_channels = [make_channel(self.aux_source)] if self.aux_source else []
        self.derivations: list = []

        self.profile = self._initial_profile()

        self._build_ui()
        self._restore_geometry()
        self._update_title()
        self._update_profile_label()
        self._update_controls()

        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(REFRESH_MS)
        self.timer.timeout.connect(self._refresh)
        self.timer.start()

        if path:
            self.open_path(Path(path))

    # -- the profile -------------------------------------------------------

    def _initial_profile(self) -> Profile:
        """--channels, else --profile, else the last one used, else the default."""
        args = self.args
        if getattr(args, "channels", None):
            names = [n.strip() for n in args.channels.split(",") if n.strip()]
            return self._adhoc_profile(names)
        candidates = [
            getattr(args, "profile", None),
            self.settings.value("profile/last", "") if not _testing() else "",
            DEFAULT_PROFILE,
        ]
        for n, candidate in enumerate(candidates):
            if not candidate:
                continue
            path = Path(candidate)
            if not path.exists():
                if n == 0:
                    self._startup_note = f"profile {path} does not exist"
                continue
            try:
                return load_profile(path)
            except ProfileError as exc:
                if n == 0:
                    self._startup_note = str(exc)
        return self._adhoc_profile(list(PAGE0_STARTER))

    _startup_note = ""

    def _adhoc_profile(self, names: list[str]) -> Profile:
        """A profile from a bare list of names: every quantity gets a trace."""
        known = [n for n in names if self._known(n)]
        return Profile(
            log=names,
            graph=[n for n in known if belongs_on_graph(n, self._unit_of(n))],
            ecu=self.channel_set.part_number or "",
        )

    def _known(self, name: str) -> bool:
        return name in self.channel_set.by_name or name in DERIVED_COLUMNS

    def _unit_of(self, name: str) -> str:
        channel = self.channel_set.by_name.get(name)
        return channel.unit_label if channel is not None else ""

    def set_profile(self, profile: Profile) -> None:
        self.profile = profile.normalised()
        if profile.path and not _testing():
            self.settings.setValue("profile/last", str(profile.path))
        self._update_profile_label()

    def _update_profile_label(self) -> None:
        self.profile_label.setText(
            f'<span style="color:{theme.TEXT_MUTED}">profile</span> '
            f'<span style="color:{theme.TEXT_SECONDARY}">{self.profile.name}'
            f"  ·  {len(self.profile.log)} logged, "
            f"{len(self.profile.graph)} drawn</span>"
        )

    # -- construction ------------------------------------------------------

    def _build_ui(self) -> None:
        self.chart = LogChart(
            antialias=bool(getattr(self.args, "smooth", False)),
            placeholder=PLACEHOLDER,
        )
        self.chart.hovered.connect(self._on_hover)
        self.chart.cursor_placed.connect(self._on_cursor_placed)
        self.chart.context_requested.connect(self._on_chart_context)

        self.panel = self._build_panel()
        self.stats = QtWidgets.QTableWidget(0, 5)
        self.stats.setHorizontalHeaderLabels(
            ["channel", "at cursor", "min", "max", "mean"]
        )
        self.stats.verticalHeader().setVisible(False)
        self.stats.setMaximumHeight(220)
        self.stats.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
        )
        # A readout, not a list to pick from: a selection highlight would paint
        # over the colour that says which trace a row belongs to.
        self.stats.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.NoSelection
        )
        self.stats.setContextMenuPolicy(
            QtCore.Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.stats.customContextMenuRequested.connect(self._on_stats_context)
        self.stats.horizontalHeader().setSectionResizeMode(
            0, QtWidgets.QHeaderView.ResizeMode.Stretch
        )

        right = QtWidgets.QWidget()
        right_box = QtWidgets.QVBoxLayout(right)
        right_box.setContentsMargins(0, 0, 0, 0)
        right_box.setSpacing(6)
        right_box.addWidget(self._build_chart_controls())
        right_box.addWidget(self.chart, stretch=1)
        right_box.addWidget(self.stats)

        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        self.splitter.addWidget(self.panel)
        self.splitter.addWidget(right)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([590, 970])

        self.table = QtWidgets.QTableView()
        self.table.setSortingEnabled(True)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
        )

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self.splitter, "Graph")
        self.tabs.addTab(self.table, "Table")
        self.tabs.setTabEnabled(1, False)

        self.meta_label = QtWidgets.QLabel()
        self.meta_label.setObjectName("caveat")
        self.meta_label.setWordWrap(True)
        self.meta_label.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.meta_label.setVisible(False)

        central = QtWidgets.QWidget()
        box = QtWidgets.QVBoxLayout(central)
        box.setContentsMargins(8, 8, 8, 8)
        box.setSpacing(6)
        box.addWidget(self._build_toolbar())
        box.addWidget(self.meta_label)
        box.addWidget(self.tabs, stretch=1)
        self.setCentralWidget(central)

        self._build_statusbar()
        QtGui.QShortcut(
            QtGui.QKeySequence.StandardKey.Open, self, self._open_dialog
        )

    def _build_toolbar(self) -> QtWidgets.QWidget:
        frame = QtWidgets.QFrame()
        frame.setObjectName("panel")
        row = QtWidgets.QHBoxLayout(frame)
        row.setContentsMargins(8, 6, 8, 6)
        row.setSpacing(8)

        row.addWidget(QtWidgets.QLabel("Port"))
        self.port_combo = QtWidgets.QComboBox()
        self.port_combo.setMinimumWidth(220)
        row.addWidget(self.port_combo)

        self.refresh_button = QtWidgets.QPushButton("Refresh")
        self.refresh_button.clicked.connect(self._refresh_ports)
        row.addWidget(self.refresh_button)

        self.speed_combo = QtWidgets.QComboBox()
        self.speed_combo.addItem("125000 baud", True)
        self.speed_combo.addItem("10417 baud (slow)", False)
        self.speed_combo.setToolTip(
            "Highspeed is worth asking for even from a PC: it shortens each frame\n"
            "from 1.056 ms to 0.088 ms, taking about 2 ms off every round trip.\n"
            "Drop to slow if the cable's transceiver cannot slew at 125 kBd."
        )
        row.addWidget(self.speed_combo)

        row.addSpacing(8)
        self.channels_button = QtWidgets.QPushButton("Channels...")
        self.channels_button.setToolTip(
            "Choose what is logged and what is drawn, and load or save that\n"
            "choice as a profile. Connect opens this first."
        )
        self.channels_button.clicked.connect(lambda: self._choose_channels(False))
        row.addWidget(self.channels_button)

        self.connect_button = QtWidgets.QPushButton("Connect")
        self.connect_button.setObjectName("primary")
        self.connect_button.clicked.connect(self._on_connect_clicked)
        row.addWidget(self.connect_button)

        self.record_button = QtWidgets.QPushButton("Record to CSV")
        self.record_button.setCheckable(True)
        self.record_button.setEnabled(False)
        self.record_button.setToolTip(
            "Recording starts by itself when the link comes up. Press to stop,\n"
            "press again to start a new file."
        )
        self.record_button.clicked.connect(self._toggle_recording)
        row.addWidget(self.record_button)

        self.pause_button = QtWidgets.QPushButton("Freeze view")
        self.pause_button.setCheckable(True)
        self.pause_button.setEnabled(False)
        self.pause_button.setToolTip(
            "Stops the chart so a trace can be read without dropping the link:\n"
            "the cursor and zoom come free. Sampling and recording continue."
        )
        self.pause_button.clicked.connect(self._toggle_pause)
        row.addWidget(self.pause_button)

        self.profile_label = QtWidgets.QLabel()
        row.addWidget(self.profile_label)

        row.addStretch(1)
        if getattr(self.args, "demo", False):
            demo = QtWidgets.QLabel("DEMO")
            demo.setToolTip("A simulated ECU is on the port list. Emulated rates.")
            demo.setStyleSheet(f"color: {theme.STATUS_WARNING};")
            row.addWidget(demo)

        self.file_label = QtWidgets.QLabel("nothing open")
        self.file_label.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
        row.addWidget(self.file_label)

        self.open_button = QtWidgets.QPushButton("Open log...")
        self.open_button.setToolTip(
            "Open a recorded log: this logger's own, or VCDS, ME7Logger, OP-COM\n"
            "and generic CSV. While connected it opens in a second window."
        )
        self.open_button.clicked.connect(self._open_dialog)
        row.addWidget(self.open_button)

        self._refresh_ports()
        return frame

    def _build_chart_controls(self) -> QtWidgets.QWidget:
        frame = QtWidgets.QFrame()
        frame.setObjectName("panel")
        row = QtWidgets.QHBoxLayout(frame)
        row.setContentsMargins(8, 6, 8, 6)
        row.setSpacing(8)

        row.addWidget(QtWidgets.QLabel("X"))
        self.x_combo = QtWidgets.QComboBox()
        self.x_combo.setMinimumWidth(150)
        self.x_combo.currentIndexChanged.connect(self._redraw)
        row.addWidget(self.x_combo)

        row.addWidget(QtWidgets.QLabel("2nd X"))
        self.x2_combo = QtWidgets.QComboBox()
        self.x2_combo.setMinimumWidth(150)
        self.x2_combo.setToolTip(
            "A row of labels along the top, read at each X tick. The plot stays\n"
            "against time: rpm falls on every gear change, so it is not a scale."
        )
        self.x2_combo.currentIndexChanged.connect(self._redraw)
        row.addWidget(self.x2_combo)

        row.addWidget(QtWidgets.QLabel("Y"))
        self.y_combo = QtWidgets.QComboBox()
        self.y_combo.addItem("shared by unit", False)
        self.y_combo.addItem("each on its own", True)
        self.y_combo.setToolTip(
            "Shared is what puts load actual against load requested on one\n"
            "scale, so you can see that actual never reaches target."
        )
        self.y_combo.currentIndexChanged.connect(self._redraw)
        row.addWidget(self.y_combo)

        self.reset_button = QtWidgets.QPushButton("Reset zoom")
        self.reset_button.clicked.connect(self.chart.reset_zoom)
        row.addWidget(self.reset_button)

        self.auto_button = QtWidgets.QPushButton("Auto ranges")
        self.auto_button.setEnabled(False)
        self.auto_button.setToolTip("Drop every fixed Y range back to auto-fit.")
        self.auto_button.clicked.connect(self._auto_ranges)
        row.addWidget(self.auto_button)

        row.addSpacing(8)
        self.window_label = QtWidgets.QLabel("Live window")
        row.addWidget(self.window_label)
        self.window_combo = QtWidgets.QComboBox()
        for label, seconds in WINDOWS:
            self.window_combo.addItem(label, seconds)
        self.window_combo.setCurrentIndex(1)
        self.window_combo.setToolTip(
            "How much history is on screen while data is arriving."
        )
        row.addWidget(self.window_combo)

        # No hint text in this row. A label's text sets the minimum width of
        # the whole chart side, and a sentence of it squeezed the channel panel
        # until its description column was gone. It lives in the status bar.
        row.addStretch(1)
        return frame

    def _build_panel(self) -> QtWidgets.QWidget:
        wrapper = QtWidgets.QWidget()
        box = QtWidgets.QVBoxLayout(wrapper)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(6)

        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Search channels and descriptions")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_filter)
        box.addWidget(self.search)

        buttons = QtWidgets.QHBoxLayout()
        buttons.setSpacing(6)
        self.suggested_button = QtWidgets.QPushButton("Suggested")
        self.suggested_button.setToolTip(
            "Reading a log: the channels a tuner looks at first.\n"
            "Logging: the profile's graph list."
        )
        self.suggested_button.clicked.connect(self._select_suggested)
        buttons.addWidget(self.suggested_button)
        none_button = QtWidgets.QPushButton("None")
        none_button.clicked.connect(lambda: self._set_selection([]))
        buttons.addWidget(none_button)
        self.count_label = QtWidgets.QLabel()
        self.count_label.setObjectName("caveat")
        buttons.addWidget(self.count_label)
        buttons.addStretch(1)
        box.addLayout(buttons)

        self.hide_flat = QtWidgets.QCheckBox("Hide channels that never change")
        self.hide_flat.setChecked(True)
        self.hide_flat.setToolTip(
            "A channel whose min equals its max over the whole log. A plotted\n"
            "one stays on the list however flat it is. Never applied while\n"
            "logging: a channel that has not moved yet may be about to."
        )
        self.hide_flat.stateChanged.connect(self._apply_filter)
        box.addWidget(self.hide_flat)

        self.channels = QtWidgets.QTableWidget(0, 5)
        self.channels.setHorizontalHeaderLabels(
            ["", "channel", "value", "unit", "description"]
        )
        self.channels.verticalHeader().setVisible(False)
        self.channels.setShowGrid(False)
        self.channels.setWordWrap(False)
        self.channels.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.channels.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.NoSelection
        )
        self.channels.setToolTip(
            "Tick a channel to draw it. Click a drawn channel's name to give it\n"
            "the left axis. Right-click for colour and a fixed Y range."
        )
        head = self.channels.horizontalHeader()
        head.setSectionResizeMode(
            self.COL_TICK, QtWidgets.QHeaderView.ResizeMode.Fixed
        )
        head.resizeSection(self.COL_TICK, 30)
        head.resizeSection(self.COL_NAME, 104)
        head.resizeSection(self.COL_VALUE, 76)
        head.resizeSection(self.COL_UNIT, 72)
        head.setSectionResizeMode(
            self.COL_ABOUT, QtWidgets.QHeaderView.ResizeMode.Stretch
        )
        self.channels.itemChanged.connect(self._on_item_changed)
        self.channels.cellClicked.connect(self._on_cell_clicked)
        self.channels.setContextMenuPolicy(
            QtCore.Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.channels.customContextMenuRequested.connect(self._on_panel_context)
        box.addWidget(self.channels, stretch=1)
        return wrapper

    def _build_statusbar(self) -> None:
        bar = self.statusBar()
        # Which ECU's map is loaded, always on screen. Addresses are not shared
        # across the M3.8 family, so this is not decoration: with the wrong
        # database every reading above page 0 is a different RAM cell, and
        # still looks plausible.
        part = self.channel_set.part_number or "unknown ECU"
        caveat = QtWidgets.QLabel(f"{part}  ·  conversions inferred - see docs")
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
        # Which sample the values are showing: live, your cursor, or the mouse.
        # A number on screen should never be ambiguous about that.
        self.source_label = QtWidgets.QLabel()
        bar.addWidget(self.source_label)
        # What the mouse does right now, which is not always the same thing:
        # nothing while live, everything once the data has stopped.
        self.hint_label = QtWidgets.QLabel()
        self.hint_label.setObjectName("caveat")
        self.hint_label.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Ignored,
            QtWidgets.QSizePolicy.Policy.Preferred,
        )
        bar.addWidget(self.hint_label, 1)

        self.state_label = QtWidgets.QLabel()
        self.rate_label = QtWidgets.QLabel()
        self.link_label = QtWidgets.QLabel()
        self.record_label = QtWidgets.QLabel()
        self.clear_button = QtWidgets.QPushButton("Clear cursor")
        self.clear_button.setEnabled(False)
        self.clear_button.clicked.connect(lambda: self._set_cursor(None))
        for widget in (self.record_label, self.rate_label, self.link_label,
                       self.state_label, self.clear_button):
            bar.addPermanentWidget(widget)
        self._set_state_text("idle")

    # -- ports -------------------------------------------------------------

    def _refresh_ports(self) -> None:
        current = self.port_combo.currentData()
        self.port_combo.clear()
        replay = getattr(self.args, "replay", None)
        if replay:
            self.port_combo.addItem(f"REPLAY - {Path(replay).name}", REPLAY_PORT)
        if getattr(self.args, "demo", False):
            self.port_combo.addItem("SIM - simulated ECU (demo)", SIM_PORT)
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

    # -- opening a log -----------------------------------------------------

    @property
    def log(self) -> ParsedLog | None:
        """The parsed log on screen, or None while live or empty."""
        return self.source.log if isinstance(self.source, LogSource) else None

    def _open_dialog(self) -> None:
        start = str(self.path.parent) if self.path else str(
            Path(getattr(self.args, "log_dir", "logs")).resolve()
        )
        chosen, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open datalog", start, FILE_FILTER
        )
        if not chosen:
            return
        if self.recorder is not None:
            # Logging: this window is busy being the live one. The old pull
            # opens beside it rather than replacing it.
            self.open_in_new_window(Path(chosen))
        else:
            self.open_path(Path(chosen))

    def open_in_new_window(self, path: Path) -> "MainWindow":
        window = MainWindow(self.channel_set, self.args)
        # Qt owns a window only while something references it.
        self._windows.append(window)
        window.destroyed.connect(
            lambda: self._windows.remove(window) if window in self._windows else None
        )
        window.show()
        window.open_path(path)
        return window

    def _read_log(self, path: Path) -> ParsedLog | None:
        """Read, decode and parse a file. Says one sentence when it cannot.

        Every failure names its own cause, because "could not open" sends the
        reader to look for a problem in the wrong place.
        """
        try:
            size_mb = path.stat().st_size / 1e6
        except OSError as exc:
            self._warn("Cannot open", f"{path.name}: {exc.strerror or exc}")
            return None
        if size_mb > LARGE_FILE_MB:
            answer = QtWidgets.QMessageBox.question(
                self, "Large file",
                f"{path.name} is {size_mb:.0f} MB. Reading and plotting it may "
                f"take a while. Open it anyway?",
            )
            if answer != QtWidgets.QMessageBox.StandardButton.Yes:
                return None
        try:
            data = path.read_bytes()
        except OSError as exc:
            self._warn("Cannot read", f"{path.name}: {exc.strerror or exc}")
            return None
        if looks_binary(data):
            self._warn(
                "Not a text log",
                f"{path.name} looks like a binary file or an archive. Logs are "
                f"text; a .xlsx has to be saved as CSV first.",
            )
            return None
        log = parse_log(decode_log_bytes(data))
        if log is None:
            self._warn(
                "Nothing to plot",
                f"No two rows of {path.name} have their numbers in the same "
                f"columns, so there is no data grid in it -- or every column is "
                f"text. If it is a measuring-block log, check it was saved as CSV.",
            )
        return log

    def open_path(self, path: Path) -> None:
        if self.recorder is not None:
            self._warn(
                "Logging", "Disconnect first, or use Open log... to see it in a "
                "second window."
            )
            return
        log = self._read_log(path)
        if log is None:
            return
        self._save_state()
        self.path = path
        self.set_log(log)

    def set_log(self, log: ParsedLog, carry: Carry | None = None) -> None:
        """Show a parsed log. ``carry`` keeps the picture a live session had."""
        source = LogSource(log, self._describe)
        self.source = source
        self.following = False
        self.chart.set_follow_latest(False)
        self.chart.clear()
        self.chart.fixed_ranges.clear()
        self.cursor_row = None
        self.hover = None
        name = self.path.name if self.path else "unsaved session"
        self.file_label.setText(
            f"{name}  ·  {log.format}  ·  {len(log.rows)} rows  ·  "
            f"{len(log.series)} channels"
        )
        self.meta_label.setText("   ·   ".join(log.meta))
        self.meta_label.setVisible(bool(log.meta))

        self._fill_x_combos()
        self._fill_panel()

        saved = None if carry else self._load_saved()
        if carry is not None:
            # A session that has just ended: every channel that had a row a
            # second ago keeps it, however flat it turned out to be. Four knock
            # columns reading 0 for a whole pull is a result, not clutter.
            self.hide_flat.setChecked(False)
            by_label = {s.label: s.key for s in log.series}
            self.colors = {
                by_label[n]: c for n, c in carry.colors.items() if n in by_label
            }
            self.axis_key = by_label.get(carry.axis or "")
            for group, span in carry.ranges.items():
                kind, _, name_ = group.partition(":")
                if kind == "k":
                    if name_ not in by_label:
                        continue
                    group = f"k:{by_label[name_]}"
                self.chart.fixed_ranges[group] = tuple(span)
            self._set_selection(source.keys_for_labels(carry.selected), remember=False)
        elif saved and saved.get("selected"):
            self.colors = {
                k: v for k, v in saved.get("colors", {}).items() if isinstance(v, str)
            }
            self.chart.fixed_ranges.update(
                {k: tuple(v) for k, v in saved.get("ranges", {}).items()}
            )
            self._set_selection(
                [k for k in saved["selected"] if k in log.by_key], remember=False
            )
        else:
            self.colors = {}
            self._select_suggested(remember=False)

        model = GridModel(log)
        self.table.setModel(model)
        self.table.selectionModel().selectionChanged.connect(self._on_table_selection)
        self.table.resizeColumnsToContents()
        self.tabs.setTabEnabled(1, True)

        if carry is not None:
            if carry.view is not None and self.chart.plotted:
                self.chart.set_x_range(*carry.view)
            # Where the live cursor was: on the last sample. Stopping should
            # not blank the readouts, and it should not move them either.
            self._set_cursor(len(log.rows) - 1)
            self._save_state()
        self._update_title()
        self._update_controls()

    def _fill_x_combos(self) -> None:
        src = self.source
        self._loading = True
        self.x_combo.clear()
        self.x2_combo.clear()
        if src is not None:
            self.x_combo.addItem("time" if src.has_clock else "sample number", "own")
            if not src.live:
                self.x_combo.addItem("sample number", None)
                for s in src.log.series:
                    if not s.constant:
                        self.x_combo.addItem(f"{s.label} (scatter)", s.col)
            rpm = src.rpm_key()
            label = next((e.label for e in src.entries if e.key == rpm), None)
            self.x2_combo.addItem(
                f"auto ({label})" if label else "auto (none found)", "auto"
            )
            self.x2_combo.addItem("none", None)
            for entry in src.entries:
                if not entry.constant:
                    self.x2_combo.addItem(entry.label, entry.key)
        self._loading = False

    # -- the channel panel -------------------------------------------------

    def _entry(self, key: str) -> Entry | None:
        if self.source is None:
            return None
        row = self._rows.get(key)
        return self.source.entries[row] if row is not None else None

    def _fill_panel(self) -> None:
        entries = self.source.entries if self.source is not None else []
        self._loading = True
        self.channels.setRowCount(len(entries))
        self._rows = {}
        for row, entry in enumerate(entries):
            self._rows[entry.key] = row
            tick = QtWidgets.QTableWidgetItem("")
            tick.setFlags(
                QtCore.Qt.ItemFlag.ItemIsEnabled | QtCore.Qt.ItemFlag.ItemIsUserCheckable
            )
            tick.setCheckState(QtCore.Qt.CheckState.Unchecked)
            tick.setData(QtCore.Qt.ItemDataRole.UserRole, entry.key)
            self.channels.setItem(row, self.COL_TICK, tick)

            name = QtWidgets.QTableWidgetItem(entry.label)
            name.setToolTip(entry.tooltip)
            self.channels.setItem(row, self.COL_NAME, name)

            value = QtWidgets.QTableWidgetItem("")
            value.setTextAlignment(
                QtCore.Qt.AlignmentFlag.AlignRight
                | QtCore.Qt.AlignmentFlag.AlignVCenter
            )
            font = value.font()
            font.setBold(True)
            value.setFont(font)
            self.channels.setItem(row, self.COL_VALUE, value)

            unit = QtWidgets.QTableWidgetItem(entry.unit)
            unit.setForeground(QtGui.QBrush(QtGui.QColor(theme.TEXT_MUTED)))
            self.channels.setItem(row, self.COL_UNIT, unit)

            about = QtWidgets.QTableWidgetItem(entry.description)
            about.setForeground(QtGui.QBrush(QtGui.QColor(theme.TEXT_SECONDARY)))
            # The column is as wide as the window allows, and some of these run
            # long ("...after traction, gearbox and drag-torque trims").
            about.setToolTip(entry.description)
            self.channels.setItem(row, self.COL_ABOUT, about)
        self.channels.resizeRowsToContents()
        self._loading = False
        # A foreign log has names the database has never heard of, and a column
        # of blanks is wasted width.
        self.channels.setColumnHidden(
            self.COL_ABOUT, bool(entries) and not any(e.description for e in entries)
        )
        self._apply_filter()

    def _apply_filter(self) -> None:
        src = self.source
        if src is None:
            self.count_label.setText("")
            return
        needle = self.search.text().strip().lower()
        hide_flat = self.hide_flat.isChecked() and not src.live
        shown = 0
        for entry in src.entries:
            plotted = entry.key in self.selected
            hidden = (
                (needle and needle not in entry.label.lower()
                 and needle not in entry.description.lower())
                or (hide_flat and entry.constant and not plotted)
            )
            self.channels.setRowHidden(self._rows[entry.key], bool(hidden))
            shown += not hidden
        flat = sum(
            1 for e in src.entries if e.constant and e.key not in self.selected
        )
        self.count_label.setText(
            f"{len(self.selected)} drawn / {len(src.entries)} "
            + ("logged" if src.live else "channels")
            + (f"  ·  {flat} flat hidden" if hide_flat and flat else "")
            + (f"  ·  {shown} shown" if needle else "")
        )

    def _on_item_changed(self, item: QtWidgets.QTableWidgetItem) -> None:
        if self._loading or item.column() != self.COL_TICK:
            return
        key = item.data(QtCore.Qt.ItemDataRole.UserRole)
        checked = item.checkState() == QtCore.Qt.CheckState.Checked
        if checked and key not in self.selected:
            self._set_selection([*self.selected, key])
        elif not checked and key in self.selected:
            self._set_selection([k for k in self.selected if k != key])

    def _on_cell_clicked(self, row: int, column: int) -> None:
        if column != self.COL_NAME or self.source is None:
            return
        key = self.channels.item(row, self.COL_TICK).data(
            QtCore.Qt.ItemDataRole.UserRole
        )
        if key in self.selected:
            # Already drawn: a click on the name promotes it to the axis
            # channel, which owns the left axis and the cursor's reference clock.
            self.axis_key = key
            self._redraw()
            self._refresh_panel_styles()
        else:
            self._set_selection([*self.selected, key])

    def _refresh_panel_styles(self) -> None:
        if self.source is None:
            return
        for entry in self.source.entries:
            row = self._rows[entry.key]
            tick = self.channels.item(row, self.COL_TICK)
            name = self.channels.item(row, self.COL_NAME)
            plotted = entry.key in self.selected
            color = self.colors.get(entry.key)
            self._loading = True
            tick.setText("■" if plotted else "")
            tick.setForeground(QtGui.QBrush(QtGui.QColor(
                color if plotted and color else theme.TEXT_MUTED
            )))
            self._loading = False
            font = name.font()
            font.setBold(entry.key == self.axis_key)
            font.setUnderline(entry.key == self.axis_key)
            name.setFont(font)
            name.setForeground(QtGui.QBrush(QtGui.QColor(
                (color or theme.TEXT_PRIMARY) if plotted else theme.TEXT_SECONDARY
            )))

    # -- selection ---------------------------------------------------------

    def _select_suggested(self, remember: bool = True) -> None:
        src = self.source
        if src is None:
            return
        if src.live:
            keys = [n for n in self.profile.graph if n in self._rows]
            keys += [c.name for c in self.aux_channels if c.name in self._rows]
            if keys and self.axis_key not in keys:
                # The profile's first drawn channel owns the left axis, not
                # whichever happens to come first in the CSV's column order.
                self.axis_key = keys[0]
            self._set_selection(keys or src.suggested(), remember=remember)
            return
        # A log this logger wrote, read with the profile it was made with:
        # draw what the profile draws. Anything else gets the tuner's first look.
        mine = src.keys_for_labels(self.profile.graph)
        if len(mine) >= 2 and len(mine) >= len(self.profile.graph) // 2:
            self._set_selection(mine, remember=remember)
        else:
            self._set_selection(src.suggested(), remember=remember)

    def _set_selection(self, keys: list[str], remember: bool = True) -> None:
        src = self.source
        if src is None:
            return
        order = {e.key: i for i, e in enumerate(src.entries)}
        self.selected = sorted({k for k in keys if k in order}, key=lambda k: order[k])

        taken = {self.colors[k] for k in self.selected if k in self.colors}
        for n, key in enumerate(self.selected):
            if key not in self.colors:
                color = pick_color(taken, n)
                self.colors[key] = color
                taken.add(color)
        if self.axis_key not in self.selected:
            self.axis_key = self.selected[0] if self.selected else None

        self._loading = True
        for entry in src.entries:
            item = self.channels.item(self._rows[entry.key], self.COL_TICK)
            item.setCheckState(
                QtCore.Qt.CheckState.Checked if entry.key in self.selected
                else QtCore.Qt.CheckState.Unchecked
            )
        self._loading = False

        self._refresh_panel_styles()
        self._apply_filter()
        self._redraw()
        if remember:
            self._remember_selection()

    def _remember_selection(self) -> None:
        """Keep what is drawn: in the profile while logging, per file otherwise."""
        src = self.source
        if src is None:
            return
        if not src.live:
            self._save_state()
            return
        aux = {c.name for c in self.aux_channels}
        graph = [k for k in self.selected if k not in aux]
        # Drawing an input that was only being read for a computed column
        # (DWKR_Z, say) makes it a channel the profile asks for by name.
        log = self.profile.log + [k for k in graph if k not in self.profile.log]
        self.profile = Profile(log, graph, self.profile.ecu, self.profile.path)
        self._update_profile_label()

    # -- drawing -----------------------------------------------------------

    def _trace(self, key: str) -> Trace:
        entry = self._entry(key)
        xs, ys, rows = self.source.points(key, self._x_col)
        return Trace(
            key=key, label=entry.label, unit=entry.unit,
            color=self.colors.get(key, theme.SERIES[0]),
            xs=xs, ys=ys, rows=rows,
        )

    def _second_x(self, scatter: bool = False) -> Trace | None:
        second = self.x2_combo.currentData()
        if scatter or second is None or self.source is None:
            return None
        key = self.source.rpm_key() if second == "auto" else second
        return self._trace(key) if key in self._rows else None

    def _redraw(self) -> None:
        src = self.source
        if self._loading or src is None:
            return
        self._x_col = self.x_combo.currentData() if not src.live else "own"
        scatter = isinstance(self._x_col, int)
        x_label = self.x_combo.currentText().replace(" (scatter)", "")
        if self._x_col == "own":
            x_label = "seconds" if src.has_clock else "sample"

        traces = [self._trace(k) for k in self.selected] if src.n_rows else []
        self.chart.draw(
            traces,
            axis_key=self.axis_key,
            scatter=scatter,
            x_label=x_label,
            own_scales=bool(self.y_combo.currentData()),
            second_x=self._second_x(scatter),
            keep_view=True,
        )
        if traces:
            self.axis_key = self.chart.axis_key
        self.chart.set_follow_latest(self.following)
        self.auto_button.setEnabled(bool(self.chart.fixed_ranges))
        self._refresh_values()
        self._refresh_stats()

    def _draw_live(self) -> None:
        """New points for the same channels: the path taken twenty times a second."""
        traces = [self._trace(k) for k in self.selected]
        if not self.chart.update_traces(traces):
            self._redraw()
            return
        self.chart.set_second_x(self._second_x())

    # -- the active row ----------------------------------------------------

    @property
    def active_row(self) -> int:
        """Live: the newest sample, always. Otherwise mouse, else cursor.

        With neither, a log reads its first sample and a frozen live view its
        last -- the one that was on screen when it stopped.
        """
        src = self.source
        if src is None:
            return 0
        last = max(src.n_rows - 1, 0)
        if self.following:
            return last
        if self.hover is not None:
            return self.hover.row
        if self.cursor_row is not None:
            return self.cursor_row
        return last if src.live else 0

    def reading(self) -> str:
        """Where the values on screen come from, in one word."""
        if self.following:
            return "Live"
        if self.hover is not None:
            return "Mouse"
        if self.cursor_row is not None:
            return "Cursor"
        src = self.source
        return "Last sample" if src is not None and src.live else "First sample"

    def _on_hover(self, hover: Hover | None) -> None:
        if self.following:
            return
        self.hover = hover
        self._refresh_values()
        self._refresh_stats()

    def _on_cursor_placed(self, row: int) -> None:
        if not self.following:
            self._set_cursor(int(row))

    def _set_cursor(self, row: int | None) -> None:
        self.cursor_row = row
        self.chart.set_cursor_row(row)
        self.clear_button.setEnabled(row is not None and not self.following)
        self._refresh_values()
        self._refresh_stats()

    def _on_table_selection(self, selected, _deselected) -> None:
        indexes = selected.indexes()
        model = self.table.model()
        if indexes and isinstance(model, GridModel):
            self._set_cursor(model.log_row(indexes[0].row()))

    def _refresh_values(self) -> None:
        """Every channel's value at the active row, drawn or not."""
        src = self.source
        if src is None:
            self.source_label.setText("")
            return
        if not src.n_rows:
            self.source_label.setText(
                f'<span style="color:{theme.TEXT_MUTED}">waiting for the first '
                "sample</span>"
            )
            return
        row = self.active_row
        values = src.values(row)
        for entry in src.entries:
            item = self.channels.item(self._rows[entry.key], self.COL_VALUE)
            if item is not None:
                item.setText(format_value(values.get(entry.key)))

        stamp = src.time(row)
        when = f"{format_value(stamp)} s" if stamp is not None else f"row {row + 1}"
        self.source_label.setText(
            f'<span style="color:{theme.TEXT_MUTED}">reading</span> '
            f'<span style="color:'
            f'{theme.STATUS_GOOD if self.following else theme.TEXT_PRIMARY}">'
            f"{self.reading()}</span>  "
            f'<span style="color:{theme.TEXT_SECONDARY}">{when}</span>'
            f'<span style="color:{theme.TEXT_MUTED}">  ·  '
            f"sample {row + 1} of {src.n_rows}</span>"
        )

    def _refresh_stats(self) -> None:
        """Min, max and mean over the visible window, per drawn channel."""
        src = self.source
        if src is None or not src.n_rows:
            self.stats.setRowCount(0)
            return
        low, high = self.chart.visible_x_range()
        values = src.values(self.active_row)
        if self.stats.rowCount() != len(self.selected):
            self.stats.setRowCount(len(self.selected))
        for row, key in enumerate(self.selected):
            entry = self._entry(key)
            at = values.get(key)
            text = format_value(at)
            table = enum_for(entry.label)
            if table and at is not None:
                meaning = describe_value(table, at)
                if meaning:
                    text = f"{text}  {meaning}"
            xs, ys, _ = src.points(key, self._x_col)
            window = ys[(xs >= low) & (xs <= high)] if len(xs) else ys
            if not len(window):
                window = ys
            window = window[np.isfinite(window)]
            cells = [
                entry.label, text,
                format_value(float(window.min())) if len(window) else "--",
                format_value(float(window.max())) if len(window) else "--",
                format_value(float(window.mean())) if len(window) else "--",
            ]
            for column, cell in enumerate(cells):
                item = self.stats.item(row, column)
                if item is None:
                    item = QtWidgets.QTableWidgetItem()
                    if column:
                        item.setTextAlignment(
                            QtCore.Qt.AlignmentFlag.AlignRight
                            | QtCore.Qt.AlignmentFlag.AlignVCenter
                        )
                    self.stats.setItem(row, column, item)
                item.setText(cell)
                item.setData(QtCore.Qt.ItemDataRole.UserRole, key)
                if column == 0:
                    item.setForeground(QtGui.QBrush(QtGui.QColor(
                        self.colors.get(key, theme.TEXT_PRIMARY)
                    )))

    # -- the right-click menu ----------------------------------------------

    def _on_panel_context(self, point: QtCore.QPoint) -> None:
        item = self.channels.itemAt(point)
        if item is None:
            return
        tick = self.channels.item(item.row(), self.COL_TICK)
        key = tick.data(QtCore.Qt.ItemDataRole.UserRole)
        self._show_menu(self.channels.viewport().mapToGlobal(point), key)

    def _on_stats_context(self, point: QtCore.QPoint) -> None:
        item = self.stats.itemAt(point)
        if item is None:
            return
        self._show_menu(
            self.stats.viewport().mapToGlobal(point),
            item.data(QtCore.Qt.ItemDataRole.UserRole),
        )

    def _on_chart_context(self, global_point, key) -> None:
        self._show_menu(global_point, key)

    def _show_menu(self, global_point, key: str | None) -> None:
        entry = self._entry(key) if key is not None else None
        if entry is None:
            return
        menu = QtWidgets.QMenu(self)
        menu.addSection(entry.label)

        plotted = key in self.selected
        toggle = menu.addAction("Remove from the chart" if plotted else "Draw it")
        axis = menu.addAction("Make it the axis channel")
        axis.setEnabled(plotted and key != self.axis_key)

        colour_menu = menu.addMenu("Colour")
        taken = {c for k, c in self.colors.items() if k in self.selected and k != key}
        for color in theme.SERIES_EXTENDED:
            action = colour_menu.addAction(color)
            pixmap = QtGui.QPixmap(16, 16)
            pixmap.fill(QtGui.QColor(color))
            action.setIcon(QtGui.QIcon(pixmap))
            action.setData(color)
            if color in taken:
                action.setEnabled(False)
                action.setText(f"{color}  (in use)")
        colour_menu.addSeparator()
        custom_colour = colour_menu.addAction("Choose...")

        group = scale_group_key(entry, bool(self.y_combo.currentData()))
        range_menu = menu.addMenu(
            f"Y range for {entry.unit or entry.label}"
            + ("  (shared)" if group.startswith("u:") else "")
        )
        auto = range_menu.addAction("Auto (fit the visible window)")
        range_menu.addSeparator()
        for label, low, high in PRESETS:
            action = range_menu.addAction(label)
            action.setData((low, high))
        range_menu.addSeparator()
        custom_range = range_menu.addAction("Custom...")

        chosen = menu.exec(global_point)
        if chosen is None:
            return
        if chosen is toggle:
            self._set_selection(
                [k for k in self.selected if k != key] if plotted
                else [*self.selected, key]
            )
        elif chosen is axis:
            self.axis_key = key
            self._redraw()
            self._refresh_panel_styles()
        elif chosen is custom_colour:
            picked = QtWidgets.QColorDialog.getColor(
                QtGui.QColor(self.colors.get(key, theme.SERIES[0])), self, "Trace colour"
            )
            if picked.isValid():
                self._set_color(key, picked.name())
        elif chosen.parent() is colour_menu and chosen.data():
            self._set_color(key, chosen.data())
        elif chosen is auto:
            self._set_range(group, None)
        elif chosen is custom_range:
            self._ask_custom_range(group, entry)
        elif chosen.data():
            self._set_range(group, tuple(chosen.data()))

    def _set_color(self, key: str, color: str) -> None:
        self.colors[key] = color
        self._refresh_panel_styles()
        self._redraw()
        self._save_state()

    def _set_range(self, group: str, span: tuple[float, float] | None) -> None:
        self.chart.set_fixed_range(group, span)
        self.auto_button.setEnabled(bool(self.chart.fixed_ranges))
        self._save_state()

    def _ask_custom_range(self, group: str, entry: Entry) -> None:
        _, ys, _ = self.source.points(entry.key, "own")
        finite = ys[np.isfinite(ys)] if len(ys) else ys
        data = (float(finite.min()), float(finite.max())) if len(finite) else (0.0, 1.0)
        current = self.chart.fixed_ranges.get(group, data)
        low, ok = QtWidgets.QInputDialog.getDouble(
            self, "Y range", f"Minimum for {entry.unit or entry.label}:", current[0],
            -1e9, 1e9, 3,
        )
        if not ok:
            return
        high, ok = QtWidgets.QInputDialog.getDouble(
            self, "Y range", f"Maximum for {entry.unit or entry.label}:", current[1],
            -1e9, 1e9, 3,
        )
        if not ok or high <= low:
            if ok:
                self._warn("Y range", "The maximum has to be above the minimum.")
            return
        self._set_range(group, (low, high))

    def _auto_ranges(self) -> None:
        for group in list(self.chart.fixed_ranges):
            self.chart.set_fixed_range(group, None)
        self.auto_button.setEnabled(False)
        self._save_state()

    # -- connecting --------------------------------------------------------

    def _choose_channels(self, connecting: bool) -> bool:
        """The window before connecting. False when it was cancelled."""
        dialog = ChannelDialog(
            self.channel_set, self.profile, self, connecting=connecting,
            measured_bytes_per_second=self._measured_rate,
        )
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return False
        self.set_profile(dialog.profile())
        return True

    def _on_connect_clicked(self) -> None:
        if self.recorder is not None:
            self._disconnect()
            return
        port = self.port_combo.currentData()
        if port is None:
            self._warn(
                "No serial port",
                "No port is selected. Plug the cable in and press Refresh.",
            )
            return
        # A replay's channels are the log's; there is nothing to choose.
        if port != REPLAY_PORT and not self._choose_channels(True):
            return
        self.connect()

    def connect(self) -> bool:
        """Start a session on the selected port with the current profile."""
        if self.recorder is not None:
            return True
        port = self.port_combo.currentData()
        if port is None:
            self._warn(
                "No serial port",
                "No port is selected. Plug the cable in and press Refresh.",
            )
            return False

        if self.recorder_factory is not None:
            recorder = self.recorder_factory(port)
            channels = recorder.channels
        elif port == REPLAY_PORT:
            try:
                recorder = ReplayRecorder.from_file(
                    self.args.replay, self.channel_set,
                    speed=float(getattr(self.args, "replay_speed", 1.0)),
                )
            except ReplayError as exc:
                self._warn("Cannot replay", str(exc))
                return False
            channels = recorder.channels
        else:
            names, unknown = split_known(
                self.profile.log,
                set(self.channel_set.by_name) | set(DERIVED_COLUMNS),
            )
            if unknown:
                self._warn(
                    "Unknown channels",
                    "Not in this ECU's database, so not logged: "
                    + ", ".join(unknown),
                )
            channels, self.derivations = expand_selection(
                self.channel_set, names,
                knock_base=getattr(self.args, "knock_base", "auto"),
            )
            if not any(not c.is_virtual for c in channels):
                self._warn(
                    "No channels",
                    "The profile logs no ECU channel. Open Channels... and tick "
                    "at least one: computed and aux columns are sampled on the "
                    "ECU's clock, so they cannot be logged on their own.",
                )
                return False
            channels = channels + self.aux_channels
            factory = None
            if port == SIM_PORT:
                from mcmess.simulator import make_demo_serial

                factory = lambda: make_demo_serial(self.channel_set)  # noqa: E731
            recorder = Recorder(
                port=port,
                channels=channels,
                highspeed=bool(self.speed_combo.currentData()),
                serial_factory=factory,
                channel_set=self.channel_set,
                aux_sources=[self.aux_source] if self.aux_source else [],
                derivations=self.derivations,
            )

        self._save_state()          # of whatever log was on screen
        self._enter_live(channels)
        self._reported_error = ""
        self._paused = False
        self.pause_button.setChecked(False)
        self.recorder = recorder
        recorder.start()

        # Record from the moment the link is up, unless told not to. A pull you
        # did not press Record for is a pull you have to drive again, and the
        # only cost of recording one you did not want is a small file: the log
        # names itself from the date and time, so nothing is overwritten.
        self._log_path = None
        self.record_button.setChecked(False)
        if getattr(self.args, "autorecord", True) and isinstance(recorder, Recorder):
            self.record_button.setChecked(True)
            self._toggle_recording()
        self._update_following()
        self._update_controls()
        self._update_title()
        return True

    def _enter_live(self, channels: list) -> None:
        """Swap the window over to a session that is about to start."""
        self.path = None
        self.source = LiveSource(channels)
        self.chart.clear()
        self.chart.fixed_ranges.clear()
        self.chart.fixed_ranges.update(self._load_live_ranges())
        self.cursor_row = None
        self.hover = None
        self.colors = {}
        self.axis_key = None
        self.selected = []
        self.table.setModel(None)
        self.tabs.setCurrentIndex(0)
        self.tabs.setTabEnabled(1, False)
        self.meta_label.setVisible(False)
        self.file_label.setText("live")
        self._fill_x_combos()
        self._fill_panel()
        self._select_suggested(remember=False)

    def _disconnect(self, note: str = "") -> None:
        """Stop the session and hand the chart to the reader."""
        recorder = self.recorder
        if recorder is None:
            return
        was_recording = self.record_button.isChecked()
        recorded = self._log_path if was_recording else None
        carry = self._carry()
        # stop() joins the sampling thread, which closes the CSV on its way
        # out: only after this is the file on disk the whole session.
        recorder.stop()
        times, data, _channels = recorder.snapshot()
        self.recorder = None
        self._paused = False
        self.pause_button.setChecked(False)
        self.record_button.setChecked(False)
        self.record_button.setText("Record to CSV")
        self._set_state_text("idle")
        self.rate_label.setText("")
        self.link_label.setText("")
        self.record_label.setText(note)
        if isinstance(recorder, ReplayRecorder):
            self._leave_live(
                times, data, None, carry,
                unsaved_note=f"{recorder.port}: nothing was recorded again",
            )
        else:
            self._leave_live(times, data, recorded, carry)
        self._update_following()
        self._update_controls()
        self._update_title()

    def _carry(self) -> Carry:
        src = self.source
        if src is None or not src.live:
            return Carry()
        return Carry(
            selected=list(self.selected),
            colors={k: v for k, v in self.colors.items() if k in self.selected},
            axis=self.axis_key,
            ranges=dict(self.chart.fixed_ranges),
            view=self.chart.visible_x_range() if self.chart.plotted else None,
        )

    def _leave_live(
        self,
        times: np.ndarray,
        data: np.ndarray,
        recorded: Path | None,
        carry: Carry,
        unsaved_note: str = "session not recorded: this is the buffer in memory",
    ) -> None:
        """Turn the session that just ended into the log on screen.

        The CSV that was being written, when there is one: re-read from disk,
        so what you now inspect is exactly what was saved and not a buffer that
        happens to resemble it. Otherwise the buffer itself, pushed through the
        same parser so it behaves identically -- and labelled as unsaved.
        """
        src = self.source
        self.following = False
        self.chart.set_follow_latest(False)
        log = None
        path = None
        if recorded is not None and recorded.exists():
            try:
                log = parse_log(decode_log_bytes(recorded.read_bytes()))
            except OSError:
                log = None
            path = recorded if log is not None else None
        if log is None and isinstance(src, LiveSource) and src.update(times, data):
            log = src.to_log([unsaved_note])
        if log is None:
            # Fewer than two samples: there is nothing to turn into a log. The
            # window stays as it is, empty-handed but unlocked.
            self._refresh_values()
            return
        self.path = path
        self.set_log(log, carry=carry)

    # -- recording ---------------------------------------------------------

    def _toggle_recording(self) -> None:
        if not isinstance(self.recorder, Recorder):
            self.record_button.setChecked(False)
            return
        if self.record_button.isChecked():
            path = auto_log_path(
                getattr(self.args, "log_dir", "logs"),
                part_number=self.channel_set.part_number,
                tag=getattr(self.args, "tag", ""),
            )
            if getattr(self.args, "ask_filename", False):
                chosen, _ = QtWidgets.QFileDialog.getSaveFileName(
                    self, "Record log to", str(path.resolve()), "CSV files (*.csv)"
                )
                if not chosen:
                    self.record_button.setChecked(False)
                    return
                path = Path(chosen)
            # One press, recording, no dialog: in a car there is rarely a free
            # hand, and a fixed filename would overwrite the previous pull.
            self.recorder.start_recording(path)
            self._log_path = path
            self.record_button.setText("Stop recording")
        else:
            self.recorder.stop_recording()
            self.record_button.setText("Record to CSV")
        self._update_title()

    def _toggle_pause(self) -> None:
        if self.recorder is None:
            self.pause_button.setChecked(False)
            return
        self._paused = self.pause_button.isChecked()
        if self._paused and isinstance(self.source, LiveSource):
            # The whole buffer at full resolution, not the last window of it:
            # a frozen view is for looking back, and looking back means
            # zooming out past what was on screen.
            times, data, _ = self.recorder.snapshot()
            if self.source.update(times, data):
                self._update_following()
                self._redraw()
                # On the newest sample, where the live cursor was. Its row
                # number is re-taken: the snapshot just changed under it.
                self._set_cursor(self.source.n_rows - 1)
        else:
            self.cursor_row = None
            self.chart.set_cursor_row(None)
        self._update_following()
        self._update_controls()

    # -- following ---------------------------------------------------------

    def _update_following(self) -> None:
        """Following means: a session is open and the view is not frozen.

        Deliberately not "the recorder says running". Through the handshake and
        through a reconnect the session is still the live one -- handing the
        cursor to the mouse for the two seconds a reconnect takes, then
        snatching it back, would be worse than either state.
        """
        following = self.recorder is not None and not self._paused
        if following == self.following:
            return
        self.following = following
        if following:
            self.hover = None
        self.chart.set_follow_latest(following)
        self._refresh_values()
        self._refresh_stats()

    def _update_controls(self) -> None:
        connected = self.recorder is not None
        live = self.source is not None and self.source.live
        self.connect_button.setText("Disconnect" if connected else "Connect")
        self.port_combo.setEnabled(not connected)
        self.refresh_button.setEnabled(not connected)
        self.speed_combo.setEnabled(not connected)
        self.channels_button.setEnabled(not connected)
        self.record_button.setEnabled(isinstance(self.recorder, Recorder))
        self.pause_button.setEnabled(connected)
        self.x_combo.setEnabled(not live)
        self.reset_button.setEnabled(not self.following)
        self.hide_flat.setVisible(not live)
        self.window_combo.setEnabled(connected)
        self.window_label.setEnabled(connected)
        self.clear_button.setEnabled(
            self.cursor_row is not None and not self.following
        )
        self.hint_label.setText(
            "live: the cursor rides on the newest sample  ·  "
            "Disconnect or Freeze view to read back"
            if self.following else
            "drag zooms  ·  shift+drag pans  ·  click places the cursor  ·  "
            "double click resets"
        )
        self.hint_label.setToolTip(
            "While data is arriving the chart follows it: no zoom, no pan, and\n"
            "the values are the newest sample. Disconnect or Freeze view to\n"
            "read back."
            if self.following else
            "Drag a box to zoom, Shift+drag to pan, wheel to zoom, click to put\n"
            "the cursor on a sample, double click to see the whole log."
        )

    def _update_title(self) -> None:
        part = self.channel_set.part_number or "unknown ECU"
        title = f"McMESS logger - Bosch M3.8 / {part}"
        if self.recorder is not None:
            port = self.port_combo.currentText().split(" - ")[0]
            name = self._log_path.name if (
                self._log_path and self.record_button.isChecked()
            ) else "not recording"
            title += f"  -  live on {port}  ->  {name}"
            self.file_label.setText(
                f"live  ·  {name}" if name == "not recording"
                else f"live  ·  recording to {name}"
            )
        elif self.source is not None and not self.source.live:
            title += f"  -  {self.path.name if self.path else 'unsaved session'}"
        self.setWindowTitle(title)

    # -- the timer ---------------------------------------------------------

    def _refresh(self) -> None:
        recorder = self.recorder
        if recorder is None:
            return

        state = recorder.state
        self._set_state_text(state)
        stats = recorder.stats()

        if state == "error" and stats.last_error and stats.last_error != self._reported_error:
            self._reported_error = stats.last_error
            # Disconnect first, so the session that was lost is already on
            # screen as a log behind the message that says why.
            self._disconnect(note="link lost")
            self._warn("Link failed", stats.last_error)
            return
        if state == FINISHED:
            self._disconnect(note="replay finished")
            return

        if isinstance(recorder, ReplayRecorder):
            self.rate_label.setText(f"{stats.sample_hz:5.1f} Hz")
            self.link_label.setText(
                f"replay x{recorder.speed:g}  ·  {recorder.duration:.0f} s"
            )
        else:
            self.rate_label.setText(
                f"{stats.sample_hz:5.1f} Hz   {stats.bytes_per_second:4.0f} B/s   "
                f"{stats.wire_bytes_per_sample} B/sample"
            )
            self.link_label.setText(
                f"{stats.baudrate} baud   rtt {stats.rtt_ms:.2f} ms   "
                f"timeouts {stats.timeouts}   reconnects {stats.reconnects}"
            )
            self._measured_rate = stats.bytes_per_second or self._measured_rate
        name = Path(stats.recording_path).name if stats.recording_path else ""
        self.record_label.setText(
            f"recording {stats.recording_rows} rows -> {name}" if name else ""
        )
        if self._paused or not isinstance(self.source, LiveSource):
            return

        seconds = self.window_combo.currentData()
        times, data, _channels = recorder.snapshot(
            seconds=seconds, max_points=MAX_PLOT_POINTS
        )
        if not len(times) or not self.source.update(times, data):
            return
        self._draw_live()
        self.chart.set_x_window(seconds, float(times[-1]))

        now = time.monotonic()
        if now - self._values_at >= VALUES_INTERVAL_S:
            self._values_at = now
            self._refresh_values()
            self._refresh_stats()

    def _set_state_text(self, state: str) -> None:
        color = theme.state_color(state)
        self.state_label.setText(
            f'<span style="color:{color}">&#9679;</span> '
            f'<span style="color:{theme.TEXT_SECONDARY}">{state}</span>'
        )

    # -- remembered state --------------------------------------------------

    def _state_key(self) -> str | None:
        if self.path is None:
            return None
        return "log/" + str(self.path.resolve()).replace("\\", "/")

    def _load_saved(self) -> dict | None:
        key = self._state_key()
        if not key:
            return None
        raw = self.settings.value(key, "")
        if not raw:
            return None
        try:
            saved = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return saved if isinstance(saved, dict) else None

    def _load_live_ranges(self) -> dict[str, tuple[float, float]]:
        """The fixed Y ranges last used while logging.

        Live they matter more than in a log: the window slides every sample, so
        an auto-fitted scale breathes continuously. Having pinned rpm to 0-8000
        once, you should not have to do it again at every key-on.
        """
        if _testing():
            return {}
        try:
            saved = json.loads(self.settings.value("live/ranges", "") or "{}")
            return {
                str(k): (float(v[0]), float(v[1])) for k, v in saved.items()
            }
        except (TypeError, ValueError, IndexError):
            return {}

    def _save_state(self) -> None:
        """What the reader set: per file for a log, once for live sessions."""
        ranges = {k: list(v) for k, v in self.chart.fixed_ranges.items()}
        if self.source is not None and self.source.live:
            if not _testing():
                self.settings.setValue("live/ranges", json.dumps(ranges))
            return
        key = self._state_key()
        if not key or self.source is None:
            return
        self.settings.setValue(key, json.dumps({
            "selected": self.selected,
            "colors": {k: v for k, v in self.colors.items() if k in self.selected},
            "ranges": ranges,
        }))

    def _restore_geometry(self) -> None:
        if _testing():
            return
        geometry = self.settings.value("main/geometry")
        if geometry:
            self.restoreGeometry(geometry)
        sizes = self.settings.value("main/splitter")
        if sizes:
            try:
                self.splitter.setSizes([int(x) for x in sizes])
            except (TypeError, ValueError):
                pass
        for column, name in (
            (self.COL_NAME, "name"), (self.COL_VALUE, "value"), (self.COL_UNIT, "unit"),
        ):
            width = self.settings.value(f"main/panel/{name}")
            if width:
                try:
                    self.channels.horizontalHeader().resizeSection(
                        column, int(width)
                    )
                except (TypeError, ValueError):
                    pass

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802
        if self.recorder is not None:
            self.recorder.stop()
            self.recorder = None
        self.timer.stop()
        if not _testing():
            # Never from a test: the offscreen platform's screen is 800 pixels
            # square, and a geometry saved there reopens the real window
            # squashed into a strip.
            self.settings.setValue("main/geometry", self.saveGeometry())
            self.settings.setValue("main/splitter", self.splitter.sizes())
            head = self.channels.horizontalHeader()
            self.settings.setValue("main/panel/name", head.sectionSize(self.COL_NAME))
            self.settings.setValue("main/panel/value", head.sectionSize(self.COL_VALUE))
            self.settings.setValue("main/panel/unit", head.sectionSize(self.COL_UNIT))
        self._save_state()
        super().closeEvent(event)

    # -- helpers -----------------------------------------------------------

    def _warn(self, title: str, message: str) -> None:
        QtWidgets.QMessageBox.warning(self, title, message)


def _testing() -> bool:
    """Under the offscreen platform nothing global is read or written.

    The tests build windows by the dozen; one of them leaving its profile or
    its Y ranges in the reader's real settings would change what the program
    does the next time it is started for real.
    """
    import os

    return os.environ.get("QT_QPA_PLATFORM", "") == "offscreen"
