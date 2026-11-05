# McMESS on Bosch M3.8 / 06A906018CG — working protocol spec

Target: **06A906018CG**, Bosch M3.8.x, VW/Audi 1.8T AGU. CPU **80C196KR @ 16 MHz**.

Every statement below is tagged with how well it is known:

| Tag | Meaning |
|---|---|
| `[RE]` | Read out of the 018CG flash in Ghidra. High confidence. Source: `D:\Repos\Reverse Engineering\M3.8\analysis\mcmess_018CG.md` |
| `[ME7]` | From the supplied `mcmess protocol.txt`, which documents **ME7.5**, a different ECU family. Background only. |
| `[INF]` | Inferred by us from `[RE]` facts. Plausible but not proven. |
| `[VERIFY]` | Must be measured on the bench ECU before it is trusted. |

> **Do not use the ME7 command set on this ECU.** The 018CG McMESS is a much smaller,
> read-only subset. See section 5.

---

## 1. Physical and frame layer

K-line: single wire, bidirectional, open-drain, pulled up to ~12 V, dominant low.

Frames are **9 data bits, no parity**. `[RE]` (ECU sets `SP_CON` to 9-bit mode 3.)

| Phase | Baud | Frame | Source |
|---|---|---|---|
| Init / handshake | **10417** | 9N1 | `[RE]` `SP_BAUD = [0x13CA] = 0x805F` |
| Slow session (after `0xA5`) | **10417** | 9N1 | `[RE]` |
| Fast session (after `0xA6`) | **125000** | 9N1 | `[RE]` `SP_BAUD = [0x13C8] = 0x8007`, `SP_CON` unchanged |

`[ME7]` says highspeed is 9N2. `[RE]` shows `SP_CON` is *not* reloaded when the 018CG
switches to 125000 baud, so the 018CG stays 9N1. We transmit 9N1 and accept either.
`[VERIFY]` — if 125k is flaky, try two stop bits on our transmit side; an extra stop bit is
only extra idle time and cannot upset a receiver expecting one.

### 1.1 Getting 9 bits out of a PC UART

A 9N1 frame is bit-for-bit identical to **8 data bits + parity + 1 stop**:

```
start | d0 d1 d2 d3 d4 d5 d6 d7 | bit9 | stop
start | d0 d1 d2 d3 d4 d5 d6 d7 | par  | stop     <- same wire pattern
```

So bit9 is produced by forcing the parity bit to a constant:

| Want | Serial parity setting |
|---|---|
| bit9 = 0 | `PARITY_SPACE` |
| bit9 = 1 | `PARITY_MARK` |

**Receive side:** we cannot read the incoming 9th bit — the UART consumes it as parity.
Windows is configured with `fErrorChar = 0` and `fAbortOnError = 0`, so a frame whose
parity bit does not match the current setting is still **delivered with its 8 data bits
intact**; only an error flag is raised, which we ignore. Confirmed by reading pyserial's
Win32 backend. Losing the received bit9 costs us nothing for logging, because this ECU's
responder has no error-vs-data encoding in bit9 (`[INF]`, `[VERIFY]`).

Changing parity costs a `SetCommState` call (a USB control transfer on FTDI, order 1-3 ms),
so the scheduler is built to switch as rarely as possible. See section 3.

---

## 2. Session start

```
1. Idle the K-line high for >= 300 ms.              (clear any half-finished state)
2. Hold the K-line LOW for ~85 ms.                  (serial break)
3. Release to high.
4. Send one handshake byte at 10417 baud, bit9 = 0:
      0xA5  -> stay at 10417 baud
      0xA6  -> switch to 125000 baud
5. ECU replies 0x0C with bit9 = 1 (i.e. 0x10C). We see the low 8 bits: 0x0C.
6. If 0xA6 was sent, the ECU raises its baud rate on its next 10 ms task call.
   Wait ~20 ms, then reconfigure our port to 125000.
```

`[RE]` for all of the above. Notes:

- **Low-pulse window is 7..9 task ticks = ~70-99 ms.** `[RE]` The handler needs a
  high-to-low edge first; too short or too long resets it. We target 85 ms, dead centre.
