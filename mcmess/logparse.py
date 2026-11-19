"""Datalog parsing: VCDS measuring blocks, VCDS advanced, ME7Logger, OP-COM,
and any other CSV a logger writes -- our own included.

A port of the TunePortal datalog parser (docs/datalog-viewer.md records where it
came from). None of these files is a plain "header row then numbers". They open
with a few lines of identification, the column names can be split over two rows
with the units in a third, and VCDS gives every group -- or, in the advanced
format, every field -- its own time column. So instead of one recipe per tool the
parser finds the landmarks that all of them share:

  1. the delimiter (comma, semicolon or tab; semicolon files use a decimal comma)
  2. the first two lines with numbers in the same columns: that is the data.
     Not "mostly numbers": OP-COM puts Active / Lean / Not Programmed in half the
     cells of every row. And not "the first line with numbers": OP-COM writes a
     [SELINFO] row of zeros ABOVE its names row.
  3. the up-to-three non-blank lines straight above it: that is the header

and only then asks which tool wrote the file, to read the header rows the way
that tool means them. Anything unrecognised is treated as generic CSV.

Three small deviations from the original, all for this project's own logs:

  * a leading header line whose first cell starts with ``#`` is dropped, the same
    way a VCDS ``Group A:`` line and an OP-COM ``[SELINFO]`` line are -- our
    recorder writes five ``#`` lines of provenance above the names row;
  * ``t_s`` and ``time_s`` count as time-column names;
  * a generic CSV keeps up to six identification lines rather than four, so the
    DAMOS-inference caveat our recorder writes is still on screen in the viewer.

Pure module: no imports beyond the standard library, no I/O except read_log().
Tested in tests/test_logparse.py against excerpts of real files.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

#: The formats the parser knows. Anything else reads as "csv".
FORMATS = ("vcds", "vcds-advanced", "me7logger", "opcom", "csv")

MAX_HEADER_LINES = 3

#: Identification lines kept, in all; a generic CSV keeps six of them.
MAX_META_LINES = 8
MAX_META_CSV = 6


@dataclass
class LogColumn:
    """One column of the file: its name, its unit, and whether it is a clock."""

    label: str
    unit: str
    is_time: bool
    #: VCDS only, and only until dedupe consumes it: the block number from the
    #: "Group A:" line, or "G2 F0" from "Group 2 - Field 0". It is what tells two
    #: "Engine Speed (G28)" columns apart.
    hint: str = ""


@dataclass
class LogSeries:
    """A plottable channel: one non-time column with at least two samples."""

    #: Stable within the file: "c<column index>".
    key: str
    label: str
    unit: str
    #: Column holding the values.
    col: int
    #: Column holding THIS series' time stamps; None = use the row number.
    #: VCDS samples its groups one after another, so each group's stamps differ
    #: by a few hundredths of a second. One shared clock would shift the traces.
    time_col: int | None
    min: float
    max: float
    #: Never changes over the log: hidden by default, never suggested.
    constant: bool
    #: Numeric samples in the column.
    count: int


@dataclass
class ParsedLog:
    """A parsed file: the grid, plus what is known about its columns.

    The grid is kept, not per-series arrays. A log can be 30,000 rows by 150
    channels; building arrays for all of them to plot six would be wasteful, and
    the channel panel reads every channel's value on a row straight out of the
    grid on every mouse move.
    """

    format: str
    #: Identification lines from the top of the file, cleaned up.
    meta: list[str] = field(default_factory=list)
    columns: list[LogColumn] = field(default_factory=list)
    #: The numeric grid, one list per data row; None where a cell is empty or text.
    rows: list[list[float | None]] = field(default_factory=list)
    series: list[LogSeries] = field(default_factory=list)

    @property
    def by_key(self) -> dict[str, LogSeries]:
        return {s.key: s for s in self.series}


# ----------------------------------------------------------------- decoding --

def decode_log_bytes(data: bytes) -> str:
    """VCDS writes Windows-1252; most other loggers write UTF-8.

    Strictly UTF-8 first. VCDS's degree sign is the byte 0xB0, which is not valid
    UTF-8, so a lenient decode would silently mangle its units into replacement
    characters -- and you would not find out until a unit looked wrong.
    """
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def looks_binary(data: bytes) -> bool:
    """A zip, an xlsx or a binary export: NUL bytes near the start."""
    head = data[:4096]
    return head.count(0) > 4 or data[:2] == b"PK"  # "PK" = zip / xlsx


def read_log(path: str | Path) -> ParsedLog | None:
    """Read a file from disk. None when it is binary or not a log at all."""
    data = Path(path).read_bytes()
    if looks_binary(data):
        return None
    return parse_log(decode_log_bytes(data))


# ------------------------------------------------------------------ helpers --

def _detect_delimiter(lines: list[str]) -> str:
    counts = {",": 0, ";": 0, "\t": 0}
    for line in lines[:80]:
        for d in counts:
            counts[d] += line.count(d)
    # A semicolon file with decimal commas has plenty of both; prefer ';' when
    # it is common.
    if counts[";"] > 0 and counts[";"] >= counts[","] * 0.5:
        return ";"
    if counts["\t"] > counts[","]:
        return "\t"
    return ","


def _split_line(line: str, delimiter: str) -> list[str]:
    if '"' not in line:
        return line.split(delimiter)
    out: list[str] = []
    cell = ""
    quoted = False
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == '"':
            if quoted and i + 1 < len(line) and line[i + 1] == '"':
                cell += '"'
                i += 1
            else:
                quoted = not quoted
        elif ch == delimiter and not quoted:
            out.append(cell)
            cell = ""
        else:
            cell += ch
        i += 1
    out.append(cell)
    return out


NUMBER_RE = re.compile(r"^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$")


def _to_number(cell: str, decimal_comma: bool) -> float | None:
    """None for an empty cell, NaN for text, otherwise the number."""
    s = cell.strip()
    if s == "":
        return None
    if decimal_comma:
        s = s.replace(",", ".", 1)
    return float(s) if NUMBER_RE.match(s) else math.nan


def _is_blank(cells: list[str]) -> bool:
    return all(c.strip() == "" for c in cells)


def _numeric_positions(cells: list[str], decimal_comma: bool) -> set[int]:
    out: set[int] = set()
    for i, c in enumerate(cells):
        n = _to_number(c, decimal_comma)
        if n is not None and not math.isnan(n):
            out.add(i)
    return out


def _is_data_pair(a: list[str], b: list[str], decimal_comma: bool) -> bool:
    """Two data rows have their numbers in the same columns.

    That holds however many text cells a row carries, and it does not hold for a
    line of numbers that merely sits above a header.
    """
    A = _numeric_positions(a, decimal_comma)
    B = _numeric_positions(b, decimal_comma)
    if len(A) < 2 or len(B) < 2:
        return False
    both = len(A & B)
    return both / max(len(A), len(B)) >= 0.8


#: Text a logger writes for a two-state signal, as the number to plot.
STATES = {
    "active": 1.0, "inactive": 0.0, "on": 1.0, "off": 0.0,
    "open": 1.0, "closed": 0.0, "yes": 1.0, "no": 0.0,
    "true": 1.0, "false": 0.0,
}


def _clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _cell(row: list[str], i: int) -> str:
    return row[i] if 0 <= i < len(row) else ""


# ------------------------------------------------------------------- parser --

def parse_log(text: str) -> ParsedLog | None:
    """Parse a datalog. None when the text holds no plottable channel."""
    if text.startswith("﻿"):
        text = text[1:]
    lines = re.split(r"\r?\n", text)
    delimiter = _detect_delimiter(lines)
    decimal_comma = delimiter == ";"
    grid = [_split_line(line, delimiter) for line in lines]

    # 2. where the data starts: two lines in a row (ignoring blanks between them)
    #    with their numbers in the same columns
    data_start = -1
    for i in range(len(grid)):
        if _is_blank(grid[i]):
            continue
        j = i + 1
        while j < len(grid) and _is_blank(grid[j]):
            j += 1
        if j < len(grid) and _is_data_pair(grid[i], grid[j], decimal_comma):
            data_start = i
            break
    if data_start < 0:
        return None

    # 3. the header block straight above it
    header: list[list[str]] = []
    i = data_start - 1
    while i >= 0 and len(header) < MAX_HEADER_LINES:
        if _is_blank(grid[i]):
            break
        header.insert(0, grid[i])
        i -= 1

    # Lines that sit in the header block but are not header rows. VCDS puts
    # "Group A:,'115,...,Group B:,'118" above its three header rows; OP-COM puts
    # "[SELINFO],0,0,0,..." straight above its names row; our own recorder writes
    # "# ..." provenance lines. All three would otherwise be read as names.
    group_line_index = _find_group_line(grid, data_start)
    while header and any(re.match(r"^Group [A-Z]:$", c.strip(), re.I) for c in header[0]):
        header.pop(0)
    while header and re.match(r"^\[[A-Z_]+\]$", _clean(_cell(header[0], 0)), re.I):
        header.pop(0)
    while header and _clean(_cell(header[0], 0)).startswith("#"):
        header.pop(0)

    top = "\n".join(lines[:min(data_start, 40)])
    header_cells = [c.strip() for row in header for c in row]
    fmt = "csv"
    if re.search(r"ME7-?Logger", top, re.I) or _clean(
        _cell(header[0], 0) if header else ""
    ).lower() == "timestamp":
        fmt = "me7logger"
    elif re.search(r"VCDS", lines[0] if lines else "", re.I) or "STAMP" in header_cells:
        advanced = any(
            re.match(r"^Group \d+ - Field \d+$", c, re.I) for c in header_cells
        ) or "ADVMB" in top
        fmt = "vcds-advanced" if advanced else "vcds"
    elif re.search(r"OP-?COM", "\n".join(lines[:7]), re.I):
        fmt = "opcom"

    # the numeric grid
    rows: list[list[float | None]] = []
    raw_rows: list[list[str]] = []
    width = max((len(h) for h in header), default=0)
    for i in range(data_start, len(grid)):
        cells = grid[i]
        if _is_blank(cells):
            continue
        row: list[float | None] = []
        for c in cells:
            n = _to_number(c, decimal_comma)
            row.append(None if n is None or math.isnan(n) else n)
        if all(v is None for v in row):
            continue
        rows.append(row)
        raw_rows.append(cells)
        width = max(width, len(row))
    for row in rows:
        row.extend([None] * (width - len(row)))

    if fmt == "me7logger":
        columns = _me7_columns(header, width)
    elif fmt in ("csv", "opcom"):
        columns = _csv_columns(header, width, rows)
    else:
        columns = _vcds_columns(
            header, width, grid[group_line_index] if group_line_index >= 0 else None
        )

    # Active / Inactive, On / Off, Open / Closed: a two-state signal is a 0/1 channel
    for c in range(width):
        if columns[c].is_time or any(r[c] is not None for r in rows):
            continue
        texts = [_clean(_cell(r, c)).lower() for r in raw_rows]
        filled = [t for t in texts if t]
        if len(filled) < 2 or not all(t in STATES for t in filled):
            continue
        for i, row in enumerate(rows):
            row[c] = STATES[texts[i]] if texts[i] else None
        columns[c].unit = "0/1"

    if fmt == "opcom":
        _repair_opcom_rpm(columns, rows, raw_rows)

    # drop columns that hold nothing at all (VCDS pads every line with commas)
    keep = [c for c in range(width) if any(r[c] is not None for r in rows)]
    final_columns = [columns[c] for c in keep]
    final_rows = [[r[c] for c in keep] for r in rows]

    _dedupe_labels(final_columns)

    time_cols = [i for i, c in enumerate(final_columns) if c.is_time]
    single_time = time_cols[0] if len(time_cols) == 1 else None

    series: list[LogSeries] = []
    last_time: int | None = None
    for c, column in enumerate(final_columns):
        if column.is_time:
            last_time = c
            continue
        low, high, count = math.inf, -math.inf, 0
        for row in final_rows:
            v = row[c]
            if v is None:
                continue
            count += 1
            low = min(low, v)
            high = max(high, v)
        if count < 2:
            continue
        series.append(LogSeries(
            key=f"c{c}", label=column.label, unit=column.unit, col=c,
            # one time column serves every series; several (VCDS) serve the
            # columns to their right
            time_col=single_time if single_time is not None else last_time,
            min=low, max=high, constant=low == high, count=count,
        ))
    if not series:
        return None

    return ParsedLog(
        format=fmt,
        meta=_meta_lines(lines, data_start - len(header), fmt),
        columns=final_columns,
        rows=final_rows,
        series=series,
    )


def _find_group_line(grid: list[list[str]], data_start: int) -> int:
    for i in range(data_start - 1, max(-1, data_start - 7), -1):
        if i < 0:
            break
        if any(re.match(r"^Group [A-Z]:$", c.strip(), re.I) for c in grid[i]):
            return i
    return -1


# ------------------------------------------------------------ column naming --

VCDS_NOISE_RE = re.compile(r"^(TIME|STAMP|Marker)$", re.I)
VCDS_FIELD_RE = re.compile(r"^Group \d+ - Field \d+$", re.I)


def _vcds_columns(
    header: list[list[str]], width: int, group_line: list[str] | None
) -> list[LogColumn]:
    """VCDS: the last header row is the units, the rows above it are the name in
    one or two pieces ("Engine Speed" over "(G28)").

    TIME/STAMP marks a time column; each value column belongs to the time column
    on its left.
    """
    unit_row = header[-1] if header else []
    name_rows = header[:-1]
    columns: list[LogColumn] = []
    group = ""
    for c in range(width):
        # "Group A:" sits above the group's TIME column, its block number next
        if group_line and re.match(r"^Group [A-Z]:$", _clean(_cell(group_line, c)), re.I):
            group = _clean(_cell(group_line, c + 1)).lstrip("'")
        cells = [_clean(_cell(r, c)) for r in header]
        is_time = any(re.match(r"^(TIME|STAMP)$", x, re.I) for x in cells)
        field_name = next((x for x in cells if VCDS_FIELD_RE.match(x)), "")
        parts = [
            x for x in (_clean(_cell(r, c)) for r in name_rows)
            if x and not VCDS_NOISE_RE.match(x) and not VCDS_FIELD_RE.match(x)
        ]
        label = "Time" if is_time else (" ".join(parts) or field_name or f"Column {c + 1}")
        unit = "s" if is_time else re.sub(r"^Marker$", "", _clean(_cell(unit_row, c)), flags=re.I)
        hint = group
        if not hint and field_name:
            # "Group 2 - Field 0" -> "G2 F0", short enough to sit in a label
            g, f = re.findall(r"\d+", field_name)[:2]
            hint = f"G{g} F{f}"
        columns.append(LogColumn(label=label, unit=unit, is_time=is_time, hint=hint))
    return columns


def _me7_columns(header: list[list[str]], width: int) -> list[LogColumn]:
    """ME7Logger: variable names, units, then the friendly alias row.

    Two header rows means names and units, no aliases. Column 0 is the time.
    """
    if len(header) >= 3:
        names, units, aliases = header[0], header[1], header[2]
    else:
        names = header[0] if len(header) > 0 else []
        units = header[1] if len(header) > 1 else []
        aliases = []
    columns: list[LogColumn] = []
    for c in range(width):
        if c == 0:
            columns.append(LogColumn(label="Time", unit="s", is_time=True))
            continue
        name = _clean(_cell(names, c))
        alias = _clean(_cell(aliases, c))
        if alias and name:
            label = f"{alias} ({name})"
        else:
            label = alias or name or f"Column {c + 1}"
        unit = _clean(_cell(units, c))
        columns.append(LogColumn(label=label, unit="" if unit == "-" else unit, is_time=False))
    return columns


TIME_NAME_RE = re.compile(
    r"^(time|timestamp|time ?stamp|zeit|t|t_s|time_s|sec|secs|seconds|elapsed"
    r"|time ?\(?(s|sec|ms)\)?)$",
    re.I,
)

#: "Boost (mbar)", "ML [kg/h]" -> name and unit, when there is no units row.
INLINE_UNIT_RE = re.compile(r"^(.*?)[\s_]*[([]([^()\[\]]{1,10})[)\]]$")


def _csv_columns(
    header: list[list[str]], width: int, rows: list[list[float | None]]
) -> list[LogColumn]:
    """Generic CSV (and OP-COM): a names row, optionally a units row under it."""
    names = header[-1] if header else []
    units: list[str] = []
    if len(header) >= 2:
        last = [_clean(c) for c in header[-1]]
        above = [_clean(c) for c in header[-2]]
        filled = [x for x in last if x]
        # A units row is short cells under a names row that is at least as full.
        looks_like_units = (
            bool(filled)
            and all(len(x) <= 8 for x in filled)
            and len([x for x in above if x]) >= len(filled)
        )
        if looks_like_units:
            names, units = header[-2], header[-1]

    columns: list[LogColumn] = []
    for c in range(width):
        raw = _clean(_cell(names, c))
        m = INLINE_UNIT_RE.match(raw) if not units else None
        unit = _clean(_cell(units, c)) or (m.group(2) if m else "")
        columns.append(LogColumn(
            label=(m.group(1) if m else raw) or f"Column {c + 1}",
            # MHD writes "(-)" for a code: that is not a unit
            unit="" if unit == "-" else unit,
            is_time=bool(TIME_NAME_RE.match(raw)),
        ))

    # no column is called time: a first column that only ever rises is one
    if not any(c.is_time for c in columns) and len(rows) > 10:
        rising = True
        prev = -math.inf
        for row in rows:
            v = row[0]
            if v is None or v < prev:
                rising = False
                break
            prev = v
        first = rows[0][0] or 0.0
        if rising and prev > first:
            columns[0].is_time = True

    # only the first time column counts in a generic file
    seen = False
    for column in columns:
        if column.is_time:
            if seen:
                column.is_time = False
            seen = True
    return columns


def _dedupe_labels(columns: list[LogColumn]) -> None:
    """"Engine Speed (G28)" twice (VCDS logs it in every group): tell them apart."""
    counts: dict[str, int] = {}
    units: dict[str, list[str]] = {}
    for c in columns:
        if c.is_time:
            continue
        counts[c.label] = counts.get(c.label, 0) + 1
        units.setdefault(c.label, []).append(c.unit)

    seen: dict[str, int] = {}
    for c in columns:
        original = c.label
        if not c.is_time and counts.get(original, 0) > 1:
            n = seen.get(original, 0) + 1
            seen[original] = n
            # OP-COM logs a sensor twice, as volts and as the value: the unit
            # says which. Only when every duplicate has a unit of its own.
            u = units[original]
            by_unit = all(u) and len(set(u)) == len(u)
            suffix = c.hint or (c.unit if by_unit else str(n))
            c.label = f"{original} [{suffix}]"
        c.hint = ""


def _meta_lines(lines: list[str], header_start: int, fmt: str) -> list[str]:
    """The identification lines worth showing above the chart."""
    out: list[str] = []
    for raw in lines[:max(0, header_start)]:
        line = re.sub(r"[,;\t]+$", "", raw).strip()
        if re.fullmatch(r"#+", line):
            line = ""
        if not line:
            continue
        if fmt == "me7logger":
            if re.match(
                r"^(Logfile created|ECU identified|Log packet|Used speed|Used mode"
                r"|VAGHWNumber|ModelId)", line, re.I
            ):
                continue
            line = re.sub(r"\s*\(requested was [^)]*\)", "", line, flags=re.I)
            line = re.sub(r"\s+=\s+", ": ", line, count=1)
            out.append(re.sub(r"\s{2,}", " ", line))
        elif fmt == "opcom":
            # "[MODELINFO],2005 (5),Astra-G,Engine" -> "2005 (5) . Astra-G . Engine"
            if re.match(r"^\[(WSINFO|MBLOCKSINFORMATION|SELINFO)\]", line, re.I):
                continue
            body = re.sub(r"^\[[A-Z_]+\],?", "", line, flags=re.I)
            cells = [x.strip() for x in body.split(",") if x.strip()]
            if cells:
                out.append(" · ".join(cells))
        elif fmt == "csv":
            # Our own recorder's "# ..." provenance lines come through here.
            if len(out) < MAX_META_CSV:
                body = re.sub(r"^#+\s*", "", line)
                out.append(re.sub(r"[,;\t]+", " · ", body))
        else:
            if re.match(r"^,*Group [A-Z]:", line, re.I) or re.match(r"^,+", line):
                continue
            cells = [re.sub(r"\s+", " ", x).strip() for x in line.split(",")]
            cells = [x for x in cells if x]
            # "Friday,22,August,2025,20:13:30:00009-VCID:...,VCDS Version: ..."
            # -> a date and a version
            stamp = None
            if len(cells) >= 5 and re.fullmatch(r"\d{1,2}", cells[1]) \
                    and re.fullmatch(r"\d{4}", cells[3]):
                stamp = re.match(r"^\d{1,2}:\d{2}:\d{2}", cells[4])
            if stamp:
                head = f"{cells[0]} {cells[1]} {cells[2]} {cells[3]} {stamp.group(0)}"
                out.append(" · ".join([head, *cells[5:]]))
            else:
                out.append(" · ".join(cells))
        if len(out) >= MAX_META_LINES:
            break
    return out


# ------------------------------------------------------------ OP-COM's rpm --

OPCOM_DOTTED_RE = re.compile(r"^\d{1,2}\.\d{3}$")


def _repair_opcom_rpm(
    columns: list[LogColumn],
    rows: list[list[float | None]],
    raw_rows: list[list[str]],
) -> None:
    """OP-COM writes engine speed with a thousands separator past 999 rpm.

    900 rpm is "900" but 1501 rpm is "1.501", which reads as one and a half. In an
    OP-COM log a speed column is repaired only when it is plausible that this
    happened: every value is either a whole number under 1000 or has the d.ddd
    shape, and at least one has. Then "1.501" becomes 1501 and "900" stays. A
    column with any other shape ("1.5", "2.25") is left alone -- it is a real
    fractional reading.
    """
    for c, column in enumerate(columns):
        if column.is_time:
            continue
        if not re.fullmatch(r"rpm", column.unit, re.I) \
                and not re.search(r"engine speed|idle speed", column.label, re.I):
            continue
        texts = [t for t in (_cell(r, c).strip() for r in raw_rows) if t]
        plausible = (
            bool(texts)
            and any(OPCOM_DOTTED_RE.match(x) for x in texts)
            and all(
                OPCOM_DOTTED_RE.match(x) or re.fullmatch(r"\d{1,3}", x) for x in texts
            )
        )
        if not plausible:
            continue
        for i, row in enumerate(rows):
            x = _cell(raw_rows[i], c).strip()
            if OPCOM_DOTTED_RE.match(x):
                row[c] = float(x.replace(".", ""))


# ------------------------------------------------------------- plot helpers --

def series_points(
    log: ParsedLog, s: LogSeries, x_col: int | None = -1
) -> tuple[list[float], list[float], list[int]]:
    """A channel as points: xs, ys, and the log row each point came from.

    The rows come back ascending, which is what makes the cursor a nearest-value
    search. ``x_col=-1`` means "the series' own clock"; None means the row number;
    another channel's column gives a scatter plot (boost against rpm).
    """
    if x_col == -1:
        x_col = s.time_col
    xs: list[float] = []
    ys: list[float] = []
    rows: list[int] = []
    for i, row in enumerate(log.rows):
        y = row[s.col]
        x = float(i) if x_col is None else row[x_col]
        if y is None or x is None:
            continue
        xs.append(x)
        ys.append(y)
        rows.append(i)
    return xs, ys, rows


#: How far either side of a row to look for a channel with no sample on it.
NEAREST_ROWS = 25


def _clamp_row(log: ParsedLog, row: float) -> int:
    return min(len(log.rows) - 1, max(0, int(round(row))))


def row_values(log: ParsedLog, row: float) -> dict[str, float]:
    """Every channel's value on one row: what the panel shows at the cursor.

    Plotted or not -- which is the point. It reads the grid directly, so 150
    values on every mouse move cost one row lookup each rather than 150 arrays.
    A channel with no sample on that row takes its nearest one within a few rows
    (the earlier on a tie); one with none nearby is left out.
    """
    out: dict[str, float] = {}
    n = len(log.rows)
    if n == 0:
        return out
    r = _clamp_row(log, row)
    for s in log.series:
        v = log.rows[r][s.col]
        d = 1
        while v is None and d <= NEAREST_ROWS:
            before = log.rows[r - d][s.col] if r - d >= 0 else None
            after = log.rows[r + d][s.col] if r + d < n else None
            v = before if before is not None else after
            d += 1
        if v is not None:
            out[s.key] = v
    return out


def row_time(log: ParsedLog, row: float) -> float | None:
    """The stamp of a row, from the log's first clock; None when it has none."""
    col = next((i for i, c in enumerate(log.columns) if c.is_time), -1)
    if col < 0 or not log.rows:
        return None
    return log.rows[_clamp_row(log, row)][col]


