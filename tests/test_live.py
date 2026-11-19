"""The logger's live overlay view: scale groups, and who owns the cursor.

The cursor rule is the whole point of this file. While data is arriving the
cursor is pinned to the newest sample, so the value table is the live reading
and there is nothing to chase with the mouse; the moment the logger disconnects
or the view is frozen the cursor becomes the reader's, to click onto any sample
and read the values there. Getting that backwards either way makes the readouts
lie about which sample they came from.

Runs on Qt's offscreen platform: no display, no hardware, no serial port.

Run with:  py tests/test_live.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np  # noqa: E402
from PySide6 import QtWidgets  # noqa: E402

from mcmess.channels import load_channels  # noqa: E402
from gui import theme  # noqa: E402
from gui.live import LivePlot  # noqa: E402
from gui.logchart import Hover  # noqa: E402

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label}:\n         got  {got!r}\n         want {want!r}")


#: N40 and NSOL are both Upm, so they must land on one scale; ZWOUT and TANS
#: each bring their own.
NAMES = ("N40", "NSOL", "ZWOUT", "TANS")


def sample(n: int, offset: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
    """n samples at 20 Hz: rpm rising, the rest doing something of their own."""
    times = offset + np.arange(n, dtype=float) * 0.05
    data = np.column_stack([
        1000.0 + np.arange(n) * 10.0,          # N40
        800.0 + np.zeros(n),                   # NSOL, flat
        20.0 - np.arange(n) * 0.05,            # ZWOUT, falling
        30.0 + np.arange(n) * 0.01,            # TANS
    ])
    return times, data


def main() -> int:
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    channels = load_channels("06A906018CG")
    missing = [n for n in NAMES if n not in channels.by_name]
    if missing:
        print(f"the channel database is missing {missing}")
        return 1
    selection = [channels.by_name[n] for n in NAMES]

    live = LivePlot()
    live.resize(1200, 800)
    live.show()
    live.rebuild(selection)

    print("The value table lists the selection")
    check("one row each", live.values.rowCount(), len(NAMES))
    check("named", [live.values.item(r, live.COL_NAME).text() for r in range(4)],
          list(NAMES))
    check("units carried", live.values.item(2, live.COL_UNIT).text(),
          channels.by_name["ZWOUT"].unit_label)
    check("the first channel is the axis channel", live.axis_key, "N40")
    check("colours are the fixed order",
          live.values.item(0, live.COL_NAME).foreground().color().name(),
          theme.SERIES[0])

    print("\nFirst data draws the chart")
    times, data = sample(40)
    live.update_data(times, data)
    check("four traces", len(live.chart.plotted), 4)
    groups = {p.label: p.group for p in live.chart.plotted}
    check("N40 and NSOL share Upm", groups["N40"], groups["NSOL"])
    check("ZWOUT does not", groups["ZWOUT"] != groups["N40"], True)
    check("three scales for four channels", len(set(groups.values())), 3)
    check("the axis channel's group is the main ViewBox",
          live.chart._groups[live.chart.group_of("N40")] is live.chart._vb, True)
    check("the left axis wears its unit",
          live.chart.plot.getAxis("left").labelText, "Upm")
    check("rpm found for the top axis",
          live.chart._top_axis._xs is not None, True)

    print("\nSame channels, new points: the cheap path")
    more_times, more_data = sample(60)
    check("update_traces takes it", live.chart.update_traces(
        [p.trace for p in live.chart.plotted]), True)
    live.update_data(more_times, more_data)
    check("the chart grew", len(live.chart.plotted[0].xs), 60)
    live.rebuild(selection[:2])
    live.update_data(*(lambda t, d: (t, d[:, :2]))(*sample(40)))
    check("a changed selection rebuilds", len(live.chart.plotted), 2)
    live.rebuild(selection)
    live.update_data(*sample(60))

    print("\nLive: the cursor is pinned to the newest sample")
    live.set_following(True)
    check("following", live.chart.follow_latest, True)
    check("the cursor is the last row", live.chart.cursor_row, 59)
    check("the readout says so", live.source(), "live")
    check("the active row is the newest", live.active_row, 59)
    live.update_data(*sample(80))
    check("and it moves with the data", live.chart.cursor_row, 79)
    # The table is throttled on the live path -- repainting ten channels' cells
    # at the full redraw rate is wasted work, and numbers changing twenty times
    # a second are unreadable. It catches up within VALUES_INTERVAL_S.
    check("the table lags the chart by design",
          live.values.item(0, live.COL_VALUE).text(), "1590")
    live._refresh_values()
    check("and catches up", live.values.item(0, live.COL_VALUE).text(), "1790")

    print("\nLive: a click does not move it")
    live.chart._on_click(float(live.chart.plotted[0].xs[10]))
    check("still on the newest", live.chart.cursor_row, 79)
    check("and nothing was placed", live.cursor_row, None)

    print("\nStopped: the cursor is the reader's")
    live.set_following(False)
    check("not following", live.chart.follow_latest, False)
    check("it stays where the live cursor was", live.chart.cursor_row, 79)
    check("so the readouts do not blank", live.source(), "cursor")
    live.chart._on_click(float(live.chart.plotted[0].xs[10]))
    check("a click places it now", live.cursor_row, 10)
    check("the active row follows it", live.active_row, 10)
    check("and the table reads that sample",
          live.values.item(0, live.COL_VALUE).text(), "1100")
    live.clear_cursor()
    check("cleared, it falls back to the last sample", live.active_row, 79)

    print("\nThe mouse beats both while it is over the chart")
    live._on_hover(Hover(x=0.25, row=5))
    check("the active row follows the mouse", live.active_row, 5)
    check("the readout says so", live.source(), "mouse")
    check("the table reads that sample",
          live.values.item(0, live.COL_VALUE).text(), "1050")
    live._on_hover(None)
    check("and lets go", live.source(), "last sample")

    print("\nReconnecting re-pins it")
    live.set_following(True)
    check("following again", live.chart.follow_latest, True)
    check("on the newest sample", live.chart.cursor_row, 79)

    print("\nmin and max are over the visible window")
    live.chart.reset_zoom()
    live._refresh_values()
    check("the whole window", live.values.item(0, live.COL_MAX).text(), "1790")
    live.chart._vb.setXRange(0.0, 1.0, padding=0)
    live._refresh_values()
    check("the first second", live.values.item(0, live.COL_MAX).text(), "1200")
    check("and its minimum", live.values.item(0, live.COL_MIN).text(), "1000")

    print("\nA fixed Y range stops the scale breathing")
    group = live.chart.group_of("N40")
    live.chart.set_fixed_range(group, (0.0, 8000.0))
    check("honoured", live.chart._groups[group].viewRange()[1], [0.0, 8000.0])
    live.update_data(*sample(90))
    check("and survives new data",
          live.chart._groups[group].viewRange()[1], [0.0, 8000.0])
    check("the button knows", live.has_fixed_ranges, True)
    live.auto_ranges()
    check("Auto Y drops it", live.has_fixed_ranges, False)

    print("\nClicking a name makes it the axis channel")
    live._on_cell_clicked(2, live.COL_NAME)
    check("picked", live.axis_key, "ZWOUT")
    check("the chart agrees", live.chart.axis_key, "ZWOUT")
    check("its group took the main ViewBox",
          live.chart._groups[live.chart.group_of("ZWOUT")] is live.chart._vb, True)
    check("and the left axis with it",
          live.chart.plot.getAxis("left").labelText,
          channels.by_name["ZWOUT"].unit_label)
    check("following survived the rebuild", live.chart.follow_latest, True)

    print("\nAn empty selection clears rather than crashes")
    live.rebuild([])
    check("no rows", live.values.rowCount(), 0)
    check("no traces", live.chart.plotted, [])
    live.update_data(*sample(10))
    check("and data for nothing is ignored", live.chart.plotted, [])

    live.close()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