- **The ack is `0x0C`, not `0x0D`.** `[ME7]` says `0x0D`; the 018CG uses `0x0C`
  ("Kommunikationskennung 0Ch" in the FR). `[RE]`
- A handshake byte sent with bit9 = **1** is ignored by the ECU. `[RE]` Parity must be SPACE here.
- 85 ms is well under one 5-baud bit time (200 ms), so this cannot be mistaken for a
  KW1281 5-baud address. `[INF]`
- The init handler lives at page5:`0xE274` and runs from the ~10 ms page-5 task. `[RE]`
  The neighbouring 5-baud receiver is gated on RPM < 8000; whether the McMESS init handler
  is gated the same way is **not known** — `[VERIFY]` that a session can be *started* with
  the engine running, not just held open.

### 2.1 Validating the link after the baud change

A cable whose transceiver cannot slew at 125 kBd does **not** go silent — the ECU still
answers every transfer, just with corrupted bytes. So a timeout check proves nothing
after the switch, and corrupted data would flow straight into a log.

There is also no chance to capture a reference at the old rate: the ECU raises its own
`SP_BAUD` on the task call right after acking `0xA6` and is deaf at 10417 from then on.
A new init pulse is the only way back to 10417. So two checks exist:

| Check | How | Catches | Cost |
|---|---|---|---|
| **stability** (automatic, every highspeed connect) | read a 32-byte page-0 window twice at 125 kBd, require ≥50% agreement | varying bit errors — what bad slew actually produces | ~64 extra bytes |
| **cross-speed** (`probe.py connect --cross-check`) | connect at `0xA5`, snapshot, re-init at `0xA6`, re-read, compare | also corruption that repeats identically | a second init pulse, less RAM-drift tolerance |

Both are heuristics: ECU RAM genuinely moves between snapshots, which is why the window
is chosen over slow-moving quantities (`0x0090`–`0x00AF`: temperatures, battery voltage,
flag registers, throttle) and the threshold is only 50%. Over 32 bytes a corrupted link
cannot reach that by chance.

### 2.2 Session timeout

Each received byte clears the timeout counter at `0xCE2F`. **200 ticks with no traffic
(~2 s)** drops the session. `[RE]` Any read keeps it alive, so a steady logging loop never
needs an explicit keepalive; an idle UI must still poll at least every ~1.5 s.

The same timeout gates **re-initialisation**, which matters more than it looks.
While a session is still live the responder ignores init pulses outright, so a
handshake retry fired a few hundred ms after a failure cannot succeed.

Measured on the bench (2025-11-05), quiet time after a live session before a new
init pulse is honoured:

| Quiet | Result |
|---|---|
| 0.3 s | fails, ack reads `0x00` |
| 1.0 s | fails, ack reads `0x00` |
| 1.5 s | fails, ack reads `0x19` -- a stale RAM byte, not silence |
| 2.0 s | connects |

`[MEASURED]` `SESSION_REARM` in `mcmess/session.py` is 2.5 s, that figure with
margin, and `connect()` waits it out between attempts. Without that wait
`attempts=3` behaves exactly like `attempts=1`: all three pulses land inside the
dead window. This was a real bug, found only on hardware -- the simulator had no
notion of a re-arm delay.

---

## 3. Reading memory

One responder, in the fixed code area at `0x28C8`-`0x2928`, running **every 1 ms** inside
the A/D ISR coroutine. `[RE]` It keeps one byte of state: the **high-address latch** at
`0xCE30`.

| We send | ECU does | ECU replies |
|---|---|---|
| byte `nn`, bit9 = **0** | latch := `nn` | `mem[0x00nn]` |
| byte `nn`, bit9 = **1** | latch unchanged | `mem[latch:nn]` |

`[RE]`, except "latch unchanged" on the bit9=1 path, which is `[INF]` + `[VERIFY]` — it is
the single most important assumption in this design, because it is what makes streaming
cheap.

Two consequences that drive the whole logger design:

**1. Page `0x00` is free.** A bit9=0 byte returns `mem[0x00nn]` as a side effect of setting
the latch. So any channel in page `0x00` costs **one wire byte and zero parity switches** —
the port can sit in `PARITY_SPACE` permanently. Most of the hot channels are there:

