"""A simulated 018CG: a fake serial port that speaks McMESS, plus an engine model.

Two jobs:

  * `FakeEcuSerial` implements the parts of serial.Serial that KLineTransport touches
    and answers like the 018CG's read-only responder. Used by the test suite.
  * `EngineModel` writes plausible, moving values into that fake ECU's RAM, so the GUI
    can be driven and checked with no cable and no engine (`--demo`).

The responder's modelled behaviour, from docs/mcmess-018cg.md:

    a low pulse inside the accepted window arms the init handler
    one handshake byte (0xA5 / 0xA6) with bit9 = 0 is answered with 0x0C
    a handshake byte with bit9 = 1 is ignored
    in session: bit9 = 0 byte nn  -> latch := nn, reply mem[0x00nn]
                bit9 = 1 byte nn  -> latch unchanged, reply mem[latch:nn]

One modelled detail matters more than it looks: after acking 0xA6 the ECU raises its
own UART to 125000 straight away, so it is deaf at 10417 from that moment on. There is
no window in which the host can still read at the old rate. A new init pulse puts it
back to 10417.
"""

from __future__ import annotations

import math
import random
import time

import serial

from .channels import Channel, ChannelSet, Conversion, get_byte_order
from .transport import BAUD_FAST, BAUD_SLOW

__all__ = [
    "BAUD_FAST",
    "BAUD_SLOW",
    "EngineModel",
    "FakeEcuSerial",
    "attach",
    "invert_conversion",
    "make_demo_serial",
]


# ============================================================== fake serial port


class FakeEcuSerial:
    """Stand-in for serial.Serial wired to a McMESS-speaking M3.8.

    echo: cheap K-line cables loop the transmitted byte back into RX. True models that.
    """

    def __init__(
        self,
        memory: dict[int, int] | None = None,
        echo: bool = True,
        ack: int = 0x0C,
        answer_handshake: bool = True,
        pulse_window: tuple[float, float] = (0.0005, 10.0),
        latch_sticky: bool = True,
        fill: int = 0x00,
        fast_ok: bool = True,
        stable_corruption: bool = False,
        seed: int = 7,
        model: "EngineModel | None" = None,
        rtt_s: float = 0.0,
    ) -> None:
        self.memory = memory if memory is not None else {}
        self.echo = echo
        self.ack = ack
        self.answer_handshake = answer_handshake
        self.pulse_window = pulse_window
        #: False models the failure case in open question 1: a bit9=1 transfer
        #: clobbering the latch with its own low byte.
        self.latch_sticky = latch_sticky
        self.fill = fill
        #: False models a cable transceiver that cannot slew at 125 kBd, so the link
        #: works at 10417 and turns to mush after the switch (open question 3).
        self.fast_ok = fast_ok
        #: True makes corruption repeat identically, which a stability check cannot
        #: detect -- the case that justifies the cross-speed check existing.
        self.stable_corruption = stable_corruption
        self._rng = random.Random(seed)
        self.model = model
        #: Emulated round-trip latency per transaction, in seconds. Zero answers
        #: instantly, which is what the tests want. Demo mode sets a realistic value
        #: so the GUI sees the rate an actual cable produces (a few hundred bytes a
        #: second) rather than tens of thousands of samples a second.
        self.rtt_s = rtt_s
        self._ready_at = 0.0

        #: What the ECU's own UART is set to, as distinct from self.baudrate, which is
        #: what the host has configured. A mismatch means garbage on the wire.
        self.ecu_baud = BAUD_SLOW
        self.baudrate = BAUD_SLOW
        self.parity = serial.PARITY_SPACE
        self.timeout = 0.05
        self.write_timeout = 1.0
        self.dtr = True
        self.rts = True
        self.is_open = True

        self._rx = bytearray()  # bytes waiting for the host to read
        self._break = False
        self._break_started: float | None = None
        self._armed = False
        self.session = False
        self.latch = 0x00
        self.sent: list[tuple[int, int]] = []  # (value, bit9) seen from the host

    # -- the bit the whole scheme depends on -------------------------------

    @property
    def _bit9(self) -> int:
        if self.parity == serial.PARITY_MARK:
            return 1
        if self.parity == serial.PARITY_SPACE:
            return 0
        raise AssertionError(
            f"host set parity to {self.parity!r}; 9-bit transfer needs MARK or SPACE"
        )

    # -- serial.Serial surface ---------------------------------------------

    @property
    def break_condition(self) -> bool:
        return self._break

    @break_condition.setter
    def break_condition(self, value: bool) -> None:
        if value and not self._break:
            self._break_started = time.perf_counter()
        elif not value and self._break:
            width = time.perf_counter() - (self._break_started or 0.0)
            low, high = self.pulse_window
            if low <= width <= high:
                self._armed = True
                self.session = False
                self.latch = 0x00
                # The init handler reloads SP_BAUD to 10417 before the handshake, so a
                # previously raised rate does not survive a new pulse.
                self.ecu_baud = BAUD_SLOW
            else:
                self._armed = False
        self._break = value

    @property
    def in_waiting(self) -> int:
        return len(self._rx)

    def read(self, size: int = 1) -> bytes:
        if self.rtt_s:
            # Clamp: the deadline can pass between the comparison and the
            # subtraction, and time.sleep() rejects a negative argument.
            remaining = self._ready_at - time.perf_counter()
            if remaining > 0:
                time.sleep(min(0.001, remaining))
                return b""
        if not self._rx:
            time.sleep(0.0002)  # keep the host's poll loop from pure spinning
            return b""
        take = min(size, len(self._rx))
        out = bytes(self._rx[:take])
        del self._rx[:take]
        return out

    def write(self, data: bytes) -> int:
        for byte in data:
            self._handle(byte)
        if self.rtt_s:
            self._ready_at = time.perf_counter() + self.rtt_s
        return len(data)

    def reset_input_buffer(self) -> None:
        self._rx.clear()

    def reset_output_buffer(self) -> None:
        pass

    def close(self) -> None:
        self.is_open = False

    # -- responder ---------------------------------------------------------

    @property
    def link_ok(self) -> bool:
        """Whether a byte can actually cross the wire intact right now."""
        if self.baudrate != self.ecu_baud:
            return False
        return self.baudrate != BAUD_FAST or self.fast_ok

    def _handle(self, byte: int) -> None:
        bit9 = self._bit9
        self.sent.append((byte, bit9))
        if self.echo:
            # The echo is our own transmission looping back through the cable, so it
            # comes back at our own baud rate whatever the ECU is doing.
            self._rx.append(byte)

        if self._armed and not self.session:
            if bit9 == 1:
                return  # a handshake byte with bit9 set is ignored
            if byte in (0xA5, 0xA6):
                if self.answer_handshake:
                    self._rx.append(self.ack)
                    self.session = True
                    self._armed = False
                    if byte == 0xA6:
                        self.ecu_baud = BAUD_FAST
            return

        if not self.session:
            return

        if not self.link_ok:
            if self.stable_corruption:
                self._rx.append((byte ^ 0x5A) & 0xFF)
            else:
                # What bad slew actually looks like: bit errors that vary per frame.
                self._rx.append(self._rng.randrange(256))
            return

        if self.model is not None:
            self.model.update()

        if bit9 == 0:
            self.latch = byte
            addr = byte
        else:
            addr = (self.latch << 8) | byte
            if not self.latch_sticky:
                self.latch = byte
        self._rx.append(self.memory.get(addr, self.fill) & 0xFF)


