"""The logger's live view: the overlay chart, plus a table of current values.

Same chart as the log viewer, fed from the recorder's ring buffer instead of a
parsed CSV. That is the whole point of it being the same chart: what you watch
during a pull and what you read afterwards behave identically -- same scale
groups, same cursor, same axes -- so there is nothing to re-learn between the
two windows and no second implementation to drift.

Three things are specific to being live:

**The cursor follows the newest sample.** It sits at the right-hand edge and
moves with the data, so the values beside it are the live ones and there is
nothing to chase with the mouse. The chart's ``follow_latest`` does this.

**Until you stop.** Disconnect or freeze the view and the cursor becomes yours:
click anywhere to put it on a sample and read the values there. The data is
static at that point -- the recorder is stopped or the redraw is paused -- so a
row index stays pointing at the sample it pointed at when you clicked.

**Fixed Y ranges matter more here than in the viewer.** A log is static, so
auto-fitting Y to the visible window settles. Live, the window slides every
sample and an auto-fitted scale breathes continuously, which makes a trace hard
to read and impossible to compare against the one next to it. Right-click for a
preset -- boost, load, rpm, lambda, timing -- and the scale stops moving.
"""

from __future__ import annotations

import time

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

from mcmess.channels import Channel
from mcmess.logparse import (
    RPM_NAME_RE,
    RPM_UNIT_RE,
    format_value,
    group_key_for,
)

from . import theme
from .logchart import Hover, LogChart, Trace

#: Common fixed Y ranges for a live chart, on the right-click menu.
PRESETS: tuple[tuple[str, float, float], ...] = (
    ("Engine speed 0 - 8000 rpm", 0, 8000),
    ("Load 0 - 10 ms/Umdr.", 0, 10),
    ("Boost 0 - 3000 mbar", 0, 3000),
    ("Air flow 0 - 1000 kg/h", 0, 1000),
    ("Air flow 0 - 300 g/s", 0, 300),
    ("Timing -10 - 50 grad KW", -10, 50),
    ("Knock retard -15 - 0 grad KW", -15, 0),
    ("Lambda 0.6 - 1.4", 0.6, 1.4),
    ("Temperature 0 - 150 grad C", 0, 150),
    ("Percent 0 - 100 %", 0, 100),
    ("Volts 0 - 16 V", 0, 16),
)

#: How often the value table repaints while data is arriving. See the note in
#: _refresh_values: at the full redraw rate it cost a fifth of the sample rate.
VALUES_INTERVAL_S = 0.2

PLACEHOLDER = (
    "Select channels on the left, then Connect.\n\n"
    "Page 0x00 channels are the cheap ones: one wire byte each and no\n"
    "parity switch, so they sample fastest."
)