| Channel | Addr | What |
|---|---|---|
| `NSOL` | `0x5D` | target RPM |
| `ZWOUT` | `0x7F` | ignition angle |
| `STATVS` | `0x88` | status cell |
| `TANS` | `0x9A` | intake air temp |
| `TMOT` | `0x9B` | coolant temp |
| `UB` | `0x9C` | battery voltage |
| `N40` | `0xA4` | RPM (40 rpm/bit) |
| `TL` | `0xA6` | load signal |
| `DKPOT` | `0xB9` | throttle position |
| `ALRST` | `0xC2` | lambda / TAE flags |

**2. Other pages cost a latch write.** For `mem[hh:ll]` with `hh != 0x00`: send `hh` with
bit9=0 (reply is `mem[0x00hh]` — discardable, or free data if you happen to want that byte),
then send `ll` with bit9=1. Subsequent channels in the same page `hh` are one byte each. So
a page group of *k* channels costs *k + 1* wire bytes.

Parity switches, for a program that starts in bit9=0: the first non-zero page costs **one**
switch (its latch byte is already at bit9=0, so only the move up to bit9=1 costs), and every
page after that costs **two** (back down to 0 for the latch, up again for the data). So *g*
page groups cost `2g - 1` switches, and a page-0-only channel set costs **zero**. Measure the
per-switch cost on your cable with `probe.py timing`, which times a bit9-alternating run
against a page-0 run.

### 3.1 Address space

| CPU address | Contents |
|---|---|
| `0x0000`-`0x01FF` | 80C196KR register file / lower RAM — page `0x00` channels live here |
| `0x1Fxx` | memory-mapped SFRs (timers, A/D, ports, UART) |
| `0xC000`-`0xCFFF` | RAM (data decode) — `0xC8xx` lambda, `0xCExx`/`0xCFxx` knock and protocol state |
| `0xE0xx` | peripheral block, incl. the code-bank latch at `0xE040` |

`[RE]`. Code and data are decoded separately on this part, so a *data* read of `0xC000+`
sees RAM, not the code page. Reads of the SFR window and the `0xE0xx` block are untested —
`[VERIFY]`.

---

## 4. Throughput

Wire frame times:

| Mode | Frame | Time |
|---|---|---|
| 10417 baud, 9N1 | 11 bits | **1.056 ms** |
| 125000 baud, 9N1 | 11 bits | **0.088 ms** |

K-line is half-duplex and the 80C196KR has a **single-byte RX buffer with no FIFO**, so this
is strictly lock-step: one byte out, one byte back, no pipelining. Sending a second byte
before the 1 ms responder consumes the first loses it to an RX overrun. `[INF]`

Floor per transaction = our frame + latch-to-tick wait (0-1 ms, avg 0.5) + ECU frame:

| Mode | Protocol floor | Ceiling |
|---|---|---|
| 10417 | ~2.6 ms | ~380 bytes/s |
| 125000 | ~0.7 ms, clamped by the 1 ms responder tick | **~1000 bytes/s** |

That ceiling needs an adapter that can turn a byte round in well under a millisecond. From a
PC, the FTDI latency timer (1 ms minimum), USB frame scheduling and Windows scheduling add
roughly **2-4 ms per round trip**, giving:

| Mode | Predicted PC-side rate |
|---|---|
| 10417 | ~150-220 bytes/s |
| 125000 | ~240-480 bytes/s |

**Measured 2025-11-05** on a genuine FTDI FT232R (COM2), 018CG on the bench, with
the FTDI latency timer set to 1 ms:

| What | Result |
|---|---|
| `probe timing`, page-0 reads | 1.92 ms/byte median -> **518 bytes/s** |
| `probe watch`, 10 page-0 channels | 56-77 Hz, 603-783 bytes/s |
| `probe watch`, 6 channels over 2 pages | **91 Hz, 810 bytes/s** |
| Cost of one parity switch | 0.43 ms |
| Errors over 8536 transfers | 0 timeouts, 0 echo mismatches, 0 strays |

So the real cable beats the prediction by roughly 2x, and sits within a factor of
~1.3 of the protocol ceiling. `[MEASURED]` Two surprises worth recording:

