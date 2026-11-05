"""The sampling engine: a background thread that polls the ECU and buffers samples.

Deliberately free of any GUI dependency. The serial path is strictly lock-step and
blocking, so it must never run on a UI thread -- and the ECU drops the session after
2 s of silence, which a blocked UI thread could easily cause. So the loop lives here,
in its own thread, and a consumer (the Qt window, a script, a test) reads a snapshot
whenever it likes.

    recorder = Recorder("COM3", channels)
    recorder.start()
    ...
    t, values = recorder.snapshot(seconds=10)   # numpy arrays, newest last
    recorder.stop()

Keeping this Qt-free is what lets the whole stack be exercised against
mcmess.simulator with no hardware.
"""

from __future__ import annotations

import csv
import threading
import time
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .channels import Channel, ChannelSet, csv_number
from .errors import McMessError, PortError, TimeoutError_
from .session import ConnectResult, McMessSession, ReadProgram, build_program
from .transport import KLineTransport

#: Samples kept in memory. 36000 is 20 minutes at 30 Hz; at 20 channels that is about
#: 6 MB, which is nothing, and it means a whole pull is always still on screen.
DEFAULT_CAPACITY = 36_000

#: Consecutive read timeouts before the session is considered lost and re-established.
TIMEOUTS_BEFORE_RECONNECT = 5

#: Consecutive failed reconnects before giving up and going to the error state.
MAX_RECONNECT_ATTEMPTS = 3


# ------------------------------------------------------------------- ring buffer


class RingBuffer:
    """Fixed-capacity numpy ring buffer: one time column plus one column per channel.

    Stores float64 so a missing byte can be NaN rather than a silently plausible zero.
    """

    def __init__(self, columns: int, capacity: int = DEFAULT_CAPACITY) -> None:
        self.capacity = capacity
        self.columns = columns
        self._time = np.zeros(capacity, dtype=np.float64)
        self._data = np.full((capacity, columns), np.nan, dtype=np.float64)
        self._write = 0
        self._count = 0

    def __len__(self) -> int:
        return self._count

    def clear(self) -> None:
        self._write = 0
        self._count = 0
        self._data.fill(np.nan)

    def append(self, timestamp: float, values: Sequence[float]) -> None:
        index = self._write
        self._time[index] = timestamp
        self._data[index, :] = values
        self._write = (index + 1) % self.capacity
        self._count = min(self._count + 1, self.capacity)

    def snapshot(
        self, seconds: float | None = None, max_points: int | None = None
    ) -> tuple[np.ndarray, np.ndarray]:
        """Return (time, data) in chronological order, newest last.

        seconds limits the window to the most recent span; max_points decimates by
        striding, which keeps plotting cheap when a long window is on screen.
        """
        if self._count == 0:
            return np.empty(0), np.empty((0, self.columns))

        if self._count < self.capacity:
            times = self._time[: self._count]
            data = self._data[: self._count]
        else:
            order = np.r_[self._write : self.capacity, 0 : self._write]
            times = self._time[order]
            data = self._data[order]

        if seconds is not None and len(times):
            cutoff = times[-1] - seconds
            start = int(np.searchsorted(times, cutoff, side="left"))
            times, data = times[start:], data[start:]

        if max_points and len(times) > max_points:
            stride = int(np.ceil(len(times) / max_points))
            times, data = times[::stride], data[::stride]

        return times, data


# ------------------------------------------------------------------------ stats


@dataclass
class RecorderStats:
    """What the status bar needs to show about link health."""

    samples: int = 0
    sample_hz: float = 0.0
    bytes_per_second: float = 0.0
    rtt_ms: float = 0.0
    timeouts: int = 0
    stray_bytes: int = 0
    echo_mismatch: int = 0
    reconnects: int = 0
    parity_switches: int = 0
    wire_bytes_per_sample: int = 0
    baudrate: int = 0
    highspeed: bool = False
    recording_rows: int = 0
    recording_path: str = ""
    last_error: str = ""

    def copy(self) -> "RecorderStats":
        return RecorderStats(**self.__dict__)


# --------------------------------------------------------------------- recorder


