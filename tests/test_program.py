"""Offline checks for the read scheduler. No ECU or serial port needed.

    py tests/test_program.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcmess.channels import (  # noqa: E402
    csv_number,
    ChannelSet,
    get_byte_order,
    load_channels,
    set_byte_order,
)
from mcmess.session import build_program  # noqa: E402

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}\n         got  {got!r}\n         want {want!r}")
        FAILURES.append(label)


def simulate(prog, memory: dict[int, int]) -> dict[int, int]:
    """Replay a program against a fake ECU implementing the documented responder.

    we send nn, bit9=0 -> latch := nn, reply mem[0x00nn]
    we send nn, bit9=1 -> latch unchanged, reply mem[latch:nn]
    """
    latch = 0
    out: dict[int, int] = {}
    for index, (value, bit9) in enumerate(prog.steps):
        if bit9 == 0:
            latch = value
            addr = value
        else:
            addr = (latch << 8) | value
        reply = memory.get(addr, 0)
        captured = prog.captures.get(index)
        if captured is not None:
            assert captured == addr, (
                f"step {index}: capture claims 0x{captured:04X} "
                f"but the responder would answer for 0x{addr:04X}"
            )
            out[captured] = reply
    return out


def main() -> int:
    print("build_program: page 0 only")
    prog = build_program([0xA4, 0xA6, 0x7F, 0x9B])
    check("wire bytes == channel count", prog.wire_bytes, 4)
    check("no parity switches", prog.parity_switches, 0)
    check("all bit9 == 0", {b for _, b in prog.steps}, {0})
    check("every step captured", len(prog.captures), 4)

    print("\nbuild_program: single non-zero page")
    prog = build_program([0xC802, 0xC803, 0xC80F])
    check("k+1 wire bytes", prog.wire_bytes, 4)
    # We arrive in bit9=0, so the latch byte is free and only the 0->1 move costs.
    check("one parity switch for the first group", prog.parity_switches, 1)
    check("first step is the latch byte", prog.steps[0], (0xC8, 0))
    check("latch byte not captured", 0 in prog.captures, False)

    print("\nbuild_program: mixed pages")
    addrs = [0xA4, 0xA6, 0xC802, 0xC803, 0xC900, 0xC901]
    prog = build_program(addrs)
    check("wire bytes = 2 + (2+1) + (2+1)", prog.wire_bytes, 8)
    # page0 (no switch) + C8 latch (no switch, still 0) + C8 data (1) +
    # C9 latch (2) + C9 data (3)  =  one for the first group, two for each after.
    check("1 + 2*(groups-1) switches", prog.parity_switches, 3)
    check("page 0 comes first", [s[0] for s in prog.steps[:2]], [0xA4, 0xA6])

    print("\nbuild_program: switch cost grows two per extra page")
    for pages, want in ((1, 1), (2, 3), (3, 5), (4, 7)):
        addrs = [(0xC0 + i) << 8 | 0x10 for i in range(pages)]
        check(
            f"{pages} page group(s)", build_program(addrs).parity_switches, want
        )

    print("\nbuild_program: latch byte doubles as free data")
    # 0x00C8 is wanted, and page 0xC8 also needs 0xC8 as its latch byte.
    prog = build_program([0x00C8, 0xC802])
    check("page-0 entry plus latch plus datum", prog.wire_bytes, 3)
    check("0x00C8 captured once", list(prog.captures.values()).count(0xC8), 1)

    print("\nsimulate against a fake responder")
    memory = {a: (a * 7 + 1) & 0xFF for a in range(0x10000)}
    for label, addrs in [
        ("page 0 only", [0xA4, 0xA6, 0x7F]),
        ("one page", [0xC802, 0xC803, 0xC80F]),
        ("mixed", [0xA4, 0xC802, 0xC900, 0x00C8, 0xC8C8]),
        ("every page", [p << 8 for p in range(0, 0x100, 0x20)]),
    ]:
        prog = build_program(addrs)
        got = simulate(prog, memory)
        want = {a: memory[a] for a in sorted(set(addrs))}
        check(f"{label}: all addresses resolved correctly", got, want)

    print("\nChannel byte addressing")
    loaded = load_channels()
    one = loaded.get("N40")
    two = loaded.get("FR2+1")
    check("N40 is 1 byte at 0x00A4", (one.addr, one.size), (0x00A4, 1))
    check("1-byte channel", one.byte_addresses, (0x00A4,))
    check("1-byte assemble", one.assemble({0x00A4: 0x33}), 0x33)
    check("missing byte yields None", one.assemble({}), None)

    print("\nConversions from the DAMOS")
    # NQ40 is 40 rpm/bit, stated outright in its FR description.
    check("NQ40: raw 50 -> 2000 rpm", one.physical(50), 2000.0)
    check("NQ40 unit", one.unit, "Upm")
    # TEMPQ is the classic Bosch 0.75*raw - 48.
    tmot = loaded.get("TMOT")
    check("TEMPQ: raw 0 -> -48 C", tmot.physical(0), -48.0)
    check("TEMPQ: raw 128 -> 48 C", tmot.physical(128), 48.0)
    check("TEMPQ: raw 255 -> 143.25 C", tmot.physical(255), 143.25)
    # UQ5 is a 5 V ADC over one byte, so 255 must land on 5000 mV. The DAMOS stores
    # the step as a truncated 19.6078, giving 4999.989 -- hence the rounding.
    uq5 = loaded.conversions["UQ5"]
    check("UQ5: raw 255 -> 5000 mV", round(uq5.apply(255)), 5000)
    check("UQ5 declared max is 5000", uq5.decl_max, 5000.0)
    # ZWVEK has a negative slope: more raw means more retard.
    zw = loaded.get("ZWOUT")
    check("ZWVEK: raw 0 -> 72 deg", zw.physical(0), 72.0)
    check("ZWVEK: raw 96 -> 0 deg", zw.physical(96), 0.0)
    check("ZWVEK: raw 126 -> -22.5 deg", zw.physical(126), -22.5)
    # FAK2L is a 16-bit factor with 0x8000 == 1.00, which is what makes the lambda
    # integrator a good byte-order discriminator.
    fak2l = loaded.conversions["FAK2L"]
    check("FAK2L: 0x8000 -> 1.0", fak2l.apply(0x8000), 1.0)
    check("FAK2L is 16-bit", fak2l.width, 16)
    # Two's-complement conversions must come back negative above half scale.
    tq10s = loaded.conversions["TQ10S"]
    check("TQ10S: raw 255 -> -0.01 s", round(tq10s.apply(255), 10), -0.01)
    check("TQ10S: raw 1 -> +0.01 s", round(tq10s.apply(1), 10), 0.01)
    # Every conversion in the file reproduced its declared range at build time.
    unvalidated = [n for n, c in loaded.conversions.items() if not c.validated]
    check("all conversions validated", unvalidated, [])
    check("222 conversions loaded", len(loaded.conversions), 222)

    print("\nDimensionless is not the same as unconvertible")
    # Two different reasons a channel shows no unit, which must not look alike:
    # FR is converted but dimensionless (a factor); ML+1 has no formula at all.
    fr = loaded.get("FR")
    check("FR converts", fr.has_physical, True)
    check("FR has no unit", fr.unit, "")
    check("FR is not labelled raw", fr.unit_label, "")
    check("FR at 0x8000 reads 1.00", fr.physical(0x8000), 1.0)
    ml1 = loaded.get("ML+1")
    check("ML+1 does not convert", ml1.has_physical, False)
    check("ML+1 is labelled raw", ml1.unit_label, "raw")
    check("negative zero is not shown", fak2l.format(0), "0.000")

    print("\nFormatting")
    # Decimal places follow the step size: TEMPQ steps 0.75 so one decimal, NQ40
    # steps 40 so none.
    check("TMOT formats with unit", tmot.format(128), "48.0 grad C")
    check("N40 formats with unit", one.format(50), "2000 Upm")

    print("\nSuspect channels report raw, not a wrong number")
    suspects = [c for c in loaded if c.suspect]
    check("DVL_SYNA+1 is tagged", [c.name for c in suspects], ["DVL_SYNA+1"])
    check("it refuses a physical value", suspects[0].physical(0x1234), None)
    check("it formats as raw", suspects[0].format(0x1234), "raw 4660")
    print(f"  ..   reason: {suspects[0].suspect}")

    print("\nChannels with no formula fall back to raw")
    no_conv = loaded.without_conversion
    check("some exist", len(no_conv) > 0, True)
    check("none claim a unit", {c.unit for c in no_conv}, {""})
    print(f"  ..   {len(no_conv)} of {len(loaded)} channels, conversions: "
          f"{sorted({c.umr for c in no_conv if not c.suspect})}")

    print("\n16-bit word addressing (open question 6, resolved from the damos)")
    # The rule: an FR name ending in "+1" has its listed address at the SECOND byte of
    # the word; every other name is at the base. Derived by comparing the A4 damos's
    # /UMP base addresses against the VS20 table in that same A4 image -- 35/35 and
    # 45/45 respectively. See the word-order block in mcmess/channels.py.
    check("FR2+1 is a '+1' entry at 0xC87D", (two.name, two.addr), ("FR2+1", 0xC87D))
    check("so its word base is 0xC87C", two.base, 0xC87C)
    check("and it occupies 0xC87C..0xC87D", two.byte_addresses, (0xC87C, 0xC87D))

    plain = loaded.get("FR")
    check("FR is a plain name at 0xC802", (plain.name, plain.addr), ("FR", 0xC802))
    check("so its base is its own address", plain.base, 0xC802)
    check("and it occupies 0xC802..0xC803", plain.byte_addresses, (0xC802, 0xC803))

    check("1-byte channels have base == addr", one.base, one.addr)

    print("\nEvery '+1' FMT:2 channel resolves one below its listed address")
    words = [c for c in loaded if c.size == 2]
    bad = [
        c.name
        for c in words
        if c.base != (c.addr - 1 if c.name.endswith("+1") else c.addr)
    ]
    check("rule holds for all of them", bad, [])
    plus_one = [c for c in words if c.name.endswith("+1")]
    print(f"  ..   {len(words)} two-byte channels, {len(plus_one)} named '+1'")

    print("\nByte order within the word (little-endian, per the 80C196)")
    original = get_byte_order()
    try:
        memory = {0xC87C: 0x34, 0xC87D: 0x12}
        check("default is little", get_byte_order(), "little")
        check("little: base is the low byte", two.assemble(memory), 0x1234)
        set_byte_order("big")
        check("big: base is the high byte", two.assemble(memory), 0x3412)
        try:
            set_byte_order("nonsense")
            check("bad order rejected", "no error", "ValueError")
        except ValueError:
            check("bad order rejected", "ValueError", "ValueError")
    finally:
        set_byte_order(original)
    check("restored", get_byte_order(), "little")

    print("\nThe build re-derives the byte-order rule every time")
    evidence = loaded.meta.get("word_order", {}).get("evidence", {})
    check("no conflicts", evidence.get("conflicts"), 0)
    check("plain names matched the damos", evidence.get("base", 0) > 30, True)
    check("'+1' names matched the damos", evidence.get("plus_one", 0) > 40, True)
    print(
        f"  ..   {evidence.get('base')} at base, {evidence.get('plus_one')} at base+1, "
        f"{evidence.get('multiword')} into multi-word vars, "
        f"{evidence.get('conflicts')} conflicts"
    )

    print("\nDatabase loader rejects unresolved rows")
    check("no channel at address 0x0000", [c.name for c in loaded if c.addr == 0], [])
    check("no purely numeric names", [c.name for c in loaded if c.name.isdigit()], [])
    check("no duplicate names", loaded.duplicates, [])
    print(f"  ..   {len(loaded)} channels kept, {len(loaded.rejected)} rejected")
    for name, why in loaded.rejected[:3]:
        print(f"         rejected: {name!r} - {why}")
    for name in ("N40", "TMOT", "TL", "UB", "DKPOT", "ZWOUT", "MLUFT", "NSOL"):
        check(f"{name} present", name in loaded.by_name, True)

    print("\nThe FR's displayed UMR is NOT used (it aliases 16-bit to 8-bit)")
    # FKT128 displayed vs FAK2L actual is a factor of 256; using the displayed name
    # would silently scale every such channel wrong.
    aliased = [c for c in loaded if c.umr_displayed and c.umr != c.umr_displayed]
    check("aliased channels exist", len(aliased) > 20, True)
    # Only assert for aliases whose /UMR: actually has a formula; the rest correctly
    # fall back to RAW rather than borrowing the displayed name's formula, which is
    # the whole point -- SIG32L has no formula, so FTEAD+1 must NOT silently use
    # TEQ32S's.
    resolvable = [c for c in aliased if c.umr in loaded.conversions]
    wrong = [c.name for c in resolvable if c.conversion.name != c.umr]
    check("resolvable aliases all use /UMR:", wrong, [])
    borrowed = [
        c.name
        for c in aliased
        if c.umr not in loaded.conversions and c.conversion.name != "RAW"
    ]
    check("unresolvable aliases do not borrow the displayed formula", borrowed, [])
    print(f"  ..   {len(aliased)} aliased, {len(resolvable)} resolvable")
    sample = next(c for c in aliased if c.umr_displayed == "FKT128")
    check(
        f"{sample.name}: displayed FKT128 vs actual {sample.umr}",
        (sample.umr_displayed, sample.umr),
        ("FKT128", "FAK2L"),
    )
    check(
        "and the two differ by 256x",
        round(loaded.conversions["FKT128"].scale / loaded.conversions["FAK2L"].scale),
        256,
    )

    print("\nChannelSet.addresses_for")
    check(
        "deduplicated and sorted",
        ChannelSet.addresses_for([one, two, one]),
        [0x00A4, 0xC87C, 0xC87D],
    )

    print("\nCSV cells and negative zero")
    # Most conversions are rational forms with a negative denominator, so they
    # return IEEE -0.0 at raw 0: NQ40 is (0 - 40x) / (0 - 1). Measured on the
    # bench, this filled every idle rpm, load and timing cell of a log with -0.
    n40 = loaded.get('N40')
    check("NQ40 at raw 0 really is negative zero",
          str(n40.physical(0)), "-0.0")
    check("but its CSV cell is plain 0",
          csv_number(n40.physical(0)), "0")
    check("display path agrees",
          n40.format(0).split()[0], "0")
    check("NaN becomes an empty cell",
          csv_number(float('nan')), "")
    check("ordinary values keep precision",
          csv_number(-46.5), "-46.5")
    check("real negatives keep their sign",
          csv_number(-0.25), "-0.25")
    check("digit limit is honoured",
          csv_number(1.23456789, 4), "1.235")

    print("\nProgram cost for the page-0 starter set")
    starter = [0xA4, 0xA6, 0xB9, 0x7F, 0x9B, 0x9A, 0x9C, 0x5D, 0x88, 0xC2]
    prog = build_program(starter)
    print(
        f"  10 channels -> {prog.wire_bytes} wire bytes, "
        f"{prog.parity_switches} parity switches"
    )
    for rate in (200, 300, 500):
        print(f"  at {rate} bytes/s: {rate / prog.wire_bytes:.1f} Hz")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
