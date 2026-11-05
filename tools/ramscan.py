"""Find RAM cells the 018CG firmware never touches, to host patch variables.

    py tools/ramscan.py                      # report against the default image
    py tools/ramscan.py --need 3             # shortlist 3 free words
    py tools/ramscan.py --json out.json      # machine-readable

WHAT THIS CAN AND CANNOT TELL YOU
---------------------------------
It cannot prove a cell is unused. Read that again before you trust the output.

An MCS-96 memory operand comes in four flavours, encoded in the low two bits of
the opcode:

    op & 3 == 0   direct      operand IS the register-file address (< 0x100)
    op & 3 == 1   immediate   no memory operand
    op & 3 == 2   indirect    address is in a register at RUNTIME
    op & 3 == 3   indexed     base register + displacement

Only the indexed form with the zero register carries an absolute address in the
instruction stream, and only that form is fully scannable. Indexed-off-a-base is
resolvable where the base is known (RW24 = 0x6900 for calibration). Indirect is
invisible: no static scan will ever find it.

The two error directions are NOT symmetric, which is the whole point:

  * False positives (a data byte that happens to look like an instruction) mark a
    free cell as used. You lose a candidate. Harmless.
  * Indirect and unknown-base access marks a used cell as free. You put a patch
    variable on top of a live one and get an intermittent fault that looks like a
    sensor problem. This is the failure this tool cannot protect you from.

So treat the output as a SHORTLIST to verify, never as an answer. The ranking
leans on the one defence that does work against indirect access: pointer walks and
struct accesses land in contiguous regions, so a cell in the middle of a 200-byte
hole is far safer than one in a 4-byte gap between two busy cells. Candidates are
scored by distance to the nearest referenced address, not merely by being unseen.

Verify dynamically before you depend on it. Log the candidates across cold start,
idle, part throttle, WOT, overrun and a key cycle; anything that moves has a writer
this scan missed. Better still, once you are flashing anyway, make the first flash
a diagnostic that writes a sentinel into each candidate and log whether it
survives -- that converts the question from inference to measurement.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

RE_ROOT = Path(os.environ.get("M38_RE_ROOT", r"D:\Repos\Reverse Engineering\M3.8"))
DEFAULT_IMAGE = RE_ROOT / "Damoses" / "WinOLS(VW Golf (Original) 352127 csnone  ).bin"
DEFAULT_DB = Path(__file__).resolve().parent.parent / "mcmess" / "data" / "m38_018cg.json"

#: The RAM window. Data accesses in 0xC000-0xCFFF hit RAM; the same addresses are
#: flash to the instruction fetcher, which is why this has to be a data-side scan.
DEFAULT_RAM = (0xC000, 0xCFFF)

#: Base register -> its value, for indexed-off-a-base operands. RW24 = 0x6900 is the
#: calibration base, established from KONFCOM (0x6D19 read as 0x419[RW24]) and
#: confirmed by KHFM (0x6A68 read as 0x168[RW24]).
KNOWN_BASES = {0x24: 0x6900}

#: Opcodes that take a memory operand, by family base. Each family occupies four
#: consecutive opcodes, one per addressing mode.
FAMILIES = {
    0x40: "AND3", 0x44: "ADD3", 0x48: "SUB3", 0x4C: "MULU3",
    0x50: "ANDB3", 0x54: "ADDB3", 0x58: "SUBB3", 0x5C: "MULUB3",
    0x60: "AND", 0x64: "ADD", 0x68: "SUB", 0x6C: "MULU",
    0x70: "ANDB", 0x74: "ADDB", 0x78: "SUBB", 0x7C: "MULUB",
    0x80: "OR", 0x84: "XOR", 0x88: "CMP", 0x8C: "DIVU",
    0x90: "ORB", 0x94: "XORB", 0x98: "CMPB", 0x9C: "DIVUB",
    0xA0: "LD", 0xA4: "ADDC", 0xA8: "SUBC", 0xAC: "LDBZE",
    0xB0: "LDB", 0xB4: "ADDCB", 0xB8: "SUBCB", 0xBC: "LDBSE",
    0xC0: "ST", 0xC4: "STB",
}
INDEXED = {base + 3: name for base, name in FAMILIES.items()}
INDIRECT = {base + 2: name for base, name in FAMILIES.items()}

#: Opcodes that WRITE their memory operand. A cell that is only ever read is still
#: in use, but a cell that is written is the one that would corrupt your variable.
WRITERS = {"ST", "STB"}


def scan_indexed(image: bytes, ram: tuple[int, int], bases: dict[int, int]):
    """Every indexed operand that resolves into the RAM window.

    Operand byte is `reg | 1` for a 16-bit displacement and `reg | 0` for 8-bit,
    because registers are word-aligned and bit 0 is therefore free to encode the
    length. Register 0 is the zero register, so operand byte 0x01 means a plain
    16-bit absolute address and 0x00 means an 8-bit one.
    """
    lo, hi = ram
    hits: dict[int, list] = {}
    for off in range(len(image) - 5):
        op = image[off]
        name = INDEXED.get(op)
        if name is None:
            continue
        idx = image[off + 1]
        reg, long_form = idx & 0xFE, idx & 1
        if long_form:
            disp = image[off + 2] | (image[off + 3] << 8)
        else:
            disp = image[off + 2]
        if reg == 0x00:
            addr = disp
        elif reg in bases:
            addr = (bases[reg] + disp) & 0xFFFF
        else:
            continue  # base unknown at scan time -- counted separately
        if lo <= addr <= hi:
            hits.setdefault(addr, []).append((off, name, "write" if name in WRITERS else "read"))
    return hits


def scan_unresolvable(image: bytes):
    """Sites this scan cannot resolve: indirect, and indexed off an unknown base.

    These are the blind spots. The count matters more than the locations: it is the
    size of the hole in the evidence, and it is why nothing here is a proof.
    """
    indirect, autoinc, unknown_base = [], [], []
    for off in range(len(image) - 3):
        op = image[off]
        if op in INDIRECT:
            idx = image[off + 1]
            (autoinc if idx & 1 else indirect).append((off, INDIRECT[op]))
        elif op in INDEXED:
            reg = image[off + 1] & 0xFE
            if reg != 0x00 and reg not in KNOWN_BASES:
                unknown_base.append((off, INDEXED[op], reg))
    return indirect, autoinc, unknown_base


def scan_environment(image: bytes, ram: tuple[int, int]) -> dict:
    """Two hazards that carry no address in the instruction at all.

    The stack is reached through SP, and windowed memory is reached through
    register-DIRECT operands after a WSR load -- neither leaves an absolute address
    anywhere for a scan to find. Both would be invisible holes in the evidence if
    they overlapped the window, so establish where they actually land.

    On the 80C196KR a WSR of 0x40-0x7F maps a 128-byte block to registers 0x80-0xFF
    and 0x20-0x3F maps a 64-byte block to 0xC0-0xFF. Both window bases are computed
    from a six-bit field, so windowing cannot reach beyond 0x1FFF however it is set.
    """
    lo, hi = ram
    sp_inits, wsr_values = [], set()
    for off in range(len(image) - 4):
        if image[off] == 0xA1 and image[off + 3] == 0x18:  # LD SP,#imm16
            sp_inits.append((off, image[off + 1] | (image[off + 2] << 8)))
        elif image[off] == 0xB1 and image[off + 2] == 0x14:  # LDB WSR,#imm8
            wsr_values.add(image[off + 1])

    reach = 0
    for w in wsr_values:
        if 0x40 <= w <= 0x7F:
            reach = max(reach, (w - 0x40) * 128 + 127)
        elif 0x20 <= w <= 0x3F:
            reach = max(reach, (w - 0x20) * 64 + 63)
    # The stack DESCENDS from its initial SP, so "the init value is not inside the
    # window" does not mean the stack cannot reach it. An SP just above the window
    # is the worst case of all: every byte of the window is in the stack's growth
    # path, and the deepest the original firmware's call nesting plus interrupt
    # frames actually go is not knowable from a static scan.
    descends_into = [v for _, v in sp_inits if v > hi and (v - hi) <= 0x400]
    return {
        "sp_inits": sp_inits,
        "sp_in_window": [v for _, v in sp_inits if lo <= v <= hi],
        "sp_descends_into": descends_into,
        "wsr_values": sorted(wsr_values),
        "window_reach": reach,
        "wsr_overlaps": reach >= lo,
    }


def db_addresses(db_path: Path, ram: tuple[int, int]) -> set[int]:
    """RAM the channel database already names. Definitely in use."""
    if not db_path.exists():
        return set()
    blob = json.loads(db_path.read_text(encoding="utf-8"))
    lo, hi = ram
    used = set()
    for ch in blob.get("channels", []):
        base = ch.get("word_base") or ch["addr"]
        for a in range(base, base + int(ch.get("size", 1))):
            if lo <= a <= hi:
                used.add(a)
    return used


def free_runs(used: set[int], ram: tuple[int, int]):
    lo, hi = ram
    runs, start = [], None
    for a in range(lo, hi + 2):
        if a <= hi and a not in used:
            if start is None:
                start = a
        elif start is not None:
            runs.append((start, a - start))
            start = None
    return sorted(runs, key=lambda r: -r[1])


def rank_candidates(used: set[int], ram: tuple[int, int], width: int, want: int):
    """Score each aligned word by how deep inside a hole it sits.

    Depth, not mere absence, is the metric that matters: the access modes this scan
    is blind to reach contiguous structures, so distance from any known-live cell is
    the best available proxy for safety.
    """
    lo, hi = ram
    out = []
    for a in range(lo, hi - width + 2, 2):
        block = range(a, a + width)
        if any(x in used for x in block):
            continue
        back = next((a - x for x in range(a - 1, lo - 1, -1) if x in used), a - lo + 1)
        fwd = next((x - (a + width - 1) for x in range(a + width, hi + 1) if x in used), hi - a + 1)
        out.append((min(back, fwd), a, back, fwd))
    out.sort(reverse=True)
    return out[:want]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--image", type=Path, default=DEFAULT_IMAGE)
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--ram", default="C000-CFFF", help="RAM window, hex, inclusive")
    p.add_argument("--width", type=int, default=2, help="bytes per variable (default 2)")
    p.add_argument("--need", type=int, default=6, help="how many candidates to rank")
    p.add_argument("--runs", type=int, default=10, help="how many free runs to list")
    p.add_argument("--check", type=lambda s: int(s, 0), nargs="*", default=[],
                   help="addresses to report on individually")
    p.add_argument("--json", type=Path, help="also write the result as JSON")
    args = p.parse_args(argv)

    if not args.image.exists():
        print(f"no image at {args.image}\nset M38_RE_ROOT or pass --image", file=sys.stderr)
        return 2
    lo, hi = (int(x, 16) for x in args.ram.split("-"))
    ram = (lo, hi)
    image = args.image.read_bytes()

    print(f"image   {args.image.name}  ({len(image)} bytes)")
    print(f"window  0x{lo:04X}-0x{hi:04X}  ({hi - lo + 1} bytes)\n")

    hits = scan_indexed(image, ram, KNOWN_BASES)
    indirect, autoinc, unknown_base = scan_unresolvable(image)
    named = db_addresses(args.db, ram)
    used = set(hits) | named
    written = {a for a, refs in hits.items() if any(k == "write" for _, _, k in refs)}

    print("RESOLVED REFERENCES")
    print(f"  {sum(len(v) for v in hits.values()):6d} indexed operands into the window")
    print(f"  {len(hits):6d} distinct addresses, {len(written)} of them written")
    print(f"  {len(named):6d} addresses named by the channel database")
    print(f"  {len(used):6d} addresses live by either measure "
          f"({100 * len(used) / (hi - lo + 1):.0f}% of the window)")
    if hits:
        print(f"  high-water mark: 0x{max(hits):04X}")

    print("\nBLIND SPOTS -- these are why nothing below is a proof")
    print(f"  {len(indirect):6d} indirect operands        [RWxx]")
    print(f"  {len(autoinc):6d} auto-increment operands  [RWxx]+   <- block copies and RAM clears")
    print(f"  {len(unknown_base):6d} indexed off a base this scan does not know")
    if unknown_base:
        freq: dict[int, int] = {}
        for _, _, reg in unknown_base:
            freq[reg] = freq.get(reg, 0) + 1
        top = sorted(freq.items(), key=lambda kv: -kv[1])[:6]
        print("         busiest bases: " + ", ".join(f"RW{r:02X} x{n}" for r, n in top))
        print("         resolve one by finding the LD RWxx,#imm that sets it, add it to")
        print("         KNOWN_BASES and re-run -- each one shrinks this hole.")

    env = scan_environment(image, ram)
    print("\nHAZARDS THAT CARRY NO ADDRESS AT ALL")
    if env["sp_inits"]:
        where = ", ".join(f"0x{v:04X}" for _, v in env["sp_inits"][:4])
        if env["sp_in_window"]:
            verdict = "INSIDE THE WINDOW -- do not place anything here"
        elif env["sp_descends_into"]:
            top = min(env["sp_descends_into"])
            verdict = (f"the stack GROWS DOWN from 0x{top:04X} into this window; an "
                       f"unreferenced run just below it is stack space, not free space")
        else:
            verdict = "outside the window and not descending into it"
        print(f"  stack   SP initialised to {where}: {verdict}")
    else:
        print("  stack   no LD SP,#imm found -- LOCATE IT before trusting any candidate")
    if env["wsr_values"]:
        verdict = "OVERLAPS THE WINDOW -- register-direct access can reach it" \
            if env["wsr_overlaps"] else "cannot reach the window"
        print(f"  WSR     {len(env['wsr_values'])} window values in use, reaching "
              f"0x{env['window_reach']:04X}: {verdict}")
    else:
        print("  WSR     no windowing seen")

    runs = free_runs(used, ram)
    print(f"\nUNREFERENCED RUNS (longest {min(args.runs, len(runs))} of {len(runs)})")
    for a, n in runs[:args.runs]:
        print(f"  0x{a:04X}  {n:4d} bytes")

    print(f"\nCANDIDATES for a {args.width}-byte variable, ranked by depth inside a hole")
    print("  depth = bytes to the nearest referenced address on either side")
    for depth, a, back, fwd in rank_candidates(used, ram, args.width, args.need):
        print(f"  0x{a:04X}  depth {depth:4d}   (prev ref -{back}, next ref +{fwd})")

    for a in args.check:
        refs = hits.get(a, [])
        tag = "NAMED in the database" if a in named else ("referenced" if refs else "not seen")
        print(f"\n0x{a:04X}: {tag}, {len(refs)} resolved operands")
        for off, name, kind in refs[:10]:
            print(f"    file 0x{off:05X}  page {off // 0x4000:X}  {name:6s} {kind}")

    if args.json:
        args.json.write_text(json.dumps({
            "image": str(args.image),
            "window": [lo, hi],
            "used": sorted(used),
            "written": sorted(written),
            "blind_spots": {
                "indirect": len(indirect),
                "auto_increment": len(autoinc),
                "unknown_base": len(unknown_base),
            },
            "free_runs": runs,
            "candidates": [
                {"addr": a, "depth": d} for d, a, _, _ in
                rank_candidates(used, ram, args.width, args.need)
            ],
        }, indent=1), encoding="utf-8", newline="")
        print(f"\nwritten: {args.json}")

    print("\nNOT A PROOF. Verify dynamically before you depend on any of this -- see the")
    print("module docstring. A cell can be live through an addressing mode this scan")
    print("cannot see, and that failure is silent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
