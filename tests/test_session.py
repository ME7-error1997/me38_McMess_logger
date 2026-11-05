"""Session-level checks against a simulated 018CG responder. No hardware needed.

    py tests/test_session.py

These do not prove the real ECU behaves this way -- they prove our client behaves
correctly *given* the behaviour documented in docs/mcmess-018cg.md, and that the failure
modes listed under "open questions" are detected rather than silently mis-handled.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcmess.simulator import BAUD_FAST, BAUD_SLOW, FakeEcuSerial, attach

from mcmess import KLineTransport, McMessSession  # noqa: E402

FAILURES: list[str] = []
# Keep the init pulse short in tests; the fake accepts any plausible width.
FAST = {"pulse": 0.002, "idle": 0.002, "attempts": 1}


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}\n         got  {got!r}\n         want {want!r}")
        FAILURES.append(label)


def make_memory(seed: int = 12345) -> dict[int, int]:
    """Structureless fake RAM.

    Must not be an arithmetic function of the address: something like
    (addr * 31 + 7) & 0xFF aliases 0x0203 onto 0xC803, which would quietly mask a
    wrong-address bug in exactly the test meant to catch one.
    """
    rng = random.Random(seed)
    return {addr: rng.randrange(256) for addr in range(0x10000)}


def build(**kwargs):
    """A transport with a fake ECU attached, plus a session on it."""
    memory = kwargs.pop("memory", None)
    if memory is None:
        memory = make_memory()
    tp = KLineTransport(port="FAKE")
    fake = attach(tp, FakeEcuSerial(memory=memory, **kwargs))
    return tp, McMessSession(tp), fake


def main() -> int:
    print("handshake: cable that echoes")
    tp, session, fake = build(echo=True)
    result = session.connect(highspeed=False, **FAST)
    check("connected", result.ok, True)
    check("echo detected", result.echo_detected, True)
    check("ack", result.ack, 0x0C)
    check("raw bytes seen", result.raw_reply, [0xA5, 0x0C])
    check("stayed slow", tp.baudrate, BAUD_SLOW)

    print("\nhandshake: cable that does not echo")
    tp, session, fake = build(echo=False)
    result = session.connect(highspeed=False, **FAST)
    check("connected", result.ok, True)
    check("echo not detected", result.echo_detected, False)
    check("raw bytes seen", result.raw_reply, [0x0C])

    print("\nhandshake: highspeed request")
    tp, session, fake = build(echo=True)
    result = session.connect(highspeed=True, **FAST)
    check("connected", result.ok, True)
    check("requested 0xA6", result.request, 0xA6)
    check("host moved to 125000", tp.baudrate, BAUD_FAST)
    check("ECU moved to 125000", fake.ecu_baud, BAUD_FAST)
    check("session flagged highspeed", session.highspeed, True)

    print("\nhandshake: cable cannot slew at 125 kBd (open question 3)")
    # The failure mode is corruption, not silence -- the ECU answers every byte, just
    # with wrong data. A timeout check cannot see this; only content can.
    tp, session, fake = build(echo=True, fast_ok=False)
    result = session.connect(highspeed=True, **FAST)
    check("reported as failure", result.ok, False)
    check("ack was still received", result.ack, 0x0C)
    check("not left marked connected", session.connected, False)
    check("highspeed flag cleared", session.highspeed, False)
    check("no timeouts occurred", tp.stats.timeouts, 0)
    check("link check ran", result.link is not None, True)
    check("link check failed", result.link.ok, False)
    check("checked by stability", result.link.kind, "stability")
    check("explains corruption", "corrupted" in result.note, True)
    print(f"  ..   {result.link.describe().splitlines()[0]}")

    print("\nhandshake: sound highspeed link passes the stability check")
    tp, session, fake = build(echo=True, fast_ok=True)
    result = session.connect(highspeed=True, **FAST)
    check("connected", result.ok, True)
    check("link check ran", result.link is not None, True)
    check("everything agreed", result.link.agreed, result.link.total)
    check("window is 32 bytes", result.link.total, 32)

    print("\nECU is deaf at 10417 once it has acked 0xA6")
    tp, session, fake = build(echo=True)
    session.connect(highspeed=True, **FAST)
    check("ECU left the slow rate", fake.ecu_baud, BAUD_FAST)
    tp.set_baudrate(BAUD_SLOW)  # pretend we tried to stay slow
    check("link is broken at the old rate", fake.link_ok, False)
    tp.set_baudrate(BAUD_FAST)
    check("and fine again at the new one", fake.link_ok, True)

    print("\nstable corruption slips past stability but not the cross-speed check")
    # Justifies verify_link() existing alongside verify_stability().
    tp, session, fake = build(echo=True, fast_ok=False, stable_corruption=True)
    result = session.connect(highspeed=True, **FAST)
    check("stability check is fooled", result.ok, True)
    check("it saw full agreement", result.link.agreed, result.link.total)
    # Now the stricter route: capture over a known-good slow link, re-init fast.
    tp, session, fake = build(echo=True, fast_ok=False, stable_corruption=True)
    session.connect(highspeed=False, **FAST)
    reference = session.snapshot()
    session.connect(highspeed=True, **FAST)
    cross = session.verify_link(reference)
    check("cross-speed check catches it", cross.ok, False)
    check("labelled cross-speed", cross.kind, "cross-speed")
    # Not "zero agreed": over a 32-byte window a corrupted byte lands on the right
    # value about 1 time in 256, so the odd coincidental match is expected.
    check("well below threshold", cross.fraction < 0.2, True)
    print(f"  ..   {cross.describe().splitlines()[0]}")

    print("\ncross-speed check passes on a sound highspeed link")
    tp, session, fake = build(echo=True, fast_ok=True)
    session.connect(highspeed=False, **FAST)
    reference = session.snapshot()
    session.connect(highspeed=True, **FAST)
    cross = session.verify_link(reference)
    check("passes", cross.ok, True)
    check("everything agreed", cross.agreed, cross.total)

    print("\nhandshake: ECU silent")
    tp, session, fake = build(echo=True, answer_handshake=False)
    result = session.connect(highspeed=False, **FAST)
    check("reported as failure", result.ok, False)
    check("no ack", result.ack, None)
    check("saw only our echo", result.raw_reply, [0xA5])
    check("blames the pulse window", "70-99 ms" in result.note, True)

    print("\nhandshake: nothing at all on the line")
    tp, session, fake = build(echo=False, answer_handshake=False)
    result = session.connect(highspeed=False, **FAST)
    check("reported as failure", result.ok, False)
    check("no bytes", result.raw_reply, [])
    check("suggests wiring checks", "ignition" in result.note, True)

    print("\nhandshake: ME7-style ack 0x0D")
    tp, session, fake = build(echo=True, ack=0x0D)
    result = session.connect(highspeed=False, **FAST)
    check("reported as failure", result.ok, False)
    check("ack reported verbatim", result.ack, 0x0D)
    check("names ME7", "ME7" in result.note, True)

    print("\nhandshake: bit9 must be 0 for the handshake byte")
    tp, session, fake = build(echo=True)
    session.connect(highspeed=False, **FAST)
    check("handshake went out with bit9=0", fake.sent[0], (0xA5, 0))

    print("\nhandshake: retries")
    tp, session, fake = build(echo=True, answer_handshake=False)
    result = session.connect(highspeed=False, pulse=0.002, idle=0.002, attempts=3)
    check("all attempts used", result.attempts, 3)
    check("three pulses, three handshake bytes", len(fake.sent), 3)

    print("\nreads: page 0 costs one byte and no parity switch")
    memory = make_memory()
    tp, session, fake = build(echo=True, memory=memory)
    session.connect(highspeed=True, **FAST)
    before = len(fake.sent)
    switches = tp.stats.parity_switches
    for addr in (0x00A4, 0x00A6, 0x007F, 0x009B):
        check(f"mem[0x{addr:04X}]", session.read_byte(addr), memory[addr])
    check("one wire byte per channel", len(fake.sent) - before, 4)
    check("no parity switches", tp.stats.parity_switches - switches, 0)

    print("\nreads: non-zero page latches once, then streams")
    tp, session, fake = build(echo=True, memory=memory)
    session.connect(highspeed=True, **FAST)
    before = len(fake.sent)
    for addr in (0xC802, 0xC803, 0xC80F):
        check(f"mem[0x{addr:04X}]", session.read_byte(addr), memory[addr])
    check("3 channels cost 4 wire bytes", len(fake.sent) - before, 4)
    check("latch byte went out first", fake.sent[before], (0xC8, 0))

    print("\nreads: scattered set via run_program")
    tp, session, fake = build(echo=True, memory=memory)
    session.connect(highspeed=True, **FAST)
    addrs = [0x00A4, 0x00A6, 0xC802, 0xC803, 0xC900, 0xCE2D, 0x00C8]
    got = session.read_bytes(addrs)
    check("all addresses present", sorted(got), sorted(set(addrs)))
    check("all values correct", got, {a: memory[a] for a in set(addrs)})

    print("\nreads: read_block across a page boundary")
    tp, session, fake = build(echo=True, memory=memory)
    session.connect(highspeed=True, **FAST)
    block = session.read_block(0xC8FD, 6)
    want = bytes(memory[a] for a in range(0xC8FD, 0xC903))
    check("contents", block, want)
    check("re-latched for the new page", (0xC9, 0) in fake.sent, True)

    print("\nreads: read_block inside page 0")
    tp, session, fake = build(echo=True, memory=memory)
    session.connect(highspeed=True, **FAST)
    block = session.read_block(0x00A0, 8)
    check("contents", block, bytes(memory[a] for a in range(0x00A0, 0x00A8)))
    check("never used bit9=1", [b for b in fake.sent if b[1] == 1], [])

    print("\nreads: latch NOT sticky (open question 1 failing case)")
    tp, session, fake = build(echo=True, memory=memory, latch_sticky=False)
    session.connect(highspeed=True, **FAST)
    # Our client assumes stickiness, so the second read of a page must come back wrong.
    # This is what probe.py latch exists to detect before anyone trusts a log.
    check(
        "test addresses are distinguishable",
        memory[0xC803] != memory[0x0203],
        True,
    )
    session.read_byte(0xC802)
    second = session.read_byte(0xC803)
    check("second read is wrong, as expected", second == memory[0xC803], False)
    check("it actually returned mem[0x0203]", second, memory[0x0203])
    print("  ..   confirms the failure mode is real and must be tested on the bench")

    print("\nnot connected")
    tp, session, fake = build(echo=True)
    try:
        session.read_byte(0x0088)
        check("refuses reads without a session", "no error", "NotConnectedError")
    except Exception as exc:
        check("refuses reads without a session", type(exc).__name__, "NotConnectedError")

    print("\ntiming bookkeeping")
    tp, session, fake = build(echo=True, memory=memory)
    session.connect(highspeed=True, **FAST)
    for _ in range(20):
        session.read_byte(0x0088)
    check("round trips recorded", len(tp.stats.rtt) >= 20, True)
    check("no timeouts", tp.stats.timeouts, 0)
    check("replies counted", tp.stats.replies >= 20, True)
    check("frame time at 125k is ~88 us", round(tp.frame_time() * 1e6), 88)
    tp.set_baudrate(BAUD_SLOW)
    check("frame time at 10417 is ~1056 us", round(tp.frame_time() * 1e6), 1056)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