RPM_NAME_RE = re.compile(r"engine ?speed|nmot|\brpm\b|drehzahl", re.I)
RPM_UNIT_RE = re.compile(r"^(rpm|/min|1/min|u/min|upm)$", re.I)

#: What a tuner looks at first, in order. The second x axis and the default
#: selection both walk this list.
FAVOURITES = (
    re.compile(r"engine ?speed|nmot|\brpm\b|drehzahl", re.I),
    re.compile(r"boost.*(actual)|pvdks|manifold.*press|\bmap\b", re.I),
    re.compile(r"boost.*(spec|desired|target|command)|plsol", re.I),
    re.compile(r"engine ?load|^load|\brl_w\b|^tl$", re.I),
    re.compile(r"mshfm|\bmaf\b|mass ?air ?flow$|^ml(_gs)?$", re.I),
    re.compile(r"mass ?air", re.I),
    re.compile(
        r"timing angle|spark angle|zwout|zwist|ignition.*(timing|advance)", re.I
    ),
    re.compile(r"lambda|afr|air.?fuel|^fr$", re.I),
    re.compile(r"throttle|wdkba|pedal|^dkpot$", re.I),
)


def find_rpm_series(log: ParsedLog) -> LogSeries | None:
    """The engine speed channel, for the chart's second x axis."""
    live = [s for s in log.series if not s.constant]
    for s in live:
        if RPM_NAME_RE.search(s.label):
            return s
    for s in live:
        if RPM_UNIT_RE.match(s.unit.strip()):
            return s
    return None


