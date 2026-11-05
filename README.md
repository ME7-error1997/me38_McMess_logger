# McMESS logger — Bosch M3.8 (06A906018CG / 1.8T AGU)

Fast ECU RAM logging over the K-line, for tuning modified engines. Replaces KW1281
logging in VCDS, which tops out around 15–30 values/s.

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

## The logger window

```bash
.venv\Scripts\python.exe gui\main.py --demo
```

`--demo` runs against a simulated ECU with a running engine model — a 40 s cycle of
idle, a pull to redline and overrun — at an emulated ~300 bytes/s, so the window
behaves like the real thing with no hardware attached. Against a real cable:

```bash
.venv\Scripts\python.exe gui\main.py --port COM3
```

Useful flags: `--channels N40,TL,FR` to preselect a set, `--database` to point at a
different channel JSON.

**Drawing costs sample rate, so the defaults favour rate.** Painting antialiased
traces holds the GIL and measurably starves the sampling thread — on the bench,
the same six channels ran at 27 Hz antialiased against 56 Hz without. Antialiasing
is therefore off; `--smooth` turns it back on if you want a prettier screenshot and
do not mind halving the rate. `--opengl` is faster again (~67 Hz) but driver-
dependent, so it is opt-in — in the captured run it hit 73 Hz with an **empty plot
area**, so check the traces actually draw before trusting a log taken with it. Full
measurements in [section 8.4](docs/mcmess-018cg.md#84-drawing-competes-with-sampling);
the four window captures behind them, including the blank OpenGL one, are in
[docs/bench](docs/bench/).

**Layout.** A searchable channel tree on the left, grouped by memory page and showing
what a selection costs per sample and the sample rate it implies — throughput is the
binding constraint here, so the picker makes it visible while you choose.

**Plots are stacked small multiples, one channel per panel, sharing one X axis.** A
tuning log mixes rpm, degrees, milliseconds and temperatures; putting two of those on
one set of axes would need a second y-scale, and a dual-axis chart invites exactly the
false correlation you are trying to read out of the data. So there is never more than
one scale per panel. Each panel is directly labelled with its channel, unit and current
value, and a crosshair reads values at any point in the trace.

Colours come from a validated 8-slot categorical palette, checked against the dark
surface for lightness, chroma, colour-vision separation and contrast. The order is fixed
and **never cycled**: a ninth channel draws in neutral ink rather than reusing a hue, so
two panels never share a colour. Identity comes from the direct label regardless.

**Threading.** The serial path is blocking and lock-step, and the ECU drops the session
after 2 s of silence — so sampling runs on its own thread in `mcmess/recorder.py` and
the window only reads buffered snapshots on a timer. That engine is Qt-free and tested
headlessly against the simulator, so the GUI is only responsible for drawing.

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

## Layout

| Path | What |
|---|---|
| `docs/mcmess-018cg.md` | Protocol spec, every claim tagged `[RE]` / `[ME7]` / `[INF]` / `[VERIFY]` |
| `mcmess/transport.py` | 9-bit frames via parity, init pulse, echo handling, timing |
| `mcmess/session.py` | Handshake, latch-aware reads, read scheduler |
| `mcmess/channels.py` | Channels and physical conversions |
| `mcmess/recorder.py` | Sampling thread, ring buffer, CSV — Qt-free |
| `mcmess/simulator.py` | Simulated 018CG responder + engine model |
| `mcmess/data/m38_018cg.json` | Generated channel + conversion database |
| `gui/main.py` | The window |
| `gui/plots.py` | Stacked small-multiple plot panels |
| `gui/picker.py` | Channel tree with live cost estimate |
| `gui/theme.py` | Validated dark palette and Qt styling |
| `tools/probe.py` | Bench bring-up and measurement |
| `tools/build_database.py` | Rebuilds the database from the RE files |
| `tests/test_program.py` | Scheduler, conversions and channel-database checks |
| `tests/test_session.py` | Session behaviour against the simulator |
| `tests/test_recorder.py` | Sampling engine, ring buffer, CSV, engine model |

## Channels and conversions

**244 channels, 226 of them in physical units.** Built from the reverse-engineering
files into one self-contained JSON:

```bash
.venv\Scripts\python.exe tools\build_database.py --report
```

All 222 DAMOS conversions parse and **every one reproduces its own declared min/max**.
The independently-known scalings come out right: `NQ40` = 40 rpm/bit, `TEMPQ` =
0.75·raw − 48 °C, `UQ5` = 19.6078 mV/bit (255 → 5000 mV exactly), `VQ` = 1.28 km/h,
`FAK2L` = raw/32768 so `0x8000` = 1.00.

Three things worth knowing before you trust a log — all detailed in
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

## Not done yet

- **The channel addresses are still unconfirmed.** The protocol, the latch, the baud
  change and the 16-bit assembly are all verified on hardware, and every conversion
  reproduces its declared range — but a conversion being right does not prove the
  address feeding it is. With bench sensors disconnected almost everything reads
  zero. The way to settle it is a running engine, or VCDS side by side on a handful
  of channels.
- **Not tested with the engine running.** Whether a session can be started, and held,
  with the engine turning is open.
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