class Recorder:
    """Polls a channel set in a background thread and buffers the results."""

    #: Lifecycle: idle -> connecting -> running -> (stopping) -> idle, or -> error.
    STATES = ("idle", "connecting", "running", "stopping", "error")

    def __init__(
        self,
        port: str,
        channels: Sequence[Channel],
        highspeed: bool = True,
        capacity: int = DEFAULT_CAPACITY,
        serial_factory: Callable[[], object] | None = None,
        pulse_ms: float = 85.0,
        echo: bool | None = None,
        on_state_change: Callable[[str], None] | None = None,
    ) -> None:
        self.port = port
        self.highspeed = highspeed
        self.capacity = capacity
        self.serial_factory = serial_factory
        self.pulse_ms = pulse_ms
        self.echo = echo
        self.on_state_change = on_state_change

        self._lock = threading.Lock()
        self._channels: list[Channel] = list(channels)
        self._pending_channels: list[Channel] | None = None
        self._buffer = RingBuffer(len(self._channels), capacity)
        self._stats = RecorderStats()
        self._state = "idle"
        self._last_raw: dict[str, int | None] = {}
        self._connect_result: ConnectResult | None = None

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        #: Full traceback of an unexpected failure, for diagnosis. stats().last_error
        #: carries the one-line version the UI shows.
        self._last_traceback = ""
        self._csv_request: Path | None = None
        self._csv_stop = False

    # -- introspection -----------------------------------------------------

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def channels(self) -> list[Channel]:
        with self._lock:
            return list(self._channels)

    @property
    def connect_result(self) -> ConnectResult | None:
        with self._lock:
            return self._connect_result

    def stats(self) -> RecorderStats:
        with self._lock:
            return self._stats.copy()

    def last_raw(self) -> dict[str, int | None]:
        with self._lock:
            return dict(self._last_raw)

    def snapshot(
        self, seconds: float | None = None, max_points: int | None = None
    ) -> tuple[np.ndarray, np.ndarray, list[Channel]]:
        """Buffered samples plus the channel list they correspond to.

        The channel list is returned alongside deliberately: changing the selection
        reallocates the buffer, and a consumer that cached an older list would
        otherwise plot the wrong column against the wrong name.
        """
        with self._lock:
            times, data = self._buffer.snapshot(seconds, max_points)
            return times, data, list(self._channels)

    def _set_state(self, state: str) -> None:
        with self._lock:
            if self._state == state:
                return
            self._state = state
        if self.on_state_change:
            self.on_state_change(state)

    # -- control -----------------------------------------------------------

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="mcmess-recorder", daemon=True)
        self._set_state("connecting")
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            self._set_state("stopping")
            thread.join(timeout)
        self._thread = None
        if self.state != "error":
            self._set_state("idle")

    def set_channels(self, channels: Sequence[Channel]) -> None:
        """Change the logged set. Takes effect at the top of the next sample.

        The buffer is reallocated for the new column count, so history is dropped --
        there is no honest way to keep it when the columns change.
        """
        with self._lock:
            self._pending_channels = list(channels)
        if not self.running:
            self._apply_channels()

    def _apply_channels(self) -> ReadProgram | None:
        with self._lock:
            if self._pending_channels is not None:
                self._channels = self._pending_channels
                self._pending_channels = None
                self._buffer = RingBuffer(len(self._channels), self.capacity)
                self._last_raw = {}
            channels = list(self._channels)
        if not channels:
            return None
        program = build_program(ChannelSet.addresses_for(channels))
        with self._lock:
            self._stats.wire_bytes_per_sample = program.wire_bytes
            self._stats.parity_switches = program.parity_switches
        return program

    def start_recording(self, path: str | Path) -> None:
        """Begin writing samples to CSV. Opened by the sampling thread."""
        self._csv_request = Path(path)

    def stop_recording(self) -> None:
        self._csv_stop = True

    # -- the loop ----------------------------------------------------------

    def _run(self) -> None:
        transport = KLineTransport(
            port=self.port,
            echo=self.echo,
            serial_factory=self.serial_factory,
        )
        writer = None
        handle = None
        try:
            transport.open()
        except PortError as exc:
            self._fail(f"cannot open {self.port}: {exc}")
            return

        session = McMessSession(transport)
        try:
            if not self._connect(session):
                return

            program = self._apply_channels()
            started = time.perf_counter()
            timeouts_in_a_row = 0
            reconnects = 0
            window: list[float] = []

            while not self._stop.is_set():
                if self._pending_channels is not None:
                    program = self._apply_channels()
                    started = time.perf_counter()
                    window.clear()

                handle, writer = self._service_csv(handle, writer)

                if program is None:
                    time.sleep(0.05)
                    continue

                loop_start = time.perf_counter()
                try:
                    values = session.run_program(program)
                    timeouts_in_a_row = 0
                    reconnects = 0
                except TimeoutError_:
                    timeouts_in_a_row += 1
                    if timeouts_in_a_row < TIMEOUTS_BEFORE_RECONNECT:
                        continue
                    reconnects += 1
                    if reconnects > MAX_RECONNECT_ATTEMPTS:
                        self._fail(
                            f"link lost: {timeouts_in_a_row} consecutive timeouts and "
                            f"{MAX_RECONNECT_ATTEMPTS} failed reconnects"
                        )
                        return
                    with self._lock:
                        self._stats.reconnects += 1
                    self._set_state("connecting")
                    if not self._connect(session):
                        return
                    timeouts_in_a_row = 0
                    continue
                except McMessError as exc:
                    self._fail(str(exc))
                    return

                now = time.perf_counter()
                self._record(now - started, values, transport, program, window, now)
                if writer is not None:
                    self._write_row(writer, now - started)
        except Exception as exc:  # noqa: BLE001
            # Anything unexpected must surface as an error state. Without this the
            # thread dies silently and the UI goes on showing "running" -- a logger
            # that has quietly stopped recording is worse than one that says so.
            # Record the diagnostic BEFORE publishing the state. _fail() is what
            # observers watch, so filling the traceback in afterwards leaves a
            # window where the UI sees 'error' and an empty traceback. The state
            # change must be the last thing that happens here.
            self._last_traceback = traceback.format_exc()
            self._fail(f"{type(exc).__name__}: {exc}")
        finally:
            if handle is not None:
                handle.close()
            transport.close()
            with self._lock:
                self._stats.recording_path = ""

    def _connect(self, session: McMessSession) -> bool:
        try:
            result = session.connect(
                highspeed=self.highspeed, pulse=self.pulse_ms / 1000.0
            )
        except McMessError as exc:
            self._fail(str(exc))
            return False
        with self._lock:
            self._connect_result = result
            self._stats.baudrate = result.baudrate
            self._stats.highspeed = session.highspeed
        if not result.ok:
            self._fail(result.note or "handshake failed")
            return False
        self._set_state("running")
        return True

    def _fail(self, message: str) -> None:
        with self._lock:
            self._stats.last_error = message
        self._set_state("error")

    def _record(
        self,
        timestamp: float,
        values: dict[int, int],
        transport: KLineTransport,
        program: ReadProgram,
        window: list[float],
        now: float,
    ) -> None:
        channels = self.channels
        row: list[float] = []
        raws: dict[str, int | None] = {}
        for channel in channels:
            raw = channel.assemble(values)
            raws[channel.name] = raw
            if raw is None:
                row.append(float("nan"))
                continue
            value = channel.physical(raw)
            row.append(float(value) if value is not None else float(raw))

        window.append(now)
        if len(window) > 64:
            del window[:-64]
        span = window[-1] - window[0] if len(window) > 1 else 0.0
        hz = (len(window) - 1) / span if span > 0 else 0.0

        stats = transport.stats
        recent = stats.rtt[-200:]
        with self._lock:
            self._buffer.append(timestamp, row)
            self._last_raw = raws
            self._stats.samples += 1
            self._stats.sample_hz = hz
            self._stats.bytes_per_second = hz * program.wire_bytes
            self._stats.rtt_ms = (sum(recent) / len(recent) * 1000.0) if recent else 0.0
            self._stats.timeouts = stats.timeouts
            self._stats.stray_bytes = stats.stray_bytes
            self._stats.echo_mismatch = stats.echo_mismatch
            self._stats.baudrate = transport.baudrate
            self._last_row = row

    # -- CSV ---------------------------------------------------------------

    def _service_csv(self, handle, writer):
        """Open or close the log file, as requested from another thread."""
        if self._csv_stop:
            self._csv_stop = False
            if handle is not None:
                handle.close()
            with self._lock:
                self._stats.recording_path = ""
            return None, None

        request = self._csv_request
        if request is not None:
            self._csv_request = None
            if handle is not None:
                handle.close()
            request.parent.mkdir(parents=True, exist_ok=True)
            handle = request.open("w", newline="", encoding="utf-8")
            writer = csv.writer(handle)
            self._write_header(handle, writer)
            with self._lock:
                self._stats.recording_path = str(request)
                self._stats.recording_rows = 0
        return handle, writer

    def _write_header(self, handle, writer) -> None:
        """A '#' preamble so a log explains itself months later, then the column row.

        The comment lines are written straight to the file rather than through the CSV
        writer: a field containing a comma gets quoted, which would put a '"' before
        the '#' and stop anything (including pandas' comment='#') from recognising it
        as a comment.
        """
        channels = self.channels
        result = self.connect_result
        lines = [
            "McMESS log, Bosch M3.8 / 06A906018CG",
            f"recorded {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"link {self.port} at {self._stats.baudrate} baud"
            + (" (highspeed)" if self.highspeed else "")
            + (f", cable echo {'yes' if result.echo_detected else 'no'}" if result else ""),
            f"sample costs {self._stats.wire_bytes_per_sample} wire bytes",
            "conversions from DAMOS 8D0907558E (Audi A4 M3.8.2) - cross-image inference,"
            " see docs/mcmess-018cg.md section 7.1",
        ]
        raw_names = [c.name for c in channels if not c.has_physical]
        if raw_names:
            lines.append(f"reported raw, no usable conversion: {', '.join(raw_names)}")
        for line in lines:
            handle.write(f"# {line}\n")
        writer.writerow(
            ["t_s"] + [f"{c.name} [{c.unit}]" if c.unit else f"{c.name} [raw]" for c in channels]
        )

    def _write_row(self, writer, timestamp: float) -> None:
        with self._lock:
            row = list(getattr(self, "_last_row", []))
            self._stats.recording_rows += 1
        writer.writerow(
            [f"{timestamp:.4f}"]
            + [csv_number(v) for v in row]
        )