- **The FTDI latency timer is the single biggest PC-side variable.** The first
  connects in this session ran with the 16 ms default and measured 15.7 ms round
  trips for **64 bytes/s** — almost exactly the ~60 bytes/s the arithmetic
  predicts. Setting it to 1 ms took the same link to 1.0–1.9 ms and 518–810
  bytes/s: a **12x** difference, and the largest single effect measured anywhere in
  this project. Check it before trusting any timing number; `probe.py latency`
  reads it per cable.
- Run-to-run variance is large (500-810 bytes/s on identical selections), so treat
  any single `timing` number as indicative rather than exact.

So requesting highspeed (`0xA6`) is worth it from a PC even though the wire is nowhere near
saturated — it removes ~2 ms of frame time from every round trip. All `[INF]`;
`tools/probe.py timing` measures the real number on your cable.

For scale: ~300 bytes/s is about **20 page-0 channels at 20 Hz**, against roughly 15-30
values/s total from KW1281 in VCDS.

---

## 5. What this ECU does *not* have

Present in `[ME7]`, absent from the 018CG responder — the FR calls it
*"Reiner Datenempfang"* (receive-only). `[RE]`

- No `0x1xx` command set at all (`0x102`, `0x104`, `0x119` getStringEPK, `0x10E`, ...)
- **No RAM write** (`0x12F`) — so this tool cannot change anything in the ECU
- No Verstellsystem read/write (`0x11F`, `0x137`, `0x138`), none of `vszw`, `vsfrk`, ...
- No EPK string read — image identification must come from the KW1281 side
- No sync-address table and no crank-synchronous mode (`0x110`, `0x131`, `0x13D`)
- Replies are **1 byte**, not 2
- Responder polls at **1 ms**, not 20 ms

Also, ME7's address mapping (`0x0000-0x3FFF -> 0x380000` EXTRAM) is C167-specific and does
**not** apply here.

---

## 6. Bench results and what is still open

First hardware session: **2025-11-05**, 06A906018CG on a bench harness, ignition on,
engine not running, genuine FTDI FT232R on COM2.

### 6.1 Answered

1. **Does bit9=1 leave the latch alone?** **YES.** `[MEASURED]` `probe latch` on page
   `0xC8`: latch-once-then-stream and re-latch-every-byte agree 15/15, while both
   disagree with the same offsets read as page 0 (4/15) -- so the latch really is
   redirecting the address space. A group of *k* channels costs *k+1* wire bytes and
   the scheduler in section 3 stands. This was the assumption the whole design rested
   on.
2. **Round-trip time.** Measured; see section 4. Better than predicted.
3. **Does 125000 baud survive the cable?** **YES.** `[MEASURED]` `probe connect
   --cross-check` returned 32/32 agreement between the proven 10417 link and the fast
   one, and the stability check passes repeatedly. No slew problem on this cable.
4. **Is the cable a genuine FTDI?** Genuine VID/PID `0403:6001` with a plausible
   FT232R serial, and -- the part that actually matters -- mark/space parity works:
   8536 transfers with zero echo mismatches and zero stray bytes. `[MEASURED]`
5. **The handshake ack is `0x0C`,** not ME7's `0x0D`, on the first attempt at both
   baud rates. `[MEASURED]` This is the clearest confirmation that the 018CG runs the
   small read-only responder described here and not the ME7.5 protocol.
6. **Byte order of 16-bit channels.** Resolved offline (section 7) and now confirmed
   on hardware: `FR` at `0xC802..0xC803` reads `00 80`, i.e. `0x8000` little-endian,
   which `FAK2L` converts to exactly **1.000**. `FR` is the multiplicative lambda
   integrator, so unity is precisely its neutral value with the engine stopped;
   big-endian would give 0.0039, which is no sensible resting value. `[MEASURED]`

### 6.2 Found on the bench, not predicted

- **The responder needs ~2 s of quiet before it will re-init.** See section 2.2. This
  broke handshake retries and would have broken reconnect-after-dropout.

### 6.3 Still open

1. `[VERIFY]` **Can a session be started with the engine running,** or only key-on?
   Untested -- the bench run had no engine.
