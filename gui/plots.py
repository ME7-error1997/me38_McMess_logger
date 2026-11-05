"""The scrolling plot stack.

One panel per channel, stacked, sharing a single X axis. This is small multiples,
chosen deliberately: a tuning log mixes rpm, degrees, millisecond load values and
temperatures, and putting two of those on one set of axes would mean a second
y-scale. A dual-axis chart invites exactly the false correlation you are trying to
read out of the data, so there is never more than one scale per panel.

Each panel is directly labelled with its channel name, unit and current value, so
identity never rests on colour.
"""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtWidgets

from mcmess.channels import Channel

from . import theme

#: Painting quality vs sample rate. See the note in ChannelPanel: antialiasing
#: costs roughly 2x the logger's throughput. main() flips this from --smooth.
ANTIALIAS = False


class ChannelPanel:
    """One channel's plot row: a trace, a direct label and a value readout."""

    def __init__(self, plot: pg.PlotItem, channel: Channel, color: str) -> None:
        self.plot = plot
        self.channel = channel
        self.color = color

        plot.showGrid(x=True, y=True, alpha=theme.GRID_ALPHA)
        plot.setMenuEnabled(False)
        plot.setMouseEnabled(x=True, y=False)
        plot.hideButtons()
        for side in ("left", "bottom"):
            axis = plot.getAxis(side)
            axis.setPen(pg.mkPen(theme.BORDER))
            axis.setTextPen(pg.mkPen(theme.TEXT_MUTED))
        plot.getAxis("left").setWidth(58)
        plot.getAxis("left").setStyle(tickFont=QtCore.QCoreApplication.instance().font())

        self.curve = plot.plot(
            pen=pg.mkPen(color, width=theme.LINE_WIDTH),
            # Antialiasing is off deliberately. Measured on the bench against a
            # real 018CG with six channels: antialiased painting holds the GIL
            # long enough to cut the sampling thread from 537 to 219 bytes/s --
            # 67 Hz down to 27 Hz. For a logger whose whole point is sample rate,
            # slightly harder line edges are a cheap price. --smooth turns it
            # back on for a screenshot.
            antialias=ANTIALIAS,
            autoDownsample=True,
            clipToView=True,
        )

        # Direct label: a short coloured rule, then the name in ink. Text never
        # wears the series colour; the rule beside it carries identity.
        unit = channel.unit_label
        self.label = pg.TextItem(anchor=(0, 0), color=theme.TEXT_SECONDARY)
        self.label.setHtml(
            f'<div style="font-size:10pt">'
            f'<span style="color:{color}">&#9644;</span> '
            f'<span style="color:{theme.TEXT_PRIMARY};font-weight:600">{channel.name}</span>'
            + (f'<span style="color:{theme.TEXT_MUTED}"> &nbsp;{unit}</span>' if unit else "")
            + "</div>"
        )
        plot.addItem(self.label, ignoreBounds=True)

        self.readout = pg.TextItem(anchor=(1, 0), color=theme.TEXT_PRIMARY)
        plot.addItem(self.readout, ignoreBounds=True)

        self.cursor = pg.InfiniteLine(
            angle=90, movable=False, pen=pg.mkPen(theme.TEXT_MUTED, width=1)
        )
        self.cursor.setVisible(False)
        plot.addItem(self.cursor, ignoreBounds=True)

        self._placed = False

    def set_data(self, times: np.ndarray, values: np.ndarray) -> None:
        self.curve.setData(times, values)

    def place_annotations(self) -> None:
        """Pin the label and readout to the panel's visual top corners.

        viewRect() is a QRectF in *data* coordinates, where y increases upward --
        so top() is the minimum y, which is the bottom of the plot on screen.
        Taking max/min explicitly avoids depending on which way round that is.
        """
        box = self.plot.vb.viewRect()
        y_top = max(box.top(), box.bottom())
        pad_x = box.width() * 0.008
        self.label.setPos(box.left() + pad_x, y_top)
        self.readout.setPos(box.right() - pad_x, y_top)
        self._placed = True

    def set_readout(self, raw: int | None, value: float | None, at_cursor: bool) -> None:
        conv = self.channel.conversion
        if raw is None:
            text = "--"
        elif value is None:
            text = f"raw {raw}"
        else:
            text = f"{conv.quantize(value):.{conv.decimals}f}"
        tint = theme.TEXT_SECONDARY if at_cursor else theme.TEXT_PRIMARY
        self.readout.setHtml(
            f'<div style="font-size:13pt;font-weight:600;color:{tint}">{text}</div>'
        )

    def show_cursor(self, x: float | None) -> None:
        if x is None:
            self.cursor.setVisible(False)
        else:
            self.cursor.setPos(x)
            self.cursor.setVisible(True)


