"""Logging in the one window: who owns the cursor, and what a session becomes.

The rule this file exists for: **while data is arriving, the chart belongs to
the data; when it stops, it belongs to the reader.** Live, the cursor is pinned
to the newest sample and the mouse can neither move it, zoom, pan nor hover --
each of those would put old numbers where the live ones are read. Disconnect, or
lose the link, and the session is opened as the log it has just become, with
everything unlocked. Getting that backwards either way makes the readouts lie
about which sample they came from.

Also here: every logged channel has a row and a value whether or not it is
drawn, with its description beside it; and a real session's CSV is what is on
screen afterwards, not a buffer that resembles it.

Runs on Qt's offscreen platform: no display, no hardware, no serial port.

Run with:  py tests/test_live.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np  # noqa: E402
import pyqtgraph as pg  # noqa: E402
from PySide6 import QtWidgets  # noqa: E402

from mcmess.channels import load_channels  # noqa: E402
from mcmess.profile import Profile, Style, load_profile, save_profile  # noqa: E402
from mcmess.recorder import RecorderStats  # noqa: E402
import gui.window as window_module  # noqa: E402
from gui.logchart import Hover  # noqa: E402
from gui.window import REPLAY_PORT, SIM_PORT, MainWindow, default_args  # noqa: E402

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label}:\n         got  {got!r}\n         want {want!r}")


#: N40 and NSOL are both Upm, so they must land on one scale. KLINFO is logged
#: and not drawn: it is a code, and the reason the two lists are separate.
LOGGED = ["N40", "NSOL", "ZWOUT", "TANS", "KLINFO"]
DRAWN = ["N40", "NSOL", "ZWOUT"]


class FakeRecorder:
    """The recorder's interface over arrays the test controls."""

    def __init__(self, channels) -> None:
        self.channels = list(channels)
        self.state = "running"
        self.port = "FAKE"
        self.error = ""
        self.stopped = False
        self.times = np.empty(0)
        self.data = np.empty((0, len(self.channels)))

    def feed(self, n: int) -> None:
        """n samples at 20 Hz: rpm rising, the rest doing something of their own."""
        index = np.arange(n)
        self.times = index * 0.05
        self.data = np.column_stack([
            1000.0 + index * 10.0,         # N40
            800.0 + np.zeros(n),           # NSOL, flat
            20.0 - index * 0.05,           # ZWOUT, falling
            30.0 + index * 0.01,           # TANS
            index % 4,                     # KLINFO, the cylinder counter
        ])

    def snapshot(self, seconds=None, max_points=None):
        times, data = self.times, self.data
        if seconds is not None and len(times):
            start = int(np.searchsorted(times, times[-1] - seconds, side="left"))
            times, data = times[start:], data[start:]
        return times, data, list(self.channels)

    def stats(self) -> RecorderStats:
        return RecorderStats(samples=len(self.times), sample_hz=20.0,
                             last_error=self.error)

    def last_raw(self) -> dict:
        return {}

    def start(self) -> None:
        pass

    def stop(self, timeout: float = 3.0) -> None:
        self.stopped = True
        if self.state != "error":
            self.state = "idle"

    def start_recording(self, path) -> None:
        pass

    def stop_recording(self) -> None:
        pass


def tick(window: MainWindow) -> None:
    """One turn of the window's timer, with the value throttle out of the way."""
    window._values_at = 0.0
    window._refresh()


def curves(chart) -> list:
    """Every curve in the chart's scene, whichever scale holds it."""
    return [
        item for item in chart._widget.scene().items()
        if isinstance(item, pg.PlotDataItem)
    ]


def value(window: MainWindow, name: str) -> str:
    return window.channels.item(window._rows[name], window.COL_VALUE).text()


def pump(app, seconds: float, until=None) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        if until is not None and until():
            return
        time.sleep(0.01)


