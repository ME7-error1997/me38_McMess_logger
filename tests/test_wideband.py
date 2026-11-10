"""Second-input tests: decoding, staleness, and the join onto the ECU clock.

Run with:  py tests/test_wideband.py
"""

from __future__ import annotations

import csv
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcmess.channels import AuxChannel, load_channels  # noqa: E402
from mcmess.recorder import Recorder  # noqa: E402
from mcmess.simulator import make_demo_serial  # noqa: E402
from mcmess.wideband import (  # noqa: E402
    AsciiDecoder,
    LinearDecoder,
    WidebandSource,
    make_channel,
)

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


class FakeDevice:
    """A serial device that hands out a scripted byte stream, then goes quiet."""

    def __init__(self, chunks: list[bytes], fail_after: int | None = None) -> None:
        self.chunks = list(chunks)
        self.fail_after = fail_after
        self.reads = 0
        self.closed = False

    @property
    def in_waiting(self) -> int:
        return len(self.chunks[0]) if self.chunks else 0

    def read(self, size: int = 1) -> bytes:
        self.reads += 1
        if self.fail_after is not None and self.reads > self.fail_after:
            raise OSError("device went away")
        if not self.chunks:
            time.sleep(0.005)
            return b""
        return self.chunks.pop(0)

    def close(self) -> None:
        self.closed = True