class PlotStack(QtWidgets.QWidget):
    """A vertical stack of ChannelPanels with a shared, linked X axis."""

    #: Emitted with the cursor's x position in seconds, or None when it leaves.
    cursor_moved = QtCore.Signal(object)

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.panels: list[ChannelPanel] = []
        self._layout_widget = pg.GraphicsLayoutWidget()
        self._layout_widget.setBackground(theme.SURFACE)

        self.placeholder = QtWidgets.QLabel(
            "Select channels on the left, then Connect.\n\n"
            "Page 0x00 channels are the cheap ones: one wire byte each and no\n"
            "parity switch, so they sample fastest."
        )
        self.placeholder.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setStyleSheet(f"color: {theme.TEXT_MUTED};")

        box = QtWidgets.QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(self.placeholder)
        box.addWidget(self._layout_widget)
        self._layout_widget.setVisible(False)

        self._layout_widget.scene().sigMouseMoved.connect(self._on_mouse_moved)
        self._cursor_x: float | None = None

    # -- building ----------------------------------------------------------

    def rebuild(self, channels: list[Channel]) -> None:
        """Replace the panels. Called when the channel selection changes."""
        self._layout_widget.clear()
        self.panels.clear()
        self._cursor_x = None

        has_channels = bool(channels)
        self.placeholder.setVisible(not has_channels)
        self._layout_widget.setVisible(has_channels)
        if not has_channels:
            return

        first: pg.PlotItem | None = None
        for row, channel in enumerate(channels):
            plot = self._layout_widget.addPlot(row=row, col=0)
            if first is None:
                first = plot
            else:
                plot.setXLink(first)
            last = row == len(channels) - 1
            plot.getAxis("bottom").setStyle(showValues=last)
            if last:
                plot.setLabel("bottom", "seconds", color=theme.TEXT_MUTED)
            self.panels.append(ChannelPanel(plot, channel, theme.series_color(row)))

    # -- updating ----------------------------------------------------------

    def set_window(self, seconds: float | None, latest: float) -> None:
        """Scroll the shared X axis.

        Until there is a window's worth of data, fit what exists rather than
        reserving the full span -- otherwise the first half-minute of a session is
        a trace squeezed into a corner of an empty chart. Once the buffer is
        longer than the window, scroll so "now" stays at the right edge.
        """
        if not self.panels:
            return
        plot = self.panels[0].plot
        if seconds is None:
            plot.enableAutoRange(axis="x")
        elif latest <= seconds:
            plot.setXRange(0.0, max(latest, 1.0), padding=0)
        else:
            plot.setXRange(latest - seconds, latest, padding=0)

    def update_data(self, times: np.ndarray, data: np.ndarray) -> None:
        for index, panel in enumerate(self.panels):
            if index >= data.shape[1]:
                continue
            column = data[:, index]
            panel.set_data(times, column)
            finite = column[np.isfinite(column)]
            if finite.size:
                low, high = float(finite.min()), float(finite.max())
                if high - low < 1e-9:
                    low, high = low - 0.5, high + 0.5
                margin = (high - low) * 0.12
                panel.plot.setYRange(low - margin, high + margin, padding=0)
            panel.place_annotations()

    def update_readouts(
        self, times: np.ndarray, data: np.ndarray, raws: dict[str, int | None]
    ) -> None:
        """Show the value at the cursor, or the latest sample when there is none."""
        if not self.panels or not len(times):
            return
        at_cursor = self._cursor_x is not None
        index = (
            int(np.clip(np.searchsorted(times, self._cursor_x), 0, len(times) - 1))
            if at_cursor
            else len(times) - 1
        )
        for column, panel in enumerate(self.panels):
            if column >= data.shape[1]:
                continue
            value = data[index, column]
            if at_cursor:
                raw = None if np.isnan(value) else int(round(value))
                panel.set_readout(raw, None if np.isnan(value) else float(value), True)
            else:
                raw = raws.get(panel.channel.name)
                panel.set_readout(
                    raw, None if np.isnan(value) else float(value), False
                )
            panel.show_cursor(times[index] if at_cursor else None)

    # -- cursor ------------------------------------------------------------

    def _on_mouse_moved(self, position) -> None:
        if not self.panels:
            return
        view = self.panels[0].plot.vb
        inside = any(
            panel.plot.sceneBoundingRect().contains(position) for panel in self.panels
        )
        if not inside:
            if self._cursor_x is not None:
                self._cursor_x = None
                for panel in self.panels:
                    panel.show_cursor(None)
                self.cursor_moved.emit(None)
            return
        self._cursor_x = float(view.mapSceneToView(position).x())
        self.cursor_moved.emit(self._cursor_x)

    @property
    def cursor_x(self) -> float | None:
        return self._cursor_x
