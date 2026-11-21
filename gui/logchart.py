"""The overlay chart: the same one for reading a log and for making one.

Traces share one plot area, and channels that share a unit share a Y scale. That
is what lets you see that `TL` never reaches `LDSHK`, or that boost actual never
reaches requested -- two numbers in a table do not show you that, and two
separate panels make you measure it by eye across a gap.

The chart knows nothing about where its points came from. The window hands it
columns out of a parsed CSV, or slices of the recorder's ring buffer while a
session is running (gui/sources.py). Both get the same cursor, the same scale
groups, the same axes. One chart means there is no second implementation to
drift. The one thing that differs live is ``follow_latest``, which pins the
cursor to the newest sample and locks the mouse out of the view.

Six behaviours are worth stating, because they are the ones a charting library
will not give without a fight:

**One Y scale per unit.** The scale group key is ``u:<unit>``, or ``k:<key>`` for
a channel with no unit (a bare number shares nothing meaningful with another bare
number) or when the reader asks for own scales. Each group is a pyqtgraph
ViewBox overlaid on the plot and X-linked to it. The left axis belongs to the
*axis channel* -- the trace the reader clicked -- and the right axis to the next
group along; the rest are drawn without an axis of their own, which is why the
value readouts matter.

**Y fits the visible window, not the whole log.** Zoom into a pull and the trace
fills the panel instead of sitting flat at the bottom of a scale set by a rev
limiter three minutes earlier. 6 % padding. A group the reader has fixed keeps
its range.

**A cursor is a row, not an x value.** That one choice makes it work in time
mode, in scatter mode, and across VCDS logs where every group has its own clock;
and it survives a change of X axis. Finding "the sample for row r" is a
nearest-value search in a trace's ascending ``rows`` array.

**Live, the cursor follows the newest sample.** ``follow_latest`` pins it to the
right-hand end, so the readouts are the live values and there is nothing to
click. Turn it off -- which the logger does the moment it disconnects or freezes
-- and the cursor is the reader's to place.

**The second X axis follows the trace.** A row of rpm labels along the top, read
at each time tick by interpolation. The plot stays against *time*: rpm is not a
linear scale over a log, because it falls on every gear change, so it cannot be
an axis -- only a set of labels.

**Decimation keeps the peaks.** pyqtgraph's "peak" downsampling draws each pixel
column's min and max, so a single-sample knock event or boost spike survives;
plain point-skipping loses exactly the events you opened the log to find. When a
log is sparse instead -- a 6 Hz VCDS log zoomed in -- each sample gets a small
square, so the reader can see where the samples really are rather than reading
meaning into straight interpolation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtWidgets

from mcmess.logenums import describe_value, enum_for
from mcmess.logparse import format_value, scale_group_key

from . import theme

#: A sample every this many pixels or fewer counts as sparse: mark each one.
SPARSE_PX = 9

#: Y padding around a group's visible range.
Y_PAD = 0.06

#: How far a refitted Y range has to move, as a fraction of its own span,
#: before it is worth applying. See the note in _refit_y.
Y_HYSTERESIS = 0.005

#: Never zoom below this fraction of the whole log.
MIN_ZOOM_FRACTION = 1 / 5000

#: Hover updates are throttled to this, so a 30,000-row log stays responsive.
HOVER_RATE_S = 0.02


@dataclass
class Trace:
    """One channel's points, ready to draw. The chart's only input.

    ``rows`` is what the cursor sits on: the log row a point came from in the
    viewer, the sample index in the logger. It must ascend. Keeping it beside
    the points is what lets a cursor placed on one trace find the matching
    sample on every other one, however ragged their clocks are.
    """

    key: str
    label: str
    unit: str
    color: str
    xs: np.ndarray
    ys: np.ndarray
    rows: np.ndarray


@dataclass
class Plotted:
    """A Trace on the chart: which scale group it landed in, and its curve."""

    trace: Trace
    group: str
    curve: pg.PlotDataItem | None = None
    #: True when x is ascending, so a hover can binary-search it. False in
    #: scatter mode, where the nearest point has to be found in pixel space.
    sorted_x: bool = True

    @property
    def key(self) -> str:
        return self.trace.key

    @property
    def label(self) -> str:
        return self.trace.label

    @property
    def unit(self) -> str:
        return self.trace.unit

    @property
    def color(self) -> str:
        return self.trace.color

    @property
    def xs(self) -> np.ndarray:
        return self.trace.xs

    @property
    def ys(self) -> np.ndarray:
        return self.trace.ys

    @property
    def rows(self) -> np.ndarray:
        return self.trace.rows

    def index_for_row(self, row: int) -> int | None:
        """The position in this trace of the sample nearest a given row."""
        if not len(self.rows):
            return None
        i = int(np.searchsorted(self.rows, row))
        if i >= len(self.rows):
            return len(self.rows) - 1
        if i > 0 and abs(self.rows[i - 1] - row) <= abs(self.rows[i] - row):
            return i - 1
        return i


@dataclass
class Hover:
    """What the mouse is over: an x position and the row under it."""

    x: float
    row: int
    values: dict[str, float] = field(default_factory=dict)


class RpmAxis(pg.AxisItem):
    """The second X axis: another channel's value at each of the X ticks.

    The ticks stay where the bottom axis puts them -- these are labels on a time
    axis, not a scale of their own.
    """

    def __init__(self) -> None:
        super().__init__(orientation="top")
        self._xs: np.ndarray | None = None
        self._ys: np.ndarray | None = None

    def set_reference(self, xs: np.ndarray | None, ys: np.ndarray | None) -> None:
        # np.interp needs ascending x; a scatter x is not, and interpolating it
        # would invent values, so it is refused rather than guessed at.
        if xs is None or ys is None or len(xs) < 2 or not np.all(np.diff(xs) >= 0):
            self._xs = self._ys = None
        else:
            self._xs, self._ys = xs, ys
        self.picture = None
        self.update()

    def tickStrings(self, values, scale, spacing):  # noqa: N802  (pyqtgraph API)
        if self._xs is None:
            return [""] * len(values)
        return [
            format_value(float(np.interp(v, self._xs, self._ys)))
            if self._xs[0] <= v <= self._xs[-1] else ""
            for v in values
        ]


class ChartViewBox(pg.ViewBox):
    """Drag zooms, Shift+drag pans, a click places the cursor.

    pyqtgraph's default is the other way round (drag pans, and there is no
    cursor), and the default is wrong for reading a log: the first thing anyone
    does with a pull is drag a box around the interesting four seconds.
    """

    #: Emitted with the x position of a single click.
    clicked = QtCore.Signal(float)
    #: Emitted on a double click.
    reset_requested = QtCore.Signal()

    def __init__(self) -> None:
        super().__init__()
        #: Live: the view belongs to the incoming data. No zoom, no pan, no
        #: cursor -- every mouse gesture is swallowed until it is unlocked.
        self.locked = False
        self.setMouseMode(pg.ViewBox.RectMode)
        # X only: a rubber band that also rescaled Y would be undone a moment
        # later by the refit, and a wheel that zoomed Y would fight it.
        self.setMouseEnabled(x=True, y=False)
        self.setMenuEnabled(False)

    def set_locked(self, locked: bool) -> None:
        self.locked = locked
        self.setMouseEnabled(x=not locked, y=False)

    def wheelEvent(self, ev, axis=None) -> None:  # noqa: N802
        if self.locked:
            ev.accept()  # swallowed, so it does not scroll something behind
            return
        super().wheelEvent(ev, axis)

    def mouseDragEvent(self, ev, axis=None) -> None:  # noqa: N802
        if self.locked:
            ev.accept()
            return
        if ev.modifiers() & QtCore.Qt.KeyboardModifier.ShiftModifier:
            previous = self.state["mouseMode"]
            self.setMouseMode(pg.ViewBox.PanMode)
            try:
                super().mouseDragEvent(ev, axis)
            finally:
                self.setMouseMode(previous)
        else:
            super().mouseDragEvent(ev, axis)

    def mouseClickEvent(self, ev) -> None:  # noqa: N802
        if self.locked and ev.button() == QtCore.Qt.MouseButton.LeftButton:
            ev.accept()
            return
        if ev.button() == QtCore.Qt.MouseButton.LeftButton:
            if ev.double():
                ev.accept()
                self.reset_requested.emit()
                return
            ev.accept()
            self.clicked.emit(float(self.mapSceneToView(ev.scenePos()).x()))
            return
        super().mouseClickEvent(ev)


class LogChart(QtWidgets.QWidget):
    """The chart. Give it a list of Traces; it draws."""

    #: The mouse moved over a trace: a Hover, or None when it left.
    hovered = QtCore.Signal(object)
    #: A click placed the cursor on this row. Not emitted while following.
    cursor_placed = QtCore.Signal(int)
    #: A right click, with the global position and the channel key under it.
    context_requested = QtCore.Signal(object, object)

    def __init__(
        self,
        parent: QtWidgets.QWidget | None = None,
        *,
        antialias: bool = True,
        placeholder: str = "Open a log to draw it.",
    ) -> None:
        super().__init__(parent)
        self.plotted: list[Plotted] = []
        self.axis_key: str | None = None
        self.fixed_ranges: dict[str, tuple[float, float]] = {}
        self.cursor_row: int | None = None
        #: Live mode: the cursor is pinned to the newest sample, and the mouse
        #: can neither move it, zoom, pan nor hover. The logger turns this off
        #: when it disconnects or freezes, and the chart becomes the reader's.
        self.follow_latest = False
        self._antialias = antialias
        self._scatter = False
        self._groups: dict[str, pg.ViewBox] = {}
        self._sparse = False

        self._widget = pg.GraphicsLayoutWidget()
        self._widget.setBackground(theme.SURFACE)
        self._top_axis = RpmAxis()
        self._vb = ChartViewBox()
        self.plot = self._widget.addPlot(
            viewBox=self._vb, axisItems={"top": self._top_axis}
        )
        self.plot.showGrid(x=True, y=True, alpha=theme.GRID_ALPHA)
        self.plot.hideButtons()
        self.plot.setMenuEnabled(False)
        self.plot.showAxis("right")
        self.plot.showAxis("top")
        for side in ("left", "bottom", "right", "top"):
            axis = self.plot.getAxis(side)
            axis.setPen(pg.mkPen(theme.BORDER))
            axis.setTextPen(pg.mkPen(theme.TEXT_MUTED))
        self.plot.getAxis("left").setWidth(66)
        self.plot.getAxis("right").setWidth(66)

        self._crosshair = pg.InfiniteLine(
            angle=90, movable=False,
            pen=pg.mkPen(theme.TEXT_MUTED, width=1, style=QtCore.Qt.PenStyle.DashLine),
        )
        self._crosshair.setVisible(False)
        self.plot.addItem(self._crosshair, ignoreBounds=True)

        self._cursor_line = pg.InfiniteLine(
            angle=90, movable=False, pen=pg.mkPen(theme.SERIES[0], width=2)
        )
        self._cursor_line.setVisible(False)
        self.plot.addItem(self._cursor_line, ignoreBounds=True)

        self._tooltip = pg.TextItem(anchor=(0, 0))
        self._tooltip.setZValue(100)
        self._tooltip.setVisible(False)
        self.plot.addItem(self._tooltip, ignoreBounds=True)

        self.placeholder = QtWidgets.QLabel(placeholder)
        self.placeholder.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setStyleSheet(f"color: {theme.TEXT_MUTED};")

        box = QtWidgets.QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(self.placeholder)
        box.addWidget(self._widget)
        self._widget.setVisible(False)

        self._vb.sigResized.connect(self._sync_groups)
        self._vb.sigXRangeChanged.connect(self._on_x_changed)
        self._vb.clicked.connect(self._on_click)
        self._vb.reset_requested.connect(self.reset_zoom)
        self._proxy = pg.SignalProxy(
            self._widget.scene().sigMouseMoved,
            rateLimit=int(1 / HOVER_RATE_S),
            slot=self._on_mouse_moved,
        )
        self._widget.scene().sigMouseClicked.connect(self._on_scene_click)

    # -- drawing -----------------------------------------------------------

    def clear(self) -> None:
        self.plotted = []
        self.cursor_row = None
        self._clear_groups()
        self.plot.clear()
        self._re_add_overlays()
        self.placeholder.setVisible(True)
        self._widget.setVisible(False)

    def draw(
        self,
        traces: list[Trace],
        *,
        axis_key: str | None = None,
        scatter: bool = False,
        x_label: str = "seconds",
        own_scales: bool = False,
        second_x: Trace | None = None,
        keep_view: bool = False,
    ) -> None:
        """Rebuild the chart from scratch.

        Called whenever the *set* of channels or the scale mode changes.
        Rebuilding rather than patching keeps one invariant that is otherwise
        fiddly: the axis channel's scale group IS the main ViewBox, so the left
        axis always belongs to it and the right axis to the next group along.

        For new points on the same channels use update(), which is cheap enough
        to run at the logger's redraw rate.
        """
        if not traces:
            self.clear()
            return

        self.placeholder.setVisible(False)
        self._widget.setVisible(True)
        # Only keep a view when there actually is one: on the first draw the
        # ViewBox still reads its default 0..1, and keeping THAT leaves a chart
        # that is correct in every detail except that the data is off-screen.
        view = self._vb.viewRange()[0] if keep_view and self.plotted else None

        self._scatter = scatter
        self._clear_groups()
        self.plot.clear()
        self._re_add_overlays()

        plotted = [
            Plotted(
                trace=t,
                group=scale_group_key(t, own_scales),
                sorted_x=not scatter,
            )
            for t in traces
        ]

        # Order the groups so the axis channel's comes first: that one becomes
        # the main ViewBox and owns the left axis.
        order: list[str] = []
        axis_group = next(
            (p.group for p in plotted if p.key == axis_key), None
        ) or plotted[0].group
        for p in plotted:
            if p.group not in order:
                order.append(p.group)
        order.remove(axis_group)
        order.insert(0, axis_group)

        self.axis_key = (
            axis_key if any(p.key == axis_key for p in plotted) else plotted[0].key
        )
        self.plotted = plotted

        # the main ViewBox takes group 0; every other group gets an overlay
        for n, group in enumerate(order):
            if n == 0:
                self._groups[group] = self._vb
                continue
            vb = pg.ViewBox()
            vb.setMouseEnabled(x=False, y=False)
            vb.setMenuEnabled(False)
            self.plot.scene().addItem(vb)
            vb.setXLink(self._vb)
            self._groups[group] = vb
            if n == 1:
                self.plot.getAxis("right").linkToView(vb)

        self._sparse = False
        for p in plotted:
            vb = self._groups[p.group]
            p.curve = pg.PlotDataItem(
                p.xs, p.ys,
                pen=None if scatter else pg.mkPen(p.color, width=theme.LINE_WIDTH),
                antialias=self._antialias,
                autoDownsample=not scatter,
                downsampleMethod="peak",
                clipToView=True,
            )
            if scatter:
                p.curve.setSymbol("o")
                p.curve.setSymbolSize(5)
                p.curve.setSymbolPen(pg.mkPen(None))
                p.curve.setSymbolBrush(pg.mkBrush(p.color))
            elif p.key == self.axis_key:
                p.curve.setPen(pg.mkPen(p.color, width=theme.LINE_WIDTH + 1.5))
            # Parent it BEFORE addItem. An overlay ViewBox lives directly in the
            # scene, so ViewBox.addItem's scene.addItem() runs while the curve
            # still has no parent item; pyqtgraph then resolves and CACHES the
            # curve's "view" as the GraphicsLayoutWidget, and the next range
            # change asks that widget for autoRangeEnabled() and raises. Giving
            # it the ViewBox's child group first means the cache is right.
            p.curve.setParentItem(vb.childGroup)
            vb.addItem(p.curve)

        self._label_axes(order, x_label)
        self.set_second_x(second_x)
        self._sync_groups()
        if view is not None:
            self._vb.setXRange(*view, padding=0)
        else:
            self.reset_zoom()
        self._refit_y()
        self._draw_cursor()

    def update_traces(self, traces: list[Trace]) -> bool:
        """New points for the channels already on the chart.

        Returns False when the set of channels has changed and the caller must
        call draw() instead. This is the path the live logger takes twenty times
        a second, so it allocates nothing and touches no ViewBox geometry.
        """
        if len(traces) != len(self.plotted):
            return False
        if any(p.key != t.key for p, t in zip(self.plotted, traces)):
            return False
        for p, t in zip(self.plotted, traces):
            p.trace = t
            if p.curve is not None:
                p.curve.setData(t.xs, t.ys)
        self._refit_y()
        self._update_sparse_marks()
        self._draw_cursor()
        return True

    def set_second_x(self, trace: Trace | None) -> None:
        if trace is not None and not self._scatter and len(trace.xs):
            self._top_axis.set_reference(trace.xs, trace.ys)
            self._top_axis.setLabel(
                f"{trace.label} ({trace.unit})" if trace.unit else trace.label,
                color=theme.TEXT_MUTED,
            )
        else:
            self._top_axis.set_reference(None, None)
            self._top_axis.setLabel("", color=theme.TEXT_MUTED)

    def _label_axes(self, order: list[str], x_label: str) -> None:
        self.plot.setLabel("bottom", x_label, color=theme.TEXT_MUTED)
        for side, n in (("left", 0), ("right", 1)):
            axis = self.plot.getAxis(side)
            if n >= len(order):
                axis.setLabel("", color=theme.TEXT_MUTED)
                axis.setStyle(showValues=n == 0)
                continue
            axis.setStyle(showValues=True)
            owners = [p for p in self.plotted if p.group == order[n]]
            # The axis wears its group's colour only when the group is one
            # channel; a shared axis belongs to several, so it stays in ink.
            color = owners[0].color if len(owners) == 1 else theme.TEXT_SECONDARY
            axis.setLabel(owners[0].unit or owners[0].label, color=color)
            axis.setTextPen(pg.mkPen(color if len(owners) == 1 else theme.TEXT_MUTED))

    def _clear_groups(self) -> None:
        for vb in self._groups.values():
            if vb is self._vb:
                continue
            vb.setXLink(None)
            self.plot.scene().removeItem(vb)
        self._groups.clear()
        self.plot.getAxis("right").linkToView(self._vb)

    def _re_add_overlays(self) -> None:
        for item in (self._crosshair, self._cursor_line, self._tooltip):
            self.plot.addItem(item, ignoreBounds=True)
        self._crosshair.setVisible(False)
        self._tooltip.setVisible(False)

    def _sync_groups(self) -> None:
        rect = self._vb.sceneBoundingRect()
        for vb in self._groups.values():
            if vb is not self._vb:
                vb.setGeometry(rect)
                vb.linkedViewChanged(self._vb, vb.XAxis)

    # -- ranges ------------------------------------------------------------

    def data_x_range(self) -> tuple[float, float]:
        lows = [p.xs.min() for p in self.plotted if len(p.xs)]
        highs = [p.xs.max() for p in self.plotted if len(p.xs)]
        if not lows:
            return 0.0, 1.0
        low, high = float(min(lows)), float(max(highs))
        return (low, high) if high > low else (low, low + 1.0)

    def visible_x_range(self) -> tuple[float, float]:
        """The zoom window. The stats are computed over exactly this."""
        low, high = self._vb.viewRange()[0]
        return float(low), float(high)

    def reset_zoom(self) -> None:
        low, high = self.data_x_range()
        self._vb.setXRange(low, high, padding=0.01)

    def set_x_range(self, low: float, high: float) -> None:
        """Show exactly this stretch of X. Used to keep the picture a live
        session had when it becomes a log."""
        if high > low:
            self._vb.setXRange(float(low), float(high), padding=0)

    def set_x_window(self, seconds: float | None, latest: float) -> None:
        """Scroll the X axis so "now" stays at the right edge.

        Until there is a window's worth of data, fit what exists rather than
        reserving the full span -- otherwise the first half-minute of a session
        is a trace squeezed into a corner of an empty chart.
        """
        if not self.plotted:
            return
        if seconds is None:
            low, _ = self.data_x_range()
            self._vb.setXRange(low, max(latest, low + 1.0), padding=0.01)
        elif latest <= seconds:
            self._vb.setXRange(0.0, max(latest, 1.0), padding=0)
        else:
            self._vb.setXRange(latest - seconds, latest, padding=0)

    def _on_x_changed(self) -> None:
        # Clamp: a wheel in an empty corner can otherwise zoom to a span no
        # sample lives in, which looks like a broken chart rather than a zoom.
        low, high = self._vb.viewRange()[0]
        d_low, d_high = self.data_x_range()
        span = d_high - d_low
        if high - low < span * MIN_ZOOM_FRACTION:
            middle = (low + high) / 2
            half = span * MIN_ZOOM_FRACTION / 2
            self._vb.setXRange(middle - half, middle + half, padding=0)
            return
        self._refit_y()
        self._update_sparse_marks()

    def _refit_y(self) -> None:
        """Fit every group to what is visible, unless the reader fixed it."""
        if not self.plotted:
            return
        low, high = self._vb.viewRange()[0]
        for group, vb in self._groups.items():
            if group in self.fixed_ranges:
                vb.setYRange(*self.fixed_ranges[group], padding=0)
                continue
            lows: list[float] = []
            highs: list[float] = []
            for p in self.plotted:
                if p.group != group or not len(p.xs):
                    continue
                mask = (p.xs >= low) & (p.xs <= high)
                ys = p.ys[mask] if mask.any() else p.ys
                ys = ys[np.isfinite(ys)]
                if ys.size:
                    lows.append(float(ys.min()))
                    highs.append(float(ys.max()))
            if not lows:
                continue
            y0, y1 = min(lows), max(highs)
            if y1 - y0 < 1e-9:
                y0, y1 = y0 - 0.5, y1 + 0.5
            pad = (y1 - y0) * Y_PAD
            y0, y1 = y0 - pad, y1 + pad
            # Live, the window slides every frame and the fitted range changes
            # by a hair each time, so the axis labels flicker and the trace
            # creeps against a grid that will not hold still. Only move the
            # scale when the move would actually be visible. (This was also
            # tried as a throughput fix -- setYRange re-renders the ViewBox --
            # but across four 15 s runs it made no measurable difference, so
            # the reason it is here is readability, not speed.)
            was0, was1 = vb.viewRange()[1]
            span = max(abs(y1 - y0), 1e-12)
            if abs(was0 - y0) / span < Y_HYSTERESIS and abs(was1 - y1) / span < Y_HYSTERESIS:
                continue
            vb.setYRange(y0, y1, padding=0)

    def _update_sparse_marks(self) -> None:
        """A log sparse enough to show its samples gets a square on each."""
        if self._scatter or not self.plotted:
            return
        width = max(1.0, self._vb.width())
        low, high = self._vb.viewRange()[0]
        axis = next((p for p in self.plotted if p.key == self.axis_key), None)
        if axis is None or not len(axis.xs):
            return
        visible = int(((axis.xs >= low) & (axis.xs <= high)).sum())
        sparse = visible > 0 and width / visible >= SPARSE_PX
        if sparse == self._sparse:
            return
        self._sparse = sparse
        for p in self.plotted:
            if p.curve is None:
                continue
            p.curve.setSymbol("s" if sparse else None)
            if sparse:
                p.curve.setSymbolSize(5)
                p.curve.setSymbolPen(pg.mkPen(None))
                p.curve.setSymbolBrush(pg.mkBrush(p.color))

    def set_fixed_range(self, group: str, span: tuple[float, float] | None) -> None:
        if span is None:
            self.fixed_ranges.pop(group, None)
        else:
            self.fixed_ranges[group] = span
        self._refit_y()

    def group_of(self, key: str) -> str | None:
        return next((p.group for p in self.plotted if p.key == key), None)

    def set_axis_key(self, key: str) -> None:
        self.axis_key = key

    # -- cursor and hover --------------------------------------------------

    def set_follow_latest(self, following: bool) -> None:
        """Pin the cursor to the newest sample, or hand the chart to the reader.

        Following locks everything the mouse can do to the view. While data is
        arriving the right-hand edge IS the reading: a zoom would hide it, a
        placed cursor would point at a sample that is about to scroll away, and
        a hover would swap the live values for old ones with nothing on screen
        to say so. Stop the data and all of it comes back.
        """
        self.follow_latest = following
        self._vb.set_locked(following)
        if following and self._crosshair.isVisible():
            self._crosshair.setVisible(False)
            self._tooltip.setVisible(False)
            self.hovered.emit(None)
        self._draw_cursor()

    def set_cursor_row(self, row: int | None) -> None:
        self.cursor_row = row
        self._draw_cursor()

    def reference(self) -> Plotted | None:
        """The trace the cursor and the row lookup are measured against."""
        if not self.plotted:
            return None
        return next(
            (p for p in self.plotted if p.key == self.axis_key), self.plotted[0]
        )

    def latest_row(self) -> int | None:
        """The newest sample's row, by the reference trace."""
        reference = self.reference()
        if reference is None or not len(reference.rows):
            return None
        return int(reference.rows[-1])

    def _draw_cursor(self) -> None:
        reference = self.reference()
        if reference is None:
            self._cursor_line.setVisible(False)
            return
        if self.follow_latest:
            # Live: the cursor IS the newest sample, so the readouts beside it
            # are the live values and there is nothing to chase with the mouse.
            self.cursor_row = self.latest_row()
        if self.cursor_row is None:
            self._cursor_line.setVisible(False)
            return
        i = reference.index_for_row(self.cursor_row)
        if i is None:
            self._cursor_line.setVisible(False)
            return
        self._cursor_line.setPos(float(reference.xs[i]))
        self._cursor_line.setVisible(not self._scatter)

    def _on_click(self, x: float) -> None:
        if self.follow_latest:
            return  # the cursor belongs to the newest sample while live
        row = self._row_at(x)
        if row is not None:
            self.cursor_placed.emit(row)

    def _on_scene_click(self, ev) -> None:
        if ev.button() != QtCore.Qt.MouseButton.RightButton:
            return
        if not self._vb.sceneBoundingRect().contains(ev.scenePos()):
            return
        ev.accept()
        self.context_requested.emit(
            ev.screenPos().toPoint(), self._nearest_key(ev.scenePos())
        )

    def _row_at(self, x: float) -> int | None:
        """The row of the reference trace's nearest sample to x."""
        reference = self.reference()
        if reference is None or not len(reference.xs):
            return None
        if reference.sorted_x:
            i = int(np.searchsorted(reference.xs, x))
            i = min(max(i, 0), len(reference.xs) - 1)
            if i > 0 and abs(reference.xs[i - 1] - x) < abs(reference.xs[i] - x):
                i -= 1
        else:
            i = int(np.argmin(np.abs(reference.xs - x)))
        return int(reference.rows[i])

    def _nearest_key(self, scene_pos) -> str | None:
        """The trace nearest a scene position, in pixels. Used by the menu."""
        best, best_d = None, float("inf")
        for p in self.plotted:
            vb = self._groups.get(p.group)
            if vb is None or not len(p.xs):
                continue
            point = vb.mapSceneToView(scene_pos)
            i = (
                int(np.searchsorted(p.xs, point.x())) if p.sorted_x
                else int(np.argmin(np.abs(p.xs - point.x())))
            )
            i = min(max(i, 0), len(p.xs) - 1)
            here = vb.mapViewToScene(QtCore.QPointF(float(p.xs[i]), float(p.ys[i])))
            d = (here - scene_pos).manhattanLength()
            if d < best_d:
                best, best_d = p.key, d
        return best

    def values_at_row(self, row: int) -> dict[str, float]:
        """Every plotted trace's value at (or nearest to) a row."""
        out: dict[str, float] = {}
        for p in self.plotted:
            i = p.index_for_row(row)
            if i is not None and len(p.ys):
                out[p.key] = float(p.ys[i])
        return out

    def _on_mouse_moved(self, args) -> None:
        position = args[0]
        if self.follow_latest:
            return  # live: the readouts are the newest sample, not the mouse
        if not self.plotted or not self._vb.sceneBoundingRect().contains(position):
            if self._crosshair.isVisible():
                self._crosshair.setVisible(False)
                self._tooltip.setVisible(False)
                self.hovered.emit(None)
            return

        x = float(self._vb.mapSceneToView(position).x())
        row = self._row_at(x)
        if row is None:
            return
        self._crosshair.setPos(x)
        self._crosshair.setVisible(True)

        values: dict[str, float] = {}
        lines: list[str] = []
        for p in self.plotted:
            i = p.index_for_row(row) if p.sorted_x else (
                int(np.argmin(np.abs(p.xs - x))) if len(p.xs) else None
            )
            if i is None:
                continue
            value = float(p.ys[i])
            values[p.key] = value
            lines.append(
                f'<span style="color:{p.color}">&#9644;</span> '
                f'<span style="color:{theme.TEXT_SECONDARY}">{p.label}</span> '
                f'<span style="color:{theme.TEXT_PRIMARY};font-weight:600">'
                f"{self.readout(p.label, p.unit, value)}</span>"
            )
        self._show_tooltip(position, x, lines)
        self.hovered.emit(Hover(x=x, row=row, values=values))

    @staticmethod
    def readout(label: str, unit: str, value: float) -> str:
        """A value with its unit, and its meaning when the channel is a code."""
        text = format_value(value)
        if unit:
            text += f" {unit}"
        table = enum_for(label)
        if table:
            meaning = describe_value(table, value)
            if meaning:
                text += f" &mdash; {meaning}"
        return text

    def _show_tooltip(self, position, x: float, lines: list[str]) -> None:
        if not lines:
            self._tooltip.setVisible(False)
            return
        head = f"x {format_value(x)}"
        self._tooltip.setHtml(
            f'<div style="background:{theme.SURFACE_RAISED};padding:4px 6px;'
            f'font-size:9pt">'
            f'<div style="color:{theme.TEXT_MUTED}">{head}</div>'
            + "<br>".join(lines)
            + "</div>"
        )
        rect = self._vb.sceneBoundingRect()
        # Flip the anchor rather than let the box run off the edge.
        right_half = position.x() > rect.center().x()
        below_half = position.y() > rect.center().y()
        self._tooltip.setAnchor((1 if right_half else 0, 1 if below_half else 0))
        self._tooltip.setPos(self._vb.mapSceneToView(position))
        self._tooltip.setVisible(True)

    def grab_png(self, path: str) -> None:
        """Save what is on screen. Used by the tests to check the layout."""
        self._widget.grab().save(path)