def main() -> int:
    print("ASCII decoding")
    d = AsciiDecoder()
    check("bare numbers", d.feed(b"14.70\n12.50\n"), [14.70, 12.50])
    check("labelled lines", d.feed(b"AFR 13.25\n"), [13.25])
    check("takes the first field of a CSV line", d.feed(b"11.90,0.81,1\n"), [11.90])
    check("ignores a line with no number", d.feed(b"booting...\n"), [])

    # A serial read lands wherever it lands; a reading split across two reads must
    # still decode exactly once, and must not emit a truncated value first.
    d = AsciiDecoder()
    check("no reading from a partial line", d.feed(b"14."), [])
    check("completed across chunks", d.feed(b"85\n"), [14.85])

    d = AsciiDecoder(low=10.0, high=20.0)
    check("drops out-of-range noise", d.feed(b"14.5\n999.0\n0.3\n15.5\n"), [14.5, 15.5])

    d = AsciiDecoder(scale=10.0 / 1023.0, offset=10.0, low=9.0, high=21.0)
    check("maps ADC counts onto AFR", [round(v, 2) for v in d.feed(b"0\n1023\n512\n")],
          [10.0, 20.0, 15.0])

    # A device that never sends a newline must not grow the buffer forever.
    d = AsciiDecoder()
    for _ in range(50):
        d.feed(b"x" * 200)
    check("unterminated stream is bounded", len(d._buf) <= 4096, True)

    print("\nBinary decoding")
    d = LinearDecoder(width=2, scale=0.001, offset=10.0, low=9.0, high=21.0)
    check("little-endian pairs", [round(v, 3) for v in d.feed(b"\xe8\x03\xd0\x07")],
          [11.0, 12.0])
    check("holds an odd trailing byte", d.feed(b"\x10"), [])
    check("completes it on the next chunk", [round(v, 3) for v in d.feed(b"\x27")],
          [20.0])

    print("\nThe source thread")
    clock = [0.0]
    device = FakeDevice([b"14.70\n"])
    src = WidebandSource(
        port="FAKE", decoder=AsciiDecoder(), serial_factory=lambda: device,
        clock=lambda: clock[0], max_age=0.25,
    )
    src.start()
    for _ in range(200):
        if src.stats().readings:
            break
        time.sleep(0.005)
    check("reading arrives", src.stats().readings, 1)
    check("value is current at the same instant", src.value(), 14.70)
    check("still current just inside max_age", src.value(now=0.2), 14.70)

    # The important one. A wideband that stops sending must not keep reporting its
    # last number: a flat, plausible AFR down the rest of a log is worse than a gap,
    # because nothing about it looks wrong.
    stale = src.value(now=0.5)
    check("goes NaN once stale", stale != stale, True)
    src.stop()
    check("port closed on stop", device.closed, True)

    print("\nSurviving a disconnect")
    attempts = []

    def flaky():
        attempts.append(len(attempts))
        if len(attempts) == 1:
            return FakeDevice([b"15.0\n"], fail_after=2)
        return FakeDevice([b"16.0\n"])

    src = WidebandSource(port="FAKE", serial_factory=flaky, max_age=10.0)
    src.start()
    for _ in range(400):
        if src.stats().reconnects:
            break
        time.sleep(0.005)
    check("reopens after the cable drops", src.stats().reconnects >= 1, True)
    src.stop()

    print("\nA source that never opens")
    src = WidebandSource(
        port="NOPE", serial_factory=lambda: (_ for _ in ()).throw(OSError("no such port"))
    )
    src.start()
    time.sleep(0.05)
    value = src.value()
    check("reports NaN rather than raising", value != value, True)
    check("records why", "no such port" in src.stats().last_error, True)
    src.stop()

    print("\nThe aux channel costs nothing on the K-line")
    device = FakeDevice([b"14.70\n"])
    src = WidebandSource(port="FAKE", serial_factory=lambda: device, max_age=10.0)
    column = make_channel(src)
    check("is an AuxChannel", isinstance(column, AuxChannel), True)
    check("needs no ECU bytes", column.byte_addresses, ())
    check("knows its unit", column.unit, "AFR")
    check("is flagged aux", column.is_aux, True)
    try:
        column.assemble({0: 1})
        check("refuses to be read from memory", "no error", "AssertionError")
    except AssertionError:
        check("refuses to be read from memory", True, True)

    print("\nJoined onto the ECU clock, end to end")
    channel_set = load_channels("06A906018CG")
    ecu = [channel_set.get(n) for n in ("N40", "TL", "TMOT")]
    device = FakeDevice([b"14.70\n"] * 200)
    src = WidebandSource(port="FAKE", serial_factory=lambda: device, max_age=10.0)
    out = Path(__file__).resolve().parent / "_wideband_tmp.csv"
    rec = Recorder(
        port="SIM",
        channels=ecu + [make_channel(src)],
        serial_factory=lambda: make_demo_serial(channel_set, rtt_s=0),
        channel_set=channel_set,
        aux_sources=[src],
    )
    rec.start()
    for _ in range(300):
        if rec.state in ("running", "error"):
            break
        time.sleep(0.01)
    check("recorder runs with an aux column", rec.state, "running")
    rec.start_recording(out)
    time.sleep(0.4)
    rec.stop_recording()
    time.sleep(0.1)
    rec.stop()

    rows = [r for r in csv.reader(out.read_text(encoding="utf-8").splitlines())
            if r and not r[0].startswith("#")]
    header, body = rows[0], rows[1:]
    check("aux column is in the header", header[-1], "AFR [AFR]")
    check("ECU columns come first", header[1:4], ["N40 [Upm]", "TL [ms/Umdr.]", "TMOT [grad C]"])
    check("rows were written", len(body) > 5, True)
    check("aux value is logged", body[-1][-1], "14.7")
    check(
        "preamble says the column is not from the ECU",
        any("is NOT from the ECU" in line for line in out.read_text(encoding="utf-8").splitlines()),
        True,
    )
    out.unlink(missing_ok=True)

    print("\nAn aux-only selection is refused")
    device = FakeDevice([b"14.70\n"])
    src2 = WidebandSource(port="FAKE", serial_factory=lambda: device, max_age=10.0)
    rec = Recorder(
        port="SIM",
        channels=[make_channel(src2)],
        serial_factory=lambda: make_demo_serial(channel_set, rtt_s=0),
        channel_set=channel_set,
        aux_sources=[src2],
    )
    rec.start()
    for _ in range(200):
        if rec.state == "error":
            break
        time.sleep(0.01)
    check("refuses to clock on nothing", rec.state, "error")
    check("says why", "at least one ECU channel" in rec.stats().last_error, True)
    rec.stop()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
