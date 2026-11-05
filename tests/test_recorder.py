"""Checks for the sampling engine and the engine model. No hardware needed.

    py tests/test_recorder.py

This exercises the whole stack below the GUI -- transport, session, scheduler,
conversions, ring buffer, CSV -- against mcmess.simulator. Anything the window
displays has already come through this path, so if these pass the GUI is only
responsible for drawing.
"""

from __future__ import annotations

import csv
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from mcmess.channels import load_channels  # noqa: E402
from mcmess.recorder import Recorder, RingBuffer, selection_cost  # noqa: E402
from mcmess.simulator import (  # noqa: E402
    EngineModel,
    invert_conversion,
    make_demo_serial,
)

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}\n         got  {got!r}\n         want {want!r}")
        FAILURES.append(label)


def check_between(label: str, value, low, high) -> None:
    if value is not None and low <= value <= high:
        print(f"  ok   {label}  ({value:.2f} in {low}..{high})")
    else:
        print(f"  FAIL {label}\n         got  {value!r}\n         want {low}..{high}")
        FAILURES.append(label)


def wait_for(predicate, timeout: float = 10.0, interval: float = 0.02) -> bool:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def main() -> int:
    channels = load_channels()

    print("RingBuffer")
    ring = RingBuffer(columns=2, capacity=5)
    check("starts empty", len(ring), 0)
    for i in range(3):
        ring.append(float(i), [i * 10.0, i * 100.0])
    times, data = ring.snapshot()
    check("holds three samples", len(times), 3)
    check("values in order", list(data[:, 0]), [0.0, 10.0, 20.0])
    for i in range(3, 8):
        ring.append(float(i), [i * 10.0, i * 100.0])
    times, data = ring.snapshot()
    check("caps at capacity", len(times), 5)
    check("keeps the newest", list(times), [3.0, 4.0, 5.0, 6.0, 7.0])
    check("wrapped data stays aligned", list(data[:, 0]), [30.0, 40.0, 50.0, 60.0, 70.0])
    times, _ = ring.snapshot(seconds=2.0)
    check("windowing by seconds", list(times), [5.0, 6.0, 7.0])
    times, _ = ring.snapshot(max_points=2)
    check("decimation caps points", len(times) <= 2, True)
    ring.clear()
    check("clear empties it", len(ring), 0)

    print("\nConversion inverse (used only by the simulator)")
    tempq = channels.conversions["TEMPQ"]
    for physical in (-48.0, 0.0, 48.0, 90.0, 143.25):
        raw = invert_conversion(tempq, physical)
        check(f"TEMPQ round trip at {physical}", round(tempq.apply(raw), 2), physical)
    nq40 = channels.conversions["NQ40"]
    check("NQ40 inverse of 2000 rpm", invert_conversion(nq40, 2000.0), 50)
    fak2l = channels.conversions["FAK2L"]
    check("FAK2L inverse of 1.00", invert_conversion(fak2l, 1.0), 0x8000)
    zwvek = channels.conversions["ZWVEK"]
    check("ZWVEK inverse of 0 deg", invert_conversion(zwvek, 0.0), 96)
    text = channels.conversions["A_BIT"]
    check("text conversions are not invertible", invert_conversion(text, 1.0), None)

    print("\nSelection cost uses the real scheduler")
    page0 = channels.select(["N40", "TL", "TMOT", "TANS", "UB"])
    cost = selection_cost(page0)
    check("page-0 set: one wire byte each", cost.wire_bytes, 5)
    check("page-0 set: no parity switches", cost.parity_switches, 0)
    check("page-0 set: one page", cost.pages, 1)
    mixed = channels.select(["N40", "FR", "FR2+1"])
    cost_mixed = selection_cost(mixed)
    check("mixed set spans two pages", cost_mixed.pages, 2)
    check_between("estimated Hz at 300 B/s", cost_mixed.estimated_hz(300.0), 30.0, 70.0)
    check("empty selection costs nothing", selection_cost([]).wire_bytes, 0)

    print("\nEngine model writes through the real conversions")
    fake = make_demo_serial(channels, rtt_s=0.0)
    model = fake.model
    check("model attached", isinstance(model, EngineModel), True)
    check("no un-invertible writes", model.write_failures, [])
    state = model.state(10.0)
    check_between("drive cycle rpm at t=10s", state["N40"], 900.0, 6600.0)
    # The cycle is 40 s long, so check it over its own timescale rather than over the
    # few milliseconds a test actually records for.
    sweep = [model.state(t)["N40"] for t in (0, 8, 14, 19, 24, 30, 36)]
    check("drive cycle sweeps rpm", max(sweep) - min(sweep) > 3000.0, True)
    throttle = [model.state(t)["DKPOT"] for t in (0, 14, 19, 30)]
    check("and throttle with it", max(throttle) - min(throttle) > 50.0, True)
    print("  ..   rpm sweep: " + ", ".join(f"{v:.0f}" for v in sweep))
    # Read back through the channel objects, i.e. the same path the GUI uses.
    for name, low, high in (
        ("N40", 700.0, 6700.0),
        ("TMOT", 40.0, 95.0),
        ("TANS", 20.0, 45.0),
        ("UB", 13.5, 14.5),
        ("DKPOT", 0.0, 90.0),
        ("FR", 0.9, 1.1),
    ):
        channel = channels.get(name)
        raw = channel.assemble({a: fake.memory.get(a, 0) for a in channel.byte_addresses})
        check_between(f"{name} reads back plausibly", channel.physical(raw), low, high)

    print("\nRecorder against the simulated ECU")
    selection = channels.select(
        ["N40", "TL", "DKPOT", "ZWOUT", "TMOT", "TANS", "UB", "FR", "STATVS"]
    )
    recorder = Recorder(
        port="SIM",
        channels=selection,
        highspeed=True,
        capacity=2000,
        serial_factory=lambda: make_demo_serial(channels, rtt_s=0.0),
    )
    states: list[str] = []
    recorder.on_state_change = states.append
    recorder.start()
    try:
        check("reaches running", wait_for(lambda: recorder.state == "running", 5.0), True)
        check("collects samples", wait_for(lambda: recorder.stats().samples > 200, 10.0), True)

        times, data, cols = recorder.snapshot()
        check("snapshot column count matches channels", data.shape[1], len(selection))
        check("snapshot returns its own channel list", [c.name for c in cols],
              [c.name for c in selection])
        check("time increases", bool(np.all(np.diff(times) >= 0)), True)
        check("no NaNs in a healthy link", int(np.isnan(data).sum()), 0)

        index = {c.name: i for i, c in enumerate(cols)}
        for name, low, high in (
            ("N40", 700.0, 6700.0),
            ("TMOT", 40.0, 95.0),
            ("UB", 13.5, 14.5),
            ("FR", 0.9, 1.1),
            ("ZWOUT", 0.0, 20.0),
        ):
            column = data[:, index[name]]
            check_between(f"{name} min", float(column.min()), low, high)
            check_between(f"{name} max", float(column.max()), low, high)

        stats = recorder.stats()
        check("wire bytes per sample recorded", stats.wire_bytes_per_sample > 0, True)
        check("no timeouts against the simulator", stats.timeouts, 0)
        check("no stray bytes", stats.stray_bytes, 0)
        check("no errors", stats.last_error, "")
        print(f"  ..   {stats.sample_hz:.0f} Hz, {stats.wire_bytes_per_sample} wire "
              f"bytes/sample, rtt {stats.rtt_ms:.3f} ms (simulated, not a real cable)")

        raws = recorder.last_raw()
        check("raw values exposed per channel", len(raws), len(selection))

        print("\nCSV recording")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.csv"
            recorder.start_recording(path)
            check("rows get written", wait_for(
                lambda: recorder.stats().recording_rows > 50, 10.0), True)
            recorder.stop_recording()
            check("path cleared on stop", wait_for(
                lambda: recorder.stats().recording_path == "", 5.0), True)
            time.sleep(0.1)

            lines = path.read_text(encoding="utf-8").splitlines()
            preamble = [ln for ln in lines if ln.startswith("#")]
            check("has a self-documenting preamble", len(preamble) >= 4, True)
            check("preamble records the provenance caveat",
                  any("cross-image" in ln for ln in preamble), True)

            rows = list(csv.reader(ln for ln in lines if not ln.startswith("#")))
            header, body = rows[0], rows[1:]
            check("header has t plus one column per channel",
                  len(header), len(selection) + 1)
            check("units in the header", header[1], "N40 [Upm]")
            check("raw channels marked as raw", header[index["STATVS"] + 1], "STATVS [dez]")
            check("body has rows", len(body) > 50, True)
            rpm_col = [float(r[index["N40"] + 1]) for r in body if r[index["N40"] + 1]]
            check_between("logged rpm is plausible", sum(rpm_col) / len(rpm_col), 700.0, 6700.0)

        print("\nChanging the selection mid-run")
        recorder.set_channels(channels.select(["N40", "UB"]))
        check("buffer reallocates", wait_for(
            lambda: recorder.snapshot()[1].shape[1] == 2, 5.0), True)
        check("new samples arrive", wait_for(
            lambda: len(recorder.snapshot()[0]) > 20, 5.0), True)
        _, data2, cols2 = recorder.snapshot()
        check("channel list follows", [c.name for c in cols2], ["N40", "UB"])
        check("no stale columns", data2.shape[1], 2)
    finally:
        recorder.stop()

    check("stops cleanly", recorder.state, "idle")
    check("thread is gone", recorder.running, False)
    check("saw connecting then running", states[:2], ["connecting", "running"])

    print("\nAn unexpected fault surfaces as an error, it does not kill the thread quietly")
    # A logger that has silently stopped recording is worse than one that says so,
    # so the loop must turn any unexpected exception into the error state.
    class Exploding:
        def __init__(self, inner):
            self.inner = inner
            self.reads = 0

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def __setattr__(self, name, value):
            if name in ("inner", "reads"):
                object.__setattr__(self, name, value)
            else:
                setattr(self.inner, name, value)

        def read(self, size=1):
            self.reads += 1
            if self.reads > 400:
                raise RuntimeError("simulated cable fault")
            return self.inner.read(size)

    boom = Recorder(
        port="SIM",
        channels=channels.select(["N40", "TL"]),
        serial_factory=lambda: Exploding(make_demo_serial(channels, rtt_s=0.0)),
    )
    boom.start()
    check("goes to error", wait_for(lambda: boom.state == "error", 15.0), True)
    check("names the exception", "simulated cable fault" in boom.stats().last_error, True)
    check("keeps a traceback", "RuntimeError" in boom._last_traceback, True)
    boom.stop()
    print(f"  ..   {boom.stats().last_error}")

    print("\nRecorder reports a failed handshake instead of hanging")
    dead = Recorder(
        port="SIM",
        channels=channels.select(["N40"]),
        serial_factory=lambda: make_demo_serial(
            channels, rtt_s=0.0, answer_handshake=False
        ),
    )
    dead.start()
    check("goes to error", wait_for(lambda: dead.state == "error", 15.0), True)
    check("records why", bool(dead.stats().last_error), True)
    dead.stop()
    print(f"  ..   {dead.stats().last_error[:70]}...")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
