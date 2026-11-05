"""Bench bring-up tool for McMESS on a Bosch M3.8 ECU.

Everything in docs/mcmess-018cg.md section 6 that is tagged [VERIFY] gets answered by
one of these subcommands. Run them roughly in this order:

    py tools/probe.py ports                      # find the cable, check it is real FTDI
    py tools/probe.py latency                    # FTDI latency timer must be 1 ms
    py tools/probe.py connect  --port COM3       # init pulse + handshake
    py tools/probe.py timing   --port COM3       # real round-trip time of your cable
    py tools/probe.py latch    --port COM3       # the critical latch assumption
    py tools/probe.py watch    --port COM3       # live channel values
    py tools/probe.py dump     --port COM3 --start 0xC800 --length 256
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from serial.tools import list_ports  # noqa: E402

from mcmess import (  # noqa: E402
    BAUD_FAST,
    BAUD_SLOW,
    KLineTransport,
    McMessError,
    McMessSession,
    TimeoutError_,
    build_program,
)
from mcmess.channels import (  # noqa: E402
    BYTE_ORDERS,
    PAGE0_STARTER,
    ChannelSet,
    csv_number,
    get_byte_order,
    load_channels,
    set_byte_order,
    word_value,
)
from mcmess.session import PING_ADDR, SESSION_REARM  # noqa: E402

RULE = "-" * 72


def hexlist(values) -> str:
    return " ".join(f"{v:02X}" for v in values)


def _load(args: argparse.Namespace) -> ChannelSet:
    """Load channel definitions, honouring --database and --byte-order."""
    order = getattr(args, "byte_order", None)
    if order:
        set_byte_order(order)
    db_path = getattr(args, "database", None)
    channel_set = load_channels(db_path) if db_path else load_channels()
    no_conv = len(channel_set.without_conversion)
    if no_conv:
        print(
            f"note: {no_conv} of {len(channel_set)} channels have no usable conversion "
            "and are reported raw ('channels --raw-only' to list them)\n"
        )
    return channel_set


def add_channel_args(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "--database", help="override the generated channel/conversion JSON"
    )
    sub.add_argument(
        "--byte-order",
        choices=BYTE_ORDERS,
        default=None,
        help=(
            "which end of a 16-bit channel is most significant "
            f"(default {get_byte_order()}, matching the 80C196)"
        ),
    )


# --------------------------------------------------------------------------- ports


def cmd_ports(args: argparse.Namespace) -> int:
    ports = list(list_ports.comports())
    if not ports:
        print("No serial ports found. Plug the K-line cable in.")
        return 1

    print(f"{len(ports)} serial port(s):\n")
    for p in ports:
        vid = f"{p.vid:04X}" if p.vid is not None else "----"
        pid = f"{p.pid:04X}" if p.pid is not None else "----"
        print(f"  {p.device}")
        print(f"      description : {p.description}")
        print(f"      VID:PID     : {vid}:{pid}")
        print(f"      serial      : {p.serial_number}")
        print(f"      hwid        : {p.hwid}")
        print(f"      verdict     : {_chip_verdict(p)}")
        print()

    print(RULE)
    print(
        "Mark/space parity is the whole basis of 9-bit transfer here. Genuine FTDI\n"
        "FT232R/FT232H handle it properly. CH340 and counterfeit FTDI parts often do\n"
        "not, and will fail in confusing ways (wrong or missing handshake ack)."
    )
    return 0


def _chip_verdict(p) -> str:
    vid, pid = p.vid, p.pid
    if vid == 0x0403:
        return "FTDI VID - good. Confirm it is genuine (FT_PROG or FTDI's detector)."
    if vid == 0x1A86:
        return "CH340/CH341 - mark/space parity is unreliable. Expect trouble."
    if vid == 0x10C4:
        return "Silicon Labs CP210x - mark/space parity support is patchy."
    if vid == 0x067B:
        return "Prolific PL2303 - avoid; known parity and timing problems."
    if vid is None:
        return "not a USB serial device (onboard/Bluetooth port) - not usable here"
    return "unknown chip - try it, but FTDI is the safe choice"


# --------------------------------------------------------------------------- latency


def cmd_latency(args: argparse.Namespace) -> int:
    print("FTDI latency timer\n")
    print(
        "The FTDI driver buffers received bytes until either 62 bytes arrive or the\n"
        "latency timer expires. The default is 16 ms, which would cap this logger at\n"
        "about 60 bytes/s. It must be set to 1 ms.\n"
    )
    found = _report_latency_timers()
    print(RULE)
    print(
        "To change it:\n"
        "  Device Manager -> Ports (COM & LPT) -> your cable -> Properties\n"
        "  -> Port Settings -> Advanced -> Latency Timer (msec) -> 1\n"
        "Then unplug and replug the cable.\n"
        "While you are there, set 'Minimum Read Timeout' and 'Minimum Write Timeout'\n"
        "to 0 if they are not already."
    )
    return 0 if found else 1


def _report_latency_timers() -> bool:
    if sys.platform != "win32":
        print("  (registry check is Windows-only)")
        return False
    try:
        import winreg
    except ImportError:
        return False

    base = r"SYSTEM\CurrentControlSet\Enum\FTDIBUS"
    found = False
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as root:
            index = 0
            while True:
                try:
                    device = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                found = True
                _report_one_latency(winreg, base, device)
    except FileNotFoundError:
        print("  No FTDIBUS devices in the registry - no FTDI cable has been attached.")
    except PermissionError:
        print("  Cannot read the registry (needs an elevated shell).")
    return found


def _report_one_latency(winreg, base: str, device: str) -> None:
    path = rf"{base}\{device}\0000\Device Parameters"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
            latency, _ = winreg.QueryValueEx(key, "LatencyTimer")
            try:
                port, _ = winreg.QueryValueEx(key, "PortName")
            except FileNotFoundError:
                port = "?"
            flag = "OK" if latency == 1 else "TOO HIGH - fix this"
            print(f"  {device}")
            print(f"      port         : {port}")
            print(f"      LatencyTimer : {latency} ms   [{flag}]")
    except FileNotFoundError:
        print(f"  {device}: no Device Parameters (not currently attached?)")
    except PermissionError:
        print(f"  {device}: access denied")


# --------------------------------------------------------------------------- helpers


def make_transport(args: argparse.Namespace) -> KLineTransport:
    echo = None
    if getattr(args, "echo", None) is not None:
        echo = args.echo
    return KLineTransport(
        port=args.port,
        baudrate=BAUD_SLOW,
        stopbits=args.stopbits,
        echo=echo,
        dtr=args.dtr,
        rts=args.rts,
    )


def open_session(args: argparse.Namespace) -> tuple[KLineTransport, McMessSession]:
    """Open the port, connect, and bail out loudly if the handshake fails."""
    tp = make_transport(args)
    tp.open()
    session = McMessSession(tp)
    highspeed = not args.slow
    print(f"Connecting on {args.port} ({'125000' if highspeed else '10417'} baud)...")
    result = session.connect(
        highspeed=highspeed, pulse=args.pulse / 1000.0, attempts=args.attempts
    )
    print(result.describe())
    print()
    if not result.ok:
        tp.close()
        raise SystemExit("handshake failed - nothing further can run")
    return tp, session


def add_link_args(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--port", required=True, help="serial port, e.g. COM3")
    sub.add_argument(
        "--slow",
        action="store_true",
        help="stay at 10417 baud instead of requesting 125000",
    )
    sub.add_argument(
        "--pulse",
        type=float,
        default=85.0,
        help="init pulse width in ms (ECU window is ~70-99, default 85)",
    )
    sub.add_argument("--attempts", type=int, default=3, help="handshake attempts")
    sub.add_argument(
        "--stopbits", type=int, choices=(1, 2), default=1, help="stop bits we transmit"
    )
    sub.add_argument(
        "--echo",
        dest="echo",
        action="store_true",
        default=None,
        help="force 'cable echoes transmitted bytes' instead of auto-detecting",
    )
    sub.add_argument(
        "--no-echo", dest="echo", action="store_false", help="force no echo"
    )
    sub.add_argument(
        "--dtr", type=int, choices=(0, 1), default=None, help="force DTR line state"
    )
    sub.add_argument(
        "--rts", type=int, choices=(0, 1), default=None, help="force RTS line state"
    )


# --------------------------------------------------------------------------- connect


def cmd_connect(args: argparse.Namespace) -> int:
    if args.cross_check:
        return _cmd_connect_cross_check(args)
    tp, session = open_session(args)
    try:
        print("Link check: reading STATVS (0x0088) ten times")
        values = []
        for _ in range(10):
            values.append(session.read_byte(PING_ADDR))
        print(f"  values: {hexlist(values)}")
        print()
        print(RULE)
        print(tp.stats.summary())
        return 0
    except McMessError as exc:
        print(f"ERROR: {exc}")
        return 1
    finally:
        tp.close()


def _cmd_connect_cross_check(args: argparse.Namespace) -> int:
    """The strict version of the highspeed check: capture slow, re-init fast, compare.

    connect(highspeed=True) validates the fast link by reading a window twice and
    checking the two reads agree, which catches bad slew producing varying bit errors.
    It cannot catch corruption that repeats identically. This does: the reference comes
    over a 10417 baud link that the handshake already proved, at the cost of a second
    init pulse and less tolerance for RAM drift in between.
    """
    if args.slow:
        print("--cross-check compares 10417 against 125000; drop --slow.")
        return 1

    tp = make_transport(args)
    tp.open()
    session = McMessSession(tp)
    try:
        print(f"Step 1/3: connecting at 10417 baud on {args.port}")
        slow = session.connect(
            highspeed=False, pulse=args.pulse / 1000.0, attempts=args.attempts
        )
        print(slow.describe())
        if not slow.ok:
            print("\nThe slow link failed, so there is nothing to compare against.")
            return 1

        print("\nStep 2/3: capturing a reference window over the slow link")
        reference = session.snapshot()
        print(f"  {len(reference)} bytes from 0x{min(reference):04X}-0x{max(reference):04X}")
        print(f"  {hexlist(reference[a] for a in sorted(reference))}")

        # The responder ignores init pulses while the slow session is still live,
        # so the second handshake has to wait that session out. Without this the
        # step fails with a stale or zero ack and looks like a cable fault.
        print(f"\nWaiting {SESSION_REARM:.1f} s for the slow session to lapse")
        time.sleep(SESSION_REARM)

        print("Step 3/3: re-initialising at 125000 baud and re-reading")
        fast = session.connect(
            highspeed=True, pulse=args.pulse / 1000.0, attempts=args.attempts
        )
        print(fast.describe())
        if not fast.ok:
            print("\nHighspeed already failed its own stability check; see the note.")
            return 1

        cross = session.verify_link(reference)
        print()
        print(RULE)
        print(cross.describe())
        print()
        if cross.ok:
            print(
                "125000 baud agrees with what the proven 10417 link reported.\n"
                "Open question 3 answered: this cable handles highspeed. Use it."
            )
            return 0
        print(
            "125000 baud disagrees with the slow link, yet repeats itself consistently\n"
            "enough to pass the stability check - that is the signature of a\n"
            "transceiver corrupting bits the same way every frame. Log with --slow.\n"
            "Some RAM drift between the two snapshots is normal; a handful of\n"
            "mismatches is fine, a wholesale disagreement is not."
        )
        return 1
    except McMessError as exc:
        print(f"ERROR: {exc}")
        return 1
    finally:
        tp.close()


# --------------------------------------------------------------------------- timing


def cmd_timing(args: argparse.Namespace) -> int:
    tp, session = open_session(args)
    try:
        print(RULE)
        print(f"A. {args.count} page-0 reads (one wire byte each, no parity switch)")
        tp.stats.rtt.clear()
        t0 = time.perf_counter()
        for _ in range(args.count):
            session.read_byte(PING_ADDR)
        wall_a = time.perf_counter() - t0
        print("   " + tp.stats.summary().replace("\n", "\n   "))
        print(
            f"   wall clock: {wall_a * 1000:.0f} ms"
            f"  ->  {args.count / wall_a:.0f} reads/s"
        )

        print()
        print(f"B. {args.count} reads alternating bit9 (worst case: parity switch each)")
        tp.stats.rtt.clear()
        switches_before = tp.stats.parity_switches
        t0 = time.perf_counter()
        for i in range(args.count):
            if i % 2:
                session.read_byte(0xC800)
            else:
                session.read_byte(PING_ADDR)
        wall_b = time.perf_counter() - t0
        switches = tp.stats.parity_switches - switches_before
        print("   " + tp.stats.summary().replace("\n", "\n   "))
        print(
            f"   wall clock: {wall_b * 1000:.0f} ms"
            f"  ->  {args.count / wall_b:.0f} reads/s"
            f"   ({switches} parity switches)"
        )
        per_switch = (wall_b - wall_a) / switches if switches else 0.0
        print(f"   implied cost of one parity switch: {per_switch * 1000:.2f} ms")

        print()
        print(RULE)
        _timing_verdict(wall_a / args.count, tp.baudrate)
        return 0
    except McMessError as exc:
        print(f"ERROR: {exc}")
        return 1
    finally:
        tp.close()


def _timing_verdict(per_byte: float, baud: int) -> None:
    rate = 1.0 / per_byte
    print(f"Measured: {per_byte * 1000:.2f} ms per byte = {rate:.0f} bytes/s at {baud} baud")
    print()
    frame_ms = 11_000.0 / baud
    floor_ms = 2 * frame_ms + 0.5
    print(
        f"Protocol floor at this baud is about {floor_ms:.2f} ms "
        f"({frame_ms:.3f} ms per frame each way, plus ~0.5 ms average wait for the\n"
        f"ECU's 1 ms responder tick). Anything above that is PC and USB overhead."
    )
    overhead = per_byte * 1000 - floor_ms
    print(f"Your overhead: {overhead:.2f} ms per byte.")
    print()
    if overhead > 8:
        print(
            "That is high. Check the FTDI latency timer is 1 ms "
            "('probe.py latency'), and close anything else using the port."
        )
    elif overhead > 3:
        print("Normal for a PC USB-serial round trip.")
    else:
        print("Better than expected - the cable and driver are behaving well.")
    print()
    print(f"At {rate:.0f} bytes/s a 10-channel page-0 set samples at {rate / 10:.0f} Hz,")
    print(f"a 20-channel set at {rate / 20:.0f} Hz.")


# --------------------------------------------------------------------------- latch


def cmd_latch(args: argparse.Namespace) -> int:
    """Answer open question 1: does a bit9=1 transfer leave the high-address latch alone?

    If it does, a page group of k channels costs k+1 wire bytes. If it does not, every
    read costs 2 bytes and throughput halves.

    The test survives live, changing RAM because it compares *where the data came from*,
    not exact values:

      S0  page 0x00 offsets, read directly
      S1  latch page P once, then stream offsets with bit9=1     <- assumes persistence
      S2  re-latch page P before every offset                    <- always correct

    S1 == S2 means the latch persists. S1 matching S0 instead means a bit9=1 transfer
    clobbers the latch with its own low byte.
    """
    page = args.page
    count = args.count
    offsets = list(range(args.offset, args.offset + count))

    tp, session = open_session(args)
    try:
        print(RULE)
        print(f"Latch persistence test on page 0x{page:02X}, offsets "
              f"0x{offsets[0]:02X}..0x{offsets[-1]:02X}\n")

        # S2 first: always-correct reference, re-latching every time.
        s2 = []
        for off in offsets:
            session.reset_latch()  # force a fresh latch byte
            s2.append(session.read_byte((page << 8) | off))

        # S1: latch once, then stream with bit9 = 1.
        session.reset_latch()
        s1 = []
        tp.xfer(page, 0, timeout=0.05)
        for off in offsets:
            reply, _ = tp.xfer(off, 1, timeout=0.05)
            s1.append(reply)

        # S0: page 0x00 for the same offsets.
        session.reset_latch()
        s0 = []
        for off in offsets:
            reply, _ = tp.xfer(off, 0, timeout=0.05)
            s0.append(reply)

        print(f"  off : {' '.join(f'{o:02X}' for o in offsets)}")
        print(f"  S0  : {hexlist(s0)}   (page 0x00, direct)")
        print(f"  S1  : {hexlist(s1)}   (latch once, stream bit9=1)")
        print(f"  S2  : {hexlist(s2)}   (re-latch every byte)")
        print()

        tail = slice(1, None)
        match_s2 = sum(a == b for a, b in zip(s1[tail], s2[tail]))
        match_s0 = sum(a == b for a, b in zip(s1[tail], s0[tail]))
        n = max(1, count - 1)
        print(f"  S1 vs S2 agreement (ignoring first byte): {match_s2}/{n}")
        print(f"  S1 vs S0 agreement (ignoring first byte): {match_s0}/{n}")
        print()
        print(RULE)
        _latch_verdict(match_s2, match_s0, n, s0, s2)
        return 0
    except McMessError as exc:
        print(f"ERROR: {exc}")
        return 1
    finally:
        tp.close()


def _latch_verdict(match_s2: int, match_s0: int, n: int, s0, s2) -> None:
    if s0[1:] == s2[1:]:
        print(
            "INCONCLUSIVE: page 0x00 and the test page hold identical bytes here, so\n"
            "the two hypotheses cannot be told apart. Re-run with a different --page."
        )
        return
    if match_s2 >= n * 0.8 and match_s2 > match_s0:
        print(
            "LATCH PERSISTS. A bit9=1 transfer does not disturb the high-address\n"
            "latch, so streaming within a page works and a group of k channels costs\n"
            "k+1 wire bytes. The design in docs/mcmess-018cg.md section 3 holds; mark\n"
            "open question 1 as confirmed."
        )
    elif match_s0 >= n * 0.8 and match_s0 > match_s2:
        print(
            "LATCH IS CLOBBERED by bit9=1 transfers - they behave like bit9=0.\n"
            "Every non-page-0 read then costs 2 wire bytes and two parity switches,\n"
            "roughly halving throughput. Fix: make session.read_byte() re-latch before\n"
            "every read, drop the page-grouping optimisation in build_program(), and\n"
            "prefer page-0 channels much more strongly when choosing what to log."
        )
    else:
        print(
            f"UNCLEAR: {match_s2}/{n} agreement with S2 and {match_s0}/{n} with S0.\n"
            "Live RAM may simply be changing too fast at these addresses. Re-run with\n"
            "--page pointing at quieter memory, with the engine off."
        )


# --------------------------------------------------------------------------- word


def cmd_word(args: argparse.Namespace) -> int:
    """Confirm a 16-bit channel reads sensibly.

    Which byte pair a FMT:2 channel occupies is no longer in question -- it was settled
    from the damos at build time (see the word-order block in mcmess/channels.py).
    What remains is the one architectural assumption: that the base address holds the
    low byte, as an 80C196 implies.

    This reads the resolved pair plus its neighbours, shows the value both ways round,
    and checks each against the conversion's DAMOS-declared range. Good targets with
    the engine idling:

      FR     0xC802  lambda integrator; FAK2L puts 1.00 at 0x8000, so expect ~0x8000
      ML     0x004E  air mass flow, small and positive at idle
      QSOLL  0xCB55  target air mass, likewise
    """
    channel_set = _load(args)
    try:
        ch = channel_set.get(args.channel)
    except KeyError as exc:
        print(f"ERROR: {exc}")
        return 1
    if ch.size != 2:
        print(f"ERROR: {ch.name} is a {ch.size}-byte channel; this test is for FMT:2")
        return 1

    conv = ch.conversion
    tp, session = open_session(args)
    try:
        print(RULE)
        print(f"{ch.name} @ 0x{ch.addr:04X}  ({ch.umr}) {ch.description}")
        if ch.has_physical and conv.decl_min is not None:
            print(
                f"valid range per DAMOS: {conv.decl_min:g} .. {conv.decl_max:g} "
                f"{conv.unit}".rstrip()
            )
        print()
        base = ch.base
        if base != ch.addr:
            print(
                f"resolved word: 0x{base:04X}..0x{base + 1:04X}  "
                "(name ends in '+1', so its listed address is the second byte)"
            )
        else:
            print(f"resolved word: 0x{base:04X}..0x{base + 1:04X}")
        print()

        addrs = [base - 1, base, base + 1, base + 2]
        print(f"{'':>11}  " + "  ".join(f"0x{a:04X}" for a in addrs))
        rows = []
        for _ in range(args.count):
            vals = session.read_bytes(addrs)
            rows.append(vals)
            cells = "  ".join(
                (f"*0x{vals[a]:02X}" if a in (base, base + 1) else f" 0x{vals[a]:02X}")
                for a in addrs
            )
            print(f"{'raw bytes':>11}   {cells}")
        print(f"{'':>11}   (* marks the two bytes this channel uses)")

        # Both byte orders, each scored against the DAMOS-declared range on every
        # repeat. An order is only credible if it stays in range every time.
        in_range: dict[str, int] = {}
        print()
        print(f"  {'order':<8} {'raw':>7}  {'physical':>18}  verdict")
        for order in BYTE_ORDERS:
            ok = 0
            shown = ("", "")
            for vals in rows:
                raw = word_value(vals[base], vals[base + 1], order)
                value = conv.apply(raw) if ch.has_physical else None
                if not shown[0]:
                    shown = (
                        f"0x{raw:04X}",
                        f"{value:.4g} {conv.unit}".strip() if value is not None else "-",
                    )
                if value is not None and conv.decl_min is not None:
                    if conv.decl_min - 1e-9 <= value <= conv.decl_max + 1e-9:
                        ok += 1
            in_range[order] = ok
            verdict = (
                "-"
                if not ch.has_physical or conv.decl_min is None
                else "in range"
                if ok == len(rows)
                else f"out of range ({ok}/{len(rows)} ok)"
            )
            print(f"  {order:<8} {shown[0]:>7}  {shown[1]:>18}  {verdict}")

        print()
        print(RULE)
        credible = [o for o, n in in_range.items() if n == len(rows)]
        if not ch.has_physical or conv.decl_min is None:
            print(
                f"{ch.name} has no usable conversion, so there is no plausibility\n"
                "check to apply. Pick a channel with a formula, e.g. --channel FR."
            )
        elif len(credible) == 1:
            print(f"Only '{credible[0]}' stays inside the DAMOS range for this channel.")
            if credible[0] != get_byte_order():
                print(
                    f"That is NOT the current default ('{get_byte_order()}'). Confirm it\n"
                    f"on a second 2-byte channel, then pass --byte-order {credible[0]}\n"
                    "and record the result in docs/mcmess-018cg.md."
                )
            else:
                print("That matches the default, so nothing needs changing.")
        elif credible:
            print(
                "Both byte orders are plausible here - this channel's range is too\n"
                "wide to separate them. Re-run on a channel whose value you can\n"
                "predict: FR should sit near 1.00 at idle, i.e. raw near 0x8000."
            )
        else:
            print(
                "Neither byte order gives a value inside the declared range. Either\n"
                "the link is returning bad data (check 'probe.py connect'), this entry\n"
                "is not a plain 16-bit value, or the conversion does not carry over\n"
                "from the A4 damos to this image. Try another channel before concluding."
            )
        print(
            f"\nDefault is '{get_byte_order()}': the base address holds the low byte, "
            "as an 80C196 implies."
        )
        return 0
    except McMessError as exc:
        print(f"ERROR: {exc}")
        return 1
    finally:
        tp.close()


# --------------------------------------------------------------------------- dump


def cmd_dump(args: argparse.Namespace) -> int:
    tp, session = open_session(args)
    try:
        print(f"Reading {args.length} bytes from 0x{args.start:04X}...")
        t0 = time.perf_counter()
        data = session.read_block(args.start, args.length)
        elapsed = time.perf_counter() - t0
        print(
            f"Got {len(data)} bytes in {elapsed:.2f} s "
            f"({len(data) / elapsed:.0f} bytes/s)\n"
        )
        for row in range(0, len(data), 16):
            chunk = data[row : row + 16]
            text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            print(f"  {args.start + row:04X}  {hexlist(chunk):<47}  {text}")
        if args.out:
            Path(args.out).write_bytes(data)
            print(f"\nWritten to {Path(args.out).resolve()}")
        print()
        print(RULE)
        print(tp.stats.summary())
        return 0
    except McMessError as exc:
        print(f"ERROR: {exc}")
        return 1
    finally:
        tp.close()


# --------------------------------------------------------------------------- watch


def cmd_watch(args: argparse.Namespace) -> int:
    channel_set = _load(args)
    names = args.channels.split(",") if args.channels else PAGE0_STARTER
    names = [n.strip() for n in names if n.strip()]
    try:
        channels = channel_set.select(names)
    except KeyError as exc:
        print(f"ERROR: {exc}")
        return 1

    addresses = ChannelSet.addresses_for(channels)
    prog = build_program(addresses)
    print(f"{len(channels)} channels, {len(addresses)} byte addresses")
    print(
        f"program: {prog.wire_bytes} wire bytes, "
        f"{prog.parity_switches} parity switches per sample\n"
    )
    for ch in channels:
        unit = ch.unit or "raw"
        print(
            f"  {ch.name:<10} 0x{ch.addr:04X}  {ch.size}B  {ch.umr:<9} "
            f"{unit:<10} {ch.description}"
        )
        if ch.suspect:
            print(f"             ^ reported raw: {ch.suspect}")
        elif not ch.has_physical:
            print(f"             ^ reported raw: no formula for {ch.umr}")
    print()

    tp, session = open_session(args)
    writer = None
    handle = None
    start = time.perf_counter()
    samples = 0
    try:
        if args.csv:
            handle = Path(args.csv).open("w", newline="", encoding="utf-8")
            writer = csv.writer(handle)
            writer.writerow(
                ["t"]
                + [f"{c.name} [{c.unit}]" if c.unit else f"{c.name} [raw]" for c in channels]
            )

        print("Ctrl+C to stop\n")
        start = time.perf_counter()
        last_print = 0.0
        while True:
            if args.seconds and (time.perf_counter() - start) >= args.seconds:
                break
            try:
                values = session.run_program(prog)
            except TimeoutError_ as exc:
                print(f"\ntimeout: {exc}")
                break
            samples += 1
            now = time.perf_counter() - start

            cells = []
            row = [f"{now:.3f}"]
            for ch in channels:
                raw = ch.assemble(values)
                if raw is None:
                    cells.append(f"{ch.name}=??")
                    row.append("")
                    continue
                cells.append(f"{ch.name}={ch.format(raw)}")
                value = ch.physical(raw)
                row.append(csv_number(value, 4) if value is not None else str(raw))
            if writer:
                writer.writerow(row)

            if now - last_print >= 0.2:
                last_print = now
                rate = samples / now if now > 0 else 0.0
                # Overwrite in place on a terminal, but one line per update
                # when redirected -- carriage returns make a piped run or a
                # captured log unreadable.
                line = f"{rate:5.1f} Hz  " + "  ".join(cells)
                if sys.stdout.isatty():
                    sys.stdout.write("\r" + line + "   ")
                else:
                    sys.stdout.write(line + "\n")
                sys.stdout.flush()
    except KeyboardInterrupt:
        pass
    finally:
        if handle:
            handle.close()
        elapsed = time.perf_counter() - start
        print("\n")
        print(RULE)
        print(f"{samples} samples in {elapsed:.1f} s = {samples / max(elapsed, 1e-9):.1f} Hz")
        print(tp.stats.summary())
        if args.csv:
            print(f"log: {Path(args.csv).resolve()}")
        tp.close()
    return 0


# --------------------------------------------------------------------------- channels


def cmd_channels(args: argparse.Namespace) -> int:
    channel_set = _load(args)
    if args.rejected:
        print(f"{len(channel_set.rejected)} unresolved row(s) in the channel CSV\n")
        for name, why in channel_set.rejected:
            print(f"  {name:<14} {why}")
        print(
            "\nThese are pseudo-address slots the FR parse could not resolve to a real\n"
            "RAM address. They are skipped rather than read as address 0x0000."
        )
        return 0
    rows = sorted(channel_set, key=lambda c: (c.page, c.addr))
    if args.page is not None:
        rows = [c for c in rows if c.page == args.page]
    if args.raw_only:
        rows = [c for c in rows if not c.has_physical]
        print("channels with no usable conversion (reported raw)\n")
    print(f"{len(rows)} channels\n")
    print(f"  {'name':<12} {'addr':<7} {'sz':<3} {'conv':<10} {'unit':<10} description")
    for ch in rows:
        unit = ch.unit or "-raw-"
        print(
            f"  {ch.name:<12} 0x{ch.addr:04X}  {ch.size:<3} {ch.umr:<10} "
            f"{unit:<10} {ch.description}"
        )
    print()
    pages: dict[int, int] = {}
    for ch in channel_set:
        pages[ch.page] = pages.get(ch.page, 0) + 1
    print("channels per page: " + ", ".join(
        f"0x{p:02X}={n}" for p, n in sorted(pages.items(), key=lambda kv: -kv[1])
    ))
    print(
        f"\nPage 0x00 holds {pages.get(0, 0)} channels and is the cheapest to log "
        "(one wire byte each, no parity switch)."
    )
    no_conv = channel_set.without_conversion
    print(
        f"{len(channel_set) - len(no_conv)}/{len(channel_set)} channels convert to "
        f"physical units; {len(no_conv)} are reported raw."
    )
    caveat = channel_set.meta.get("caveat")
    if caveat and args.provenance:
        print()
        print(RULE)
        print("Provenance")
        print(RULE)
        sources = channel_set.meta.get("sources", {})
        for key, value in sources.items():
            print(f"  {key:<20} {value}")
        print()
        for line in caveat.split(". "):
            if line.strip():
                print(f"  {line.strip().rstrip('.')}.")
    return 0


# --------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="probe.py",
        description="McMESS bench bring-up for Bosch M3.8 / 06A906018CG",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("ports", help="list serial ports and judge the chipset")
    sub.add_parser("latency", help="check the FTDI latency timer")

    p = sub.add_parser("connect", help="init pulse + handshake only")
    add_link_args(p)
    p.add_argument(
        "--cross-check",
        action="store_true",
        help="strictly validate 125000 baud against a reference read at 10417",
    )

    p = sub.add_parser("timing", help="measure real round-trip time and throughput")
    add_link_args(p)
    p.add_argument("--count", type=int, default=300, help="transactions per test")

    p = sub.add_parser("latch", help="verify the high-address latch assumption")
    add_link_args(p)
    p.add_argument(
        "--page", type=lambda s: int(s, 0), default=0xC8, help="page to test (default 0xC8)"
    )
    p.add_argument("--offset", type=lambda s: int(s, 0), default=0x00, help="first offset")
    p.add_argument("--count", type=int, default=16, help="offsets to compare")

    p = sub.add_parser("dump", help="read a contiguous memory range")
    add_link_args(p)
    p.add_argument("--start", type=lambda s: int(s, 0), required=True)
    p.add_argument("--length", type=lambda s: int(s, 0), default=64)
    p.add_argument("--out", help="also write the raw bytes to this file")

    p = sub.add_parser("word", help="settle the 16-bit byte order of a FMT:2 channel")
    add_link_args(p)
    add_channel_args(p)
    p.add_argument("--channel", default="FR", help="2-byte channel to test (default FR)")
    p.add_argument("--count", type=int, default=3, help="repeat reads")

    p = sub.add_parser("watch", help="live channel values, optionally logged to CSV")
    add_link_args(p)
    add_channel_args(p)
    p.add_argument("--channels", help="comma-separated names (default: page-0 starter set)")
    p.add_argument("--csv", help="write samples to this CSV file")
    p.add_argument("--seconds", type=float, default=0.0, help="stop after N seconds")

    p = sub.add_parser("channels", help="list known channels")
    add_channel_args(p)
    p.add_argument("--page", type=lambda s: int(s, 0), default=None, help="filter by page")
    p.add_argument(
        "--rejected", action="store_true", help="show rows that could not be resolved"
    )
    p.add_argument(
        "--raw-only", action="store_true", help="only channels with no usable conversion"
    )
    p.add_argument(
        "--provenance", action="store_true", help="show where the data came from"
    )

    args = parser.parse_args(argv)
    handlers = {
        "ports": cmd_ports,
        "latency": cmd_latency,
        "connect": cmd_connect,
        "timing": cmd_timing,
        "latch": cmd_latch,
        "word": cmd_word,
        "dump": cmd_dump,
        "watch": cmd_watch,
        "channels": cmd_channels,
    }
    try:
        return handlers[args.command](args)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}")
        return 1
    except McMessError as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
