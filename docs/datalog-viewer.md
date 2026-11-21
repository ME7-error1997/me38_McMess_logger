# The datalog viewer: where it came from, and where it differs

The viewer is a port. The original is the datalog viewer in **TunePortal**, a
Next.js portal for a tuning shop; its author wrote the design up as two files,
`datalog-viewer-notes.md` and `datalog-viewer-source.md`, for exactly this —
rebuilding it elsewhere. This file records what was carried over, what was
changed for a Qt desktop app, and which of those changes are judgement calls
someone might reasonably want back.

| There | Here |
|---|---|
| `src/lib/parse-log.ts` | [`mcmess/logparse.py`](../mcmess/logparse.py) |
| `src/lib/log-enums.ts` | [`mcmess/logenums.py`](../mcmess/logenums.py) |
| `log-chart.tsx` (canvas) | [`gui/logchart.py`](../gui/logchart.py) (pyqtgraph, shared with the logger) |
| `log-viewer.tsx` (React) | [`gui/window.py`](../gui/window.py) (Qt; also the logger) |
| `test/parse-log.test.ts`, `test/log-enums.test.ts` | [`tests/test_logparse.py`](../tests/test_logparse.py) |
| — | [`tests/test_viewer.py`](../tests/test_viewer.py) |
| — | [`gui/sources.py`](../gui/sources.py), [`tests/test_live.py`](../tests/test_live.py) |

The notes call the parser tests the acceptance suite: *"a port is right when the
same excerpts give the same columns, units and values."* They are ported one for
one, with the same real-file excerpts — VCDS measuring blocks, VCDS advanced,
ME7Logger, OP-COM with its `[SELINFO]` row and its `1.501` rpm — and they all
pass. Everything below is a difference from that baseline.

## 1. Parser: three additions, for this project's own logs

The parser is otherwise a line-for-line port.

**A leading `#` header line is dropped.** Our recorder writes five provenance
lines above the names row. The header block is the up-to-three non-blank lines
straight above the data, so without this, two of those `#` lines would be read as
column names. The original already drops a VCDS `Group A:` line and an OP-COM
`[SELINFO]` line for the same reason; this is a third member of that list, and
the dropped lines then show up where they belong, in the meta line above the
chart.

**`t_s` and `time_s` count as time-column names.** Our clock column is called
`t_s`. The original's fallback — a first column that only ever rises is the clock
— did find it, but called the channel `t_s` rather than `Time`.

**A generic CSV keeps six identification lines, not four.** We write five, and
the fifth is the DAMOS-inference caveat. In the live logger that caveat is
permanently in the status bar; in the viewer, which may be looking at a log
copied off another machine, the line in the file is the only place it exists.

One behaviour was *extended* rather than added: `default_selection` now takes its
engine-speed slot from `find_rpm_series`, which matches on the unit as well as
the name. Our channel is `N40` and VAG's German logs say `Drehzahl`; neither
reads as "rpm", but both carry `Upm` or `1/min`, and a suggested set with no rpm
trace in it is not worth drawing.

## 2. Code tables: our own ECU added, BMW kept

The original's tables are BMW MG1 as MHD logs it. They are kept verbatim — a
customer's MHD log opens here too, and the tables cost nothing.

Added: **`KLINFO`**, which this logger writes, and which is not a plain code. The
FR calls it *"Aktuelle Zylindernummer, klopfen ja/nein"*: the byte carries the
cylinder in its low bits and a knock flag in bit 7, so `130` is cylinder 2 *with
knock*. That needed a third kind beside the original's `value` and `flags` — a
`packed` kind, which unpacks with a function rather than a lookup. `KNK_CYL1..4`,
the demultiplexed flag, is an ordinary `value` table so the readout says *knock
detected* rather than `1`.

## 2a. One window: the viewer became the logger

Not in the original notes at all. The viewer ported here is now also the
**logger**: `gui/window.py` is one window that shows either a parsed log or the
recorder's live buffer, and the separate logger window and its stacked
small-multiples view are gone. `gui/viewer.py` is kept as a door into the same
window.

