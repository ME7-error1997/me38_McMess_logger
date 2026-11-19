"""The log viewer: the window, the chart and the raw-data model.

Drives the real widgets on Qt's offscreen platform, so this runs with no display
and no hardware. It checks the behaviours that are easy to break and invisible in
a screenshot: that the scale groups come out one per unit, that the axis
channel's group owns the left axis, that a cursor is a ROW and survives a change
of X axis, that Y is fitted to the visible window rather than the whole log, and
that the raw table's sort reorders the log instead of a page of it.

Run with:  py tests/test_viewer.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore, QtWidgets  # noqa: E402

from mcmess.logparse import parse_log  # noqa: E402
from gui import theme  # noqa: E402
from gui.logchart import Hover  # noqa: E402
from gui.viewer import GridModel, LogViewerWindow, pick_color  # noqa: E402

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label}:\n         got  {got!r}\n         want {want!r}")


#: Two channels in mbar (so they share a scale), one in rpm, one bare number, one
#: that never moves, and a ragged column with a hole in it.
SAMPLE = "\n".join(
    ["t_s,rpm [1/min],boost [mbar],target [mbar],fuel,flat [%]"]
    + [
        f"{i * 0.1:.1f},{1000 + i * 100},{900 + i * 40},{950 + i * 45},"
        f"{'' if i == 7 else round(0.98 - i * 0.01, 3)},50"
        for i in range(20)
    ]
)


def main() -> int:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    # Every failure path in the viewer ends in a modal box. Offscreen Qt still
    # waits for a click on one, so they are collected here instead of shown --
    # otherwise this test hangs forever on a dialog nobody can see.
    warned: list[str] = []
    LogViewerWindow._warn = lambda self, title, message: warned.append(title)

    log = parse_log(SAMPLE)
    if log is None:
        print("the sample log did not parse")
        return 1

    window = LogViewerWindow()
    window.resize(1400, 900)
    window.show()
    window.path = Path("tests/_viewer_sample.csv")  # a key for the saved state
    # Start from nothing remembered. The viewer keeps a reader's selection per
    # file in QSettings, and a previous run of this test leaving its own choices
    # behind would make the suggestion checks below pass or fail by accident.
    window.settings.remove(window._state_key())
    window.set_log(log)
    chart = window.chart

    print("Opening a log")
    check("the channels are listed", window.channels.rowCount(), len(log.series))
    check("something is plotted", len(window.selected) >= 2, True)
    check("rpm is suggested first", log.by_key[window.selected[0]].label, "rpm")
    check("the flat channel is not plotted",
          any(log.by_key[k].label == "flat" for k in window.selected), False)
    check("and it is hidden from the list",
          window.channels.isRowHidden(
              window._rows[next(s.key for s in log.series if s.label == "flat")]),
          True)
    check("the axis channel is the first plotted", window.axis_key, window.selected[0])

    print("\nColours: never two the same on one chart")
    colors = [window.colors[k] for k in window.selected]
    check("all distinct", len(set(colors)), len(colors))
    check("taken from the validated order first", colors[0], theme.SERIES[0])
    check("a full chart falls back to neutral ink",
          pick_color(set(theme.SERIES_EXTENDED), 99), theme.TEXT_SECONDARY)

    print("\nScale groups: one per unit, the axis channel's owns the left axis")
    window._set_selection([
        next(s.key for s in log.series if s.label == label)
        for label in ("boost", "target", "rpm", "fuel")
    ])
    groups = {p.series.label: p.group for p in chart.plotted}
    check("boost and target share mbar", groups["boost"], groups["target"])
    check("rpm is on its own", groups["rpm"] != groups["boost"], True)
    check("a channel with no unit never shares",
          groups["fuel"].startswith("k:"), True)
    check("four channels, three scales", len(set(groups.values())), 3)
    boost_key = next(s.key for s in log.series if s.label == "boost")
    window.axis_key = boost_key
    window._redraw()
    check("the axis channel's group is the main ViewBox",
          chart._groups[chart.group_of(boost_key)] is chart._vb, True)
    check("the left axis wears its unit",
          chart.plot.getAxis("left").labelText, "mbar")

    print("\nOwn scales on request")
    window.y_combo.setCurrentIndex(1)
    check("four channels, four scales",
          len({p.group for p in chart.plotted}), 4)
    window.y_combo.setCurrentIndex(0)

    print("\nY fits the visible window, not the whole log")
    chart.reset_zoom()
    whole = chart._groups[chart.group_of(boost_key)].viewRange()[1]
    chart._vb.setXRange(0.0, 0.5, padding=0)
    zoomed = chart._groups[chart.group_of(boost_key)].viewRange()[1]
    check("the zoomed range is tighter",
          (zoomed[1] - zoomed[0]) < (whole[1] - whole[0]) / 2, True)
    check("and it covers the visible samples",
          zoomed[0] <= 900 and zoomed[1] >= 950 + 5 * 45, True)

    print("\nA fixed range stays fixed")
    group = chart.group_of(boost_key)
    chart.set_fixed_range(group, (0.0, 3000.0))
    check("honoured", chart._groups[group].viewRange()[1], [0.0, 3000.0])
    chart.reset_zoom()
    check("and survives a zoom", chart._groups[group].viewRange()[1], [0.0, 3000.0])
    window._auto_ranges()
    check("Auto ranges drops it", chart.fixed_ranges, {})

    print("\nThe cursor is a log row, not an x value")
    chart.reset_zoom()
    window._on_cursor_placed(11)
    check("placed", window.cursor_row, 11)
    check("the active row is the cursor's", window.active_row, 11)
    check("the clear button lights up", window.clear_button.isEnabled(), True)
    rpm_row11 = log.rows[11][log.by_key[
        next(s.key for s in log.series if s.label == "rpm")].col]
    check("the panel reads that row", rpm_row11, 2100)
    row_before = window.cursor_row
    window.x_combo.setCurrentIndex(window.x_combo.count() - 1)  # scatter
    check("a change of X axis does not move it", window.cursor_row, row_before)
    check("scatter mode has no cursor line", chart._cursor_line.isVisible(), False)
    window.x_combo.setCurrentIndex(0)
    check("back in time mode it is drawn again",
          chart._cursor_line.isVisible(), True)

    print("\nHover beats the cursor while the mouse is over the chart")
    window._on_hover(Hover(x=0.3, row=3))
    check("the active row follows the mouse", window.active_row, 3)
    check("the label says so", "Mouse" in window.source_label.text(), True)
    window._on_hover(None)
    check("and falls back to the cursor", window.active_row, 11)
    check("the label says so", "Cursor" in window.source_label.text(), True)
    window._set_cursor(None)
    check("with neither, the first sample", window.active_row, 0)
    check("the label says so", "First sample" in window.source_label.text(), True)

    print("\nThe nearest sample to a row, per trace")
    boost = next(p for p in chart.plotted if p.series.label == "boost")
    check("an exact hit", boost.index_for_row(5), 5)
    check("past the end clamps", boost.index_for_row(999), len(boost.rows) - 1)
    fuel = next(p for p in chart.plotted if p.series.label == "fuel")
    check("the ragged channel skips its hole", 7 in list(fuel.rows), False)
    check("and row 7 takes a neighbour", fuel.rows[fuel.index_for_row(7)] in (6, 8), True)

    print("\nStats are computed over the zoom window")
    window._set_selection([boost_key])
    chart.reset_zoom()
    window._refresh_stats()
    whole_max = window.stats.item(0, 3).text()
    chart._vb.setXRange(0.0, 0.5, padding=0)
    window._refresh_stats()
    zoom_max = window.stats.item(0, 3).text()
    check("the whole log", whole_max, "1660")
    check("the first half second", zoom_max, "1100")

    print("\nThe raw table: sorting reorders the log, not a page of it")
    model = GridModel(log)
    rpm_col = next(i for i, c in enumerate(log.columns) if c.label == "rpm")
    model.sort(rpm_col, QtCore.Qt.SortOrder.DescendingOrder)
    check("the fastest row is first", model.log_row(0), 19)
    check("the header still says which log row that is",
          model.headerData(0, QtCore.Qt.Orientation.Vertical), "20")
    model.sort(rpm_col, QtCore.Qt.SortOrder.AscendingOrder)
    check("and back", model.log_row(0), 0)
    fuel_col = next(i for i, c in enumerate(log.columns) if c.label == "fuel")
    model.sort(fuel_col, QtCore.Qt.SortOrder.AscendingOrder)
    check("the empty cell sorts last", model.log_row(len(log.rows) - 1), 7)
    check("every column is named",
          model.headerData(rpm_col, QtCore.Qt.Orientation.Horizontal),
          "rpm [1/min]")

    print("\nThe second X axis reads another channel at the X ticks")
    window._set_selection([boost_key])
    chart.reset_zoom()
    labels = chart._top_axis.tickStrings([0.0, 0.5, 1.0], 1, 0.5)
    check("rpm at t=0, 0.5 and 1.0", labels, ["1000", "1500", "2000"])
    window.x2_combo.setCurrentIndex(1)  # none
    check("turned off, it says nothing",
          chart._top_axis.tickStrings([0.5], 1, 0.5), [""])

    print("\nWhat the reader set is remembered per file")
    window.x2_combo.setCurrentIndex(0)
    window._set_selection([boost_key])
    chart.set_fixed_range(chart.group_of(boost_key), (0.0, 3000.0))
    window._save_state()
    second = LogViewerWindow()
    second.path = window.path
    second.set_log(parse_log(SAMPLE))
    check("the selection comes back", second.selected, [boost_key])
    check("so does the fixed range",
          second.chart.fixed_ranges.get(second.chart.group_of(boost_key)),
          (0.0, 3000.0))
    window.settings.remove(window._state_key())
    second.close()

    print("\nA log that is not a log is refused, not crashed into")
    scratch = Path(__file__).resolve().parent / "_not_a_log.txt"
    scratch.write_text("hello\nworld\nnothing here is a grid\n", encoding="utf-8")
    empty = LogViewerWindow()
    empty.open_path(scratch)
    check("nothing was loaded", empty.log, None)
    check("and the reader was told why", warned[-1], "Nothing to plot")
    scratch.write_bytes(b"PK\x03\x04\x00\x00\x00\x00binary")
    empty.open_path(scratch)
    check("a zip is caught before parsing", warned[-1], "Not a text log")
    empty.open_path(scratch.with_name("_no_such_log.csv"))
    check("a missing file is caught too", warned[-1], "Cannot open")
    scratch.unlink(missing_ok=True)
    empty.close()

    window.close()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