def attach(transport, fake: FakeEcuSerial) -> FakeEcuSerial:
    """Wire a FakeEcuSerial into a KLineTransport without opening a real port."""
    transport.ser = fake
    transport._bit9 = 0
    fake.parity = serial.PARITY_SPACE
    return fake


# ============================================================= inverse conversion


def invert_conversion(conv: Conversion, physical: float) -> int | None:
    """Find the raw value that a conversion would turn into `physical`.

    Needed only by the simulator, to put a chosen physical value into fake RAM. The
    DAMOS form is

        phys = (p2 - p4*(x - p5)) / (p3*(x - p5) - p1)

    which inverts analytically in the two shapes that actually occur:

        p3 = 0            (linear)      x' = (phys*p1 + p2) / p4
        p1 = 0 and p4 = 0 (reciprocal)  x' = p2 / (phys * p3)

    then x = x' + p5, wrapped back to unsigned if the conversion is signed. Anything
    else returns None rather than guessing.
    """
    if conv.kind != "formula" or len(conv.coeffs) != 6:
        return None
    p1, p2, p3, p4, p5, p6 = conv.coeffs
    try:
        if p3 == 0:
            if p4 == 0:
                return None
            x = (physical * p1 + p2) / p4
        elif p1 == 0 and p4 == 0:
            if physical == 0:
                return None
            x = p2 / (physical * p3)
        else:
            return None
    except ZeroDivisionError:
        return None
    x += p5
    if p6:
        x %= p6
    return int(round(x))


# ================================================================== engine model


