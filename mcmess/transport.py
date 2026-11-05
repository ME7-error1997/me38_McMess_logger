"""9-bit K-line transport over a plain FTDI serial cable.

The one trick this module exists for: a 9N1 UART frame is bit-for-bit identical to
8 data bits + parity + 1 stop bit, so the 9th data bit can be produced by pinning the
parity bit to a constant.

    bit9 = 0  ->  PARITY_SPACE
    bit9 = 1  ->  PARITY_MARK

On receive we cannot recover the incoming 9th bit (the UART eats it as parity), but
pyserial's Win32 backend sets fErrorChar = 0 and fAbortOnError = 0, so a frame whose
parity bit disagrees with the current setting is still delivered with its 8 data bits
intact. Only an error flag is raised, and we ignore it.

See docs/mcmess-018cg.md section 1.1.
"""

from __future__ import annotations

import ctypes
import sys
import time
from dataclasses import dataclass, field

import serial

from .errors import PortError, TimeoutError_

# Parity value that puts the given level on the wire in the 9th bit position.
_PARITY_FOR_BIT9 = {0: serial.PARITY_SPACE, 1: serial.PARITY_MARK}

BAUD_SLOW = 10417
BAUD_FAST = 125000


# --------------------------------------------------------------------------- timing


class _WindowsTimerResolution:
    """Raise the Windows scheduler tick to 1 ms for the life of the process.

    Without this, time.sleep() granularity is 15.6 ms, which makes the 70-99 ms
    init-pulse window impossible to hit reliably.
    """

    _depth = 0

    @classmethod
    def acquire(cls) -> None:
        if sys.platform != "win32":
            return
        if cls._depth == 0:
            try:
                ctypes.windll.winmm.timeBeginPeriod(1)
            except (AttributeError, OSError):
                pass
        cls._depth += 1

    @classmethod
    def release(cls) -> None:
        if sys.platform != "win32":
            return
        cls._depth = max(0, cls._depth - 1)
        if cls._depth == 0:
            try:
                ctypes.windll.winmm.timeEndPeriod(1)
            except (AttributeError, OSError):
                pass


def sleep_until(deadline: float, spin_margin: float = 0.002) -> None:
    """Sleep until perf_counter() reaches deadline, busy-waiting the last bit.

    Coarse sleep for everything but the final spin_margin seconds, then spin. Keeps
    the error on an 85 ms pulse down to tens of microseconds instead of milliseconds.
    """
    coarse = deadline - spin_margin
    now = time.perf_counter()
    if coarse > now:
        time.sleep(coarse - now)
    while time.perf_counter() < deadline:
        pass


# --------------------------------------------------------------------------- stats


