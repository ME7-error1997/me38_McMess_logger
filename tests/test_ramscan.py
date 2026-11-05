"""Checks for tools/ramscan.py, against a synthetic image.

Synthetic rather than the real .bin on purpose: these assert that the MCS-96
operand decoding is right, and a hand-built image is the only way to know the
expected answer exactly. The real image is checked separately, by the --check
flag, against cells we located by hand in Ghidra.

Runs offline. No cable, no ECU, no reverse-engineering files needed.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import ramscan  # noqa: E402

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}\n         got  {got!r}\n         want {want!r}")
        FAILURES.append(label)


def image_of(*chunks: bytes) -> bytes:
    """Pad to a plausible size so offsets look like the real thing."""
    body = b"".join(chunks)
    return body + bytes(0x100 - len(body) % 0x100)


RAM = (0xC000, 0xCFFF)


def main() -> int:
    print("Indexed operands resolve to absolute addresses")
    # ST RWF0, 0xC123   ->  C3 01 23 C1 F0   (zero register, 16-bit displacement)
    img = image_of(bytes([0xC3, 0x01, 0x23, 0xC1, 0xF0]))
    hits = ramscan.scan_indexed(img, RAM, ramscan.KNOWN_BASES)
    check("finds the address", sorted(hits), [0xC123])
    check("classifies ST as a write", hits[0xC123][0][2], "write")
    # LD reads rather than writes.
    img = image_of(bytes([0xA3, 0x01, 0x23, 0xC1, 0xF0]))
    hits = ramscan.scan_indexed(img, RAM, ramscan.KNOWN_BASES)
    check("classifies LD as a read", hits[0xC123][0][2], "read")

    print("\nA known base register is followed, an unknown one is not")
    # MULU RLF8, 0x168[RW24] with RW24 = 0x6900 lands on KHFM at 0x6A68, which is
    # outside the RAM window -- so it must NOT appear as a RAM reference.
    img = image_of(bytes([0x6F, 0x25, 0x68, 0x01, 0xF8]))
    check("base-relative outside the window is ignored",
          ramscan.scan_indexed(img, RAM, ramscan.KNOWN_BASES), {})
    # The same form aimed into the window must resolve.
    disp = 0xC100 - 0x6900
    img = image_of(bytes([0xA3, 0x25, disp & 0xFF, disp >> 8, 0xF0]))
    check("base-relative inside the window resolves",
          sorted(ramscan.scan_indexed(img, RAM, ramscan.KNOWN_BASES)), [0xC100])
    # An unrecognised base cannot be resolved, so it is counted as a blind spot
    # rather than silently dropped -- silently dropping it is what would make a
    # live cell look free.
    img = image_of(bytes([0xA3, 0x0B, 0x00, 0xC1, 0xF0]))
    check("unknown base yields no address", ramscan.scan_indexed(img, RAM, ramscan.KNOWN_BASES), {})
    _, _, unknown = ramscan.scan_unresolvable(img)
    check("unknown base is counted", len(unknown), 1)

    print("\nIndirect and auto-increment are counted, never resolved")
    img = image_of(bytes([0xA2, 0xF0, 0x00]))          # LD RW00,[RWF0]
    indirect, autoinc, _ = ramscan.scan_unresolvable(img)
    check("indirect counted", (len(indirect), len(autoinc)), (1, 0))
    img = image_of(bytes([0xC2, 0xF1, 0x00]))          # ST RW00,[RWF0]+
    indirect, autoinc, _ = ramscan.scan_unresolvable(img)
    check("auto-increment counted separately", (len(indirect), len(autoinc)), (0, 1))

    print("\nImmediate and direct forms are not memory references")
    # A1 = LD immediate. The bytes that follow look exactly like an address and
    # must not be read as one; this is the decoder's main chance to be wrong.
    img = image_of(bytes([0xA1, 0x23, 0xC1, 0xF0]))
    check("immediate is not a reference", ramscan.scan_indexed(img, RAM, ramscan.KNOWN_BASES), {})
    img = image_of(bytes([0xA0, 0x23, 0xF0]))          # LD RWF0,RW23 (register direct)
    check("register-direct is not a window reference",
          ramscan.scan_indexed(img, RAM, ramscan.KNOWN_BASES), {})

    print("\nThe stack and the window registers are located")
    env = ramscan.scan_environment(image_of(bytes([0xA1, 0x00, 0x05, 0x18])), RAM)
    check("SP init found", [v for _, v in env["sp_inits"]], [0x0500])
    check("a stack outside the window is not flagged", env["sp_in_window"], [])
    env = ramscan.scan_environment(image_of(bytes([0xA1, 0x00, 0xC5, 0x18])), RAM)
    check("a stack inside the window IS flagged", env["sp_in_window"], [0xC500])
    # WSR 0x7E maps 0x1F00-0x1F7F into the upper register file. The window base is
    # a six-bit field, so no WSR value can ever reach 0xC000 -- which is the only
    # reason register-direct access can be ignored for this RAM window.
    # A stack whose init sits just ABOVE the window still reaches into it, because
    # it grows down. Reporting that as "outside the window" is how you end up
    # placing a variable in stack space: on the 018CG, SP = 0x0500 and the
    # 119-byte "unreferenced run" at 0x0489-0x04FF is the stack, not free RAM.
    env = ramscan.scan_environment(image_of(bytes([0xA1, 0x00, 0x05, 0x18])), (0x400, 0x4FF))
    check("a stack above the window is flagged as descending into it",
          env["sp_descends_into"], [0x0500])
    check("...and is not reported as being inside it", env["sp_in_window"], [])
    env = ramscan.scan_environment(image_of(bytes([0xA1, 0x00, 0x05, 0x18])), (0xC000, 0xCFFF))
    check("a stack far below the window does not descend into it",
          env["sp_descends_into"], [])

    env = ramscan.scan_environment(image_of(bytes([0xB1, 0x7E, 0x14])), RAM)
    check("WSR reach computed", env["window_reach"], 0x1F7F)
    check("WSR cannot reach the window", env["wsr_overlaps"], False)

    print("\nCandidates are ranked by depth, not merely by absence")
    # Two holes: a wide one and a narrow one. The wide one must win, because the
    # access modes the scan is blind to reach contiguous structures.
    used = set(range(0xC000, 0xC100)) | {0xC110} | set(range(0xC200, 0xCFFF))
    ranked = ramscan.rank_candidates(used, RAM, 2, 3)
    check("deepest candidate sits in the wide hole", 0xC160 <= ranked[0][1] <= 0xC1A0, True)
    check("the narrow gap at 0xC100 is not preferred", ranked[0][1] != 0xC100, True)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): " + ", ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