def default_selection(log: ParsedLog, limit: int = 5) -> list[str]:
    """What to plot before the reader has chosen.

    Walk the favourites and take the first non-constant match for each. Fewer
    than two hits means the list did not fit this log at all, so fall back to the
    first few channels that actually move.
    """
    live = [s for s in log.series if not s.constant]
    picked: list[str] = []
    # The engine speed slot goes through find_rpm_series rather than the first
    # favourite, because that one also matches on the unit. Our own logs call the
    # channel N40 and VAG's German ones Drehzahl -- neither reads as "rpm", but
    # both carry Upm / 1/min, and a pull with no rpm trace is not worth drawing.
    speed = find_rpm_series(log)
    if speed:
        picked.append(speed.key)
    for pattern in FAVOURITES:
        def match(s: LogSeries, pattern=pattern) -> bool:
            return bool(pattern.search(s.label)) and s.key not in picked
        # OP-COM logs a sensor twice, as volts and as the value: the value is
        # the one to look at
        hit = next((s for s in live if match(s) and s.unit != "V"), None) \
            or next((s for s in live if match(s)), None)
        if hit:
            picked.append(hit.key)
        if len(picked) >= limit:
            break
    if len(picked) >= 2:
        return picked
    return [s.key for s in live[:min(4, len(live))]]


def format_value(v: float | None) -> str:
    """Values on screen: 0 decimals from 1000, 1 from 100, 2 from 1, else 3.

    Trailing zeros are trimmed after the decimal point only, so 3040 stays 3040
    and 1.50 becomes 1.5.
    """
    if v is None or not math.isfinite(v):
        return "--"
    a = abs(v)
    digits = 0 if a >= 1000 else 1 if a >= 100 else 2 if a >= 1 else 3
    s = f"{v:.{digits}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    # Collapse negative zero: a retard of exactly 0 should not read "-0".
    return "0" if s in ("-0", "") else s


def group_key_for(key: str, unit: str, own_scale: bool = False) -> str:
    """The scale a channel is drawn on: one per unit, or one per channel.

    Sharing by unit is what puts boost actual against boost requested on the same
    axis. A channel with no unit always gets its own -- a bare number shares
    nothing meaningful with another bare number.
    """
    return f"k:{key}" if own_scale or not unit else f"u:{unit}"


def scale_group_key(s, own_scale: bool = False) -> str:
    """group_key_for, for anything carrying a ``key`` and a ``unit``."""
    return group_key_for(s.key, s.unit, own_scale)