# ------------------------------------------------------------------ cost model


@dataclass
class SelectionCost:
    """What a channel selection will cost per sample, for the picker UI."""

    channel_count: int = 0
    byte_count: int = 0
    wire_bytes: int = 0
    parity_switches: int = 0
    pages: int = 0
    page0_channels: int = 0

    def estimated_hz(self, bytes_per_second: float) -> float:
        if not self.wire_bytes or bytes_per_second <= 0:
            return 0.0
        return bytes_per_second / self.wire_bytes

    def describe(self, bytes_per_second: float = 0.0) -> str:
        if not self.channel_count:
            return "no channels selected"
        text = (
            f"{self.channel_count} channels, {self.wire_bytes} wire bytes/sample, "
            f"{self.parity_switches} parity switches, {self.pages} pages"
        )
        rate = self.estimated_hz(bytes_per_second)
        if rate:
            text += f"  ->  ~{rate:.1f} Hz"
        return text


def selection_cost(channels: Sequence[Channel]) -> SelectionCost:
    """Cost of one sample for this selection, using the real scheduler."""
    if not channels:
        return SelectionCost()
    addresses = ChannelSet.addresses_for(list(channels))
    program = build_program(addresses)
    return SelectionCost(
        channel_count=len(channels),
        byte_count=len(addresses),
        wire_bytes=program.wire_bytes,
        parity_switches=program.parity_switches,
        pages=len({a >> 8 for a in addresses}),
        page0_channels=sum(1 for c in channels if c.page == 0),
    )