def main() -> int:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    warned: list[str] = []
    MainWindow._warn = lambda self, title, message: warned.append(title)

    channels = load_channels("06A906018CG")
    missing = [n for n in LOGGED if n not in channels.by_name]
    if missing:
        print(f"the channel database is missing {missing}")
        return 1

    scratch = Path(tempfile.mkdtemp(prefix="mcmess-test-"))
    # The toolbar lists the presets in this folder. The real one is the
    # reader's own, and a test should neither depend on it nor write to it.
    window_module.PROFILE_DIR = scratch
    args = default_args()
    args.demo = True
    args.log_dir = str(scratch)
    args.knock_base = 0
    args.channels = ",".join(LOGGED)

    window = MainWindow(channels, args)
    window.set_profile(Profile(log=list(LOGGED), graph=list(DRAWN)))
    window.resize(1500, 950)
    window.show()
    window.port_combo.setCurrentIndex(window.port_combo.findData(SIM_PORT))
    chart = window.chart

    fake = FakeRecorder(channels.by_name[n] for n in LOGGED)
    window.recorder_factory = lambda port: fake

    print("Before connecting")
    check("nothing is following", window.following, False)
    check("the table tab has nothing to show", window.tabs.isTabEnabled(1), False)
    check("the channel window is offered", window.channels_button.isEnabled(), True)

    print("\nConnected: every logged channel has a row")
    check("it connects", window.connect(), True)
    check("one row each", window.channels.rowCount(), len(LOGGED))
    check("named", [window.source.entries[r].label for r in range(5)], LOGGED)
    check("described",
          window.channels.item(window._rows["TANS"], window.COL_ABOUT).text(),
          channels.by_name["TANS"].about)
    check("the description column is on show",
          window.channels.isColumnHidden(window.COL_ABOUT), False)
    check("only the profile's graph list is drawn", window.selected, DRAWN)
    check("so the code channel is not",
          "KLINFO" in window.selected, False)
    check("the channel window is locked while connected",
          window.channels_button.isEnabled(), False)
    check("there is no flat-channel filter while logging",
          window.hide_flat.isVisibleTo(window), False)

    print("\nFirst data draws the chart")
    fake.feed(40)
    tick(window)
    check("three traces", len(chart.plotted), 3)
    groups = {p.label: p.group for p in chart.plotted}
    check("N40 and NSOL share Upm", groups["N40"], groups["NSOL"])
    check("ZWOUT does not", groups["ZWOUT"] != groups["N40"], True)
    check("the profile's first drawn channel owns the left axis",
          window.axis_key, "N40")
    check("rpm found for the top axis", chart._top_axis._xs is not None, True)

    print("\nLive: the cursor is pinned to the newest sample")
    check("following", window.following, True)
    check("the chart agrees", chart.follow_latest, True)
    check("the cursor is the last row", chart.cursor_row, 39)
    check("the active row is the newest", window.active_row, 39)
    check("the status bar says so", window.reading(), "Live")
    check("the panel reads the newest sample", value(window, "N40"), "1390")
    check("an undrawn channel still shows its value", value(window, "KLINFO"), "3")
    fake.feed(80)
    tick(window)
    check("and it moves with the data", chart.cursor_row, 79)
    check("the values move with it", value(window, "N40"), "1790")
    check("the top axis follows too", len(chart._top_axis._xs), 80)

    print("\nLive: the mouse can do nothing to the view")
    chart._on_click(float(chart.plotted[0].xs[10]))
    check("a click does not move the cursor", chart.cursor_row, 79)
    check("and nothing was placed", window.cursor_row, None)
    window._on_hover(Hover(x=0.25, row=5))
    check("a hover does not change the reading", window.active_row, 79)
    check("it still says live", window.reading(), "Live")
    check("the view box is locked", chart._vb.locked, True)
    check("zoom by mouse is off", chart._vb.state["mouseEnabled"][0], False)
    check("so is Reset zoom", window.reset_button.isEnabled(), False)
    check("and Clear cursor", window.clear_button.isEnabled(), False)

    print("\nLive: ticking a channel draws it, and the profile remembers")
    window._set_selection([*window.selected, "KLINFO"])
    tick(window)
    check("four traces", len(chart.plotted), 4)
    check("the profile's graph list has it", "KLINFO" in window.profile.graph, True)
    window._set_selection([k for k in window.selected if k != "KLINFO"])
    tick(window)
    check("and unticking takes it off again", window.profile.graph, DRAWN)
    check("without stopping it being logged", "KLINFO" in window.profile.log, True)
    check("still following after the redraw", chart.follow_latest, True)

    print("\nFreeze view: the same bargain without dropping the link")
    window.pause_button.setChecked(True)
    window._toggle_pause()
    check("not following", window.following, False)
    check("the chart is unlocked", chart._vb.locked, False)
    check("the cursor stays on the last sample", window.cursor_row, 79)
    chart._on_click(float(chart.plotted[0].xs[10]))
    check("a click places it now", window.cursor_row, 10)
    check("the panel reads that sample", value(window, "N40"), "1100")
    window._on_hover(Hover(x=0.25, row=5))
    check("and the mouse beats the cursor", window.active_row, 5)
    window._on_hover(None)
    fake.feed(90)
    tick(window)
    check("new samples do not move a frozen view", window.source.n_rows, 80)
    window.pause_button.setChecked(False)
    window._toggle_pause()
    tick(window)
    check("released, it is pinned again", chart.cursor_row, 89)
    check("and the placed cursor is forgotten", window.cursor_row, None)
    check("locked again", chart._vb.locked, True)

    print("\nA fixed Y range stops the scale breathing")
    group = chart.group_of("N40")
    window._set_range(group, (0.0, 8000.0))
    check("honoured", chart._groups[group].viewRange()[1], [0.0, 8000.0])
    fake.feed(100)
    tick(window)
    check("and survives new data", chart._groups[group].viewRange()[1], [0.0, 8000.0])

    print("\nDisconnected: the session becomes a log, and the chart is yours")
    colors = {k: window.colors[k] for k in window.selected}
    view = chart.visible_x_range()
    window._disconnect()
    check("the recorder was stopped", fake.stopped, True)
    check("no longer live", window.source.live, False)
    check("it is a log now", window.log is not None, True)
    check("every sample is in it", len(window.log.rows), 100)
    check("nothing was recorded, and it says so",
          "unsaved session" in window.file_label.text(), True)
    check("the same channels are drawn",
          [window._entry(k).label for k in window.selected], DRAWN)
    check("in the same colours",
          {window._entry(k).label: window.colors[k] for k in window.selected},
          colors)
    check("on the same stretch of time", chart.visible_x_range(), view)
    check("the fixed range came along",
          chart._groups[chart.group_of(window.selected[0])].viewRange()[1],
          [0.0, 8000.0])
    check("every logged channel still has its row",
          sum(not window.channels.isRowHidden(r)
              for r in range(window.channels.rowCount())), len(LOGGED))
    check("not following", window.following, False)
    check("the chart is unlocked", chart._vb.locked, False)
    check("zoom by mouse is back", chart._vb.state["mouseEnabled"][0], True)
    check("the cursor is where the live one was", window.cursor_row, 99)
    check("so the readouts did not blank", value(window, window.selected[0]), "1990")
    chart._on_click(float(chart.plotted[0].xs[10]))
    check("a click places it", window.cursor_row, 10)
    check("the status bar says cursor", window.reading(), "Cursor")
    check("the panel reads that sample", value(window, window.selected[0]), "1100")
    check("the raw table is there", window.tabs.isTabEnabled(1), True)
    check("the channel window is offered again",
          window.channels_button.isEnabled(), True)

    print("\nA lost link ends the same way, with the reason")
    fake = FakeRecorder(channels.by_name[n] for n in LOGGED)
    window.connect()
    fake.feed(30)
    tick(window)
    check("live again", window.following, True)
    check("the raw table is put away", window.tabs.isTabEnabled(1), False)
    fake.state = "error"
    fake.error = "link lost: 5 consecutive timeouts"
    tick(window)
    check("the reader is told", warned[-1], "Link failed")
    check("the session is on screen as a log", len(window.log.rows), 30)
    check("and the cursor is free", chart._vb.locked, False)

    print("\nA second connection draws only itself")
    # The bug this pins: the axis channel's scale kept every curve it had ever
    # been given, so after reconnecting the previous session was drawn through
    # the new one -- two blue rpm lines where there should be one.
    fake = FakeRecorder(channels.by_name[n] for n in LOGGED)
    asked: list[str] = []
    window.run_dialog = lambda dialog: asked.append(type(dialog).__name__) or False
    window._on_connect_clicked()
    fake.feed(25)
    tick(window)
    check("the Connect button connects", window.recorder is fake, True)
    check("without opening the channel window", asked, [])
    check("one curve per trace, and no more", len(curves(chart)), len(chart.plotted))
    check("none of them the previous session's",
          sorted(len(c.xData) for c in curves(chart)), [25, 25, 25])
    window._set_selection(window.selected)      # a redraw with nothing changed
    check("a redraw does not add any", len(curves(chart)), len(chart.plotted))
    window._disconnect()
    check("nor does the session ending", len(curves(chart)), len(chart.plotted))
    check("and they are the log's", sorted(len(c.xData) for c in curves(chart)),
          [25, 25, 25])

    print("\nPresets: picked on the toolbar")
    first = save_profile(
        Profile(list(LOGGED), list(DRAWN), channels.part_number), scratch / "first.json")
    save_profile(
        Profile(["N40", "TANS"], ["TANS"], channels.part_number,
                style={"TANS": Style("#d95926", (0.0, 150.0))}),
        scratch / "second.json")
    window.set_profile(load_profile(first))
    combo = window.preset_combo
    check("every preset file is listed",
          [combo.itemText(i) for i in range(combo.count())], ["first", "second"])
    check("on the one in use", combo.currentText(), "first")
    window._on_preset_chosen(combo.findText("second"))
    check("choosing one loads it", window.profile.log, ["N40", "TANS"])
    check("with what it keeps for each channel",
          (window.profile.color_of("TANS"), window.profile.range_of("TANS")),
          ("#d95926", (0.0, 150.0)))
    (scratch / "broken.json").write_text("{ nope", encoding="utf-8")
    window._update_profile_label()
    window._on_preset_chosen(combo.findText("broken"))
    check("a broken one is refused", warned[-1], "Cannot load the preset")
    check("and the one in use stays", combo.currentText(), "second")
    (scratch / "broken.json").unlink()
    window.set_profile(load_profile(first))

    print("\nDouble-click a channel: its colour and Y range, kept in the preset")
    fake = FakeRecorder(channels.by_name[n] for n in LOGGED)
    window.connect()
    fake.feed(40)
    tick(window)
    seen: dict = {}

    def answer(dialog) -> bool:
        seen["title"] = dialog.windowTitle()
        seen["starts on"] = dialog.color()
        dialog.set_color("#c2308e")
        dialog.set_span((0.0, 8000.0))
        return True

    window.run_dialog = answer
    was = window.colors["N40"]
    window._on_cell_double_clicked(window._rows["N40"], window.COL_NAME)
    check("it opens on that channel", seen["title"].startswith("N40"), True)
    check("showing the colour it has", seen["starts on"], was)
    check("the trace takes the new colour",
          next(p.color for p in chart.plotted if p.key == "N40"), "#c2308e")
    check("its scale takes the range",
          chart._groups[chart.group_of("N40")].viewRange()[1], [0.0, 8000.0])
    check("the preset has the colour", window.profile.color_of("N40"), "#c2308e")
    check("and the range", window.profile.range_of("N40"), (0.0, 8000.0))
    check("NSOL is on the same scale, so the range is its too",
          window.profile.range_of("NSOL"), (0.0, 8000.0))
    check("but the colour is not", window.profile.color_of("NSOL"), "")
    on_disk = load_profile(first)
    check("it is in the preset's file at once",
          (on_disk.color_of("N40"), on_disk.range_of("N40")),
          ("#c2308e", (0.0, 8000.0)))
    check("still following after all that", chart.follow_latest, True)

    opened: list[int] = []
    window.run_dialog = lambda dialog: opened.append(1) or False
    window._on_cell_double_clicked(window._rows["N40"], window.COL_TICK)
    check("a double click on the tick box is two ticks, not a window", opened, [])
    window._on_cell_double_clicked(window._rows["TANS"], window.COL_ABOUT)
    check("any other column opens it", opened, [1])
    check("cancelled, nothing changes", window.profile.color_of("TANS"), "")
    window._on_cell_clicked(window._rows["TANS"], window.COL_NAME)
    check("a click on an undrawn name does not draw it",
          "TANS" in window.selected, False)

    def to_auto(dialog) -> bool:
        dialog.set_span(None)
        return True

    window.run_dialog = to_auto
    window.edit_style("NSOL")
    check("Auto takes the range off the whole scale",
          (window.profile.range_of("N40"), window.profile.range_of("NSOL")),
          (None, None))
    check("and off the chart", chart.fixed_ranges, {})
    check("the colour is left alone", window.profile.color_of("N40"), "#c2308e")
    window.run_dialog = answer
    window.edit_style("N40")
    window._disconnect()
    check("the log the session becomes is drawn the same",
          window.colors[window.selected[0]], "#c2308e")

    print("\nThe next session starts the way the last one was left")
    fake = FakeRecorder(channels.by_name[n] for n in LOGGED)
    window.connect()
    fake.feed(20)
    tick(window)
    check("in that colour", window.colors["N40"], "#c2308e")
    check("on that range",
          chart._groups[chart.group_of("N40")].viewRange()[1], [0.0, 8000.0])
    check("and no other trace was given the colour",
          [k for k in window.selected if window.colors[k] == "#c2308e"], ["N40"])
    window._set_selection([k for k in window.selected if k != "ZWOUT"])
    check("a tick is written to the preset's file too",
          load_profile(first).graph, ["N40", "NSOL"])
    window._disconnect()
    other = MainWindow(channels, args)
    other.set_profile(load_profile(first))
    check("a new window reads it all back from the file",
          (other.profile.color_of("N40"), other.profile.range_of("NSOL"),
           other.profile.graph),
          ("#c2308e", (0.0, 8000.0), ["N40", "NSOL"]))
    other.close()
    window.run_dialog = lambda dialog: False

    print("\nA real session: the CSV that was written is what you read")
    window.recorder_factory = None
    window.set_profile(Profile(log=list(LOGGED), graph=list(DRAWN)))
    check("it connects to the simulator", window.connect(), True)
    pump(app, 20.0, until=lambda: window.source.live and window.source.n_rows >= 12)
    check("samples arrived", window.source.n_rows >= 12, True)
    check("recording started by itself", window.record_button.isChecked(), True)
    recorded = window._log_path
    check("into the log directory", recorded.parent, scratch)
    window._disconnect()
    check("the file is the log on screen", window.path, recorded)
    lines = [
        line for line in recorded.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    check("row for row", len(window.log.rows), len(lines) - 1)
    check("its header is shown", "McMESS log" in window.meta_label.text(), True)
    check("the cursor is free", chart._vb.locked, False)
    files_before = sorted(p.name for p in scratch.glob("*.csv"))

    print("\nA replay: a recorded pull through the live path")
    args.replay = str(recorded)
    args.replay_speed = 50.0
    window._refresh_ports()
    window.port_combo.setCurrentIndex(window.port_combo.findData(REPLAY_PORT))
    check("it connects", window.connect(), True)
    check("with the log's own channels",
          [e.label for e in window.source.entries][:2], ["N40", "NSOL"])
    check("described from the database",
          window.channels.item(window._rows["TANS"], window.COL_ABOUT).text(),
          channels.by_name["TANS"].about)
    check("following", window.following, True)
    pump(app, 20.0, until=lambda: window.recorder is None)
    check("when the log runs out it disconnects by itself", window.recorder, None)
    check("and says why", window.record_label.text(), "replay finished")
    check("the whole pull is on screen", len(window.log.rows), len(lines) - 1)
    check("with the cursor free", chart._vb.locked, False)
    check("and nothing new was written",
          sorted(p.name for p in scratch.glob("*.csv")), files_before)

    window.close()
    # The window remembers what was drawn per log file. These files are about
    # to be deleted; their entries should not outlive them in the settings.
    for key in window.settings.allKeys():
        if "mcmess-test-" in key:
            window.settings.remove(key)
    shutil.rmtree(scratch, ignore_errors=True)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