What made that possible is that the chart never knew what a parsed log was. It
takes a list of `Trace` (key, label, unit, colour, xs, ys, rows). The window gets
those from a *source* (`gui/sources.py`): `LogSource` wraps a `ParsedLog`,
`LiveSource` wraps the recorder's latest snapshot, and both answer the same five
questions — the channels, how many rows, one channel as points, every channel's
value on a row, a row's time. The channel panel, the stats table, the cursor and
the right-click menu are written once against that.

Three things were added for the live case:

**`follow_latest`, and it locks the view.** While data is arriving the cursor is
pinned to the newest sample and the mouse can do nothing: no click, no zoom, no
pan, no hover. The first version only ignored clicks, and hovering the chart
quietly swapped the live values in the panel for the ones under the mouse — old
numbers in the place where the live ones are read, with only a word in the
status bar to say so. Now the chart is the data's until the data stops.

**`update_traces`.** A fast path for "same channels, new points" that calls
`setData` and nothing else, because `draw()` tears down and rebuilds every
ViewBox and that cannot run twenty times a second. The second X axis is fed on
this path too; it was not at first, and its rpm labels went stale after the
first frame.

**A session ends by becoming a log.** On disconnect the window does not keep
showing the buffer: it re-reads the CSV that was being written and shows that,
so what is inspected is what was saved. Selection, colours, fixed ranges and the
visible stretch of time are carried across by channel *name*, because a live
key is the name and a parsed key is a column number. With no CSV (recording was
off, or it was a replay) the buffer is pushed through the same parser as text,
so the two cases cannot behave differently, and the window says it is unsaved.
A row index is only safe to hold once the data is static — a free cursor over a
sliding window would silently drift onto a different sample as old ones fall
out — which is why the cursor is the reader's only from that moment.

**What is logged and what is drawn are separate lists**, kept in a preset
(`mcmess/profile.py`). The original viewer only ever had "plotted"; a logger
also has "recorded but not worth a trace" — `KLINFO` is the case that forced it.