class EngineModel:
    """Drives plausible, moving values into a fake ECU's RAM.

    Exists so the GUI can be developed and checked end to end with no hardware: the
    same read path, the same conversions, the same plots. Values are written as raw
    bytes through the inverse of each channel's own conversion, so what the GUI
    displays has made the full round trip through the real conversion code.

    The cycle is a 40 s loop: idle, a pull to redline, overrun back to idle, hold.
    """

    CYCLE = 40.0

    def __init__(self, channels: ChannelSet, memory: dict[int, int]) -> None:
        self.channels = channels
        self.memory = memory
        self.started = time.perf_counter()
        self._last_update = -1.0
        self._counter = 0
        self.write_failures: list[str] = []
        self.update(force=True)

    # -- the drive cycle ---------------------------------------------------

    def state(self, t: float) -> dict[str, float]:
        """Physical values at time t seconds since start."""
        phase = t % self.CYCLE

        # Throttle and engine speed: idle, pull, overrun, settle.
        if phase < 6.0:
            throttle = 0.04
            rpm = 820 + 25 * math.sin(phase * 3.1)
        elif phase < 20.0:
            k = (phase - 6.0) / 14.0
            throttle = min(0.95, 0.1 + k * 0.9)
            rpm = 900 + k * 5600
        elif phase < 27.0:
            k = (phase - 20.0) / 7.0
            throttle = max(0.03, 0.9 - k * 0.9)
            rpm = 6500 - k * 5500
        else:
            throttle = 0.04
            rpm = 830 + 30 * math.sin(phase * 2.6)

        load = max(0.05, min(1.0, throttle * (0.55 + 0.45 * rpm / 6500.0)))
        # TL is injection time per revolution; roughly proportional to load.
        tl = 1.2 + load * 10.0
        air_mass = 8.0 + load * (rpm / 6500.0) * 230.0

        # Warm-up over the first two minutes, then stable.
        warm = min(1.0, (time.perf_counter() - self.started) / 120.0)
        coolant = 42.0 + warm * 48.0
        intake = 24.0 + warm * 9.0 + load * 6.0

        # Ignition advance: pulled back under load, plus a little knock retard high up.
        advance = 16.0 - load * 7.0 - max(0.0, (rpm - 5200) / 1300.0) * 2.0
        knock = -(1.5 if (load > 0.8 and rpm > 4200) else 0.0)

        # Battery sags slightly under load; lambda integrator hunts around 1.00 in
        # closed loop and parks at 1.00 at full load.
        battery = 14.25 - load * 0.45
        lam = 1.0 if load > 0.85 else 1.0 + 0.035 * math.sin(phase * 4.7)

        return {
            "N40": rpm,
            "NSOL": 820.0,
            "TL": tl,
            "TLROH": tl * 1.02,
            "MLUFT": air_mass,
            "ML": air_mass,
            "DKPOT": throttle * 88.0,
            "ZWOUT": advance,
            "ZWBEG": advance + 0.75,
            "DWKR_Z": knock,
            "TMOT": coolant,
            "TANS": intake,
            "UB": battery,
            "FR": lam,
        }

    # -- writing into fake RAM --------------------------------------------

    def update(self, force: bool = False) -> None:
        """Refresh fake RAM, at most every 10 ms."""
        now = time.perf_counter()
        if not force and (now - self._last_update) < 0.010:
            return
        self._last_update = now
        t = now - self.started

        for name, value in self.state(t).items():
            channel = self.channels.by_name.get(name)
            if channel is not None:
                self._write(channel, value)

        # A couple of channels that are only ever raw, so the GUI shows those too.
        self._counter = (self._counter + 1) & 0xFF
        self.memory[0x0088] = self._counter  # STATVS
        self.memory[0x00C2] = 0b0000_0011  # ALRST flags

    def _write(self, channel: Channel, physical: float) -> None:
        conv = channel.conversion
        raw = invert_conversion(conv, physical)
        if raw is None:
            note = f"{channel.name}: cannot invert {conv.name}"
            if note not in self.write_failures:
                self.write_failures.append(note)
            return

        width_max = 0xFFFF if channel.size == 2 else 0xFF
        raw = max(0, min(width_max, raw))

        if channel.size == 2:
            base = channel.base
            high, low = (raw >> 8) & 0xFF, raw & 0xFF
            if get_byte_order() == "little":
                self.memory[base], self.memory[base + 1] = low, high
            else:
                self.memory[base], self.memory[base + 1] = high, low
        else:
            self.memory[channel.addr] = raw & 0xFF


#: Emulated per-transaction latency for demo mode. Chosen to land in the 240-480
#: bytes/s band that docs/mcmess-018cg.md section 4 predicts for a PC and an FTDI
#: cable, so the GUI's sample rate and plot density look like the real thing.
DEMO_RTT_S = 0.0025


def make_demo_serial(
    channels: ChannelSet, rtt_s: float = DEMO_RTT_S, **kwargs
) -> FakeEcuSerial:
    """A fake ECU with a running engine model behind it, for --demo.

    Pass rtt_s=0 to answer instantly, which is what the tests want.
    """
    memory: dict[int, int] = {}
    fake = FakeEcuSerial(memory=memory, rtt_s=rtt_s, **kwargs)
    fake.model = EngineModel(channels, memory)
    return fake
