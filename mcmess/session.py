"""McMESS session handling for Bosch M3.8 / 06A906018CG.

This is the read-only responder found in the 018CG flash, not the ME7 command set.
See docs/mcmess-018cg.md for what is proven versus assumed.

Protocol in one table (the only two things the ECU does):

    we send byte nn, bit9 = 0   ->  latch := nn,        ECU replies mem[0x00nn]
    we send byte nn, bit9 = 1   ->  latch unchanged,    ECU replies mem[latch:nn]

so page 0x00 is free (one wire byte, no parity switch) and every other page costs one
extra byte to set the latch.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .errors import HandshakeError, NotConnectedError, TimeoutError_
from .transport import BAUD_FAST, BAUD_SLOW, KLineTransport, sleep_until

#: The 018CG answers the handshake with 0x0C (bit9 set). ME7 answers 0x0D.
ACK_BYTE = 0x0C

#: Handshake request bytes, sent with bit9 = 0 at 10417 baud.
REQ_SLOW = 0xA5
REQ_FAST = 0xA6

#: Session dies after ~2 s of silence (200 ticks of the ECU's 10 ms task).
SESSION_TIMEOUT = 2.0

#: Quiet time the responder needs before it will honour a NEW init pulse. While a
#: session is still live it ignores them, so a retry fired a few hundred ms after a
#: failure is guaranteed to fail too. Measured on a bench 06A906018CG (2025-11-05):
#: 1.5 s of quiet still returned stale bytes as the "ack", 2.0 s connected cleanly.
#: 2.5 s is that figure with margin.  [MEASURED]
SESSION_REARM = 2.5
KEEPALIVE_INTERVAL = 1.2

#: Harmless, always-present address used for keepalives and link checks (STATVS).
PING_ADDR = 0x0088

#: Page-0 window used to validate a link. Deliberately picked over slow-moving
#: quantities -- temperatures, battery voltage, flag registers, throttle -- so two
#: snapshots taken a few tens of ms apart should mostly agree.
LINK_CHECK_WINDOW = tuple(range(0x0090, 0x00B0))

#: Fraction of the window that must agree for a link to pass. Low enough to tolerate a
#: running engine moving several bytes, high enough that a corrupted link cannot reach
#: it by chance over 32 bytes.
LINK_CHECK_THRESHOLD = 0.5


# --------------------------------------------------------------------------- planning


@dataclass
class ReadProgram:
    """A precompiled lock-step byte sequence for one sample of a channel set.

    steps:    (value_to_send, bit9) in order
    captures: step index -> the address whose content that step's reply holds
    """

    steps: list[tuple[int, int]] = field(default_factory=list)
    captures: dict[int, int] = field(default_factory=dict)

    @property
    def wire_bytes(self) -> int:
        return len(self.steps)

    @property
    def parity_switches(self) -> int:
        n = 0
        prev = 0  # programs are built assuming we arrive in bit9 = 0
        for _, bit9 in self.steps:
            if bit9 != prev:
                n += 1
                prev = bit9
        return n


def build_program(addresses: list[int]) -> ReadProgram:
    """Compile a set of 16-bit byte addresses into the cheapest lock-step sequence.

    Page 0x00 goes first and costs one byte per address with no parity switch. Each
    other page costs one latch byte plus one byte per address, and two parity switches.
    The latch byte's reply is mem[0x00hh], which is captured for free when that address
    was wanted too.
    """
    wanted = sorted(set(a & 0xFFFF for a in addresses))
    prog = ReadProgram()
    still_needed = set(wanted)

    pages: dict[int, list[int]] = {}
    for addr in wanted:
        pages.setdefault(addr >> 8, []).append(addr & 0xFF)

    # Page 0x00 first: no latch byte needed, no parity switch.
    for low in pages.pop(0x00, []):
        prog.captures[len(prog.steps)] = low
        prog.steps.append((low, 0))
        still_needed.discard(low)

    for page in sorted(pages):
        lows = pages[page]
        # Latch byte. Its reply is mem[0x00:page] -- free data if we wanted it.
        latch_step = len(prog.steps)
        prog.steps.append((page, 0))
        if page in still_needed:
            prog.captures[latch_step] = page
            still_needed.discard(page)
        for low in lows:
            prog.captures[len(prog.steps)] = (page << 8) | low
            prog.steps.append((low, 1))

    return prog


# --------------------------------------------------------------------------- results


@dataclass
class LinkCheck:
    """Result of checking that a link returns *correct* bytes, not merely some bytes.

    A cable whose K-line transceiver cannot slew at 125 kBd still answers every
    transfer -- it just answers with corrupted bytes. So "did a byte come back" proves
    nothing, and the content has to be checked. Two kinds:

    "stability"
        Read the same window twice at the current baud rate and compare. Needs only
        one speed, so it works after the ECU has already left 10417 baud. Catches the
        realistic failure, where bad slew produces *varying* bit errors. It cannot
        catch a corruption that happens to be stable and repeatable.

    "cross-speed"
        Compare a window captured over a known-good 10417 link against the same window
        read at 125000. Catches stable corruption too, but costs a second init pulse
        and tolerates less RAM drift, so the probe tool does it on request rather than
        every connect.

    Either way this is a heuristic, not a proof: ECU RAM genuinely changes between
    snapshots. Over a 32-byte window, though, a corrupted link cannot reach the
    threshold by chance, and a sound link only falls below it if nearly every byte in
    the window moved.
    """

    ok: bool
    agreed: int
    total: int
    mismatches: list[tuple[int, int, int]]  # (addr, first, second)
    kind: str = "stability"

    @property
    def fraction(self) -> float:
        return self.agreed / self.total if self.total else 0.0

    def describe(self) -> str:
        pct = self.fraction * 100
        head = (
            f"link check ({self.kind}): {self.agreed}/{self.total} bytes agree "
            f"({pct:.0f}%)"
        )
        if self.ok or not self.mismatches:
            return head
        shown = ", ".join(
            f"0x{a:04X}: {b:02X}/{c:02X}" for a, b, c in self.mismatches[:6]
        )
        return f"{head}\n  disagreements: {shown}"


@dataclass
class ConnectResult:
    """Everything the bench needs to know about how the handshake went."""

    ok: bool
    highspeed: bool
    pulse_ms: float
    request: int
    raw_reply: list[int]
    echo_detected: bool
    ack: int | None
    baudrate: int
    attempts: int
    note: str = ""
    link: LinkCheck | None = None

    def describe(self) -> str:
        lines = [
            f"  result       : {'CONNECTED' if self.ok else 'FAILED'}",
            f"  attempts     : {self.attempts}",
            f"  pulse width  : {self.pulse_ms:.1f} ms  (ECU window ~70-99 ms)",
            f"  request      : 0x{self.request:02X} "
            f"({'highspeed' if self.highspeed else 'slow'})",
            f"  bytes back   : {[f'0x{b:02X}' for b in self.raw_reply]}",
            f"  cable echo   : {'yes' if self.echo_detected else 'no'}",
            f"  ack          : "
            + (f"0x{self.ack:02X}" if self.ack is not None else "none"),
            f"  baud now     : {self.baudrate}",
        ]
        if self.link is not None:
            lines.append(f"  {self.link.describe()}".replace("\n", "\n  "))
        if self.note:
            lines.append(f"  note         : {self.note}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- session


class McMessSession:
    """A live McMESS read session."""

    def __init__(self, transport: KLineTransport) -> None:
        self.tp = transport
        self.connected = False
        self.highspeed = False
        self._latch: int | None = None
        self._last_activity = 0.0

    # -- connection --------------------------------------------------------

    def connect(
        self,
        highspeed: bool = True,
        pulse: float = 0.085,
        attempts: int = 3,
        idle: float = 0.3,
    ) -> ConnectResult:
        """Run the init pulse and handshake.

        Sequence (docs/mcmess-018cg.md section 2): idle high, ~85 ms low, release, send
        0xA5 or 0xA6 at 10417 baud with bit9 = 0, expect 0x0C back. Then, for 0xA6 only,
        wait for the ECU's next 10 ms task call and move our port to 125000 baud.
        """
        request = REQ_FAST if highspeed else REQ_SLOW
        last = ConnectResult(
            ok=False,
            highspeed=highspeed,
            pulse_ms=0.0,
            request=request,
            raw_reply=[],
            echo_detected=False,
            ack=None,
            baudrate=BAUD_SLOW,
            attempts=0,
            note="no attempt ran",
        )

        for attempt in range(1, attempts + 1):
            self.connected = False
            self._latch = None

            self.tp.set_baudrate(BAUD_SLOW)
            # A failed attempt most often means the responder is still inside a
            # live session, where it ignores init pulses outright. Retrying after
            # the usual short idle would spend every remaining attempt inside that
            # dead window, making attempts=3 behave exactly like attempts=1 -- so
            # a retry waits out the session timeout first. See SESSION_REARM.
            self.tp.idle_high(idle if attempt == 1 else max(idle, SESSION_REARM))
            pulse_ms = self.tp.break_pulse(pulse) * 1000.0

            raw = self.tp.probe_echo(request, bit9=0, timeout=0.2)
            echo, ack, note = self._interpret_handshake(request, raw)

            last = ConnectResult(
                ok=False,
                highspeed=highspeed,
                pulse_ms=pulse_ms,
                request=request,
                raw_reply=raw,
                echo_detected=echo,
                ack=ack,
                baudrate=self.tp.baudrate,
                attempts=attempt,
                note=note,
            )

            if ack != ACK_BYTE:
                continue

            self.tp.echo = echo
            self.connected = True
            self.highspeed = False
            self._touch()

            if not highspeed:
                if not self.link_answers():
                    last.note = (
                        "ack received, but reads at 10417 baud do not answer. Check "
                        "the cable's receive path."
                    )
                    self.connected = False
                    return last
                last.ok = True
                last.baudrate = self.tp.baudrate
                return last

            # The ECU reloads SP_BAUD on its next 10 ms task call, and from then on it
            # is deaf at 10417 -- so there is no window in which to capture a slow
            # reference. Give it margin, follow it up to 125000, then validate by
            # re-reading the same window and checking the two reads agree.
            sleep_until(time.perf_counter() + 0.020)
            self.tp.set_baudrate(BAUD_FAST)
            self.tp.drain()
            self.highspeed = True

            check = self.verify_stability()
            last.link = check
            last.baudrate = self.tp.baudrate

            if check.ok:
                last.ok = True
                return last

            last.note = (
                "the handshake and the 10417 baud link are both fine, but at 125000 "
                "baud two reads of the same memory window disagree -- so bytes are "
                "being corrupted, not lost. The usual cause is the cable's K-line "
                "transceiver being unable to slew that fast. Re-run with --slow."
            )
            self.connected = False
            self.highspeed = False
            return last

        return last

    def snapshot(self, addrs=LINK_CHECK_WINDOW) -> dict[int, int]:
        """Read the link-check window (or any address set) in one pass."""
        return self.read_bytes(list(addrs))

    def verify_stability(
        self,
        addrs=LINK_CHECK_WINDOW,
        threshold: float = LINK_CHECK_THRESHOLD,
    ) -> LinkCheck:
        """Read a window twice at the current baud rate and compare the two reads.

        Works at a single speed, which is what makes it usable after the ECU has
        already left 10417 baud. See LinkCheck for what it can and cannot catch.
        """
        try:
            first = self.snapshot(addrs)
            second = self.snapshot(addrs)
        except TimeoutError_:
            return LinkCheck(
                ok=False, agreed=0, total=len(addrs), mismatches=[], kind="stability"
            )
        return self._compare(first, second, threshold, "stability")

    def verify_link(
        self,
        reference: dict[int, int],
        threshold: float = LINK_CHECK_THRESHOLD,
    ) -> LinkCheck:
        """Compare a window captured earlier (at another baud rate) against now.

        Stricter than verify_stability because it also catches corruption that is
        stable, but it needs a reference taken over a known-good link -- which, since
        the ECU leaves 10417 baud the moment it acks 0xA6, means a separate slow
        session and a second init pulse. The probe tool drives that.
        """
        try:
            after = self.snapshot(list(reference))
        except TimeoutError_:
            return LinkCheck(
                ok=False,
                agreed=0,
                total=len(reference),
                mismatches=[],
                kind="cross-speed",
            )
        return self._compare(reference, after, threshold, "cross-speed")

    @staticmethod
    def _compare(
        first: dict[int, int],
        second: dict[int, int],
        threshold: float,
        kind: str,
    ) -> LinkCheck:
        mismatches = [
            (addr, value, second.get(addr, -1))
            for addr, value in sorted(first.items())
            if second.get(addr) != value
        ]
        total = len(first)
        agreed = total - len(mismatches)
        ok = total > 0 and (agreed / total) >= threshold
        return LinkCheck(
            ok=ok, agreed=agreed, total=total, mismatches=mismatches, kind=kind
        )

    @staticmethod
    def _interpret_handshake(
        request: int, raw: list[int]
    ) -> tuple[bool, int | None, str]:
        """Work out echo presence and the ack from the raw bytes seen.

        Done by position, not value: a cable that echoes always echoes, so the first
        byte is the echo and the second is the ECU. Deciding this once here keeps
        xfer() from having to guess per transfer.
        """
        if not raw:
            return False, None, (
                "nothing came back. Check: ignition on, K-line and ground wired, "
                "correct COM port, and that the cable is not an inverting type."
            )
        if len(raw) == 1:
            if raw[0] == ACK_BYTE:
                return False, ACK_BYTE, "cable does not echo transmitted bytes"
            if raw[0] == request:
                return True, None, (
                    "saw our own echo but no ack from the ECU. The init pulse was "
                    "probably outside the ~70-99 ms window, or the ECU is not in a "
                    "state that accepts a McMESS session."
                )
            return False, raw[0], f"unexpected single byte 0x{raw[0]:02X}"
        if raw[0] == request:
            ack = raw[1]
            note = "cable echoes transmitted bytes"
            if ack != ACK_BYTE:
                note = (
                    f"echo seen, but ack was 0x{ack:02X} instead of 0x{ACK_BYTE:02X}. "
                    "If this is 0x0D the ECU is running an ME7-style responder, not "
                    "the 018CG one."
                )
            return True, ack, note
        if ACK_BYTE in raw:
            return False, ACK_BYTE, (
                f"ack found but the byte stream is odd: {[hex(b) for b in raw]}. "
                "Possible framing or baud-rate mismatch."
            )
        return False, raw[0], (
            f"unexpected bytes {[hex(b) for b in raw]} -- likely a baud-rate or "
            "parity mismatch, or a counterfeit cable mishandling mark/space parity."
        )

    def close(self) -> None:
        """Let the session lapse. There is no disconnect command; silence is enough."""
        self.connected = False
        self._latch = None

    def __enter__(self) -> "McMessSession":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- bookkeeping -------------------------------------------------------

    def _touch(self) -> None:
        self._last_activity = time.perf_counter()

    @property
    def idle_seconds(self) -> float:
        return time.perf_counter() - self._last_activity

    def reset_latch(self) -> None:
        """Forget the cached high-address latch, forcing the next read to set it.

        Used by bench tests that need to control exactly which latch bytes go out.
        """
        self._latch = None

    def _require(self) -> None:
        if not self.connected:
            raise NotConnectedError("no live McMESS session; call connect() first")

    def link_answers(self) -> bool:
        """Whether the link still returns bytes at all.

        Deliberately narrow: this says nothing about whether those bytes are correct.
        Use verify_link() after any baud change, where corruption rather than silence
        is the failure mode.
        """
        try:
            for _ in range(3):
                self.read_byte(PING_ADDR)
            return True
        except (TimeoutError_, HandshakeError):
            return False

    def keepalive(self, force: bool = False) -> None:
        """Keep the 2 s session timeout at bay when not actively logging."""
        self._require()
        if force or self.idle_seconds > KEEPALIVE_INTERVAL:
            self.read_byte(PING_ADDR)

    # -- reads -------------------------------------------------------------

    def _step(self, value: int, bit9: int, timeout: float) -> int:
        reply, _rtt = self.tp.xfer(value, bit9, timeout=timeout)
        if bit9 == 0:
            self._latch = value & 0xFF
        self._touch()
        return reply

    def read_byte(self, addr: int, timeout: float = 0.05) -> int:
        """Read one byte of ECU memory."""
        self._require()
        addr &= 0xFFFF
        page, low = addr >> 8, addr & 0xFF
        if page == 0x00:
            # A bit9 = 0 byte both sets the latch and returns mem[0x00:low].
            return self._step(low, 0, timeout)
        if self._latch != page:
            self._step(page, 0, timeout)
        return self._step(low, 1, timeout)

    def read_bytes(
        self, addresses: list[int], timeout: float = 0.05
    ) -> dict[int, int]:
        """Read a set of scattered byte addresses in the cheapest order."""
        self._require()
        prog = build_program(addresses)
        return self.run_program(prog, timeout=timeout)

    def run_program(
        self, prog: ReadProgram, timeout: float = 0.05
    ) -> dict[int, int]:
        """Execute a precompiled program once. This is the logger's hot loop."""
        self._require()
        # Programs are compiled assuming we start in bit9 = 0.
        self.tp.set_bit9(0)
        out: dict[int, int] = {}
        for index, (value, bit9) in enumerate(prog.steps):
            reply = self._step(value, bit9, timeout)
            addr = prog.captures.get(index)
            if addr is not None:
                out[addr] = reply
        return out

    def read_block(
        self, start: int, length: int, timeout: float = 0.05
    ) -> bytes:
        """Read a contiguous memory range. Used for dumps, not for logging."""
        self._require()
        out = bytearray()
        addr = start & 0xFFFF
        end = addr + length
        while addr < end:
            page, low = addr >> 8, addr & 0xFF
            if page == 0x00:
                out.append(self._step(low, 0, timeout))
                addr += 1
                continue
            if self._latch != page:
                self._step(page, 0, timeout)
            # Stay in bit9 = 1 for the rest of this page: one byte each.
            while addr < end and (addr >> 8) == page:
                out.append(self._step(addr & 0xFF, 1, timeout))
                addr += 1
        return bytes(out)