**Colours and Y ranges live in the preset too, per channel.** The original
remembered them per *file*, in browser storage, which is right for a viewer:
each log is its own thing. A logger makes a new file at every key-on, so
per-file memory means setting rpm to 0–8000 again every time. Here a channel
the preset knows takes its colour and range from the preset wherever it is
drawn — live, or in any log of it — and only channels the preset has never
heard of (a VCDS log's) fall back to per-file memory. Anything changed in the
window is written to the preset's file at once.

**One bug worth recording, because it hid for a long time.** Curves are added to
their ViewBoxes directly (section 3 explains why), so `PlotItem.clear()` has
never heard of them. Overlay ViewBoxes are thrown away on every redraw and took
their curves with them; the main ViewBox is never replaced, so the axis
channel's scale kept every curve it had ever been given. With one log on screen
the stale ones sat exactly under the fresh ones. It only showed once the logger
could reconnect: the previous session drawn through the new one. `_clear_groups`
now removes every curve by hand, and `tests/test_live.py` counts them.

**The honest caveat, which the original did not have to make.** A viewer is
opened deliberately; a live chart is glanced at. One plot with a scale per unit
is a multi-scale chart, and the vertical distance between two traces in
*different* units is an artefact of two independently fitted scales — it will
make unrelated traces look like they track each other. The comparison the chart
is built for is same-unit (actual against target). Across units, read the
values beside it. The stacked view that had exactly one scale per panel was the
remedy for this and has been removed with the old logger window; *Y: each on
its own* and the value column are what is left, and the README says so where
someone looking at the chart will see it.

## 3. Chart: pyqtgraph, as section 12 of the notes suggests

The original draws on a raw canvas, for reasons it states: tens of thousands of
points, channels whose ranges have nothing to do with each other, and exact
behaviours no charting library gives without a fight. Section 12 of the notes
maps those onto pyqtgraph, and that mapping is what is implemented.

**Scale groups are overlaid ViewBoxes, X-linked.** One per unit, as specified.
The one difference is structural: **the axis channel's group is always the main
ViewBox**. The original relinks the left axis to whichever group the reader
clicked; here the chart is rebuilt so that group *is* the main box, which keeps
the left axis, the mouse handling and the cursor's reference clock all pointing
at the same place without a web of relinking. The rebuild only happens when the
reader changes something, and the extracted points are cached per
(channel, x column), so it costs a few `setData` calls.

**Decimation is pyqtgraph's own `peak` mode** rather than a hand-rolled
per-pixel min/max, as section 12 says. Same guarantee: each pixel column's
extremes survive.

**Drag zooms, shift+drag pans.** pyqtgraph's default is the opposite, and the
default is wrong for reading a log — the first thing anyone does with a pull is
drag a box round the interesting four seconds. `ChartViewBox` swaps them. The
ViewBox is X-only (`setMouseEnabled(y=False)`), so a rubber band that also
rescaled Y cannot fight the refit.

**One pyqtgraph trap, written down because it cost time.** An overlay ViewBox
lives directly in the scene, not under the PlotItem. `ViewBox.addItem` calls
`scene.addItem(item)` *before* setting the item's parent, and at that moment
pyqtgraph resolves the item's "view" by walking parents, finds none, falls back
to the GraphicsLayoutWidget — and **caches it**. The next range change then asks
that widget for `autoRangeEnabled()` and raises `AttributeError`. The fix is one
line: parent the curve to `vb.childGroup` before calling `addItem`, so the cache
is right the first time.

## 4. Viewer: two deliberate differences

**The raw-data tab is a virtualised model, not 100-row pages.** `GridModel` is a
`QAbstractTableModel` over the whole grid; Qt only builds the visible rows
anyway, so paging would be work for nothing, and sorting can reorder the entire
log without a page boundary to reason about. The row header keeps the original
log row number, so a sorted view still says where each row came from.

**Geometry and per-file state live in `QSettings`, not browser storage.** Same
three things remembered per file — selection, colours, fixed ranges — and the
same pixel sizes remembered for all files.

**The hover tooltip is a `TextItem` on the chart** that lists every plotted
channel with its decoded meaning, as specified, and flips its anchor near an
edge. The channel panel still shows *all* channels at the hover row, as
specified, so the tooltip is a convenience for eyes already on the chart.

## 5. What was dropped, and why

**The server endpoint and the 25 MB limit** (section 9 of the notes) are a web
concern: the bytes came from an HTTP route that had to check who was asking. Here
the file comes from a file dialog. The size check survives in spirit as a
*question* at 64 MB rather than a refusal at 25 — a local file that big is
usually a stuck logger, but it is the reader's machine and their call.

**The draggable value/unit column widths** are plain `QHeaderView` sections,
which drag for free; the explicit min/max clamps and the double-click-to-reset
were not ported.

**The `too_large` / `binary` / `unparsed` / `failed` state machine** collapsed
into four message boxes, each saying one sentence about what is actually wrong.
`tests/test_viewer.py` checks that each path is reached and names the right
problem, because "could not open" sends the reader to look in the wrong place.

## 6. Colours

The palette's validated set is eight slots, in a fixed order that is **never
cycled**.

The chart is an overlay, where a reader may reasonably want twelve
traces, and neutral ink for traces nine onward would make them unreadable. So
`theme.SERIES_EXTENDED` adds eight more, taken from the reference viewer's own
sixteen. **Those eight are not part of the validated set** — the lightness band,
chroma floor and pairwise colour-vision separation were checked for the first
eight against the `#1a1a19` surface, and adding these was not re-validated
pairwise across all sixteen. They are legal here only because identity in the
viewer never rests on colour at all: every plotted channel is named beside its
swatch in the panel, named again in the stats table, and named in the tooltip. A
reader who cannot tell slot 11 from slot 14 has three places that say which is
which. Past sixteen, a trace still falls back to neutral ink.
