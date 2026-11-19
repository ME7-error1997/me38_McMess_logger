"""Derived columns: the knock demultiplexer and the g/s air mass.

Run with:  py tests/test_derived.py
"""

from __future__ import annotations

import csv
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcmess.channels import load_channels  # noqa: E402
from mcmess.derived import (  # noqa: E402
    AirMassGramsPerSecond,
    DerivedChannel,
    KnockPerCylinder,
    apply,
    expand_selection,
)
from mcmess.recorder import Recorder  # noqa: E402
from mcmess.simulator import make_demo_serial  # noqa: E402

NAN = float("nan")
FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def isnan(x) -> bool:
    return x != x


def main() -> int:
    print("Air mass in g/s")
    d = AirMassGramsPerSecond()
    check("36 kg/h is 10 g/s", round(d.compute({"ML": 36.0})["ML_GS"], 6), 10.0)
    check("zero stays zero", d.compute({"ML": 0.0})["ML_GS"], 0.0)
    check("NaN in, NaN out", isnan(d.compute({"ML": NAN})["ML_GS"]), True)
    check("missing input is NaN", isnan(d.compute({})["ML_GS"]), True)

    print("\nKnock demux: the base is decided by proof, not by guesswork")
    k = KnockPerCylinder()
    # Values 1,2,3 are consistent with BOTH conventions, so nothing may be emitted:
    # under 1..4 they are cylinders 1,2,3; under 0..3 they are cylinders 2,3,4.
    out = {}
    for info, retard in ((1, -3.0), (2, -1.5), (3, 0.0)):
        out = k.compute({"KLINFO": float(info), "DWKR_Z": retard})
    check("ambiguous values decide nothing", all(isnan(v) for v in out.values()), True)
    check("and the note says so", "undecided" in k.note(), True)

    # A 4 cannot occur under 0..3, so it proves 1..4.
    out = k.compute({"KLINFO": 4.0, "DWKR_Z": -6.0})
    check("a 4 proves 1..4", k.note().startswith("KLINFO reads 1..4"), True)
    check("cylinder 4 filled", out["KR_CYL4"], -6.0)

    k = KnockPerCylinder()
    for info, retard in ((1, -3.0), (2, -1.5)):
        k.compute({"KLINFO": float(info), "DWKR_Z": retard})
    out = k.compute({"KLINFO": 0.0, "DWKR_Z": -9.0})
    check("a 0 proves 0..3", k.note().startswith("KLINFO reads 0..3"), True)
    check("KLINFO 0 is cylinder 1", out["KR_CYL1"], -9.0)

    print("\nKnock demux: holding between a cylinder's turns")
    k = KnockPerCylinder(base=1)
    k.compute({"KLINFO": 1.0, "DWKR_Z": -4.5})
    k.compute({"KLINFO": 2.0, "DWKR_Z": -1.0})
    out = k.compute({"KLINFO": 3.0, "DWKR_Z": 0.0})
    check("each cylinder keeps its own value",
          [out["KR_CYL1"], out["KR_CYL2"], out["KR_CYL3"]], [-4.5, -1.0, 0.0])
    check("one not yet seen stays NaN", isnan(out["KR_CYL4"]), True)
    out = k.compute({"KLINFO": 1.0, "DWKR_Z": -6.0})
    check("revisiting updates only that cylinder",
          [out["KR_CYL1"], out["KR_CYL2"]], [-6.0, -1.0])

    print("\nKnock demux: bad input does not corrupt a column")
    k = KnockPerCylinder(base=1)
    k.compute({"KLINFO": 2.0, "DWKR_Z": -2.0})
    out = k.compute({"KLINFO": 9.0, "DWKR_Z": -99.0})
    check("out-of-range KLINFO is dropped", out["KR_CYL2"], -2.0)
    check("and counted in the note", "outside that range" in k.note(), True)
    out = k.compute({"KLINFO": 2.0, "DWKR_Z": NAN})
    check("NaN retard does not overwrite the hold", out["KR_CYL2"], -2.0)
    out = k.compute({"KLINFO": NAN, "DWKR_Z": -7.0})
    check("NaN KLINFO changes nothing", out["KR_CYL2"], -2.0)

    print("\nKLINFO carries a knock flag in bit 7")
    # Caught on a real pull: KLINFO read 0x82, bit 7 over cylinder 2. The FR calls
    # the variable "Zylindernummer, klopfen ja/nein", so the byte holds both. Before
    # this, such a sample looked out-of-range and was discarded -- the one sample you
    # least want to lose.
    k = KnockPerCylinder(base=0)
    for info, retard in ((0, -1.0), (1, -2.0), (2, -3.0), (3, -4.0)):
        k.compute({"KLINFO": float(info), "DWKR_Z": retard})
    out = k.compute({"KLINFO": float(0x82), "DWKR_Z": -2.25})
    check("flagged sample is still routed", out["KR_CYL3"], -2.25)
    check("and its cylinder is flagged", out["KNK_CYL3"], 1.0)
    check("others are not", [out["KNK_CYL1"], out["KNK_CYL2"], out["KNK_CYL4"]],
          [0.0, 0.0, 0.0])
    check("the note counts it", "knock flag caught on cyl3x1" in k.note(), True)
    out = k.compute({"KLINFO": 2.0, "DWKR_Z": -2.25})
    check("flag clears on a clean event", out["KNK_CYL3"], 0.0)

    # Only bit 7 is stripped. Masking to three bits would turn a bogus 9 into
    # cylinder 1 and file a reading under the wrong cylinder.
    k = KnockPerCylinder(base=1)
    k.compute({"KLINFO": 2.0, "DWKR_Z": -2.0})
    out = k.compute({"KLINFO": 9.0, "DWKR_Z": -99.0})
    check("a bogus value is still rejected, not masked into range",
          out["KR_CYL2"], -2.0)

    print("\nreset() forgets the base and the held values")
    k.reset()
    out = k.compute({"KLINFO": 2.0, "DWKR_Z": -2.0})
    check("hold cleared", isnan(out["KR_CYL1"]), True)
    k = KnockPerCylinder()
    k.compute({"KLINFO": 4.0, "DWKR_Z": -1.0})
    k.reset()
    check("auto base re-armed", "undecided" in k.note(), True)

    print("\nSelection expansion pulls in what a column needs")
    cs = load_channels("06A906018CG")
    channels, derivations = expand_selection(cs, ["N40", "ML_GS"])
    names = [c.name for c in channels]
    check("ML added automatically", names, ["N40", "ML", "ML_GS"])
    check("one derivation", len(derivations), 1)
    check("derived column is a DerivedChannel",
          isinstance(channels[-1], DerivedChannel), True)
    check("costs no ECU bytes", channels[-1].byte_addresses, ())
    check("is flagged derived and virtual",
          (channels[-1].is_derived, channels[-1].is_virtual, channels[-1].is_aux),
          (True, True, False))
    check("carries its unit", channels[-1].unit, "g/s")

    channels, derivations = expand_selection(cs, ["KR_CYL1", "KR_CYL3"])
    check("knock inputs added once",
          [c.name for c in channels], ["KLINFO", "DWKR_Z", "KR_CYL1", "KR_CYL3"])
    check("one derivation for both columns", len(derivations), 1)

    channels, _ = expand_selection(cs, ["N40", "ML", "ML_GS"])
    check("an input already asked for is not duplicated",
          [c.name for c in channels], ["N40", "ML", "ML_GS"])

    try:
        channels[-1].assemble({0: 1})
        check("refuses to be read from memory", "no error", "AssertionError")
    except AssertionError:
        check("refuses to be read from memory", True, True)

    print("\napply() runs every derivation over one sample")
    _, derivations = expand_selection(cs, ["ML_GS", "KR_CYL1"], knock_base=1)
    out = apply(derivations, {"ML": 18.0, "KLINFO": 1.0, "DWKR_Z": -2.0})
    check("both derivations contributed",
          (round(out["ML_GS"], 3), out["KR_CYL1"]), (5.0, -2.0))

    print("\nEnd to end through the recorder")
    cs = load_channels("06A906018CG")
    channels, derivations = expand_selection(cs, ["N40", "ML", "ML_GS"])
    out = Path(__file__).resolve().parent / "_derived_tmp.csv"
    rec = Recorder(
        port="SIM",
        channels=channels,
        serial_factory=lambda: make_demo_serial(cs, rtt_s=0),
        channel_set=cs,
        derivations=derivations,
    )
    rec.start()
    for _ in range(300):
        if rec.state in ("running", "error"):
            break
        time.sleep(0.01)
    check("recorder runs with a derived column", rec.state, "running")
    rec.start_recording(out)
    time.sleep(0.4)
    rec.stop_recording()
    time.sleep(0.1)
    rec.stop()

    rows = [r for r in csv.reader(out.read_text(encoding="utf-8").splitlines())
            if r and not r[0].startswith("#")]
    header, body = rows[0], rows[1:]
    check("header carries the derived column", header, ["t_s", "N40 [Upm]", "ML [kg/h]", "ML_GS [g/s]"])
    check("rows were written", len(body) > 5, True)
    last = body[-1]
    ml, ml_gs = float(last[2]), float(last[3])
    check("g/s really is kg/h over 3.6", round(ml_gs, 4), round(ml / 3.6, 4))
    out.unlink(missing_ok=True)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