class LivePlot(QtWidgets.QWidget):
    """The overlay chart and its value readouts, driven by the recorder."""

    #: Which sample the value table is showing changed: "live", "cursor",
    #: "mouse" or "last sample". The window puts it in the status bar, because
    #: a number on screen should never be ambiguous about where it came from.
    readout_changed = QtCore.Signal(str)

    COL_NAME, COL_VALUE, COL_UNIT, COL_MIN, COL_MAX = range(5)

    def __init__(
        self, parent: QtWidgets.QWidget | None = None, *, antialias: bool = False
    ) -> None:
        super().__init__(parent)
        self.channels: list[Channel] = []
        self.axis_key: str | None = None
        self.hover: Hover | None = None
        self.cursor_row: int | None = None
        self.following = False
        self._rows: np.ndarray = np.zeros(0, dtype=int)
        self._traces: list[Trace] = []
        self._values_at = 0.0
        self._source = ""

        self.chart = LogChart(antialias=antialias, placeholder=PLACEHOLDER)
        self.chart.hovered.connect(self._on_hover)
        self.chart.cursor_placed.connect(self._on_cursor_placed)
        self.chart.context_requested.connect(self._on_context)

        self.values = QtWidgets.QTableWidget(0, 5)
        self.values.setHorizontalHeaderLabels(
            ["channel", "value", "unit", "min", "max"]
        )
        self.values.verticalHeader().setVisible(False)
        self.values.setShowGrid(False)
        self.values.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
        )
        # A readout, not a list to pick from: a selection highlight would paint
        # over the colour that says which trace a row belongs to.
        self.values.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.NoSelection
        )
        self.values.setToolTip(
            "Click a channel to make it the axis channel: it draws thicker and\n"
            "its unit owns the left axis. Right-click for a fixed Y range."
        )
        head = self.values.horizontalHeader()
        head.setSectionResizeMode(
            self.COL_NAME, QtWidgets.QHeaderView.ResizeMode.Stretch
        )
        for column, width in (
            (self.COL_VALUE, 90), (self.COL_UNIT, 80), (self.COL_MIN, 80),
            (self.COL_MAX, 80),
        ):
            head.resizeSection(column, width)
        self.values.cellClicked.connect(self._on_cell_clicked)
        self.values.setContextMenuPolicy(
            QtCore.Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.values.customContextMenuRequested.connect(self._on_table_context)

        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        self.splitter.addWidget(self.chart)
        self.splitter.addWidget(self.values)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes([640, 280])

        box = QtWidgets.QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(self.splitter)

    # -- building ----------------------------------------------------------

    def rebuild(self, channels: list[Channel]) -> None:
        """The selection changed. The next update_data draws the new set."""
        self.channels = list(channels)
        self.hover = None
        self.cursor_row = None
        self._traces = []
        self.chart.clear()
        if not channels:
            self.values.setRowCount(0)
            return
        if self.axis_key not in {c.name for c in channels}:
            self.axis_key = channels[0].name
        self._fill_table()

    def _fill_table(self) -> None:
        self.values.setRowCount(len(self.channels))
        for row, channel in enumerate(self.channels):
            name = QtWidgets.QTableWidgetItem(channel.name)
            name.setData(QtCore.Qt.ItemDataRole.UserRole, channel.name)
            name.setForeground(
                QtGui.QBrush(QtGui.QColor(theme.viewer_color(row)))
            )
            name.setToolTip(
                getattr(channel, "about", "")
                or getattr(channel, "description_en", "")
                or channel.name
            )
            self.values.setItem(row, self.COL_NAME, name)

            unit = QtWidgets.QTableWidgetItem(channel.unit_label)
            unit.setForeground(QtGui.QBrush(QtGui.QColor(theme.TEXT_MUTED)))
            self.values.setItem(row, self.COL_UNIT, unit)

            for column in (self.COL_VALUE, self.COL_MIN, self.COL_MAX):
                item = QtWidgets.QTableWidgetItem("")
                item.setData(QtCore.Qt.ItemDataRole.UserRole, channel.name)
                item.setTextAlignment(
                    QtCore.Qt.AlignmentFlag.AlignRight
                    | QtCore.Qt.AlignmentFlag.AlignVCenter
                )
                if column == self.COL_VALUE:
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                self.values.setItem(row, column, item)
        self.values.resizeRowsToContents()
        self._mark_axis_channel()

    def _mark_axis_channel(self) -> None:
        for row, channel in enumerate(self.channels):
            item = self.values.item(row, self.COL_NAME)
            if item is None:
                continue
            font = item.font()
            font.setBold(channel.name == self.axis_key)
            font.setUnderline(channel.name == self.axis_key)
            item.setFont(font)

    # -- data --------------------------------------------------------------

    def update_data(self, times: np.ndarray, data: np.ndarray) -> None:
        """One snapshot of the ring buffer: redraw and refresh the readouts."""
        if not self.channels or not len(times) or data.shape[1] != len(self.channels):
            return
        if len(self._rows) != len(times):
            # Row indices into the snapshot. Reallocated only when the window
            # length changes, not on every frame.
            self._rows = np.arange(len(times), dtype=int)

        self._traces = [
            Trace(
                key=channel.name,
                label=channel.name,
                unit=channel.unit_label,
                color=theme.viewer_color(index),
                xs=times,
                ys=data[:, index],
                rows=self._rows,
            )
            for index, channel in enumerate(self.channels)
        ]
        if not self.chart.update_traces(self._traces):
            self.chart.draw(
                self._traces,
                axis_key=self.axis_key,
                x_label="seconds",
                second_x=self._rpm_trace(),
            )
            self.chart.set_follow_latest(self.following)
        self._refresh_values(force=False)

    def _rpm_trace(self) -> Trace | None:
        """The engine speed channel, for the labels along the top.

        By name first, then by unit -- this ECU calls it N40 and a VAG log calls
        it Drehzahl, and neither reads as "rpm", but both carry Upm or 1/min.
        """
        for match in (
            lambda t: RPM_NAME_RE.search(t.label),
            lambda t: RPM_UNIT_RE.match(t.unit.strip()),
        ):
            for trace in self._traces:
                if match(trace):
                    return trace
        return None

    def set_window(self, seconds: float | None, latest: float) -> None:
        # Only while the data is advancing. Once it stops, the view is the
        # reader's -- scrolling it out from under a zoom would be maddening.
        if self.following:
            self.chart.set_x_window(seconds, latest)

    def set_following(self, following: bool) -> None:
        """Live (cursor pinned to the newest sample) or stopped (cursor free)."""
        if following and not self.following:
            self.hover = None
        self.following = following
        self.chart.set_follow_latest(following)
        if not following and self.cursor_row is None:
            # Leave it where the live cursor was, so stopping does not blank
            # every readout the moment you press Disconnect.
            self.cursor_row = self.chart.cursor_row
        self._refresh_values()

    # -- readouts ----------------------------------------------------------

    @property
    def active_row(self) -> int | None:
        """Mouse, else cursor, else the newest sample. The viewer's rule."""
        if self.hover is not None:
            return self.hover.row
        if self.following:
            return self.chart.latest_row()
        if self.cursor_row is not None:
            return self.cursor_row
        return self.chart.latest_row()

    def source(self) -> str:
        if self.hover is not None:
            return "mouse"
        if self.following:
            return "live"
        return "cursor" if self.cursor_row is not None else "last sample"

    def _refresh_values(self, force: bool = True) -> None:
        """Repaint the value table.

        Throttled on the live path. Measured on the simulator, repainting ten
        channels' worth of cells at the full 20 FPS redraw rate cost the logger
        about a fifth of its sample rate -- 23.5 Hz against 28.6 -- and numbers
        changing twenty times a second are unreadable anyway. Anything the
        reader did (a hover, a click, a new selection) still repaints at once:
        a readout that lags your own mouse feels broken.
        """
        source = self.source()
        if source != self._source:
            self._source = source
            self.readout_changed.emit(source)
        if not self._traces:
            return
        now = time.monotonic()
        if not force and now - self._values_at < VALUES_INTERVAL_S:
            return
        self._values_at = now
        row = self.active_row
        values = self.chart.values_at_row(row) if row is not None else {}
        low, high = self.chart.visible_x_range()
        for index, channel in enumerate(self.channels):
            value = values.get(channel.name)
            item = self.values.item(index, self.COL_VALUE)
            if item is not None:
                item.setText(format_value(value))
            if index >= len(self._traces):
                continue
            trace = self._traces[index]
            window = trace.ys[(trace.xs >= low) & (trace.xs <= high)]
            window = window[np.isfinite(window)]
            for column, text in (
                (self.COL_MIN, format_value(float(window.min())) if window.size else "--"),
                (self.COL_MAX, format_value(float(window.max())) if window.size else "--"),
            ):
                cell = self.values.item(index, column)
                if cell is not None:
                    cell.setText(text)

    def _on_hover(self, hover: Hover | None) -> None:
        self.hover = hover
        self._refresh_values()

    def _on_cursor_placed(self, row: int) -> None:
        self.cursor_row = int(row)
        self.chart.set_cursor_row(self.cursor_row)
        self._refresh_values()

    def clear_cursor(self) -> None:
        self.cursor_row = None
        self.chart.set_cursor_row(None)
        self._refresh_values()

    # -- interaction -------------------------------------------------------

    def _on_cell_clicked(self, row: int, column: int) -> None:
        if column != self.COL_NAME or row >= len(self.channels):
            return
        self.axis_key = self.channels[row].name
        self.chart.set_axis_key(self.axis_key)
        self._mark_axis_channel()
        if self._traces:
            self.chart.draw(
                self._traces,
                axis_key=self.axis_key,
                x_label="seconds",
                second_x=self._rpm_trace(),
                keep_view=True,
            )
            self.chart.set_follow_latest(self.following)

    def _on_table_context(self, point: QtCore.QPoint) -> None:
        item = self.values.itemAt(point)
        if item is None:
            return
        self._show_menu(
            self.values.viewport().mapToGlobal(point),
            self.values.item(item.row(), self.COL_NAME).data(
                QtCore.Qt.ItemDataRole.UserRole
            ),
        )

    def _on_context(self, global_point, key) -> None:
        self._show_menu(global_point, key)

    def _show_menu(self, global_point, key: str | None) -> None:
        channel = next((c for c in self.channels if c.name == key), None)
        if channel is None:
            return
        group = group_key_for(channel.name, channel.unit_label)
        menu = QtWidgets.QMenu(self)
        menu.addSection(channel.name)
        axis = menu.addAction("Make it the axis channel")
        axis.setEnabled(channel.name != self.axis_key)
        range_menu = menu.addMenu(
            f"Y range for {channel.unit_label or channel.name}"
            + ("  (shared)" if group.startswith("u:") else "")
        )
        auto = range_menu.addAction("Auto (fit the visible window)")
        range_menu.addSeparator()
        for label, low, high in PRESETS:
            action = range_menu.addAction(label)
            action.setData((low, high))

        chosen = menu.exec(global_point)
        if chosen is None:
            return
        if chosen is axis:
            row = next(
                (i for i, c in enumerate(self.channels) if c.name == channel.name), 0
            )
            self._on_cell_clicked(row, self.COL_NAME)
        elif chosen is auto:
            self.chart.set_fixed_range(group, None)
        elif chosen.data():
            self.chart.set_fixed_range(group, tuple(chosen.data()))

    def auto_ranges(self) -> None:
        for group in list(self.chart.fixed_ranges):
            self.chart.set_fixed_range(group, None)

    @property
    def has_fixed_ranges(self) -> bool:
        return bool(self.chart.fixed_ranges)