@dataclass
class TransportStats:
    """Counters for diagnosing a flaky cable or a desynced stream."""

    writes: int = 0
    replies: int = 0
    timeouts: int = 0
    echo_mismatch: int = 0
    stray_bytes: int = 0
    parity_switches: int = 0
    baud_switches: int = 0
    rtt: list[float] = field(default_factory=list)

    def summary(self) -> str:
        if self.rtt:
            srt = sorted(self.rtt)
            n = len(srt)
            mn = srt[0] * 1000
            md = srt[n // 2] * 1000
            p95 = srt[min(n - 1, int(n * 0.95))] * 1000
            mx = srt[-1] * 1000
            rate = 1.0 / (sum(self.rtt) / n)
            timing = (
                f"rtt min/med/p95/max = {mn:.2f}/{md:.2f}/{p95:.2f}/{mx:.2f} ms"
                f"  ->  {rate:.0f} bytes/s"
            )
        else:
            timing = "no round trips recorded"
        return (
            f"{timing}\n"
            f"writes={self.writes} replies={self.replies} timeouts={self.timeouts} "
            f"echo_mismatch={self.echo_mismatch} stray={self.stray_bytes} "
            f"parity_switches={self.parity_switches} baud_switches={self.baud_switches}"
        )


# --------------------------------------------------------------------------- transport


class KLineTransport:
    """A K-line port that can send and receive 9-bit frames.

    echo: cheap K-line cables loop the transmitted byte back into RX, because the
    transceiver's receiver sees the wire we are driving. None means "detect it during
    the handshake"; True/False forces it.
    """

    def __init__(
        self,
        port: str,
        baudrate: int = BAUD_SLOW,
        stopbits: int = 1,
        read_timeout: float = 0.05,
        echo: bool | None = None,
        dtr: bool | None = None,
        rts: bool | None = None,
        serial_factory=None,
    ) -> None:
        #: Optional zero-argument callable returning something with serial.Serial's
        #: surface, used instead of opening a real port. The simulator passes one so
        #: the whole stack -- transport, session, scheduler, recorder -- can run with
        #: no hardware.
        self._serial_factory = serial_factory
        self.port_name = port
        self.stopbits = stopbits
        self.read_timeout = read_timeout
        self.echo = echo
        self._dtr = dtr
        self._rts = rts
        self._bit9: int | None = None
        self.stats = TransportStats()
        self.ser: serial.Serial | None = None
        self._initial_baud = baudrate

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> None:
        _WindowsTimerResolution.acquire()
        if self._serial_factory is not None:
            self.ser = self._serial_factory()
            self._bit9 = 0
            self.ser.parity = serial.PARITY_SPACE
            self.ser.timeout = self.read_timeout
            self.ser.baudrate = self._initial_baud
            if self._dtr is not None:
                self.ser.dtr = self._dtr
            if self._rts is not None:
                self.ser.rts = self._rts
            self.drain()
            return
        try:
            self.ser = serial.Serial(
                port=self.port_name,
                baudrate=self._initial_baud,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_SPACE,  # bit9 = 0; the handshake needs it
                stopbits=(
                    serial.STOPBITS_TWO if self.stopbits == 2 else serial.STOPBITS_ONE
                ),
                timeout=self.read_timeout,
                write_timeout=1.0,
                rtscts=False,
                dsrdtr=False,
                xonxoff=False,
            )
        except (serial.SerialException, ValueError, OSError) as exc:
            _WindowsTimerResolution.release()
            raise PortError(f"cannot open {self.port_name}: {exc}") from exc

        self._bit9 = 0
        if self._dtr is not None:
            self.ser.dtr = self._dtr
        if self._rts is not None:
            self.ser.rts = self._rts
        self.drain()

    def close(self) -> None:
        if self.ser is not None:
            try:
                self.ser.break_condition = False
            except Exception:
                pass
            try:
                self.ser.close()
            finally:
                self.ser = None
                _WindowsTimerResolution.release()

    def __enter__(self) -> "KLineTransport":
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def _port(self) -> serial.Serial:
        if self.ser is None:
            raise PortError("port is not open")
        return self.ser

    # -- configuration -----------------------------------------------------

    def set_bit9(self, bit9: int) -> None:
        """Select the 9th bit value for subsequent transmits.

        Costs a SetCommState (a USB control transfer on FTDI, order 1-3 ms), so the
        scheduler groups requests to switch as rarely as possible. Verified: pyserial's
        Win32 _reconfigure_port does not purge the receive buffer, so an in-flight byte
        is not lost -- but we are lock-step anyway, so there never is one.
        """
        if bit9 not in (0, 1):
            raise ValueError(f"bit9 must be 0 or 1, got {bit9!r}")
        if self._bit9 == bit9:
            return
        self._port.parity = _PARITY_FOR_BIT9[bit9]
        self._bit9 = bit9
        self.stats.parity_switches += 1

    def set_baudrate(self, baudrate: int) -> None:
        if self._port.baudrate == baudrate:
            return
        self._port.baudrate = baudrate
        self.stats.baud_switches += 1

    @property
    def baudrate(self) -> int:
        return int(self._port.baudrate)

    def frame_time(self) -> float:
        """Seconds on the wire for one 9-bit frame at the current baud rate."""
        bits = 1 + 9 + self.stopbits
        return bits / float(self._port.baudrate)

    # -- raw line control --------------------------------------------------

    def drain(self) -> int:
        """Discard anything buffered. Returns how many bytes were thrown away."""
        ser = self._port
        n = 0
        try:
            waiting = ser.in_waiting
        except OSError:
            waiting = 0
        if waiting:
            n = len(ser.read(waiting))
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        return n

    def idle_high(self, seconds: float = 0.3) -> None:
        """Hold the line in its idle (high) state, clearing any partial ECU state."""
        self._port.break_condition = False
        sleep_until(time.perf_counter() + seconds)
        self.drain()

    def break_pulse(self, seconds: float = 0.085) -> float:
        """Pull the K-line low for the given time. Returns the measured width.

        The ECU's init handler accepts 7..9 of its ~10 ms task ticks, i.e. roughly
        70-99 ms, and needs a high-to-low edge first. 85 ms sits dead centre.
        Setting and clearing break are each a USB control transfer, so the measured
        width is reported back for sanity checking.
        """
        ser = self._port
        ser.reset_output_buffer()
        ser.break_condition = True
        t0 = time.perf_counter()
        sleep_until(t0 + seconds)
        ser.break_condition = False
        measured = time.perf_counter() - t0
        # The break edge itself looks like framing noise to our own receiver.
        time.sleep(0.005)
        self.drain()
        return measured

    # -- framed transfer ---------------------------------------------------

    def _read_exact(self, count: int, timeout: float) -> bytes:
        """Read exactly count bytes or return fewer if the deadline passes."""
        ser = self._port
        deadline = time.perf_counter() + timeout
        buf = bytearray()
        while len(buf) < count:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break
            chunk = ser.read(count - len(buf))
            if chunk:
                buf += chunk
        return bytes(buf)

    def xfer(self, value: int, bit9: int, timeout: float = 0.05) -> tuple[int, float]:
        """Send one 9-bit frame, wait for the ECU's one-byte answer.

        Returns (reply, round_trip_seconds). Raises TimeoutError_ if nothing comes back.

        Strictly lock-step by necessity: the K-line is half-duplex and the 80C196KR has
        a single-byte receive buffer with no FIFO, so a second byte sent before the
        ECU's 1 ms responder consumes the first would be lost to an RX overrun.
        """
        ser = self._port
        self.set_bit9(bit9)

        stray = ser.in_waiting
        if stray:
            self.stats.stray_bytes += stray
            ser.read(stray)

        t0 = time.perf_counter()
        ser.write(bytes([value & 0xFF]))
        self.stats.writes += 1

        if self.echo:
            got = self._read_exact(1, timeout)
            if not got:
                self.stats.timeouts += 1
                raise TimeoutError_(
                    f"no echo for 0x{value:02X} (bit9={bit9}) within {timeout * 1e3:.0f} ms"
                )
            if got[0] != (value & 0xFF):
                self.stats.echo_mismatch += 1

        got = self._read_exact(1, timeout)
        rtt = time.perf_counter() - t0
        if not got:
            self.stats.timeouts += 1
            raise TimeoutError_(
                f"no reply to 0x{value:02X} (bit9={bit9}) within {timeout * 1e3:.0f} ms"
            )

        self.stats.replies += 1
        self.stats.rtt.append(rtt)
        return got[0], rtt

    def probe_echo(self, value: int, bit9: int, timeout: float = 0.2) -> list[int]:
        """Send one frame and return every byte that comes back, without assumptions.

        Used by the handshake to work out whether this cable echoes transmitted bytes,
        before xfer() is allowed to rely on byte position.
        """
        ser = self._port
        self.set_bit9(bit9)
        self.drain()
        ser.write(bytes([value & 0xFF]))
        self.stats.writes += 1

        deadline = time.perf_counter() + timeout
        out = bytearray()
        quiet_after_first = 0.03
        while time.perf_counter() < deadline:
            chunk = ser.read(1)
            if chunk:
                out += chunk
                # Once something arrived, give the line a short grace period for more.
                deadline = min(deadline, time.perf_counter() + quiet_after_first)
        return list(out)