2. `[VERIFY]` **Are the channel addresses right?** The conversions are confirmed
   arithmetically (section 7), but with sensors disconnected most of page 0 reads 0
   and the few non-zero channels cannot be cross-checked. `TMOT`/`TANS` read -46.5 /
   -45.75 degC (raw 2 and 3, consistent with ungrounded NTC inputs) and `MLUFT` reads
   100 kg/h (raw 25) with the engine stopped. Those are plausible for a bench, but
   only a running engine -- or VCDS side by side -- will confirm the addresses.
3. `[VERIFY]` Whether reads of the `0x1Fxx` SFR window and `0xE0xx` return anything
   useful.
4. Not needed for logging, but the big prize: the **8-address raster push stream**
   (`0xF3A7`, list at `0xCD04`, TX ISR `0x2CE4`, gated on `RD3` bit 6). That mode
   pushes bytes continuously with no tester byte per sample -- order 10 kbyte/s at
   125k. It is cleared by init, we have no write path to fill the list, and
   `KONFCOM` (`0x6D19`, `0x00` in this image) selects the mode. Reaching it likely
   needs a flash change.

---
## 7. Channels and conversions

Built by `tools/build_database.py` into `mcmess/data/m38_018cg.json`. Three inputs:

| Input | Gives | File |
|---|---|---|
| DAMOS | conversion formulas | `Damoses/8D0907558E.DAM` |
| FR | channel names, descriptions, conversion per channel, byte width | `FR/30O.FRM` |
| ECU image | the VS20 table: pseudo-address to real RAM address | `Damoses/WinOLS(VW Golf ...).bin` |

Result: **244 channels**, 226 of which convert to physical units. 22 channels sit in
page `0x00` and are the cheap ones to log.

### 7.1 Provenance caveat

**There is no DAMOS for the 018CG.** The formulas come from `8D0907558E` — an Audi A4,
M3.8.2, DAMOS 30N — while the channel list is FR 30O and the addresses are the 018CG's
own. Conversion names are shared across the M3.8 family so the formulas are expected to
apply, but this is a cross-image inference, not a measurement. The caveat ships with the
data in the JSON's `meta.caveat` and prints under `probe.py channels --provenance`.

### 7.2 The DAMOS conversion format

A `/REG` record declares a conversion; the `/REP` record immediately after it carries
six coefficients. All 222 are one rational form:

```
x    = raw, reinterpreted as two's complement modulo p6 when p6 != 0
phys = (p2 - p4*(x - p5)) / (p3*(x - p5) - p1)
```

Every record is a degenerate case: linear (`p3 = 0`), reciprocal (`p1 = p4 = 0`), or
identity. Two other record kinds exist — `/VTB` + `/VWP` for text labels (7 of them,
e.g. `0 = Inaktiv`) and `/TAB` + `/WP` for discrete lookup tables (2).

`[VERIFY]`-grade confidence: **all 222 formulas reproduce their own declared min/max**,
and the ones with independently known scalings come out right — `NQ40` = 40 rpm/bit,
`TEMPQ` = 0.75·raw − 48 °C, `UQ5` = 19.6078 mV/bit (255 → 5000 mV exactly), `VQ` = 1.28
km/h, `PCENT` = 100/256, `FAK2L` = raw/32768 so `0x8000` = 1.00.

Two parsing traps worth recording:

- Records **wrap across lines in place of a comma**, so a field pair `1,7` can appear as
  `...,1` then `7,...`. Continuations rejoin with a comma, never whitespace.
- The two display-digit fields can be **negative** (`-2,7` on `FAK2L10S` and `THAINTL`).

### 7.3 Use `/UMR:`, never the bracketed name

FR lines look like:

```
N40    ;Drehzahl    2001h   [NQ40]   /UMR:NQ40 /FMT:1
```

The bracketed name is what Bosch's tool *displays*; `/UMR:` is what applies. They differ
on **37 entries**, and the displayed one is usually the 8-bit alias of a 16-bit
conversion:

| Displayed | Actual | Error if you use the displayed one |
|---|---|---|
| `FKT128` | `FAK2L` | 256× |
| `DEZ` | `DEZL` | wrong width |
| `TEQS` | `TEQSL` | 32× |
| `AFAK2` | `AFAK2L` | 256× |
| `KQHQ` | `KGHQX` | different conversion |

