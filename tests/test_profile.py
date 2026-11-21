"""Presets (profile files), the channel window, the colour window, and replay.

A preset is what is logged, what is drawn, and how each channel looks. This
checks the file format round-trips, that a bad file says one sentence about what
is wrong with it, and that the channel window keeps the two lists honest --
nothing drawn that is not logged, codes logged but not drawn by default, and the
inputs of a computed column counted in the cost even though nobody ticked them.

Replay is here too because it is the other way a session's channel list gets
decided: by the log being played back.

Runs on Qt's offscreen platform: no display, no hardware, no serial port.

Run with:  py tests/test_profile.py
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

from PySide6 import QtCore, QtWidgets  # noqa: E402

from mcmess.channels import load_channels  # noqa: E402
from mcmess.derived import DERIVED_COLUMNS  # noqa: E402
from mcmess.logparse import parse_log  # noqa: E402
from mcmess.profile import (  # noqa: E402
    DEFAULT_PROFILE,
    Profile,
    ProfileError,
    Style,
    belongs_on_graph,
    load_profile,
    save_profile,
)
from mcmess.replay import FINISHED, ReplayError, ReplayRecorder, replay_channels  # noqa: E402
from gui import theme  # noqa: E402
from gui.picker import ChannelDialog, ChannelPicker  # noqa: E402
from gui.style import StyleDialog  # noqa: E402

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label}:\n         got  {got!r}\n         want {want!r}")


def error_of(call) -> str:
    try:
        call()
    except (ProfileError, ReplayError) as exc:
        return str(exc)
    return ""


CHECKED = QtCore.Qt.CheckState.Checked
UNCHECKED = QtCore.Qt.CheckState.Unchecked

OURS = "\n".join(
    ["# McMESS log, Bosch M3.8 / 06A906018CG",
     "t_s,N40 [Upm],TMOT [grad C],KLINFO [dez],made_up [bar]"]
    + [f"{100 + i * 0.05:.2f},{1000 + i * 40},{80 + i * 0.1:.1f},{i % 4},{i * 0.01:.2f}"
       for i in range(30)]
)


def main() -> int:
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    warned: list[str] = []
    ChannelDialog._warn = lambda self, title, message: warned.append(title)

    channels = load_channels("06A906018CG")
    scratch = Path(tempfile.mkdtemp(prefix="mcmess-test-"))

    print("A profile is two lists, and the second is inside the first")
    profile = Profile(
        log=["N40", "TL", "N40", " KLINFO ", ""],
        graph=["TL", "ZWOUT", "TL"],
        ecu="06A906018CG",
    ).normalised()
    check("duplicates and blanks are dropped", profile.log, ["N40", "TL", "KLINFO"])
    check("nothing is drawn that is not logged", profile.graph, ["TL"])

    print("\nIt round-trips through its file")
    path = save_profile(
        Profile(["N40", "TL", "KLINFO"], ["N40", "TL"], "06A906018CG"),
        scratch / "mine.json",
    )
    back = load_profile(path)
    check("log", back.log, ["N40", "TL", "KLINFO"])
    check("graph", back.graph, ["N40", "TL"])
    check("ecu", back.ecu, "06A906018CG")
    check("named after its file", back.name, "mine")
    check("one name per line, so two profiles diff",
          '\n    "TL",\n' in path.read_text(encoding="utf-8"), True)

    print("\nIt keeps each channel's colour and Y range")
    styled = Profile(
        ["N40", "TL", "KLINFO"], ["N40", "TL"], "06A906018CG",
        style={
            "N40": Style("#3987e5", (0.0, 8000.0)),
            "TL": Style(range=(0.0, 10.0)),
            "KLINFO": Style(),
        },
    )
    back_styled = load_profile(save_profile(styled, scratch / "styled.json"))
    check("colour and range", (back_styled.color_of("N40"), back_styled.range_of("N40")),
          ("#3987e5", (0.0, 8000.0)))
    check("a range without a colour", (back_styled.color_of("TL"), back_styled.range_of("TL")),
          ("", (0.0, 10.0)))
    check("a channel nobody styled has no entry", "KLINFO" in back_styled.style, False)
    check("and asking about it is not an error",
          (back_styled.color_of("KLINFO"), back_styled.range_of("KLINFO")), ("", None))
    check("setting says whether anything changed",
          (back_styled.set_color("TL", "#d95926"), back_styled.set_color("TL", "#d95926")),
          (True, False))
    check("auto removes the range", back_styled.set_range("N40", None), True)
    check("and leaves the colour", back_styled.color_of("N40"), "#3987e5")
    back_styled.set_color("TL", "")
    back_styled.set_range("TL", None)
    check("with nothing left, the entry goes", "TL" in back_styled.style, False)
    check("a copy does not share its styles",
          styled.copy().style["N40"] is styled.style["N40"], False)
    check("a preset with no styles writes no style block",
          '"style"' in path.read_text(encoding="utf-8"), False)
    (scratch / "hand.json").write_text(
        '{"log": ["N40", "TL", "ML"], "style": {'
        '"N40": {"color": "blue", "range": [0, 8000]},'
        '"TL": {"color": "#D95926", "range": [10, 0]},'
        '"ML": "nonsense"}}', encoding="utf-8")
    hand = load_profile(scratch / "hand.json")
    check("a hand-edited file loses a bad colour, not the entry",
          (hand.color_of("N40"), hand.range_of("N40")), ("", (0.0, 8000.0)))
    check("an upside-down range is dropped", hand.range_of("TL"), None)
    check("a colour is kept in lower case", hand.color_of("TL"), "#d95926")
    check("nonsense is ignored", "ML" in hand.style, False)

    print("\nA bad file says what is wrong with it")
    check("missing", "cannot read" in error_of(
        lambda: load_profile(scratch / "nope.json")), True)
    (scratch / "broken.json").write_text("{ not json", encoding="utf-8")
    check("not JSON", "not valid JSON" in error_of(
        lambda: load_profile(scratch / "broken.json")), True)
    (scratch / "other.json").write_text('{"channels": ["N40"]}', encoding="utf-8")
    check("not a profile", 'no "log" list' in error_of(
        lambda: load_profile(scratch / "other.json")), True)
    (scratch / "empty.json").write_text('{"log": []}', encoding="utf-8")
    check("empty", "nothing to log" in error_of(
        lambda: load_profile(scratch / "empty.json")), True)

    print("\nWhat belongs on a graph by default")
    check("a quantity does", belongs_on_graph("N40", "Upm"), True)
    check("KLINFO does not: it is a packed code", belongs_on_graph("KLINFO", "dez"), False)
    check("nor does a status byte", belongs_on_graph("STATVS", "dez"), False)
    check("nor a channel with no conversion", belongs_on_graph("XNDIV", "raw"), False)
    (scratch / "old.json").write_text(
        '{"log": ["N40", "KLINFO", "TL"]}', encoding="utf-8")
    check("a file with no graph list draws the quantities",
          load_profile(scratch / "old.json").graph, ["N40", "TL"])

    print("\nThe profile that ships")
    shipped = load_profile(DEFAULT_PROFILE)
    known = set(channels.by_name) | set(DERIVED_COLUMNS)
    check("every name is one this ECU has",
          [n for n in shipped.log if n not in known], [])
    check("it logs more than it draws", len(shipped.log) > len(shipped.graph) > 0, True)
    check("it is for this ECU", shipped.ecu, channels.part_number)

    print("\nThe picker: two ticks per channel")
    picker = ChannelPicker(channels)
    picker.set_selection(["TL", "N40", "KLINFO"], ["TL", "N40"])
    check("the order given is the order kept", picker.log_names(), ["TL", "N40", "KLINFO"])
    check("graph", picker.graph_names(), ["TL", "N40"])
    item = picker._items["ZWOUT"]
    item.setCheckState(picker.COL_NAME, CHECKED)
    check("logging a quantity draws it", "ZWOUT" in picker.graph_names(), True)
    check("new ticks go on the end", picker.log_names()[-1], "ZWOUT")
    picker._items["STATVS"].setCheckState(picker.COL_NAME, CHECKED)
    check("logging a code does not", "STATVS" in picker.graph_names(), False)
    picker._items["STATVS"].setCheckState(picker.COL_GRAPH, CHECKED)
    check("but it can be asked for", "STATVS" in picker.graph_names(), True)
    picker._items["TMOT"].setCheckState(picker.COL_GRAPH, CHECKED)
    check("asking for a trace logs the channel", "TMOT" in picker.log_names(), True)
    item.setCheckState(picker.COL_NAME, UNCHECKED)
    check("no longer logged, no longer drawn", "ZWOUT" in picker.graph_names(), False)
    picker.set_selection(["N40", "KLINFO"])
    check("with no graph list given, the quantities", picker.graph_names(), ["N40"])

    print("\nComputed columns bring their inputs")
    picker.set_selection(["N40", "KR_CYL1"])
    check("a computed column can be ticked", picker.log_names(), ["N40", "KR_CYL1"])
    check("its inputs are read without being asked for",
          sorted(picker.implied_inputs()), ["DWKR_Z", "KLINFO"])
    check("and counted in what the sample costs",
          sorted(c.name for c in picker.ecu_channels()), ["DWKR_Z", "KLINFO", "N40"])
    check("and the cost line says so",
          "KLINFO" in picker.cost_label.text(), True)
    picker.set_selection([])
    check("nothing ticked, nothing read", picker.ecu_channels(), [])

    print("\nThe filter")
    picker.set_selection(["N40", "TMOT"])
    picker.search.setText("coolant")
    shown = [n for n, it in picker._items.items() if not it.isHidden()]
    check("it searches the descriptions", "TMOT" in shown, True)
    check("and hides the rest", "N40" in shown, False)
    picker.search.setText("")
    picker.only_ticked.setChecked(True)
    shown = [n for n, it in picker._items.items() if not it.isHidden()]
    check("only ticked shows just the logged ones", sorted(shown), ["N40", "TMOT"])

    print("\nThe channel window")
    dialog = ChannelDialog(channels, back)
    check("it opens on the profile", dialog.profile().log, ["N40", "TL", "KLINFO"])
    check("unchanged, there is nothing to save", dialog.modified, False)
    dialog.picker._items["ZWOUT"].setCheckState(dialog.picker.COL_NAME, CHECKED)
    check("a tick makes it changed", dialog.modified, True)
    check("saved back to its file", dialog.save_path(path), True)
    check("which is then up to date", dialog.modified, False)
    check("and holds the new channel", load_profile(path).log[-1], "ZWOUT")
    check("drawn, because it is a quantity", "ZWOUT" in load_profile(path).graph, True)

    save_profile(Profile(["N40", "NOT_A_CHANNEL", "TL"], ["N40"], "06A906018CG"),
                 scratch / "stale.json")
    check("a profile naming an unknown channel loads",
          dialog.load_path(scratch / "stale.json"), True)
    check("and says so", warned[-1], "Unknown channels")
    check("without it", dialog.profile().log, ["N40", "TL"])
    save_profile(Profile(["N40"], ["N40"], "8D0907558E"), scratch / "other_ecu.json")
    dialog.load_path(scratch / "other_ecu.json")
    check("a preset for another ECU is flagged",
          "A preset for another ECU" in warned, True)
    check("a bad file is refused", dialog.load_path(scratch / "broken.json"), False)
    check("and the ticks are left alone", dialog.profile().log, ["N40"])
    dialog.picker.set_selection([])
    check("with nothing ticked there is nothing to connect with",
          dialog.ok_button.isEnabled(), False)
    dialog.close()

    styled_dialog = ChannelDialog(channels, load_profile(scratch / "styled.json"))
    styled_dialog.picker._items["ZWOUT"].setCheckState(styled_dialog.picker.COL_NAME, CHECKED)
    check("colours and ranges pass through the channel window untouched",
          (styled_dialog.profile().color_of("N40"), styled_dialog.profile().range_of("TL")),
          ("#3987e5", (0.0, 10.0)))
    styled_dialog.save_path(scratch / "styled.json")
    check("and through its Save",
          load_profile(scratch / "styled.json").range_of("N40"), (0.0, 8000.0))
    styled_dialog.close()

    print("\nThe colour and range window")
    editor = StyleDialog(
        label="TL", unit="ms/Umdr.", description="Engine load signal",
        color="#d95926", span=None, data_span=(0.5, 7.7),
        shared_with=["LDSHK", "LDSKF"],
    )
    check("it opens on the channel's colour", editor.color(), "#d95926")
    check("and on auto when no range is fixed", editor.span(), None)
    check("with the data's own span ready in the boxes",
          (editor.low_spin.value(), editor.high_spin.value()), (0.5, 7.7))
    editor.high_spin.setValue(10.0)
    check("typing a number means fixed", editor.span(), (0.5, 10.0))
    editor.common.setCurrentIndex(editor.common.findText("Load 0 - 10 ms/Umdr."))
    editor._on_common(editor.common.currentIndex())
    check("a common range fills both boxes", editor.span(), (0.0, 10.0))
    editor.set_color("#0CA30C")
    check("a colour is picked", editor.color(), "#0ca30c")
    editor.low_spin.setValue(20.0)
    editor._accept()
    check("an upside-down range is not accepted",
          (editor.result(), editor.error_label.isHidden()), (0, False))
    editor.set_span(None)
    editor._accept()
    check("auto is", (editor.result(), editor.span()), (1, None))
    fixed = StyleDialog(label="N40", color="", span=(0.0, 8000.0))
    check("it opens on a fixed range when there is one", fixed.span(), (0.0, 8000.0))
    check("and on the first palette colour when there is none",
          fixed.color(), theme.SERIES[0].lower())
    fixed.close()
    editor.close()

    print("\nReplay: a log's own columns, at its own pace")
    log = parse_log(OURS)
    replayed = replay_channels(log, channels)
    check("one channel per column",
          [c.name for c in replayed], ["N40", "TMOT", "KLINFO", "made_up"])
    check("described from the database where it knows the name",
          replayed[1].about, channels.by_name["TMOT"].about)
    check("the unit is the log's", replayed[0].unit_label, "Upm")
    check("an unknown column is carried, undescribed",
          (replayed[3].unit_label, replayed[3].about), ("bar", ""))
    check("none of them costs a wire byte", [c.byte_addresses for c in replayed],
          [()] * 4)

    recorder = ReplayRecorder(log, replayed, speed=20.0)
    check("it knows how long the log is", round(recorder.duration, 2), 1.45)
    recorder.start()
    check("running", recorder.state, "running")
    deadline = time.monotonic() + 10.0
    while recorder.state != FINISHED and time.monotonic() < deadline:
        time.sleep(0.01)
    check("it finishes rather than stopping silently", recorder.state, FINISHED)
    times, data, columns = recorder.snapshot()
    check("every row was played", len(times), 30)
    check("the clock starts at zero, as a live session's does",
          round(float(times[0]), 3), 0.0)
    check("and runs at the log's own spacing", round(float(times[-1]), 2), 1.45)
    check("the values are the log's", [float(data[0, 0]), float(data[-1, 0])],
          [1000.0, 2160.0])
    check("with the channel list alongside", [c.name for c in columns][:2],
          ["N40", "TMOT"])
    recorder.stop()
    check("stopping a finished replay leaves it finished", recorder.state, FINISHED)

    slow = ReplayRecorder(log, replayed, speed=0.5)
    slow.start()
    time.sleep(0.15)
    began = time.monotonic()
    slow.stop()
    check("Disconnect is answered at once, mid-log",
          time.monotonic() - began < 1.0, True)
    check("and leaves it idle", slow.state, "idle")
    check("part played", 0 < len(slow.snapshot()[0]) < 30, True)

    check("a file that is not a log is refused in a sentence",
          "not a log" in error_of(
              lambda: ReplayRecorder.from_file(scratch / "broken.json")), True)
    check("so is a missing one", "cannot read" in error_of(
        lambda: ReplayRecorder.from_file(scratch / "gone.csv")), True)

    shutil.rmtree(scratch, ignore_errors=True)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
