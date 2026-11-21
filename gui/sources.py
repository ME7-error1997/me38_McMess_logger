"""What the window is showing: a log off disk, or the live buffer.

The window is one window. What changes between reading a log and making one is
only where the numbers come from, so that difference lives here, behind one small
interface, and everything else -- the channel panel, the chart, the stats, the
cursor -- is written once.

    entries          the channels, in panel order
    n_rows           how many samples there are
    points(key)      one channel as (xs, ys, rows) for the chart
    values(row)      every channel's value on one row, for the panel
    time(row)        that row's stamp

``LogSource`` wraps a parsed file and never changes. ``LiveSource`` wraps the
recorder's latest snapshot and is replaced wholesale on every redraw; its row
numbers index the snapshot, which is why the cursor is only the reader's once the
data has stopped moving.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np

from mcmess.channels import Channel, ChannelSet, column_label, csv_number
from mcmess.derived import DERIVATIONS
from mcmess.logenums import enum_for
from mcmess.logparse import (
    RPM_NAME_RE,
    RPM_UNIT_RE,
    LogSeries,
    ParsedLog,
    default_selection,
    find_rpm_series,
    format_value,
    parse_log,
    row_time,
    row_values,
    series_points,
)
from mcmess.profile import default_graph

Describe = Callable[[str], str]


@dataclass
class Entry:
    """One line of the channel panel."""

    key: str
    label: str
    unit: str
    #: What the channel is, in words: "Engine coolant temperature".
    description: str = ""
    tooltip: str = ""
    #: Never changes over the log. Always False live -- it has not finished.
    constant: bool = False


def describer(channel_set: ChannelSet | None) -> Describe:
    """A lookup from a channel name to what it measures.

    Built from the ECU's channel database plus the computed columns, so a log
    this logger wrote reads "TMOT -- Engine coolant temperature" whether it is
    live or opened off disk a month later. A name the database has never heard
    of -- every channel of a VCDS log -- gets an empty string, not a guess.
    """
    table: dict[str, str] = {}
    if channel_set is not None:
        for channel in channel_set:
            table[channel.name] = channel.about
    for cls in DERIVATIONS.values():
        for column in cls().channels():
            table[column.name] = column.about
    return lambda label: table.get(label, "")


def _code_tooltip(label: str) -> str | None:
    table = enum_for(label)
    if not table:
        return None
    if table.kind == "flags":
        return "A bit mask: several meanings can be set at once."
    known = ", ".join(f"{k} = {v}" for k, v in list(table.values.items())[:10])
    return known or "A code, not a quantity."


class LogSource:
    """A parsed log. Static: a row number means the same sample forever."""

    live = False

    def __init__(self, log: ParsedLog, describe: Describe | None = None) -> None:
        self.log = log
        describe = describe or (lambda label: "")
        self._by_key = log.by_key
        self._cache: dict[tuple, tuple] = {}
        self.entries = [
            Entry(
                key=s.key,
                label=s.label,
                unit=s.unit,
                description=describe(s.label),
                tooltip=self._tooltip(s, describe(s.label)),
                constant=s.constant,
            )
            for s in log.series
        ]

    @staticmethod
    def _tooltip(s: LogSeries, description: str) -> str:
        lines = [description] if description else []
        code = _code_tooltip(s.label)
        if code:
            lines.append(code)
        else:
            unit = f" {s.unit}" if s.unit else ""
            lines.append(
                f"{format_value(s.min)} ... {format_value(s.max)}{unit}"
                f"   ({s.count} samples)"
            )
            if s.constant:
                lines.append("never changes over this log")
        return "\n".join(lines)

    @property
    def n_rows(self) -> int:
        return len(self.log.rows)

    @property
    def has_clock(self) -> bool:
        return any(c.is_time for c in self.log.columns)

    def series(self, key: str) -> LogSeries:
        return self._by_key[key]

    def points(self, key: str, x_col: int | None | str = "own") -> tuple:
        """Cached (xs, ys, rows) for one channel against an X column.

        ``"own"`` is the series' own clock, None the row number, and another
        channel's column a scatter. Cached because switching the axis channel,
        a colour or the scale mode should cost no re-extraction.
        """
        s = self._by_key[key]
        col = s.time_col if x_col == "own" else x_col
        hit = self._cache.get((key, col))
        if hit is None:
            xs, ys, rows = series_points(self.log, s, col)
            hit = (
                np.asarray(xs, dtype=float),
                np.asarray(ys, dtype=float),
                np.asarray(rows, dtype=int),
            )
            self._cache[(key, col)] = hit
        return hit

    def values(self, row: int) -> dict[str, float]:
        return row_values(self.log, row)

    def time(self, row: int) -> float | None:
        return row_time(self.log, row)

    def rpm_key(self) -> str | None:
        rpm = find_rpm_series(self.log)
        return rpm.key if rpm else None

    def suggested(self) -> list[str]:
        return default_selection(self.log)

    def keys_for_labels(self, labels: list[str]) -> list[str]:
        """Series keys for channel names, in the order asked; unknown ones dropped."""
        by_label = {s.label: s.key for s in self.log.series}
        return [by_label[n] for n in labels if n in by_label]


class LiveSource:
    """The recorder's latest snapshot. Replaced on every redraw."""

    live = True

    def __init__(self, channels: list[Channel]) -> None:
        self.channels = list(channels)
        self._index = {c.name: i for i, c in enumerate(self.channels)}
        self.times = np.empty(0)
        self.data = np.empty((0, len(self.channels)))
        self._rows = np.arange(0, dtype=int)
        self.entries = [
            Entry(
                key=c.name,
                label=c.name,
                unit=c.unit_label,
                description=c.about,
                tooltip="\n".join(
                    t for t in (c.about, _code_tooltip(c.name)) if t
                ) or c.name,
            )
            for c in self.channels
        ]

    def update(self, times: np.ndarray, data: np.ndarray) -> bool:
        """Take a new snapshot. False when it does not match the channel list."""
        if data.ndim != 2 or data.shape[1] != len(self.channels):
            return False
        self.times, self.data = times, data
        if len(self._rows) != len(times):
            self._rows = np.arange(len(times), dtype=int)
        return True

    @property
    def n_rows(self) -> int:
        return len(self.times)

    has_clock = True

    def points(self, key: str, x_col: int | None | str = "own") -> tuple:
        return self.times, self.data[:, self._index[key]], self._rows

    def values(self, row: int) -> dict[str, float]:
        if not len(self.times):
            return {}
        r = min(max(int(row), 0), len(self.times) - 1)
        line = self.data[r]
        return {
            c.name: float(line[i])
            for i, c in enumerate(self.channels)
            if np.isfinite(line[i])
        }

    def time(self, row: int) -> float | None:
        if not len(self.times):
            return None
        return float(self.times[min(max(int(row), 0), len(self.times) - 1)])

    def rpm_key(self) -> str | None:
        """By name first, then by unit: this ECU calls it N40, which does not
        read as "rpm", but it carries Upm."""
        for match in (
            lambda c: RPM_NAME_RE.search(c.name),
            lambda c: RPM_UNIT_RE.match(c.unit_label.strip()),
        ):
            for channel in self.channels:
                if match(channel):
                    return channel.name
        return None

    def suggested(self) -> list[str]:
        return default_graph(self.channels)

    def to_log(self, meta: list[str] | None = None) -> ParsedLog | None:
        """The snapshot as a parsed log, for when no CSV was being written.

        Goes through the same text and the same parser as a file would, rather
        than building a ParsedLog by hand: a session that was not recorded
        should behave exactly like one that was, and the cheapest way to
        guarantee that is to make it one.
        """
        return snapshot_to_log(self.times, self.data, self.channels, meta)


def snapshot_to_log(
    times: np.ndarray,
    data: np.ndarray,
    channels: list[Channel],
    meta: list[str] | None = None,
) -> ParsedLog | None:
    if len(times) < 2:
        return None
    lines = [f"# {line}" for line in (meta or [])]
    lines.append(",".join(["t_s"] + [column_label(c) for c in channels]))
    for t, row in zip(times, data):
        lines.append(
            ",".join([f"{t:.4f}"] + [csv_number(float(v)) for v in row])
        )
    return parse_log("\n".join(lines) + "\n")
