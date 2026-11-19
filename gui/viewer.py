"""The datalog viewer: open a log, plot it, read exact values off it.

    py gui/viewer.py                      # open empty, then File > Open
    py gui/viewer.py logs/432.csv         # straight into a log

It opens this logger's own CSVs and other people's: VCDS measuring blocks and
advanced measuring blocks, ME7Logger, OP-COM, and generic CSV from MHD,
bootmod3 or anything else that writes text. mcmess/logparse.py does the reading;
this file is the window around it.

The shape of it, and why:

**Which row the values come from.** ``active_row = hover row, else cursor row,
else 0``. Under the mouse while it is over the chart, else the cursor the reader
placed with a click, else the first sample. A label says which, so a number on
screen is never ambiguous about where it came from.

**Every channel shows a value, plotted or not.** The channel panel reads the grid
on every mouse move -- 150 channels is 150 row lookups, not 150 arrays -- so you
can scan down the whole list at one instant of a pull without plotting anything.

**A cursor is a log row, not a time.** See the note in gui/logchart.py.

**The chart does not own the readout.** The panel, the stats table under the
chart and the tooltip all name their channels, which is what makes it legal for
the overlay chart to run past eight colours (see theme.SERIES_EXTENDED).

Two deliberate differences from the reference implementation this was ported
from: the raw-data tab is a virtualised Qt model over the whole grid rather than
100-row pages -- Qt only builds the visible rows anyway, so paging would be work
for nothing -- and the geometry the reader drags is remembered in QSettings
rather than browser storage.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pyqtgraph as pg  # noqa: E402
from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402

from mcmess.logenums import describe_value, enum_for  # noqa: E402
from mcmess.logparse import (  # noqa: E402
    LogSeries,
    ParsedLog,
    decode_log_bytes,
    default_selection,
    find_rpm_series,
    format_value,
    looks_binary,
    parse_log,
    row_time,
    row_values,
    scale_group_key,
    series_points,
)

from gui import theme  # noqa: E402
from gui.logchart import Hover, LogChart, Trace  # noqa: E402

#: Common fixed Y ranges, offered on the right-click menu. A starting point --
#: custom min / max is always there too.
PRESETS: tuple[tuple[str, float, float], ...] = (
    ("Boost 0 - 3000 mbar", 0, 3000),
    ("Boost 0 - 5 bar", 0, 5),
    ("Boost 0 - 300 kPa", 0, 300),
    ("Boost -1 - 2 bar (relative)", -1, 2),
    ("Load 0 - 10 ms/Umdr.", 0, 10),
    ("Air mass 0 - 1500 mg/stk", 0, 1500),
    ("Air flow 0 - 1000 kg/h", 0, 1000),
    ("Air flow 0 - 300 g/s", 0, 300),
    ("Engine speed 0 - 8000 rpm", 0, 8000),
    ("Lambda 0.6 - 1.4", 0.6, 1.4),
    ("AFR 8 - 20", 8, 20),
    ("Temperature 0 - 150 °C", 0, 150),
    ("EGT 0 - 1000 °C", 0, 1000),
    ("Timing -10 - 50 °", -10, 50),
    ("Knock retard -15 - 0 °", -15, 0),
    ("Percent 0 - 100 %", 0, 100),
)

#: Opening a log bigger than this is likely a mistake -- a few hundred MB of CSV
#: is not a pull, it is a stuck logger. Asked about rather than refused.
LARGE_FILE_MB = 64

FILE_FILTER = "Logs (*.csv *.txt *.log *.tsv *.CSV);;All files (*)"


def pick_color(taken: set[str], n: int) -> str:
    """A colour nobody else on the chart is using."""
    for color in theme.SERIES_EXTENDED:
        if color not in taken:
            return color
    return theme.viewer_color(n)


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


class LogViewerWindow(QtWidgets.QMainWindow):
    """One window, one log."""

    COL_TICK, COL_NAME, COL_VALUE, COL_UNIT = range(4)

    def __init__(self, path: str | Path | None = None) -> None:
        super().__init__()
        self.setWindowTitle("McMESS log viewer")
        self.resize(1560, 980)

        self.log: ParsedLog | None = None
        self.path: Path | None = None
        self.selected: list[str] = []
        self.colors: dict[str, str] = {}
        self.axis_key: str | None = None
        self.cursor_row: int | None = None
        self.hover: Hover | None = None
        self._rows: dict[str, int] = {}          # series key -> panel row
        self._points_cache: dict[tuple, tuple] = {}
        self._x_col: int | None | str = "own"
        self._loading = False
        self.settings = QtCore.QSettings("McMESS", "logviewer")

        self._build_ui()
        self._restore_geometry()
        if path:
            self.open_path(Path(path))

    # -- construction ------------------------------------------------------

    def _build_ui(self) -> None:
        self.chart = LogChart()
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
        head = self.stats.horizontalHeader()
        head.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)

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
        self.splitter.setSizes([460, 1100])

        self.table = QtWidgets.QTableView()
        self.table.setSortingEnabled(True)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
        )

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self.splitter, "Graph")
        self.tabs.addTab(self.table, "Table")

        self.meta_label = QtWidgets.QLabel()
        self.meta_label.setObjectName("caveat")
        self.meta_label.setWordWrap(True)
        self.meta_label.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )

        central = QtWidgets.QWidget()
        box = QtWidgets.QVBoxLayout(central)
        box.setContentsMargins(8, 8, 8, 8)
        box.setSpacing(6)
        box.addWidget(self._build_toolbar())
        box.addWidget(self.meta_label)
        box.addWidget(self.tabs, stretch=1)
        self.setCentralWidget(central)

        self.source_label = QtWidgets.QLabel()
        self.clear_button = QtWidgets.QPushButton("Clear cursor")
        self.clear_button.setEnabled(False)
        self.clear_button.clicked.connect(lambda: self._set_cursor(None))
        bar = self.statusBar()
        bar.addWidget(self.source_label)
        bar.addPermanentWidget(self.clear_button)

        QtGui.QShortcut(
            QtGui.QKeySequence.StandardKey.Open, self, self._open_dialog
        )

    def _build_toolbar(self) -> QtWidgets.QWidget:
        frame = QtWidgets.QFrame()
        frame.setObjectName("panel")
        row = QtWidgets.QHBoxLayout(frame)
        row.setContentsMargins(8, 6, 8, 6)
        row.setSpacing(8)

        open_button = QtWidgets.QPushButton("Open log...")
        open_button.setObjectName("primary")
        open_button.clicked.connect(self._open_dialog)
        row.addWidget(open_button)

        self.file_label = QtWidgets.QLabel("no log open")
        self.file_label.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
        row.addWidget(self.file_label)
        row.addStretch(1)
        return frame

    def _build_chart_controls(self) -> QtWidgets.QWidget:
        frame = QtWidgets.QFrame()
        frame.setObjectName("panel")
        row = QtWidgets.QHBoxLayout(frame)
        row.setContentsMargins(8, 6, 8, 6)
        row.setSpacing(8)

        row.addWidget(QtWidgets.QLabel("X"))
        self.x_combo = QtWidgets.QComboBox()
        self.x_combo.setMinimumWidth(180)
        self.x_combo.currentIndexChanged.connect(self._redraw)
        row.addWidget(self.x_combo)

        row.addWidget(QtWidgets.QLabel("2nd X"))
        self.x2_combo = QtWidgets.QComboBox()
        self.x2_combo.setMinimumWidth(180)
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
            "Shared is what puts boost actual against boost requested on one\n"
            "scale, so you can see that actual never reaches target."
        )
        self.y_combo.currentIndexChanged.connect(self._redraw)
        row.addWidget(self.y_combo)

        reset = QtWidgets.QPushButton("Reset zoom")
        reset.clicked.connect(self.chart.reset_zoom)
        row.addWidget(reset)

        self.auto_button = QtWidgets.QPushButton("Auto ranges")
        self.auto_button.setEnabled(False)
        self.auto_button.setToolTip("Drop every fixed Y range back to auto-fit.")
        self.auto_button.clicked.connect(self._auto_ranges)
        row.addWidget(self.auto_button)

        row.addStretch(1)
        hint = QtWidgets.QLabel(
            "drag zooms  ·  shift+drag pans  ·  wheel zooms  ·  "
            "click places the cursor  ·  double click resets"
        )
        hint.setObjectName("caveat")
        row.addWidget(hint)
        return frame

    def _build_panel(self) -> QtWidgets.QWidget:
        wrapper = QtWidgets.QWidget()
        box = QtWidgets.QVBoxLayout(wrapper)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(6)

        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Search channels")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_filter)
        box.addWidget(self.search)

        buttons = QtWidgets.QHBoxLayout()
        buttons.setSpacing(6)
        suggested = QtWidgets.QPushButton("Suggested")
        suggested.setToolTip("The channels a tuner looks at first in this log.")
        suggested.clicked.connect(self._select_suggested)
        buttons.addWidget(suggested)
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
            "one stays on the list however flat it is."
        )
        self.hide_flat.stateChanged.connect(self._apply_filter)
        box.addWidget(self.hide_flat)

        self.channels = QtWidgets.QTableWidget(0, 4)
        self.channels.setHorizontalHeaderLabels(["", "channel", "value", "unit"])
        self.channels.verticalHeader().setVisible(False)
        self.channels.setShowGrid(False)
        self.channels.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.channels.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.NoSelection
        )
        head = self.channels.horizontalHeader()
        head.setSectionResizeMode(
            self.COL_TICK, QtWidgets.QHeaderView.ResizeMode.Fixed
        )
        head.resizeSection(self.COL_TICK, 30)
        head.setSectionResizeMode(
            self.COL_NAME, QtWidgets.QHeaderView.ResizeMode.Stretch
        )
        head.resizeSection(self.COL_VALUE, 84)
        head.resizeSection(self.COL_UNIT, 64)
        self.channels.itemChanged.connect(self._on_item_changed)
        self.channels.cellClicked.connect(self._on_cell_clicked)
        self.channels.setContextMenuPolicy(
            QtCore.Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.channels.customContextMenuRequested.connect(self._on_panel_context)
        box.addWidget(self.channels, stretch=1)
        return wrapper

    # -- opening -----------------------------------------------------------

    def _open_dialog(self) -> None:
        start = str(self.path.parent) if self.path else str(
            Path(__file__).resolve().parent.parent / "logs"
        )
        chosen, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open datalog", start, FILE_FILTER
        )
        if chosen:
            self.open_path(Path(chosen))

    def open_path(self, path: Path) -> None:
        """Read, decode and parse a file, then draw it.

        Every failure here says one sentence about what is wrong, because "could
        not open" sends the reader to look for a problem in the wrong place.
        """
        try:
            size_mb = path.stat().st_size / 1e6
        except OSError as exc:
            self._warn("Cannot open", f"{path.name}: {exc.strerror or exc}")
            return
        if size_mb > LARGE_FILE_MB:
            answer = QtWidgets.QMessageBox.question(
                self, "Large file",
                f"{path.name} is {size_mb:.0f} MB. Reading and plotting it may "
                f"take a while. Open it anyway?",
            )
            if answer != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        try:
            data = path.read_bytes()
        except OSError as exc:
            self._warn("Cannot read", f"{path.name}: {exc.strerror or exc}")
            return
        if looks_binary(data):
            self._warn(
                "Not a text log",
                f"{path.name} looks like a binary file or an archive. Logs are "
                f"text; a .xlsx has to be saved as CSV first.",
            )
            return
        log = parse_log(decode_log_bytes(data))
        if log is None:
            self._warn(
                "Nothing to plot",
                f"No two rows of {path.name} have their numbers in the same "
                f"columns, so there is no data grid in it -- or every column is "
                f"text. If it is a measuring-block log, check it was saved as CSV.",
            )
            return
        self.path = path
        self.set_log(log)

    def set_log(self, log: ParsedLog) -> None:
        self.log = log
        self.chart.clear()
        self.chart.fixed_ranges.clear()
        self._points_cache.clear()
        self.cursor_row = None
        self.hover = None
        name = self.path.name if self.path else "log"
        self.file_label.setText(
            f"{name}  ·  {log.format}  ·  {len(log.rows)} rows  ·  "
            f"{len(log.series)} channels"
        )
        self.setWindowTitle(f"McMESS log viewer - {name}")
        self.meta_label.setText("   ·   ".join(log.meta))
        self.meta_label.setVisible(bool(log.meta))

        self._fill_x_combos()
        self._fill_panel()
        saved = self._load_saved()
        if saved and saved.get("selected"):
            self.colors = {
                k: v for k, v in saved.get("colors", {}).items() if isinstance(v, str)
            }
            self.chart.fixed_ranges = {
                k: tuple(v) for k, v in saved.get("ranges", {}).items()
            }
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

    def _fill_x_combos(self) -> None:
        self._loading = True
        has_clock = any(c.is_time for c in self.log.columns)
        self.x_combo.clear()
        self.x_combo.addItem("time" if has_clock else "sample number", "own")
        self.x_combo.addItem("sample number", None)
        for s in self.log.series:
            if not s.constant:
                self.x_combo.addItem(f"{s.label} (scatter)", s.col)

        self.x2_combo.clear()
        rpm = find_rpm_series(self.log)
        self.x2_combo.addItem(
            f"auto ({rpm.label})" if rpm else "auto (none found)", "auto"
        )
        self.x2_combo.addItem("none", None)
        for s in self.log.series:
            if not s.constant:
                self.x2_combo.addItem(s.label, s.key)
        self._loading = False

    # -- the channel panel -------------------------------------------------

    def _fill_panel(self) -> None:
        self._loading = True
        self.channels.setRowCount(len(self.log.series))
        self._rows = {}
        for row, s in enumerate(self.log.series):
            self._rows[s.key] = row
            tick = QtWidgets.QTableWidgetItem("")
            tick.setFlags(
                QtCore.Qt.ItemFlag.ItemIsEnabled | QtCore.Qt.ItemFlag.ItemIsUserCheckable
            )
            tick.setCheckState(QtCore.Qt.CheckState.Unchecked)
            tick.setData(QtCore.Qt.ItemDataRole.UserRole, s.key)
            self.channels.setItem(row, self.COL_TICK, tick)

            name = QtWidgets.QTableWidgetItem(s.label)
            name.setToolTip(self._channel_tooltip(s))
            self.channels.setItem(row, self.COL_NAME, name)

            value = QtWidgets.QTableWidgetItem("")
            value.setTextAlignment(
                QtCore.Qt.AlignmentFlag.AlignRight
                | QtCore.Qt.AlignmentFlag.AlignVCenter
            )
            self.channels.setItem(row, self.COL_VALUE, value)

            unit = QtWidgets.QTableWidgetItem(s.unit)
            unit.setForeground(QtGui.QBrush(QtGui.QColor(theme.TEXT_MUTED)))
            self.channels.setItem(row, self.COL_UNIT, unit)
        self.channels.resizeRowsToContents()
        self._loading = False
        self._apply_filter()

    def _channel_tooltip(self, s: LogSeries) -> str:
        table = enum_for(s.label)
        if table:
            if table.kind == "flags":
                return "A bit mask: several meanings can be set at once."
            known = ", ".join(
                f"{k} = {v}" for k, v in list(table.values.items())[:10]
            )
            return known or "A code, not a quantity."
        unit = f" {s.unit}" if s.unit else ""
        return (
            f"{format_value(s.min)} ... {format_value(s.max)}{unit}"
            f"   ({s.count} samples)"
            + ("\nnever changes over this log" if s.constant else "")
        )

    def _apply_filter(self) -> None:
        if self.log is None:
            return
        needle = self.search.text().strip().lower()
        hide_flat = self.hide_flat.isChecked()
        shown = 0
        for s in self.log.series:
            row = self._rows[s.key]
            plotted = s.key in self.selected
            hidden = (
                (needle and needle not in s.label.lower())
                or (hide_flat and s.constant and not plotted)
            )
            self.channels.setRowHidden(row, bool(hidden))
            shown += not hidden
        flat = sum(
            1 for s in self.log.series if s.constant and s.key not in self.selected
        )
        self.count_label.setText(
            f"{len(self.selected)} plotted / {len(self.log.series)} channels"
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
        if column != self.COL_NAME or self.log is None:
            return
        key = self.channels.item(row, self.COL_TICK).data(
            QtCore.Qt.ItemDataRole.UserRole
        )
        if key in self.selected:
            # Already plotted: a click on the name promotes it to the axis
            # channel, which owns the left axis and the cursor's reference clock.
            self.axis_key = key
            self._redraw()
            self._refresh_panel_styles()
        else:
            self._set_selection([*self.selected, key])

    def _refresh_panel_styles(self) -> None:
        for s in self.log.series:
            row = self._rows[s.key]
            tick = self.channels.item(row, self.COL_TICK)
            name = self.channels.item(row, self.COL_NAME)
            plotted = s.key in self.selected
            color = self.colors.get(s.key)
            tick.setText("■" if plotted else "")
            tick.setForeground(QtGui.QBrush(QtGui.QColor(
                color or theme.TEXT_MUTED
            )))
            font = name.font()
            font.setBold(s.key == self.axis_key)
            font.setUnderline(s.key == self.axis_key)
            name.setFont(font)
            name.setForeground(QtGui.QBrush(QtGui.QColor(
                theme.TEXT_PRIMARY if plotted else theme.TEXT_SECONDARY
            )))

    # -- selection ---------------------------------------------------------

    def _select_suggested(self, remember: bool = True) -> None:
        if self.log is not None:
            self._set_selection(default_selection(self.log), remember=remember)

    def _set_selection(self, keys: list[str], remember: bool = True) -> None:
        if self.log is None:
            return
        order = {s.key: i for i, s in enumerate(self.log.series)}
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
        for s in self.log.series:
            item = self.channels.item(self._rows[s.key], self.COL_TICK)
            item.setCheckState(
                QtCore.Qt.CheckState.Checked if s.key in self.selected
                else QtCore.Qt.CheckState.Unchecked
            )
        self._loading = False

        self._refresh_panel_styles()
        self._apply_filter()
        self._redraw()
        if remember:
            self._save_state()

    # -- drawing -----------------------------------------------------------

    def _points_for(self, s: LogSeries) -> tuple:
        """Cached (xs, ys, rows) for one channel against the current X axis.

        The chart is handed finished points, so it never has to know what a
        parsed log is -- which is what lets the live logger feed it slices of a
        ring buffer instead. Caching here means switching the axis channel, the
        colours or the scale mode costs no re-extraction.
        """
        col = s.time_col if self._x_col == "own" else self._x_col
        key = (s.key, col)
        hit = self._points_cache.get(key)
        if hit is None:
            xs, ys, rows = series_points(self.log, s, col)
            hit = (
                np.asarray(xs, dtype=float),
                np.asarray(ys, dtype=float),
                np.asarray(rows, dtype=int),
            )
            self._points_cache[key] = hit
        return hit

    def _trace(self, s: LogSeries) -> Trace:
        xs, ys, rows = self._points_for(s)
        return Trace(
            key=s.key, label=s.label, unit=s.unit,
            color=self.colors.get(s.key, theme.SERIES[0]),
            xs=xs, ys=ys, rows=rows,
        )

    def _redraw(self) -> None:
        if self._loading or self.log is None:
            return
        by_key = self.log.by_key
        self._x_col = self.x_combo.currentData()
        scatter = isinstance(self._x_col, int)
        x_label = self.x_combo.currentText().replace(" (scatter)", "")
        if self._x_col == "own":
            x_label = "seconds" if any(c.is_time for c in self.log.columns) else "sample"

        second = self.x2_combo.currentData()
        if scatter or second is None:
            second_series = None
        elif second == "auto":
            second_series = find_rpm_series(self.log)
        else:
            second_series = by_key.get(second)

        self.chart.draw(
            [self._trace(by_key[k]) for k in self.selected],
            axis_key=self.axis_key,
            scatter=scatter,
            x_label=x_label,
            own_scales=bool(self.y_combo.currentData()),
            second_x=self._trace(second_series) if second_series else None,
            keep_view=True,
        )
        self.axis_key = self.chart.axis_key
        self.auto_button.setEnabled(bool(self.chart.fixed_ranges))
        self._refresh_values()
        self._refresh_stats()

    # -- the active row ----------------------------------------------------

    @property
    def active_row(self) -> int:
        if self.hover is not None:
            return self.hover.row
        if self.cursor_row is not None:
            return self.cursor_row
        return 0

    def _on_hover(self, hover: Hover | None) -> None:
        self.hover = hover
        self._refresh_values()
        self._refresh_stats()

    def _on_cursor_placed(self, row: int) -> None:
        self._set_cursor(int(row))

    def _set_cursor(self, row: int | None) -> None:
        self.cursor_row = row
        self.chart.set_cursor_row(row)
        self.clear_button.setEnabled(row is not None)
        self._refresh_values()
        self._refresh_stats()

    def _on_table_selection(self, selected, _deselected) -> None:
        indexes = selected.indexes()
        model = self.table.model()
        if indexes and isinstance(model, GridModel):
            self._set_cursor(model.log_row(indexes[0].row()))

    def _refresh_values(self) -> None:
        """Every channel's value at the active row, plotted or not."""
        if self.log is None or not self.log.rows:
            return
        row = self.active_row
        values = row_values(self.log, row)
        for s in self.log.series:
            item = self.channels.item(self._rows[s.key], self.COL_VALUE)
            if item is not None:
                item.setText(format_value(values.get(s.key)))

        where = (
            "Mouse" if self.hover is not None
            else "Cursor" if self.cursor_row is not None
            else "First sample"
        )
        stamp = row_time(self.log, row)
        when = f"{format_value(stamp)} s" if stamp is not None else f"row {row + 1}"
        self.source_label.setText(
            f'<span style="color:{theme.TEXT_MUTED}">{where}</span>  '
            f'<span style="color:{theme.TEXT_SECONDARY}">{when}</span>'
            f'<span style="color:{theme.TEXT_MUTED}">  ·  '
            f"sample {row + 1} of {len(self.log.rows)}</span>"
        )

    def _refresh_stats(self) -> None:
        """Min, max and mean over the zoomed window, per plotted channel."""
        if self.log is None:
            return
        low, high = self.chart.visible_x_range()
        values = row_values(self.log, self.active_row)
        self.stats.setRowCount(len(self.selected))
        for row, key in enumerate(self.selected):
            s = self.log.by_key[key]
            at = values.get(key)
            text = format_value(at)
            table = enum_for(s.label)
            if table and at is not None:
                meaning = describe_value(table, at)
                if meaning:
                    text = f"{text}  {meaning}"
            xs, ys, _ = self._points_for(s)
            window = ys[(xs >= low) & (xs <= high)] if len(xs) else ys
            if not len(window):
                window = ys
            cells = [
                s.label, text,
                format_value(float(window.min())) if len(window) else "--",
                format_value(float(window.max())) if len(window) else "--",
                format_value(float(window.mean())) if len(window) else "--",
            ]
            for column, cell in enumerate(cells):
                item = QtWidgets.QTableWidgetItem(cell)
                item.setData(QtCore.Qt.ItemDataRole.UserRole, key)
                if column == 0:
                    item.setForeground(QtGui.QBrush(QtGui.QColor(
                        self.colors.get(key, theme.TEXT_PRIMARY)
                    )))
                else:
                    item.setTextAlignment(
                        QtCore.Qt.AlignmentFlag.AlignRight
                        | QtCore.Qt.AlignmentFlag.AlignVCenter
                    )
                self.stats.setItem(row, column, item)

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
        if self.log is None or key is None:
            return
        s = self.log.by_key[key]
        menu = QtWidgets.QMenu(self)
        menu.addSection(s.label)

        plotted = key in self.selected
        toggle = menu.addAction("Remove from the chart" if plotted else "Plot it")
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

        group = scale_group_key(s, bool(self.y_combo.currentData()))
        range_menu = menu.addMenu(
            f"Y range for {s.unit or s.label}"
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
                self.colors[key] = picked.name()
                self._refresh_panel_styles()
                self._redraw()
                self._save_state()
        elif chosen.parent() is colour_menu and chosen.data():
            self.colors[key] = chosen.data()
            self._refresh_panel_styles()
            self._redraw()
            self._save_state()
        elif chosen is auto:
            self.chart.set_fixed_range(group, None)
            self.auto_button.setEnabled(bool(self.chart.fixed_ranges))
            self._save_state()
        elif chosen is custom_range:
            self._ask_custom_range(group, s)
        elif chosen.data():
            self.chart.set_fixed_range(group, tuple(chosen.data()))
            self.auto_button.setEnabled(True)
            self._save_state()

    def _ask_custom_range(self, group: str, s: LogSeries) -> None:
        current = self.chart.fixed_ranges.get(group, (s.min, s.max))
        low, ok = QtWidgets.QInputDialog.getDouble(
            self, "Y range", f"Minimum for {s.unit or s.label}:", current[0],
            -1e9, 1e9, 3,
        )
        if not ok:
            return
        high, ok = QtWidgets.QInputDialog.getDouble(
            self, "Y range", f"Maximum for {s.unit or s.label}:", current[1],
            -1e9, 1e9, 3,
        )
        if not ok or high <= low:
            if ok:
                self._warn("Y range", "The maximum has to be above the minimum.")
            return
        self.chart.set_fixed_range(group, (low, high))
        self.auto_button.setEnabled(True)
        self._save_state()

    def _auto_ranges(self) -> None:
        for group in list(self.chart.fixed_ranges):
            self.chart.set_fixed_range(group, None)
        self.auto_button.setEnabled(False)
        self._save_state()

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

    def _save_state(self) -> None:
        key = self._state_key()
        if not key:
            return
        self.settings.setValue(key, json.dumps({
            "selected": self.selected,
            "colors": {k: v for k, v in self.colors.items() if k in self.selected},
            "ranges": {k: list(v) for k, v in self.chart.fixed_ranges.items()},
        }))

    def _restore_geometry(self) -> None:
        geometry = self.settings.value("window/geometry")
        if geometry:
            self.restoreGeometry(geometry)
        sizes = self.settings.value("window/splitter")
        if sizes:
            try:
                self.splitter.setSizes([int(x) for x in sizes])
            except (TypeError, ValueError):
                pass
        for column, name in ((self.COL_VALUE, "value"), (self.COL_UNIT, "unit")):
            width = self.settings.value(f"panel/{name}")
            if width:
                try:
                    self.channels.horizontalHeader().resizeSection(
                        column, int(width)
                    )
                except (TypeError, ValueError):
                    pass

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802
        self.settings.setValue("window/geometry", self.saveGeometry())
        self.settings.setValue("window/splitter", self.splitter.sizes())
        head = self.channels.horizontalHeader()
        self.settings.setValue("panel/value", head.sectionSize(self.COL_VALUE))
        self.settings.setValue("panel/unit", head.sectionSize(self.COL_UNIT))
        self._save_state()
        super().closeEvent(event)

    # -- helpers -----------------------------------------------------------

    def _warn(self, title: str, message: str) -> None:
        QtWidgets.QMessageBox.warning(self, title, message)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="McMESS datalog viewer")
    parser.add_argument("log", nargs="?", help="a CSV to open straight away")
    parser.add_argument(
        "--screenshot", help="render the window to this PNG and exit (for checking)"
    )
    args = parser.parse_args(argv)

    pg.setConfigOptions(
        antialias=True, background=theme.SURFACE, foreground=theme.TEXT_SECONDARY
    )
    app = QtWidgets.QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    app.setStyleSheet(theme.STYLESHEET)

    window = LogViewerWindow(args.log)
    window.show()

    if args.screenshot:
        path = Path(args.screenshot)
        path.parent.mkdir(parents=True, exist_ok=True)

        def shoot() -> None:
            window.grab().save(str(path))
            print(f"screenshot: {path.resolve()}")
            window.close()
            app.quit()

        QtCore.QTimer.singleShot(1200, shoot)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
