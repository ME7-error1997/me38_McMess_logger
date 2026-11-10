"""A second input stream, logged onto the same clock as the ECU channels.

Why a second port instead of an ECU input
-----------------------------------------
The obvious idea is to feed a wideband controller's 0-5 V output into a spare ECU
analog input and read it back as a channel. On this ECU that does not work. The
06A906018 connector has exactly one lambda input, pin 26 (LS1, pre-cat), and no
rear-O2 pin at all -- the Europe variant leaves the post-cat sensor unfitted, so
there is no cavity to inject into. The only full-scale 0-5 V input is pin 61, the
ambient pressure sensor, which is in use: driving it would corrupt `fho`/`fdkha`,
the altitude correction, and change fuelling while you are trying to measure it.

Reading the controller directly costs nothing on the K-line, sets no DTCs, cannot
affect how the engine runs, and keeps the wideband's full resolution. The entire
~800 byte/s McMESS budget stays available for ECU channels.

How the two streams are joined
------------------------------
The ECU sampler is the clock. Every time it completes a sample it asks each source
for its most recent reading and writes that into the same row -- a zero-order hold,
no interpolation. Interpolating would invent values between two measurements of a
quantity that moves fast and matters a lot; a held value is at least a number the
sensor actually produced, and the hold interval is bounded by `max_age`.

Past `max_age` the column goes to NaN rather than holding. A wideband that has
stopped sending -- unplugged, still warming up, in free-air calibration -- would
otherwise log as a flat, plausible AFR for as long as you keep driving. An empty
cell is obviously missing data; a frozen one is not.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

import serial

#: How stale a reading may be before the column reports NaN instead of holding.
#: Widebands update at 10-100 Hz, so anything approaching a tenth of a second means
#: the stream has stopped rather than merely slowed.
DEFAULT_MAX_AGE = 0.25

#: Reopen attempts use this gap. The cable to a gauge in a moving car is a far more
#: likely thing to lose than the K-line, so the source reconnects quietly instead of
#: ending the log.
RECONNECT_DELAY = 1.0


class Decoder(Protocol):
    """Turns bytes from the device into physical values."""

    #: Unit the decoded values are in, e.g. "AFR" or "lambda".
    unit: str

    def feed(self, data: bytes) -> list[float]:
        """Consume bytes and return whatever complete readings they produced."""


@dataclass
class AsciiDecoder:
    """Line-oriented ASCII: one reading per line, extracted by a regex.

    Covers a USB ADC or microcontroller bridging an analog output, and controllers
    running in a text mode. The default pattern takes the first number on the line,
    which handles bare values ("14.72"), labelled ones ("AFR 14.72") and CSV-ish
    output ("14.72,0.98,1") alike.

    `scale` and `offset` map whatever the device prints onto the unit you want, so
    an ADC printing counts 0-1023 for 0-5 V, with the controller set to 10-20 AFR
    across that range, is scale=10/1023 offset=10.
    """

    unit: str = "AFR"
    pattern: str = r"[-+]?\d*\.?\d+"
    scale: float = 1.0
    offset: float = 0.0
    #: Values outside this are dropped as framing noise rather than logged. A
    #: half-received line is far more likely than a genuine AFR of 0.4.
    low: float = 5.0
    high: float = 30.0

    _buf: bytearray = field(default_factory=bytearray, repr=False)
    _re: re.Pattern[str] | None = field(default=None, repr=False)

    def feed(self, data: bytes) -> list[float]:
        if self._re is None:
            self._re = re.compile(self.pattern)
        self._buf.extend(data)
        # Guard against a device that never sends a newline: without this the buffer
        # grows without bound for the whole session.
        if len(self._buf) > 4096:
            del self._buf[:-512]
        out: list[float] = []
        while True:
            cut = self._buf.find(b"\n")
            if cut < 0:
                break
            line = bytes(self._buf[:cut]).decode("ascii", "replace").strip()
            del self._buf[: cut + 1]
            found = self._re.search(line)
            if not found:
                continue
            try:
                value = float(found.group()) * self.scale + self.offset
            except ValueError:
                continue
            if self.low <= value <= self.high:
                out.append(value)
        return out


@dataclass
class LinearDecoder:
    """Fixed-width binary samples, one unsigned integer each, scaled linearly.

    For a bridge that streams raw ADC samples rather than text. `width` bytes per
    sample, `big_endian` picks the order, and scale/offset map counts onto the unit.
    There is no framing, so this only suits a device that cannot desynchronise --
    use AsciiDecoder if yours can.
    """

    unit: str = "AFR"
    width: int = 2
    big_endian: bool = False
    scale: float = 1.0
    offset: float = 0.0
    low: float = 5.0
    high: float = 30.0

    _buf: bytearray = field(default_factory=bytearray, repr=False)

    def feed(self, data: bytes) -> list[float]:
        self._buf.extend(data)
        order = "big" if self.big_endian else "little"
        out: list[float] = []
        while len(self._buf) >= self.width:
            chunk = bytes(self._buf[: self.width])
            del self._buf[: self.width]
            value = int.from_bytes(chunk, order) * self.scale + self.offset
            if self.low <= value <= self.high:
                out.append(value)
        return out


#: Decoders selectable by name from the command line. Binary controller protocols
#: (Innovate ISP2, Zeitronix, AEM) are deliberately absent: their framing has not
#: been verified against a real device here, and a decoder that mis-frames produces
#: confident wrong AFR rather than an error. Capture a device with
#: 'probe.py wideband --sniff' first; the bytes make the format obvious.
DECODERS: dict[str, Callable[[], Decoder]] = {
    "ascii": AsciiDecoder,
    "linear": LinearDecoder,
}


@dataclass
class SourceStats:
    readings: int = 0
    decode_errors: int = 0
    reconnects: int = 0
    last_error: str = ""
    connected: bool = False


class WidebandSource:
    """Reads one serial device on its own thread and keeps the latest reading.

    Deliberately never blocks the ECU sampler: the sampler only ever takes a
    snapshot of the last value under a lock, and a source that is failing reports
    NaN rather than raising into the logging loop.
    """

    def __init__(
        self,
        port: str,
        decoder: Decoder | None = None,
        baudrate: int = 9600,
        name: str = "AFR",
        key: str = "wideband",
        max_age: float = DEFAULT_MAX_AGE,
        serial_factory: Callable[[], object] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.port = port
        self.decoder = decoder or AsciiDecoder()
        self.baudrate = baudrate
        self.name = name
        self.key = key
        self.max_age = max_age
        self.serial_factory = serial_factory
        self.clock = clock

        self._lock = threading.Lock()
        self._latest: tuple[float, float] | None = None
        self._stats = SourceStats()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"wideband-{self.key}", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- reading -----------------------------------------------------------

    def value(self, now: float | None = None) -> float:
        """The current reading, or NaN when there is none recent enough.

        This is what the sampler calls once per row. It must be cheap and must never
        raise: a dead wideband is a missing column, not a failed log.
        """
        now = self.clock() if now is None else now
        with self._lock:
            latest = self._latest
        if latest is None:
            return float("nan")
        when, value = latest
        if now - when > self.max_age:
            return float("nan")
        return value

    def stats(self) -> SourceStats:
        with self._lock:
            return SourceStats(**vars(self._stats))

    # -- thread ------------------------------------------------------------

    def _open(self):
        if self.serial_factory is not None:
            return self.serial_factory()
        return serial.Serial(
            port=self.port, baudrate=self.baudrate, timeout=0.05
        )

    def _run(self) -> None:
        handle = None
        while not self._stop.is_set():
            if handle is None:
                try:
                    handle = self._open()
                except Exception as exc:  # noqa: BLE001 - reported, never raised on
                    with self._lock:      # the logging thread
                        self._stats.last_error = f"{type(exc).__name__}: {exc}"
                        self._stats.connected = False
                    self._stop.wait(RECONNECT_DELAY)
                    continue
                with self._lock:
                    self._stats.connected = True
            try:
                waiting = getattr(handle, "in_waiting", 0) or 1
                data = handle.read(max(1, waiting))
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self._stats.last_error = f"{type(exc).__name__}: {exc}"
                    self._stats.connected = False
                    self._stats.reconnects += 1
                try:
                    handle.close()
                except Exception:  # noqa: BLE001
                    pass
                handle = None
                self._stop.wait(RECONNECT_DELAY)
                continue

            if not data:
                continue
            try:
                values = self.decoder.feed(data)
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self._stats.decode_errors += 1
                    self._stats.last_error = f"decode: {type(exc).__name__}: {exc}"
                continue
            if values:
                now = self.clock()
                with self._lock:
                    # Only the newest matters: the sampler holds the last value, so a
                    # burst that arrived between rows collapses to its most recent.
                    self._latest = (now, values[-1])
                    self._stats.readings += len(values)

        if handle is not None:
            try:
                handle.close()
            except Exception:  # noqa: BLE001
                pass


def add_arguments(parser) -> None:
    """The --wb-* options. Shared by probe.py and the window so they stay identical."""
    parser.add_argument("--wb-port", help="serial port of the wideband/aux device, e.g. COM5")
    parser.add_argument("--wb-baud", type=int, default=9600, help="its baud rate (default 9600)")
    parser.add_argument("--wb-name", default="AFR", help="column name (default AFR)")
    parser.add_argument("--wb-unit", default="AFR", help="unit label (default AFR)")
    parser.add_argument(
        "--wb-decoder",
        choices=sorted(DECODERS),
        default="ascii",
        help="how to read the device (default ascii: one number per line)",
    )
    parser.add_argument("--wb-scale", type=float, default=1.0, help="multiply the decoded number")
    parser.add_argument("--wb-offset", type=float, default=0.0, help="then add this")
    parser.add_argument(
        "--wb-range",
        default="5,30",
        help="low,high bound; readings outside are dropped as framing noise",
    )
    parser.add_argument(
        "--wb-max-age",
        type=float,
        default=DEFAULT_MAX_AGE,
        help="blank the column when the newest reading is older than this",
    )


def build_decoder(args) -> Decoder:
    decoder = DECODERS[args.wb_decoder]()
    decoder.unit = args.wb_unit
    decoder.scale = args.wb_scale
    decoder.offset = args.wb_offset
    decoder.low, decoder.high = (float(x) for x in args.wb_range.split(","))
    return decoder


def from_args(args) -> WidebandSource | None:
    """Build the aux source from --wb-* options, or None when no port was given."""
    port = getattr(args, "wb_port", None)
    if not port:
        return None
    return WidebandSource(
        port=port,
        decoder=build_decoder(args),
        baudrate=args.wb_baud,
        name=args.wb_name,
        max_age=args.wb_max_age,
    )


def make_channel(source: WidebandSource):
    """The AuxChannel column this source fills."""
    from .channels import AuxChannel

    return AuxChannel(
        name=source.name,
        addr=-1,
        size=0,
        umr="",
        description=f"{source.decoder.unit} from {source.port}",
        pseudo=None,
        source_key=source.key,
        aux_unit=source.decoder.unit,
        source=f"second serial port {source.port}",
    )
