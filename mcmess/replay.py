"""Play a recorded log back as if it were arriving from the car.

    py gui/main.py --replay logs/2026-11-19_16-38-33_018CG.csv

The simulator proves the protocol; it does not look like an engine. Its rpm is a
ramp and its knock is nothing. To see what the live window does with a real pull
-- real boost onset, real knock retard, real noise -- the honest input is a pull
that was actually driven. This feeds one back through the recorder's own
interface, sample by sample at the pace it was recorded, so the window cannot
tell the difference and nothing in it is special-cased for the demonstration.

It is not a Recorder subclass: there is no serial port, no handshake and no
program to build. It only quacks the same -- ``state``, ``stats()``,
``snapshot()``, ``start()``, ``stop()`` -- and that is all the window uses.

Nothing is written. A replay does not record a CSV, because the result would be
a second file claiming to be a pull that was never driven.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Sequence

import numpy as np

from .channels import Channel, ChannelSet
from .derived import DERIVATIONS, _column
from .logparse import ParsedLog, read_log
from .recorder import DEFAULT_CAPACITY, RecorderStats, RingBuffer

#: The state a replay ends in. Not an error and not idle: the log ran out.
FINISHED = "finished"


class ReplayError(ValueError):
    """A log that cannot be replayed. One sentence for a human."""


def replay_channels(
    log: ParsedLog, channel_set: ChannelSet | None = None
) -> list[Channel]:
    """A channel per column of the log, described from the database where known.

    Every one is a computed column as far as the rest of the program is
    concerned -- no address, no wire cost -- carrying the unit the *log* gives
    it, so a log taken with an older conversion is shown as it was written.
    """
    about: dict[str, str] = {}
    if channel_set is not None:
        about.update({c.name: c.about for c in channel_set})
    for cls in DERIVATIONS.values():
        about.update({c.name: c.about for c in cls().channels()})
    return [
        _column(s.label, s.unit, about.get(s.label, ""), key="replay")
        for s in log.series
    ]


class ReplayRecorder:
    """Feeds a parsed log into a ring buffer at the pace it was recorded."""

    def __init__(
        self,
        log: ParsedLog,
        channels: Sequence[Channel],
        speed: float = 1.0,
        capacity: int = DEFAULT_CAPACITY,
        port: str = "REPLAY",
    ) -> None:
        if not log.rows or not log.series:
            raise ReplayError("the log has no rows to replay")
        self.port = port
        self.speed = max(float(speed), 0.01)
        self._channels = list(channels)

        # Everything up front, as arrays: the playback thread should do nothing
        # but sleep and append, so its pacing is the log's and not the parser's.
        clock = next((s.time_col for s in log.series if s.time_col is not None), None)
        stamps: list[float] = []
        rows: list[list[float]] = []
        for i, row in enumerate(log.rows):
            t = row[clock] if clock is not None else i * 0.05
            if t is None:
                continue
            stamps.append(float(t))
            rows.append([
                float("nan") if row[s.col] is None else float(row[s.col])
                for s in log.series
            ])
        if len(stamps) < 2:
            raise ReplayError("the log has fewer than two timed rows")
        # Rebased to zero. A log that started recording three minutes into a
        # session has stamps from 175 s; live, the chart counts from connect.
        self._times = np.asarray(stamps) - stamps[0]
        self._rows = np.asarray(rows, dtype=float)

        self._lock = threading.Lock()
        self._buffer = RingBuffer(len(self._channels), capacity)
        self._stats = RecorderStats()
        self._state = "idle"
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @classmethod
    def from_file(
        cls,
        path: str | Path,
        channel_set: ChannelSet | None = None,
        speed: float = 1.0,
    ) -> "ReplayRecorder":
        path = Path(path)
        try:
            log = read_log(path)
        except OSError as exc:
            raise ReplayError(f"cannot read {path.name}: {exc.strerror or exc}") from exc
        if log is None:
            raise ReplayError(f"{path.name} is not a log this program can read")
        return cls(log, replay_channels(log, channel_set), speed=speed,
                   port=f"replay of {path.name}")

    # -- the recorder's interface ------------------------------------------

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def channels(self) -> list[Channel]:
        return list(self._channels)

    @property
    def duration(self) -> float:
        return float(self._times[-1])

    def stats(self) -> RecorderStats:
        with self._lock:
            return self._stats.copy()

    def last_raw(self) -> dict[str, int | None]:
        return {}

    def snapshot(
        self, seconds: float | None = None, max_points: int | None = None
    ) -> tuple[np.ndarray, np.ndarray, list[Channel]]:
        with self._lock:
            times, data = self._buffer.snapshot(seconds, max_points)
            return times, data, list(self._channels)

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        with self._lock:
            self._buffer.clear()
            self._stats = RecorderStats()
            self._state = "running"
        self._thread = threading.Thread(
            target=self._run, name="mcmess-replay", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        self._thread = None
        with self._lock:
            if self._state != FINISHED:
                self._state = "idle"

    # A replay has one channel list and writes nothing; these exist so the
    # window does not need to know which kind of recorder it is holding.
    def set_channels(self, channels: Sequence[Channel]) -> None:
        return None

    def start_recording(self, path: str | Path) -> None:
        return None

    def stop_recording(self) -> None:
        return None

    # -- playback ----------------------------------------------------------

    def _run(self) -> None:
        began = time.perf_counter()
        window: list[float] = []
        for t, row in zip(self._times, self._rows):
            # Sleep in short slices so Disconnect is answered at once even when
            # the log has a long gap in it.
            while not self._stop.is_set():
                ahead = t / self.speed - (time.perf_counter() - began)
                if ahead <= 0:
                    break
                time.sleep(min(ahead, 0.02))
            if self._stop.is_set():
                return
            window.append(time.perf_counter())
            if len(window) > 64:
                del window[:-64]
            span = window[-1] - window[0] if len(window) > 1 else 0.0
            with self._lock:
                self._buffer.append(float(t), row)
                self._stats.samples += 1
                self._stats.sample_hz = (len(window) - 1) / span if span > 0 else 0.0
        with self._lock:
            self._state = FINISHED