An earlier extraction of this channel list recorded the displayed name, which is why
this is called out. A channel whose `/UMR:` has no formula is reported **raw** — it never
falls back to the displayed name's formula.

### 7.4 16-bit channels: the `+1` rule

This was open question 6, and it is settled from the data rather than guessed.

The A4 DAMOS's `/UMP` records give every variable's true base address *for the A4
image*, and the FR's VS20 table in that same image gives what each entry resolves to.
Comparing them across all 80 `FMT:2` entries:

| FR name | VS20 address | Count |
|---|---|---|
| no `+1` suffix | `== base` | **35 / 35** |
| `+1` suffix | `== base + 1` | **45 / 45** |

So the `+1` is **literal**: such an entry points at the second byte of the word. Either
way the word occupies `(base, base+1)`, which makes this a **per-channel** property
decided by the name — not a global setting. The base is stored per channel as
`word_base`, and `tools/build_database.py` re-derives the rule on every build so that
swapping in a different DAMOS or FR revision cannot silently invalidate it.

One entry, `QAD+1`, resolves `+2` past its DAMOS address: `QAD` spans `0xC3F4`–`0xC3F7`
(`QADLL` starts at `0xC3F8`), so the entry addresses the upper word of a 4-byte
variable. Reported separately from conflicts.

Which byte is most significant does **not** come from this evidence — it follows from
the 80C196 being little-endian, so the base address holds the low byte. That is the one
remaining assumption, overridable with `--byte-order big` or
`mcmess.channels.set_byte_order()`. Confirm it on the bench with:

```
probe.py word --channel FR
```

`FR` is the lambda integrator and `FAK2L` puts 1.00 at `0x8000`, so at idle the raw word
should read close to `0x8000`. The tool also checks each byte order against the
conversion's declared range and says which survives.

### 7.5 Channels reported raw

18 of 244, for two different reasons.

**No formula for the conversion the FR names** (13 channels): `LUQ`, `LUQX`, `SIG05`,
`SIG32L`, `TQ100FP` are absent from the A4 DAMOS, plus a few FR entries that name no
conversion at all. Mostly Laufunruhe (misfire roughness) and signal thresholds.

**Tagged suspect** (1 channel): `DVL_SYNA+1` is `FMT:2` but its conversion `UQ5` is an
8-bit ADC voltage, and its FR description reads *"Speicher DVL_SYN + DDVLSYN"* — it is
**two packed 8-bit values** sharing one entry, not a 16-bit number. Assembling it as a
word and scaling it would produce a confident, wrong reading, so it is reported raw.
`probe.py channels --raw-only` lists them all.

### 7.6 Not fitted

13 FR entries resolve to VS20 address `0x0000` and are dropped. Six are named — `LDPRS`,
`TAGR`, `TAGRLL`, `TAGRD`, `TIMAGRLL`, `TIMAGRTL` — all EGR and leak-detection-pump
variables, which the AGU does not have, so the table entry is legitimately empty. The
rest are unnamed slots. `probe.py channels --rejected`.

---

## 8. Logger architecture

Three layers, deliberately separated so the lowest two can be tested with no hardware
and no Qt.

```
gui/            PySide6 window - draws only, never touches the serial port
mcmess/recorder.py   sampling thread, ring buffer, CSV        (Qt-free)
mcmess/session.py    handshake, latch-aware reads, scheduler
mcmess/transport.py  9-bit frames via parity, init pulse, echo
```

### 8.1 Why sampling is on its own thread

The serial path is blocking and strictly lock-step, and the ECU ends the session after
2 s of silence (section 2.2). A UI that shared the thread would drop the session
whenever it did any real work — a file dialog alone would do it. So `Recorder` owns the
loop, and the window reads a buffered snapshot on a 50 ms timer. Redraw cost is
decoupled from sample rate, and the window can be frozen for reading without
interrupting sampling or recording.

