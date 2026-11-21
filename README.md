# McMESS logger — Bosch M3.8 (1.8T AGU)

Fast ECU RAM logging over the K-line, for tuning modified engines. Replaces KW1281
logging in VCDS, which tops out around 15–30 values/s.

One window does both jobs: it **logs** — samples and records — and it
**[reads a log back](#reading-a-log)**: yours, or a VCDS, ME7Logger, OP-COM or MHD log
from someone else.

**To just use it:** download `McMessLogger-<version>-win64.zip` from
[Releases](https://github.com/ME7-error1997/me38_McMess_logger/releases), unzip it
anywhere, and run `McMessLogger.exe`. Nothing to install; the `README.txt` in the folder
is the user's guide. Everything below is for running it from source, or
[building that zip yourself](#building-a-release).

Supports **06A906018CG** and **06A906018AQ**, one channel database each, with the
connected ECU checked against the loaded database before anything is logged — see
[ECU variants](#ecu-variants).

Runs from a **plain FTDI K-line cable** — no microcontroller adapter. The protocol needs
9-bit serial frames, which are produced by pinning the UART's parity bit:

```
bit9 = 0  ->  PARITY_SPACE
bit9 = 1  ->  PARITY_MARK
```

A 9N1 frame is bit-for-bit identical to 8 data bits + parity + 1 stop bit, so this is
exact, not a hack around the edges.

## Status

**Working against a real ECU.** First hardware session 2025-11-05: 06A906018CG on a
bench harness, genuine FTDI FT232R, ignition on and engine not running. The logs and
window screenshots from it are in [docs/bench](docs/bench/), so the numbers below can
be checked rather than taken on trust.

- Handshake connects first attempt at both 10417 and 125000 baud, ack `0x0C`
- 125000 baud verified correct against the slow link, 32/32 bytes
- The high-address latch persists — the assumption the throughput design rested on
- 16-bit word order confirmed on hardware (`FR` reads exactly 1.000)
- **810 bytes/s / 91 Hz** headless, zero errors in 8536 transfers

Two real bugs surfaced that the simulator could not: handshake retries fired inside
the responder's ~2 s re-arm window, and both CSV writers emitted `-0`. Both fixed.

Still unverified: the channel **addresses**. The conversions check out arithmetically,
but with bench sensors disconnected almost everything reads zero, so nothing yet
cross-checks an address against a known physical value. See
[bench results](docs/mcmess-018cg.md#6-bench-results-and-what-is-still-open).

## This ECU is not ME7

The widely circulated McMESS description covers ME7.5. The 018CG implements a much
smaller, **read-only** responder: no command set, no RAM writes, no Verstellsystem, no
EPK read, no crank-synchronous mode. It acks the handshake with `0x0C`, not `0x0D`, and
its responder polls every 1 ms rather than 20 ms. Full details and provenance tags in
[docs/mcmess-018cg.md](docs/mcmess-018cg.md).

Because there is no write path, this tool **cannot change anything in the ECU**.

## Expected throughput

K-line is half-duplex and the 80C196KR has no receive FIFO, so transfers are strictly
lock-step — one byte out, one byte back. Throughput is therefore set by round-trip
latency, not by baud rate.

| | |
|---|---|
| Protocol ceiling (1 ms responder tick) | ~1000 bytes/s |
| **Measured at 125000 baud** | **500–810 bytes/s** |
| Predicted before the bench run | ~240–480 bytes/s |

The real cable beat the estimate by about 2x — but only with the FTDI latency
timer at 1 ms. The same cable and ECU measured **64 bytes/s** at the 16 ms default,
a 12x difference. Set it first; nothing else in this project comes close.

Requesting highspeed (`0xA6`) is still worth it from a PC: it shortens each frame from
1.056 ms to 0.088 ms, removing ~2 ms per round trip.

At the measured ~800 bytes/s a 10-channel page-0 set samples at **~77 Hz**. In the
window, where drawing competes for the GIL, expect roughly **50 Hz** — see below.

The design leans on two properties of this responder:

- **Page `0x00` is free.** A bit9=0 byte sets the high-address latch *and* returns
  `mem[0x00nn]`, so page-0 channels cost one wire byte and zero parity switches. 34 of
  the 244 known channels live there, including RPM, load, throttle, ignition angle and
  both temperatures.
- **The latch persists**, so channels sharing a page cost one byte each after a single
  latch byte. This was the design's biggest assumption; `probe.py latch` **confirmed**
  it on hardware (15/15 agreement).

## The window

One window makes a log and reads one. **Double-click `log.bat`**: it opens on COM2,
connects with the preset you used last and starts recording — no typing, which matters
when the laptop is balanced on a wing. Pass a port to override: `log.bat COM5`.

```bash
.venv\Scripts\python.exe gui\main.py --port COM3              # log from the car
.venv\Scripts\python.exe gui\main.py logs\432.csv             # read a log
.venv\Scripts\python.exe gui\main.py --demo                   # simulated ECU
.venv\Scripts\python.exe gui\main.py --replay logs\432.csv    # a real pull, played back
```

**There is one rule.** *While data is arriving, the chart belongs to the data.* The
cursor sits on the newest sample and the values down the left are the live ones; the
mouse can not move the cursor, zoom, pan or hover, because each of those would put old
numbers on screen in the place where the live ones are read. *When it stops, the chart
belongs to you.* Disconnect — or lose the link — and the session is opened as the log
it has just become: the CSV that was being written, re-read from disk, so what you
inspect is exactly what was saved. Click to place the cursor, drag to zoom, read any
channel at any sample. Nothing else changes at that moment: same traces, same colours,
same Y scales, same stretch of time on screen. The status bar always says which sample
the values are from: *Live*, *Cursor*, *Mouse*, *First sample*.

**Freeze view** is the same bargain without dropping the link: the chart stops, the
cursor and zoom come free, sampling and recording carry on underneath, and releasing
it pins the cursor to the newest sample again.

### Presets

What a session logs, what it draws and how each channel looks is a **preset**. Pick
one from the **Preset** list on the toolbar and press **Connect** — Connect never asks
anything. Choosing channels is done once and kept; connecting is done at every key-on,
often with one hand.

**Channels…** edits the preset. Every channel has two ticks there, because logging and
drawing are different decisions:

- **log** — the channel is read from the ECU, written to the CSV, and has a row with
  its live value in the window. Log everything you might want afterwards: a channel you
  did not log is a pull you have to drive again.
- **graph** — it is also drawn. Draw only what you want to watch.

They differ on purpose. `KLINFO` has to be logged for the per-cylinder knock columns to
exist, and drawn it is a cylinder counter stepping 0-1-2-3 forty times a second — noise
that flattens every trace sharing a chart with it. So ticking **log** on a quantity
ticks **graph** too, and ticking it on a code or a status byte does not; either can be
changed by hand. The window shows what the selection costs per sample and the sample
rate it implies, counting the channels a computed column needs even though nobody
ticked them. **Save as…** there makes a new preset; it then appears in the toolbar
list.

The channel set can only be edited while disconnected, because changing it mid-session
reallocates the buffer and changes the CSV's columns — which is how a pull ends up
split across two files. One session is one file with one set of columns. What is
*drawn* can be changed at any time: tick a row in the left panel.

**Double-click a channel in the left panel for its colour and Y range.** Pick a colour
from the palette or any other; leave the range on *Auto* (fit what is on screen) or fix
it — type the two numbers, or take a common one (0–8000 rpm, 0–10 ms/Umdr., …). A fixed
range matters more live than in a log: a log is static, so an auto-fitted scale
settles, but a sliding window refits every sample and the scale breathes continuously.
Channels that share a unit share a scale, so a range given to `TL` is `LDSHK`'s too,
and the window says so before it moves them.

**The preset remembers all of it, by itself.** A tick, a colour, a range: each is
written to the preset's file as it is made, and the next session — or a log opened a
month later — is drawn the same way. There is no save step to forget. A preset is a
small JSON file in `profiles/`:

```json
{
  "format": "mcmess-profile/1",
  "ecu": "06A906018CG",
  "log":   ["N40", "TL", "LDSHK", "KR_CYL1", "..."],
  "graph": ["N40", "TL", "LDSHK", "KR_CYL1"],
  "style": {
    "N40": {"color": "#3987e5", "range": [0, 8000]},
    "TL":  {"range": [0, 10]}
  }
}
```

The program starts with the last preset used, else `profiles/tuning.json`;
`--profile path` names one, and `--channels N40,TL,FR` skips presets for a bare list.
A preset naming a channel this ECU's database does not have connects without it and
says so; one saved for another ECU is flagged in the channel window before you trust
it. Reading somebody else's log — VCDS, say — colours and ranges are kept with that
log file instead, since its channels are not ones a preset will ever see again.

### What is on screen

**Left: every logged channel**, drawn or not — tick, name, value, unit, and what the
channel is (`TMOT` — *Engine coolant temperature*). The descriptions come from the
ECU's channel database, so they are there live and when the log is opened a month
later. Reading a log from another tool, where the database knows none of the names,
the column is hidden rather than left blank.

**Right: the chart**, with a table under it of each drawn channel's value at the
cursor and its min, max and mean over the visible window. Channels that share a unit
share a Y scale, which is the whole point: `TL` lands against `LDSHK` and you can see
the gap rather than comparing two numbers.

> **The scales across units are independent, so vertical distance between two traces
> in different units means nothing.** One plot showing rpm, degrees and milliseconds
> has one scale per unit, each auto-fitted to the visible window — that is a
> multi-scale chart, and a multi-scale chart will happily make two unrelated traces
> look like they track each other. Only two of those scales get an axis (left and
> right); the rest are drawn with no axis at all, which is why the values are always
> beside it. The comparison this chart is built for is **same-unit**: actual against
> target, this cylinder's retard against that one's. For anything across units, read
> the numbers.

Right-click a channel, on the chart or in either table, for the same colour and range
choices as a double click, and to give it the left axis. **Auto ranges** drops every
fixed range at once.

**Open log…** while connected opens the log in a second window, so an earlier pull can
sit beside the live one.

### Replay

`--replay logs\432.csv` adds the log to the port list. Connecting plays it back sample
by sample at the pace it was recorded (`--replay-speed 4` to hurry it), through the
same interface the recorder has, so the window cannot tell the difference: the cursor
is pinned, the view is locked, and when the log runs out the session ends and
everything unlocks. The simulator proves the protocol; it does not look like an engine.
This is how to see what the live window does with real boost onset and real knock
retard without a car. Nothing is written — a replay that recorded would leave a second
file claiming to be a pull that was never driven.

### Flags

`--knock-base 0` for this ECU's cylinder numbering, `--connect` to connect as soon as
the window is up (what `log.bat` does), `--select` to open the channel window first,
`--no-autorecord` to wait for the Record button instead of recording from the moment
the link comes up, `--database` to point at a different channel JSON, `--tag pull2` to
name the log.

**Drawing costs sample rate, so the defaults favour rate.** Painting antialiased
traces holds the GIL and measurably starves the sampling thread — on the bench,
the same six channels ran at 27 Hz antialiased against 56 Hz without. Antialiasing
is therefore off; `--smooth` turns it back on if you want a prettier screenshot and
do not mind halving the rate. `--opengl` is faster again (~67 Hz) but driver-
dependent, so it is opt-in — in the captured run it hit 73 Hz with an **empty plot
area**, so check the traces actually draw before trusting a log taken with it. Full
measurements in [section 8.4](docs/mcmess-018cg.md#84-drawing-competes-with-sampling);
the four window captures behind them, including the blank OpenGL one, are in
[docs/bench](docs/bench/). The value column and the stats repaint five times a second,
not twenty: at the full redraw rate they cost about a fifth of the sample rate, and
numbers changing that fast are unreadable anyway.

Colours come from a validated 8-slot categorical palette, checked against the dark
surface for lightness, chroma, colour-vision separation and contrast. The order is
fixed and **never cycled**. The chart runs to 16 slots because twelve traces on one
plot need them, and identity never rests on colour: every drawn channel is named
beside its swatch in the panel, in the table under the chart, and in the hover
tooltip. See the note on `SERIES_EXTENDED` in `gui/theme.py`.

**Threading.** The serial path is blocking and lock-step, and the ECU drops the session
after 2 s of silence — so sampling runs on its own thread in `mcmess/recorder.py` and
the window only reads buffered snapshots on a timer. That engine is Qt-free and tested
headlessly against the simulator, so the GUI is only responsible for drawing.

## Reading a log

**Double-click `view.bat`** and the window opens on the newest log in `logs/` — almost
always the pull you have just driven. Drag a CSV onto it, or pass one: `view.bat
logs\432.csv`. It is the same window as the logger, with nothing connected.

**Nothing logged yet?** [examples/018CG-full-throttle-pull.csv](examples/018CG-full-throttle-pull.csv)
is a real one, recorded with this program on 2026-11-21: 45 seconds on a tuned
06A906018CG, wide open to 7360 rpm, with knock retard down to −3° from about 5000 rpm up, so the
per-cylinder knock columns have something to show. `view.bat
examples\018CG-full-throttle-pull.csv` opens it; `replay.bat` with the same path plays
it back as if the car were connected. It ships in the release too, in `examples\`.

It is not limited to this logger's own CSVs. It reads **VCDS** measuring blocks and
advanced measuring blocks, **ME7Logger**, **OP-COM**, and generic CSV from MHD,
bootmod3 or anything else that writes text — so a customer's log opens in the same
window as yours. `mcmess/logparse.py` does the reading and has no UI imports;
`tests/test_logparse.py` is its acceptance suite, run against excerpts of real files
from each tool.

**It looks for the data, not for a header row.** None of these files is "names then
numbers": they open with identification lines, VCDS splits a name over two rows with
the units in a third, and OP-COM writes `Active` and `Not Programmed` in half the
cells of every row. So the parser finds the first two lines whose *numbers are in the
same columns* — that is the data — reads the up-to-three lines above it as the
header, and only then asks which tool wrote the file. That one rule is what made
OP-COM and VCDS work without a special case each.

**Channels that share a unit share a Y scale.** Boost actual and boost requested land
on the same axis, which is how you see that actual never reaches target. A channel
with no unit gets its own scale — a bare number shares nothing meaningful with
another bare number — and *Y: each on its own* gives every channel one. Y is fitted
to **what is visible in the current zoom**, not to the whole log, or zooming into a
pull leaves the trace flat at the bottom of a scale set by a rev limiter three
minutes earlier. Right-click a channel for a fixed range instead (presets for boost,
load, air flow, rpm, lambda, timing, knock retard) or for its colour.

**The cursor is a log row, not a time.** Click to place it. That one choice makes it
work in time mode, in scatter mode, and across VCDS logs where every group has its
own clock, and it survives a change of X axis. `active row = mouse, else cursor, else
the first sample`, and the status bar says which — a number on screen is never
ambiguous about where it came from.

**Every channel shows a value, plotted or not.** The panel reads the grid directly,
so a 150-channel log costs 150 row lookups on a mouse move rather than 150 arrays,
and you can scan the whole list at one instant of a pull without plotting anything. A
channel with no sample on that row takes its nearest one within 25 rows, which is
what makes ragged VCDS logs readable.

| | |
|---|---|
| drag | zoom to the dragged span |
| shift + drag | pan |
| wheel | zoom at the mouse |
| click | place the cursor |
| double click | reset the zoom |
| right click | colour, Y range, axis channel |

**X can be any channel**, which turns the plot into a scatter: boost against rpm, or
timing against load. In time mode a second row of labels along the top reads rpm at
each tick. The plot stays against time deliberately — rpm falls on every gear change,
so it is not a scale, only labels that follow the trace.

**Decimation keeps the peaks.** pyqtgraph draws each pixel column's min and max, so a
single-sample knock event survives; plain point-skipping loses exactly the events you
opened the log to find. When a log is sparse instead — a 6 Hz VCDS log, zoomed in —
every sample gets a small square, so you can see where the samples really are rather
than reading meaning into straight interpolation.

**Coded channels say what they mean.** `KLINFO` reads as *cylinder 2 (raw), knock
detected* rather than `130`; a BMW MG1 log's `RF Max Index 5` reads as *intake
temperature, knock or driving-behaviour retard*. The tables are in
`mcmess/logenums.py`; another ECU family is one more table and one more name pattern.

The **Table** tab is the raw grid, every column sortable; the sort reorders the whole
log and the row header still says which log row each row was. Clicking a row places
the cursor on it.

The chart runs past the logger's eight validated hues, up to sixteen — an overlay
chart of twelve traces needs them, and identity never rests on colour here: every
plotted channel is named beside its swatch in the panel, in the stats table under the
chart, and in the hover tooltip. See the note on `SERIES_EXTENDED` in `gui/theme.py`.

Where it came from, and where it differs, is in
[docs/datalog-viewer.md](docs/datalog-viewer.md).

## Setup

```bash
py -3.14 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Set the FTDI latency timer to **1 ms** before anything else. This is not a tuning
tweak — it is measured, on this cable and this ECU, as a **12x** difference:
64 bytes/s at the 16 ms default against 518-810 bytes/s at 1 ms. Nothing else in
this project comes close to mattering as much.

```
Device Manager -> Ports (COM & LPT) -> cable -> Properties
  -> Port Settings -> Advanced -> Latency Timer (msec) -> 1
```

## Building a release

**Double-click `build_release.bat`.** It produces
`dist\McMessLogger-<version>-win64.zip`: one folder with `McMessLogger.exe` in it,
which runs on a Windows 10 or 11 machine with nothing installed. The version is
`__version__` in `mcmess/__init__.py`; bump it before building.

```bash
.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.venv\Scripts\python.exe tools\build_release.py
```

What is in the folder: the exe, `Log.bat` (connect on a port at once),
`profiles\tuning.json`, `examples\` with a real log, a `README.txt` written for somebody who has never seen this
repository, the licence, and `_internal\` with Python, Qt and the program. Presets and
logs live beside the exe, not inside `_internal\` (`mcmess/paths.py`), so a newer
version unzipped elsewhere cannot eat a preset somebody spent an afternoon on.

**The build runs what it built.** A PyInstaller bundle that builds is not a bundle that
starts — a missing data file only shows when the exe is launched. So the script starts
the exe against the simulated ECU, lets it connect, record and disconnect, and checks
that it drew, that a CSV landed in the `logs` folder beside the exe rather than in the
working directory, and that the shipped preset is untouched. A logger window opens and
closes by itself for a few seconds; that is the check. If any of it fails there is no
zip.

**It is packaged, not compiled.** The program is in the bundle as Python bytecode. That
is not readable in an editor, and it keeps a recipient from needing Python, but anyone
determined can turn it back into something close to the source. If the code has to stay
secret, this is not the tool; a real compiler such as Nuitka is.

It is one folder rather than a single self-extracting exe on purpose: a single exe
unpacks itself to a temp directory at every start, which costs seconds each time and
trips more antivirus heuristics, and Qt for Python is LGPL, which expects the Qt
libraries to stay replaceable — in a folder they are plain DLLs. `THIRD-PARTY.txt` in
the release lists what is inside and under which licence. The exe is not signed, so
Windows SmartScreen will warn the first time it is run.

## Bench bring-up

Run in this order. Each step answers a `[VERIFY]` item in the spec. All of these
have been run against a real 018CG — see
[bench results](docs/mcmess-018cg.md#6-bench-results-and-what-is-still-open) for what
they returned. Start with `latency`: a wrong latency timer makes every later number
meaningless.

```bash
.venv\Scripts\python.exe tools\probe.py ports
```

```bash
.venv\Scripts\python.exe tools\probe.py latency
```

```bash
.venv\Scripts\python.exe tools\probe.py connect --port COM3
```

Every highspeed connect automatically checks that 125 kBd returns *correct* bytes, not
merely some bytes — a cable that can't slew that fast keeps answering with corrupted
data rather than going quiet. For the strict version:

```bash
.venv\Scripts\python.exe tools\probe.py connect --port COM3 --cross-check
```

```bash
.venv\Scripts\python.exe tools\probe.py timing --port COM3
```

```bash
.venv\Scripts\python.exe tools\probe.py latch --port COM3
```

```bash
.venv\Scripts\python.exe tools\probe.py word --port COM3 --channel FR
```

`word` confirms the one remaining 16-bit assumption: `FR` is the lambda integrator and
`FAK2L` puts 1.00 at `0x8000`, so at idle the raw word should read close to `0x8000`.

```bash
.venv\Scripts\python.exe tools\probe.py watch --port COM3 --csv logs\run1.csv
```

Add `--slow` to any of these to stay at 10417 baud — the first thing to try if 125000
misbehaves, since cheap cable transceivers may not slew fast enough.

Offline, with no cable or ECU:

```bash
.venv\Scripts\python.exe tests\test_program.py
```

```bash
.venv\Scripts\python.exe tools\probe.py channels --page 0x00
```

```bash
.venv\Scripts\python.exe tests\test_session.py
```

```bash
.venv\Scripts\python.exe tests\test_ramscan.py
```

## Finding RAM for a patch

A code patch needs somewhere to keep its variables, and picking a RAM cell by eye
is how you get an intermittent fault that reads like a sensor problem.

```bash
.venv\Scripts\python.exe tools\ramscan.py --need 6
```

It decodes every MCS-96 memory operand in the image, resolves the ones that carry
an address, and ranks free cells by how deep inside an unreferenced run they sit —
depth rather than mere absence, because the access modes it cannot see reach
contiguous structures.

**It cannot prove a cell is unused, and the two error directions are not
symmetric.** A false positive costs you a candidate. A cell reached only through
indirect addressing looks free and is not, and that failure is silent. The report
prints the size of its own blind spot so you can weigh it: on the 018CG, ~3000
indirect operands and ~2800 auto-increment ones against ~6100 it did resolve.

It does clear two hazards that carry no address at all. The stack initialises to
`0x0500`, and WSR windowing reaches only `0x1F7F` because the window base is a
six-bit field — so neither can touch `0xC000-0xCFFF`, which is why register-direct
operands can be ignored for that window. Treat the output as a shortlist, then
verify on the car.

## Layout

| Path | What |
|---|---|
| `docs/mcmess-018cg.md` | Protocol spec, every claim tagged `[RE]` / `[ME7]` / `[INF]` / `[VERIFY]` |
| `docs/datalog-viewer.md` | Where the viewer was ported from, and where it differs |
| `mcmess/transport.py` | 9-bit frames via parity, init pulse, echo handling, timing |
| `mcmess/session.py` | Handshake, latch-aware reads, read scheduler |
| `mcmess/channels.py` | Channels and physical conversions |
| `mcmess/identify.py` | Checks the connected ECU against the loaded database |
| `mcmess/wideband.py` | Second input on its own port: decoders, source thread |
| `mcmess/derived.py` | Columns computed from others: knock demux, g/s air mass |
| `mcmess/recorder.py` | Sampling thread, ring buffer, CSV — Qt-free |
| `mcmess/simulator.py` | Simulated 018CG responder + engine model |
| `mcmess/data/m38_018cg.json` | Generated channel + conversion database, one per ECU |
| `mcmess/data/descriptions_en.json` | English description for every channel |
| `mcmess/logparse.py` | Reads a datalog: VCDS, ME7Logger, OP-COM, any CSV. No UI |
| `mcmess/logenums.py` | Channels that log a code: KLINFO, BMW MG1 limiters |
| `mcmess/profile.py` | Presets: what is logged, what is drawn, each channel's colour and Y range |
| `mcmess/replay.py` | Plays a recorded log back through the recorder's interface |
| `mcmess/paths.py` | Where presets and logs live: the repo from source, beside the exe in a release |
| `profiles/tuning.json` | The preset the program starts with |
| `examples/` | A real log from the car, to open or replay with nothing connected |
| `gui/main.py` | The command line |
| `gui/window.py` | The window: logger and log viewer in one |
| `gui/sources.py` | What the window shows: a parsed log, or the live buffer |
| `gui/picker.py` | The channel window: log and graph ticks, cost estimate, presets |
| `gui/style.py` | The colour and Y range window a double click opens |
| `gui/theme.py` | Validated dark palette and Qt styling |
| `gui/viewer.py` | The old viewer entry point; opens the same window |
| `gui/logchart.py` | The chart: scale groups, row cursor, second X axis, live lock |
| `log.bat` | Double-click launcher: connect with the last preset and record |
| `view.bat` | Double-click launcher: opens the newest log |
| `replay.bat` | Double-click launcher: plays the newest log back as if live |
| `build_release.bat` | Double-click launcher: builds the release zip |
| `tools/build_release.py` | Builds, prunes, runs and zips the release |
| `tools/release/` | What goes beside the exe: the user's README, `Log.bat`, licence notes |
| `tools/probe.py` | Bench bring-up and measurement |
| `tools/build_database.py` | Rebuilds the database from the RE files |
| `tools/ramscan.py` | Finds RAM the firmware never touches, for patch variables |
| `tests/test_program.py` | Scheduler, conversions and channel-database checks |
| `tests/test_session.py` | Session behaviour against the simulator |
| `tests/test_recorder.py` | Sampling engine, ring buffer, CSV, engine model |
| `tests/test_ramscan.py` | MCS-96 operand decoding in the RAM scanner |
| `tests/test_wideband.py` | Decoders, staleness, and the join onto the ECU clock |
| `tests/test_derived.py` | Knock demultiplexer and derived-column plumbing |
| `tests/test_logparse.py` | The datalog parser, against excerpts of real files |
| `tests/test_viewer.py` | Reading a log: the window and chart, on Qt's offscreen platform |
| `tests/test_live.py` | Logging: who owns the cursor, and what a session becomes |
| `tests/test_profile.py` | Presets, the channel window, the colour window, and replay |

## Logging a wideband

AFR goes in the same CSV, on the same timestamps, read from the controller's **own
serial port** rather than through the ECU.

Not through the ECU because there is nowhere to put it. The 06A906018 connector has
exactly one lambda input — pin 26, `LS1`, pre-cat — and **no rear-O2 pin at all**:
the Europe variant leaves the post-cat sensor unfitted, so the usual trick of
hijacking an unused narrowband input has no cavity to hijack. The only full-scale
0–5 V input is pin 61, the ambient pressure sensor, which is in use; driving it
corrupts `fho`/`fdkha`, the altitude correction, and changes fuelling while you are
trying to measure it.

Reading the controller directly is better anyway: it costs **zero K-line bandwidth**,
so the whole ~800 byte/s budget stays available for ECU channels, it sets no DTCs,
it cannot affect how the engine runs, and it keeps the wideband's full resolution.

```bash
.venv\Scripts\python.exe tools\probe.py watch --port COM3 --wb-port COM5 --csv logs\pull.csv
```

```bash
.venv\Scripts\python.exe gui\main.py --port COM3 --wb-port COM5
```

The AFR gets its own panel in the window and its own column in the log.

## Logs name themselves

**Recording starts on connect.** You do not press anything. The moment the link
comes up a CSV is open and rows are going into it, and the Record button is already
showing *Stop recording*. A pull you forgot to press Record for is a pull you have
to drive again; the only cost of a log you did not want is a small file, and since
logs name themselves nothing is ever overwritten. `--no-autorecord` waits for the
button instead.

The name cannot collide with the last one:

```
logs/2026-11-19_12-34-21_018CG_pull2.csv
```

Date first so a directory listing is chronological, time to the second, then the
ECU and an optional `--tag`. The window's status bar shows the filename beside the
row count, so you can tell which of several logs is the live one.

The previous default was a fixed `logs/run.csv` behind a save dialog — accept it
twice and the second pull quietly overwrote the first. Losing a pull you cannot
easily repeat is worse than an ugly filename, so a name is never reused: two starts
inside one second get `_2`, `_3`.

```bash
.venv\Scripts\python.exe gui\main.py --port COM2 --tag pull2
```

`--log-dir` puts them elsewhere, and `--ask-filename` brings the save dialog back.
On `probe.py watch`, `--csv` with no value does the same thing, and `--csv somedir/`
auto-names inside that directory:

```bash
.venv\Scripts\python.exe tools\probe.py watch --port COM2 --csv --tag pull2
```


**Look at the device first.** Before trusting any decode:

```bash
.venv\Scripts\python.exe tools\probe.py wideband --wb-port COM5 --seconds 5
```

That dumps what actually arrives, says whether it is ASCII or binary, and reports
what the chosen decoder made of it — min, mean, max and rate.

### Decoders

| `--wb-decoder` | For |
|---|---|
| `ascii` (default) | One reading per line. Handles `14.72`, `AFR 14.72` and `14.72,0.98,1` alike |
| `linear` | Fixed-width unsigned binary samples, no framing |

Map whatever the device sends onto the unit you want with `--wb-scale` and
`--wb-offset`. For a USB ADC or Arduino bridging a 0–5 V analog output, with the
controller set to 10–20 AFR across that range and the ADC printing 0–1023:

```bash
--wb-decoder ascii --wb-scale 0.009775 --wb-offset 10 --wb-range 9,21
```

`--wb-range` is a sanity filter, not decoration: a half-received line decodes to
*something*, and dropping anything outside a believable AFR window is what stops
that something reaching the log.

**Binary controller protocols are deliberately not built in.** Innovate ISP2,
Zeitronix and AEM all have one, but none has been verified against a real device
here, and a decoder that mis-frames reports a confident wrong AFR instead of
failing — the same class of bug as the wrong channel database. Run
`probe.py wideband --wb-port ...` on yours; the dump makes the framing obvious and
a decoder for it is a small addition.

### How the two streams are joined

The ECU sampler is the clock. Each time it finishes a sample it takes the
wideband's most recent reading and writes it into the same row — a zero-order
hold, no interpolation. Interpolating would invent values between two measurements
of something that moves fast and matters a lot; a held value is at least a number
the sensor really produced.

Past `--wb-max-age` (default 0.25 s) the column goes **blank instead of holding**.
A controller that has stopped sending — unplugged, still warming up, in free-air
calibration — would otherwise log a flat, entirely plausible AFR for as long as you
keep driving. An empty cell is obviously missing data; a frozen one is not.

The aux column costs nothing on the wire, which the picker's cost line shows: three
ECU channels plus a wideband still reads `3 channels, 3 bytes`.

## Derived columns

Two things the ECU does not hand you directly. Ask for them by name in `--channels`
like any other channel; the inputs they need are selected automatically, and the
columns themselves cost **nothing** on the K-line.

| Column | From | |
|---|---|---|
| `ML_GS` | `ML` | air mass in g/s rather than kg/h |
| `KR_CYL1`..`KR_CYL4` | `KLINFO` + `DWKR_Z` | per-cylinder knock retard |

### Why per-cylinder knock has to be reconstructed

**M3.8 has no `DWKR1..4`.** Knock control works one cylinder at a time and publishes
a pair: `DWKR_Z` is the retard of whichever cylinder it is processing right now, and
`KLINFO` says which one that is. Logged raw, `DWKR_Z` jumps between cylinders and is
close to unreadable. The demultiplexer routes each sample to the right column and
holds the other three at their last known value — retard persists between a
cylinder's turns, so holding shows the current state rather than a comb.

**The `KLINFO` encoding is not documented.** The FR calls it *"Akt. Zyl.Nr. der
Klopfreg."* with the plain `DEZ` conversion, which leaves open whether it counts
`1..4` or `0..3`. Guessing would silently relabel every cylinder, so `--knock-base
auto` (the default) waits for proof instead: an observed `0` proves 0-based, an
observed `4` proves 1-based, and neither can occur under the other convention. Until
one shows up the columns report blank — a fraction of a second at idle. The run
prints what it settled on:

```
KLINFO reads 0..3 (values seen: [0, 1, 2, 3])
```

### KLINFO is not only a cylinder number

A car pull caught `KLINFO` reading **0x82** — bit 7 set over cylinder 2. The FR calls
the variable *"Aktuelle Zylindernummer, klopfen ja/nein"*: the byte holds the cylinder
**and** whether that cylinder knocked. Bit 7 is the indication `[INF]`.

That matters because the demultiplexer used to read the whole byte as a number, so a
flagged sample looked out of range and was discarded — the one sample you least want
to lose. It is now stripped and routed, and the flag is available as its own columns:

| Column | |
|---|---|
| `KR_CYL1`..`4` | retard held per cylinder |
| `KNK_CYL1`..`4` | 1 when that cylinder knocked at the last knock-control event |

Only bit 7 is stripped. Masking to three bits would turn a bogus 9 into cylinder 1
and file a reading under the wrong cylinder, so anything still out of range is
counted as unmapped rather than guessed at. The run reports what it caught:

```
KLINFO reads 0..3 (values seen: [0, 1, 2, 3, 130]); knock flag caught on cyl3x1
```

Sampling at tens of Hz against knock control running per combustion, the flag is
caught only occasionally — it is a spot check, not a knock count.

**That is the answer, measured on a running 018CG:** `KLINFO` counts cylinders from
zero. So `--knock-base 0` skips the wait on this ECU. Values outside the resolved
range are dropped rather than written to a cylinder, and counted in that same line.

### Page grouping costs more than you think

A parity switch is not free. Flipping the 9th bit means draining and reprogramming
the UART between frames, and measured on a running car it costs **about one
byte-time of round trip**. Three runs, predicted against actual:

| Selection | wire bytes | switches | actual | bytes only | bytes + switches |
|---|---|---|---|---|---|
| 10 channels, 2 pages | 12.1 | 2 | 26.2 ms | 22.6 ms | 26.3 ms |
| 9 channels, 2 pages | 11.0 | 2 | 23.0 ms | 19.7 ms | 23.3 ms |
| 22 channels, 5 pages | 25.2 | 8 | 53.8 ms | 39.5 ms | **52.1 ms** |

Charging only for bytes is 27% optimistic on the five-page set — the difference
between the picker promising 26 Hz and the car giving 18.6 — so the estimate now
charges one byte-time per switch as well. The practical consequence: **a channel
added inside a page you already read is nearly free, and one that opens a new page
costs roughly two bytes, not one.** Page `0x00` is cheapest of all: no latch byte
and no parity switch at all.

## ECU variants

**The M3.8 family shares a channel list but not a RAM layout.** Comparing the two
images on hand, **121 of the 244 shared channels sit at different addresses** in
`06A906018AQ` than in `06A906018CG`. The whole `0xC9xx`–`0xCFxx` region is shifted.

Page 0 happens to agree exactly — all 22 channels, including rpm, load, throttle,
both temperatures and battery — so the fast starter set reads the same either way.
Everything above it does not.

That makes using the wrong database the most dangerous mistake available here,
because **it does not fail**. The handshake succeeds, every read is answered, no
timeout fires; you simply get the neighbouring RAM cell, converted through the right
formula into a number in an entirely believable range. A log like that is fiction
you cannot spot by looking at it.

So the ECU is asked who it is. Every 018 image carries its part number as ASCII at
`0xBF0A`, and the responder serves any address — below `0xC000` that address space
is flash — so identifying the ECU is an 11-byte read over the same link that does
the logging. On a mismatch the tools stop rather than warn:

```
ECU IDENTIFIES AS 06A906018AQ, but the loaded database is for 06A906018CG.
Channel addresses differ across the M3.8 family, so logging now would read the
wrong RAM cells and still look plausible.
Use --database .../m38_018aq.json instead.
```

Pick a database by part number — `--database 06A906018AQ` — or by path. Both the
window and `probe.py` print which one is loaded, and the window keeps it in the
status bar. `--skip-id-check` exists for a variant that stores its part number
elsewhere; it is not for getting past a real mismatch.

### Adding another ECU

Point the builder at its binary. Nothing else is needed — the part number and the
VS20 table offset are both found in the image:

```bash
.venv\Scripts\python.exe tools\build_database.py --image path\to\that.bin
```

It writes `mcmess/data/m38_<suffix>.json`, which the logger then discovers on its
own. The VS20 table is located by a structural property rather than a signature
scan: the FR lists every 16-bit channel as a pair, `X` and `X+1`, and only at the
true offset does each `+1` entry resolve to exactly one past its base. Both known
images score 12/12 there and at most 7/12 at any other offset.

Channels that came out of Ghidra rather than the VS20 table carry an `ecus` list in
`mcmess/data/extra_channels.json` naming the images their addresses were read from,
and are skipped for any other ECU. There is no way to carry a hand-found `0xCAxx`
address to another variant safely.

## Channels and conversions

**018CG: 249 channels, 229 in physical units. 018AQ: 244 and 226** — the difference
is the five hand-found channels, which are pinned to the CG image they were read
from. Each database is built from the reverse-engineering files into one
self-contained JSON:

```bash
.venv\Scripts\python.exe tools\build_database.py --report
```

All 222 DAMOS conversions parse and **every one reproduces its own declared min/max**.
The independently-known scalings come out right: `NQ40` = 40 rpm/bit, `TEMPQ` =
0.75·raw − 48 °C, `UQ5` = 19.6078 mV/bit (255 → 5000 mV exactly), `VQ` = 1.28 km/h,
`FAK2L` = raw/32768 so `0x8000` = 1.00.

Four things worth knowing before you trust a log — all detailed in
[the spec](docs/mcmess-018cg.md#7-channels-and-conversions):

- **Provenance.** There is no DAMOS for the 018CG. Formulas come from `8D0907558E` (Audi
  A4, M3.8.2); the channel list and addresses are the 018CG's own. Names are shared
  across the M3.8 family so the formulas should apply, but it is an inference.
  `probe.py channels --provenance`.
- **16-bit channels are resolved**, not guessed. An FR name ending in `+1` points at the
  *second* byte of its word — confirmed 35/35 and 45/45 against the damos's own
  addresses. Only the little-endian assumption remains, and that is the CPU.
- **18 channels report raw**, either because the conversion they name is absent from the
  A4 damos or because the entry holds two packed bytes rather than one word.
  `probe.py channels --raw-only`.
- **5 channels are not from the VS20 table at all.** The table names channels; it does
  not limit them, because the responder serves any RAM address. `XKFPU`, `MLROH`,
  `TLR_16`, `XKFPUSL` and `XNDIV` were read out of the load-path code in Ghidra and live in
  [`mcmess/data/extra_channels.json`](mcmess/data/extra_channels.json), which the
  builder merges. Each carries a `source` naming the instruction its address came
  from. Their scalings are argued from the code's fixed-point arithmetic, not taken
  from a DAMOS entry, and none has been checked against a running engine.

## Not done yet

- **The channel addresses are still unconfirmed.** The protocol, the latch, the baud
  change and the 16-bit assembly are all verified on hardware, and every conversion
  reproduces its declared range — but a conversion being right does not prove the
  address feeding it is. With bench sensors disconnected almost everything reads
  zero. The way to settle it is a running engine, or VCDS side by side on a handful
  of channels.
- **Not tested with the engine running.** Whether a session can be started, and held,
  with the engine turning is open.
- **The 018AQ database has never seen an 018AQ.** It is generated from that ECU's own
  image by the same code path that produced the CG one, and the VS20 offset it found
  scores a perfect 12/12 on the word-pair test — but no 018AQ has been on the end of
  a cable. The identity check means a wrong-database log is not a risk; an 018AQ that
  does not answer at all, or answers differently, has simply not been ruled out.
- **No channel-set presets.** `--channels` takes a list on the command line, but there
  is no save/load of named sets in the window yet.

## Source material

The channel database in `mcmess/data/m38_018cg.json` is **generated** from the files
below, and the generated JSON is committed, so you do not need any of them to run the
logger. You need them only to re-run `tools/build_database.py`.

They are not in this repository and will not be: they are Bosch and VAG material.
Supply your own copies.

| File | What it provides |
|---|---|
| `analysis/mcmess_018CG.md` | Ghidra findings on the 018CG responder |
| `Damoses/8D0907558E.DAM` | Audi A4 M3.8.2 damos — the conversion formulas |
| `Damoses/WinOLS(VW Golf ...).bin` | The 018CG image — channel list and VS20 table |
| `FR/30O.FRM` | Bosch function framework — pinouts, A/D map, function definitions |
| `mcmess protocol.txt` | The circulating ME7.5 description. Background only |

Point the builder at your copies with `M38_RE_ROOT`, or override each path:

```bash
M38_RE_ROOT="/path/to/M3.8" .venv/Scripts/python.exe tools/build_database.py --report
```

```bash
.venv\Scripts\python.exe tools\build_database.py --dam PATH --fr PATH --image PATH
```

The layout under `M38_RE_ROOT` is the one in the table. The A4 image is also used, at
`--damos-image`, purely to re-verify the 16-bit byte-order rule at build time.

## Licence

MIT — see [LICENSE](LICENSE).

The licence covers the code in this repository. It does not cover the Bosch and VAG
source material above, which is not distributed here, and it is not a claim over the
reverse-engineered facts the generated database records.
