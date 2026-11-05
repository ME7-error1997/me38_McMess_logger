# Bench evidence — 06A906018CG, 2025-11-05

All of it from one session: a 06A906018CG on a bench harness, genuine FTDI FT232R
on COM2, FTDI latency timer at 1 ms, ignition on and **engine not running**. Most
sensors are disconnected, which is why `TMOT` reads −46.5 °C and `N40` reads 0 —
those are open inputs, not a conversion fault. `UB` ≈ 11.9 V and `FR` = 1.000 are
the two values that are genuinely meaningful here.

## Window screenshots

Same six channels throughout (`TMOT`, `UB`, `N40`, `MLUFT`, `TL`, `FR`) —
7 bytes/sample, 5 of them in page `0x00`, 8 wire bytes/sample, 1 parity switch,
2 pages. Every run: 125000 baud, `timeouts 0`, `reconnects 0`.

| File | Flags | Rate | Throughput | Round trip |
|---|---|---|---|---|
| [`gui-smooth.png`](gui-smooth.png) | `--smooth` | 38.5 Hz | 308 B/s | 2.46 ms |
| [`gui-default.png`](gui-default.png) | none | 49.5 Hz | 396 B/s | 1.96 ms |
| [`gui-default-repeat.png`](gui-default-repeat.png) | none | 51.0 Hz | 408 B/s | 1.81 ms |
| [`gui-opengl.png`](gui-opengl.png) | `--opengl` | 73.0 Hz | 584 B/s | 1.54 ms |

`gui-opengl.png` is the reason `--opengl` is opt-in rather than the default: the
rate is the best of the four, but **the plot area is empty**. Whether the traces
failed to render or merely failed to survive `QWidget.grab()` was never settled, so
the flag is there to be tried, not trusted. Check your traces actually draw before
you rely on a log taken with it.

> **These four runs are not the source of the 27 Hz / 56 Hz antialiasing figures in
> the top-level README.** Those came from a separate bisect. Here the same comparison
> is 38.5 Hz against ~50 Hz — the same direction, a smaller gap. Treat the headline
> ratio as run-dependent: antialiasing costs rate, but how much varies.

## `probe.py watch` CSVs

Ten channels, written headless, so no drawing competes for the GIL.

| File | Samples | Span | Rate |
|---|---|---|---|
| [`watch-10ch-prefix.csv`](watch-10ch-prefix.csv) | 847 | 15.00 s | 56.5 Hz |
| [`watch-10ch-77hz.csv`](watch-10ch-77hz.csv) | 617 | 8.01 s | 77.0 Hz |
| [`watch-10ch-73hz.csv`](watch-10ch-73hz.csv) | 584 | 8.02 s | 72.9 Hz |

`watch-10ch-prefix.csv` is kept deliberately: it was written **before** the
negative-zero fix, so every idle cell in it reads `-0` rather than `0`. The other
two are from after. Most conversions are rational forms with a negative
denominator — `NQ40` is (0 − 40x) / (0 − 1) — which returns IEEE −0.0 at raw 0, and
nothing downstream collapsed it. `mcmess/channels.py:csv_number` does now, and
`tests/test_program.py` guards it.