Samples land in a fixed-capacity numpy ring buffer (36000 samples, about 20 minutes at
30 Hz). `snapshot()` returns the window of interest, decimated by striding, **together
with the channel list it corresponds to** — changing the selection reallocates the
buffer, and a caller holding a stale list would otherwise plot the wrong column under
the wrong name.

Two failure modes are handled explicitly:

- **Timeouts** — five consecutively triggers a reconnect, up to three attempts, then the
  error state. Reconnects are counted and shown.
- **Anything unexpected** — the loop catches it and goes to the error state with the
  message and a stored traceback. Without that the thread dies silently while the UI
  still reads "running", which is worse than an obvious failure: a logger that has
  quietly stopped recording.

### 8.2 The simulator

`mcmess/simulator.py` is a fake serial port implementing the responder from section 3,
plus an engine model that writes plausible moving values into its RAM **through the
inverse of each channel's real conversion**. So a demo value makes the full round trip —
inverse conversion, fake RAM, the real scheduler, the real transport, the real forward
conversion — and anything the GUI shows has been through the same code as live data.

It also models the awkward parts, which is what makes it useful rather than decorative:
the baud rate changing the instant `0xA6` is acked, a cable that echoes transmitted
bytes, a transceiver that corrupts rather than drops bytes at 125 kBd, a non-sticky
address latch, and a configurable per-transaction latency (2.5 ms in demo mode, landing
in the 240–480 bytes/s band section 4 predicts).

### 8.3 Charts

One channel per panel, stacked, sharing one X axis — small multiples. A tuning log mixes
rpm, crank degrees, milliseconds and temperatures, so a shared scale would mean a second
y-axis, and a dual-axis chart manufactures apparent correlations. One scale per panel,
always.

The categorical palette is the validated 8-slot dark-mode order; it passes the lightness
band, chroma floor, colour-vision separation, normal-vision floor and 3:1 contrast
against the `#1a1a19` surface. Hues are assigned in fixed order and never cycled — the
ninth channel onward draws in neutral ink rather than repeating a colour. Every panel
carries its channel name, unit and current value as a direct label, so identity never
depends on colour at all.

One display distinction worth keeping: a channel with a dimensionless conversion (a
factor such as the lambda integrator `FR`) shows **no unit**, while a channel with no
usable conversion shows **"raw"**. They look the same otherwise, and conflating them
would make a perfectly good physical value look untrustworthy.

---

### 8.4 Drawing competes with sampling

The threading split in 8.1 keeps the UI from *blocking* the link, but it does not
make drawing free: pyqtgraph paints in Python and holds the GIL while it does.
Measured on the bench, six channels, real ECU, identical selection throughout:

| Configuration | Rate |
|---|---|
| `probe watch` -- main thread, tight loop | 810 bytes/s, 91 Hz |
| `Recorder` on its own thread, no Qt in the process | 639 bytes/s, 80 Hz |
| GUI, window **hidden**, refresh timer running | 735 bytes/s, 92 Hz |
| GUI, window shown, refresh timer **stopped** | 675 bytes/s, 84 Hz |
| GUI, window shown and refreshing, **antialiased** | 219 bytes/s, 27 Hz |
| GUI, window shown and refreshing, antialias off | 451 bytes/s, 56 Hz |
| ... and with OpenGL as well | 537 bytes/s, 67 Hz |

`[MEASURED]` The hidden-window and stopped-timer rows are what make this conclusive:
neither the data bookkeeping nor Qt's mere presence costs anything much. It is the
**painting of updated curves**, and specifically antialiasing, which alone cost more
than half the logger's throughput.

So antialiasing is **off by default** -- on a tool whose entire purpose is sample
rate, harder line edges are a cheap price for 2x the data. `--smooth` turns it back
on for a screenshot.

OpenGL is faster again but is **not** the default: it is driver-dependent, and a
`grab()` of the window came back with an empty plot area, which may be a capture
limitation or a real failure. It is opt-in via `--opengl`, and the help text says to
check the traces actually draw before trusting a log taken with it. Shipping a
default that cannot be verified to render is not worth 1.5x.

Two things follow for anyone extending this. Throughput depends on what is on
screen, so quote the channel selection and the render settings with any rate. And
the headless `Recorder` is the honest measure of the link -- `tools/probe.py` will
always beat the window.
