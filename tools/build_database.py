"""Build the logger's channel + conversion database from the reverse-engineering files.

Produces one self-contained JSON so the logger does not read the RE folder at runtime:

    mcmess/data/m38_018cg.json

Three inputs:

  * a DAMOS .DAM  -- conversion formulas (/REG + /REP), text tables (/VTB + /VWP),
                     lookup tables (/TAB + /WP)
  * the FR .FRM   -- the VS20 pseudo-address list: channel names, descriptions, the
                     conversion each one uses, and its byte width
  * the ECU .bin  -- the VS20 table itself, which maps pseudo-address to real RAM
                     address for THIS image

CAVEAT, recorded in the output and worth repeating: there is no DAMOS for the 018CG.
The formulas come from 8D0907558E (an Audi A4 M3.8.2, DAMOS 30N) while the channel list
comes from FR 30O and the addresses from the 018CG image. Conversion names are shared
across the M3.8 family and the formulas are expected to match, but this is a
cross-image inference, not a measurement.

Usage (defaults point at the existing RE folder):

    py tools/build_database.py
    py tools/build_database.py --report          # verbose per-conversion dump
"""

from __future__ import annotations

import argparse
import json
import os
import re
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

#: Where the reverse-engineering sources live. They are NOT in this repository: the
#: DAMOS, the function framework and the flash images are Bosch/VAG material, so you
#: have to supply your own copies. Set M38_RE_ROOT to the directory holding them, or
#: pass --dam / --fr / --image explicitly. The fallback is the path this database was
#: originally generated from, which is useful to nobody else and is only kept so the
#: generated JSON remains reproducible on the machine it came from.
RE_ROOT = Path(os.environ.get("M38_RE_ROOT", r"D:\Repos\Reverse Engineering\M3.8"))
DEFAULT_DAM = RE_ROOT / "Damoses" / "8D0907558E.DAM"
DEFAULT_FR = RE_ROOT / "FR" / "30O.FRM"
DEFAULT_IMAGE = RE_ROOT / "Damoses" / "WinOLS(VW Golf (Original) 352127 csnone  ).bin"
#: Where generated databases live. One per ECU part number, named m38_<suffix>.json.
DATA_DIR = Path(__file__).resolve().parent.parent / "mcmess" / "data"

#: Channels found by reading code rather than by walking the VS20 table. Merged into
#: the generated database by merge_extra(); see that function for why they are separate.
DEFAULT_EXTRA = DATA_DIR / "extra_channels.json"

#: The image the DAMOS above actually describes, plus its own VS20 table offset. Used
#: only to re-verify the FMT:2 byte-order rule at build time -- the DAMOS's addresses
#: are meaningless against the 018CG image.
DEFAULT_DAMOS_IMAGE = (
    RE_ROOT / "Damoses" / "WinOLS(Audi A4 (Original) 359157 csnone Audi A4 ).bin"
)
DEFAULT_DAMOS_VS20_TABLE = 0x0A16

#: Conversion record types seen in this DAMOS.
TYPE_FORMULA = 6
TYPE_TEXT = 10
TYPE_TABLE = (1, 9)

RULE = "-" * 74


# ======================================================================= DAMOS


@dataclass
class Conversion:
    """One DAMOS conversion ("Umrechnung")."""

    name: str
    kind: str  # "formula" | "table" | "text"
    type_id: int
    description: str
    unit: str
    decl_min: float | None
    decl_max: float | None
    coeffs: list[float] = field(default_factory=list)  # formula: p1..p6
    table: list[tuple[int, float]] = field(default_factory=list)  # table: raw -> value
    text: list[tuple[int, str]] = field(default_factory=list)  # text: raw -> label
    width: int | None = None  # 8 or 16, inferred
    validated: bool = False
    note: str = ""

    # -- the formula -------------------------------------------------------

    def apply(self, raw: int) -> float | None:
        """Convert a raw value to physical units.

        The DAMOS formula is a single rational form:

            x    = raw, reinterpreted as two's complement modulo p6 when p6 != 0
            phys = (p2 - p4*(x - p5)) / (p3*(x - p5) - p1)

        Every record in this file is one of three degenerate cases of it:

            p3 = 0            ->  plain linear, phys = (p4*x' - p2) / p1
            p1 = 0 and p4 = 0 ->  reciprocal,   phys = p2 / (p3 * x')
            p1 = p4 = 1, rest 0 -> identity

        where x' = x - p5.
        """
        if self.kind == "table":
            for key, value in self.table:
                if key == raw:
                    return value
            return None
        if self.kind == "text":
            return None
        if len(self.coeffs) != 6:
            return None

        p1, p2, p3, p4, p5, p6 = self.coeffs
        x = float(raw)
        if p6:
            # Two's complement about p6: values at or above half the modulus are
            # negative. p6 is 256 for signed bytes, 65536 for signed words.
            if x >= p6 / 2.0:
                x -= p6
        x -= p5
        numerator = p2 - p4 * x
        denominator = p3 * x - p1
        if denominator == 0:
            return None
        return numerator / denominator

    def label(self, raw: int) -> str | None:
        for key, text in self.text:
            if key == raw:
                return text
        return None

    # -- linear shorthand --------------------------------------------------

    @property
    def is_linear(self) -> bool:
        return self.kind == "formula" and len(self.coeffs) == 6 and self.coeffs[2] == 0

    @property
    def scale_offset(self) -> tuple[float, float] | None:
        """(scale, offset) such that phys = scale*x' + offset, for linear records.

        Derived from the rational form with p3 = 0:
            phys = (p4*(x - p5) - p2) / p1
        so scale = p4/p1 and offset = -(p2 + p4*p5)/p1, applied to the sign-corrected
        raw value. Useful for display and for sanity-checking against known Bosch
        quantisations; apply() remains the authority.
        """
        if not self.is_linear:
            return None
        p1, p2, _p3, p4, p5, _p6 = self.coeffs
        if p1 == 0:
            return None
        return (p4 / p1, -(p2 + p4 * p5) / p1)

    def to_json(self) -> dict:
        out: dict = {
            "kind": self.kind,
            "type_id": self.type_id,
            "description": self.description,
            "unit": self.unit,
            "min": self.decl_min,
            "max": self.decl_max,
            "width": self.width,
            "validated": self.validated,
        }
        if self.kind == "formula":
            out["coeffs"] = self.coeffs
            linear = self.scale_offset
            if linear:
                out["scale"], out["offset"] = linear
            out["signed_modulus"] = self.coeffs[5] or 0
        elif self.kind == "table":
            out["table"] = [[k, v] for k, v in self.table]
        elif self.kind == "text":
            out["text"] = [[k, t] for k, t in self.text]
        if self.note:
            out["note"] = self.note
        return out


#: The two digit-count fields can be NEGATIVE (e.g. "-2,7" on FAK2L10S and THAINTL,
#: whose values are of order 1e-3), so they must not be matched as unsigned.
_REG = re.compile(
    r"^(\d+),/REG,([^,]+),\{(.*?)\},\s*(\d+),\s*(\d+),\{(.*?)\},"
    r"\s*(-?\d+),\s*(-?\d+),\s*([^,]+),\s*(.+)$"
)
_VTB = re.compile(r"^(\d+),/VTB,([^,]+),\s*(\d+),\s*(\d+)\s*$")
_TAB = re.compile(r"^(\d+),/TAB,([^,]+),\s*(\d+),\s*(\d+)\s*$")

#: /UMP,%UR_LR,DFRST,{ stationaerer Regelhub Bank 1 },$C810,514,42,FAK2L,3,$FFFF,K;
#: Gives a variable's real base address, its conversion, and a width mask.
_UMP = re.compile(
    r"^/UMP,([^,]*),([^,]+),\{(.*?)\},\$([0-9A-Fa-f]+),(\d+),(\d+),([^,]*),(\d+),"
    r"\$([0-9A-Fa-f]+)"
)


@dataclass
class UmpVariable:
    """A variable as the DAMOS describes it: base address, conversion, width mask."""

    name: str
    addr: int
    mask: int
    conversion: str

    @property
    def width(self) -> int:
        return 16 if self.mask > 0xFF else 8


def parse_ump(path: Path) -> dict[str, UmpVariable]:
    """Extract the DAMOS variable list (addresses are for the DAMOS's own image)."""
    out: dict[str, UmpVariable] = {}
    for line in path.read_text(encoding="latin-1").splitlines():
        match = _UMP.match(line)
        if match:
            name = match.group(2).strip()
            out[name] = UmpVariable(
                name=name,
                addr=int(match.group(4), 16),
                mask=int(match.group(9), 16),
                conversion=match.group(7).strip(),
            )
    return out


def _logical_records(text: str) -> list[str]:
    """Split the DAMOS into logical records, undoing its line wrapping.

    The writer breaks a long record across lines *in place of a comma* -- so a field
    pair like "1,7" can appear as "...,1" then "7,...". Continuations are therefore
    rejoined with a comma, never with whitespace. A line starting with "/" or matching
    "<n>,/XXX," begins a new record.
    """
    starts = re.compile(r"^(/[A-Z0-9_]+,|\d+,/[A-Z0-9_]+,)")
    records: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not line:
            continue
        if starts.match(line) or not records:
            records.append(line)
        else:
            records[-1] = records[-1] + "," + line
    return records


def _floats(blob: str) -> list[float]:
    out = []
    for piece in blob.split(","):
        piece = piece.strip().rstrip(";").strip()
        if not piece:
            continue
        try:
            out.append(float(piece))
        except ValueError:
            return []
    return out


def parse_dam(path: Path) -> tuple[dict[str, Conversion], dict]:
    """Extract every conversion from a DAMOS file."""
    text = path.read_text(encoding="latin-1")
    records = _logical_records(text)

    conversions: dict[str, Conversion] = {}
    order: list[Conversion] = []
    pending: Conversion | None = None
    pending_text: tuple[str, int] | None = None
    pending_table: tuple[str, int] | None = None
    meta: dict = {}

    for record in records:
        # Free-text lines (e.g. "PGM-Stand vom: ...") look like continuations to the
        # unwrapper, so take only the braced payload rather than the whole record.
        for prefix, key in (("/EPK,", "epk"), ("/YNR,", "part_number"), ("/PRO,", "project")):
            if record.startswith(prefix):
                braced = re.search(r"\{(.*?)\}", record)
                meta[key] = braced.group(1).strip() if braced else record[len(prefix):].strip()
        if record[:5] in ("/EPK,", "/YNR,", "/PRO,"):
            continue

        match = _REG.match(record)
        if match:
            (
                _idx,
                name,
                desc,
                type_id,
                sub_id,
                unit,
                _digits_int,
                _digits_total,
                decl_min,
                decl_max,
            ) = match.groups()
            type_id = int(type_id)
            kind = (
                "formula"
                if type_id == TYPE_FORMULA
                else "text"
                if type_id == TYPE_TEXT
                else "table"
                if type_id in TYPE_TABLE
                else "unknown"
            )
            try:
                lo = float(decl_min.strip())
            except ValueError:
                lo = None
            try:
                hi = float(decl_max.strip().rstrip(";"))
            except ValueError:
                hi = None
            pending = Conversion(
                name=name.strip(),
                kind=kind,
                type_id=type_id,
                description=" ".join(desc.split()),
                unit=unit.strip(),
                decl_min=lo,
                decl_max=hi,
            )
            conversions[pending.name] = pending
            order.append(pending)
            continue

        if record.startswith("/REP,") and pending is not None:
            pending.coeffs = _floats(record[5:])
            pending = None
            continue

        match = _VTB.match(record)
        if match:
            pending_text = (match.group(2).strip(), 0)
            continue

        if record.startswith("/VWP,") and pending_text is not None:
            name = pending_text[0]
            pairs = re.findall(r"(-?\d+),\{(.*?)\}", record)
            conv = conversions.get(name)
            if conv is not None:
                conv.text = [(int(k), v.strip()) for k, v in pairs]
            pending_text = None
            continue

        match = _TAB.match(record)
        if match:
            pending_table = (match.group(2).strip(), 0)
            continue

        if record.startswith("/WP,") and pending_table is not None:
            name = pending_table[0]
            values = _floats(record[4:])
            conv = conversions.get(name)
            if conv is not None and len(values) >= 2:
                conv.table = [
                    (int(values[i]), values[i + 1])
                    for i in range(0, len(values) - 1, 2)
                ]
            pending_table = None
            continue

    meta["conversion_count"] = len(conversions)
    return conversions, meta


# ================================================================== validation


#: The injection-time quantisations are counts of a CPU timer tick, so their scale
#: follows the crystal. The DAMOS is an Audi A4 M3.8.2 and the target is a 16 MHz
#: 018CG, and every one of these comes out 4/3 too large. Two independent proofs:
#:
#: 1. The DAMOS contradicts itself. TEQ32L and TEQ32SL are *named* "32us" and
#:    declared at 42.667 us/bit; TEQ2048 is named "2048" and declared at 2730.67;
#:    TEQST implies 10.667 where 8 is the round figure. Every ratio is exactly
#:    4/3, and 4/3 is 16 MHz / 12 MHz -- the 018CG's crystal over the A4's, the
#:    latter inferred from its SP_BAUD values in the Ghidra notes.
#:
#: 2. VCDS disagrees with us about injection time, and stops disagreeing after
#:    this correction. Logged against group 002 on a running 018CG, TI0 read
#:    1.47x the tester's "Inj. period" with the ratio drifting from 1.53 at idle
#:    to 1.47 at wide-open throttle -- not a clean scale error. Scaled by 0.75 the
#:    residual becomes a *constant* 0.98 ms across a 5x range (spread 0.19 ms),
#:    which is injector dead time: VCDS shows the computed time, TI0 the commanded
#:    one. A wrong factor could not turn a drifting ratio into a flat offset.
#:
#: Conversions whose quantum is NOT a timer tick are untouched and were checked
#: the same way: TIQ is a clean 0.05 ms/bit and TL matched VCDS's "Actual load" to
#: 0.05 ms, MLQ025L is a clean 0.25 kg/h and matched air mass within 1%.
CLOCK_CORRECTION = 0.75

#: Quantisations that follow the CPU clock. All are "Quantisierung von te" family.
CLOCK_SCALED = (
    "TEQL", "TEQL2", "TEQSL", "SIGTEQL", "TEQ32L", "TEQ32SL", "TEQST", "TEQ2048",
)


def apply_clock_correction(conversions: dict[str, Conversion]) -> list[str]:
    """Scale the timer-derived quantisations onto this ECU's clock.

    Scales the declared range with the formula, so validate_conversions() still
    checks a self-consistent pair rather than silently losing its grip on these.
    """
    done: list[str] = []
    for name in CLOCK_SCALED:
        conv = conversions.get(name)
        if conv is None or conv.kind != "formula" or len(conv.coeffs) != 6:
            continue
        # value = (p2 - p4*x) / (p3*x - p1): scaling the numerator scales the
        # result, and keeps working for the signed forms where p6 is set.
        conv.coeffs[1] *= CLOCK_CORRECTION
        conv.coeffs[3] *= CLOCK_CORRECTION
        if conv.decl_min is not None:
            conv.decl_min *= CLOCK_CORRECTION
        if conv.decl_max is not None:
            conv.decl_max *= CLOCK_CORRECTION
        conv.note = (
            (conv.note + "; " if conv.note else "")
            + f"scaled by {CLOCK_CORRECTION} from the A4 DAMOS onto this ECU's clock"
        )
        done.append(name)
    return done


def validate_conversions(conversions: dict[str, Conversion], verbose: bool = False) -> dict:
    """Infer each conversion's raw width and check it reproduces its declared range.

    A conversion's declared min/max is the *valid* physical range, which is often
    narrower than what the full raw range would produce (ignition angle, for example,
    is declared -22.5..72 but the byte would reach -119). So the check is: are both
    declared endpoints actually produced by some integer raw value, at 8 bits or at
    16 bits? If yes, the formula and the width are consistent with the declaration.
    """
    stats = {"validated": 0, "unverified": 0, "no_range": 0, "by_width": {8: 0, 16: 0}}
    for conv in conversions.values():
        if conv.kind != "formula" or len(conv.coeffs) != 6:
            if conv.kind in ("table", "text"):
                conv.width = 8
                conv.validated = bool(conv.table or conv.text)
                if conv.validated:
                    stats["validated"] += 1
                else:
                    stats["unverified"] += 1
            continue
        if conv.decl_min is None or conv.decl_max is None:
            stats["no_range"] += 1
            conv.note = "no declared range to check against"
            continue

        for width in (8, 16):
            values = []
            for raw in range(1 << width):
                value = conv.apply(raw)
                if value is not None:
                    values.append(value)
            if not values:
                continue
            tol = max(abs(conv.decl_min), abs(conv.decl_max), 1.0) * 1e-4
            # Declared maxima are sometimes the last representable value (DEZ: 255)
            # and sometimes the exclusive upper bound (WQ1536: 1536 = 256 * 6 deg).
            # Accept either, or 8-bit conversions get mistaken for 16-bit ones.
            one_past = conv.apply(1 << width)
            bounds = list(values) + ([one_past] if one_past is not None else [])
            hit_min = any(abs(v - conv.decl_min) <= tol for v in bounds)
            hit_max = any(abs(v - conv.decl_max) <= tol for v in bounds)
            if hit_min and hit_max:
                conv.width = width
                conv.validated = True
                break
        if conv.validated:
            stats["validated"] += 1
            stats["by_width"][conv.width] += 1
        else:
            stats["unverified"] += 1
            conv.note = "formula does not reproduce the declared range at 8 or 16 bits"
            if verbose:
                print(
                    f"  unverified: {conv.name:<10} coeffs={conv.coeffs} "
                    f"declared {conv.decl_min}..{conv.decl_max}"
                )
    return stats


# ========================================================================== FR


@dataclass
class FrEntry:
    pseudo: int
    name: str
    description: str
    umr: str  # the authoritative /UMR: name
    umr_displayed: str  # the bracketed name, often an 8-bit alias
    size: int


_FR_LINE = re.compile(
    r"^\s+(\S+)\s+;(.*?)\s+([0-9A-Fa-f]{4})[hH]"
    r"(?:\s*\[([^\]]*)\])?"
    r"(?:\s*/UMR:(\S+))?"
    r"(?:\s*/FMT:(\d+))?"
)


def parse_fr_vs20(path: Path) -> tuple[list[FrEntry], list[str]]:
    """Parse the FR's VS20 pseudo-address list.

    Each line looks like:

        N40    ;Drehzahl    2001h   [NQ40]   /UMR:NQ40 /FMT:1

    The bracketed name is what Bosch's tool *displays*; /UMR: is the one that actually
    applies. They differ on dozens of entries, and the displayed one is usually the
    8-bit alias of a 16-bit conversion (DEZ for DEZL, FKT128 for FAK2L, TEQS for
    TEQSL). Using the displayed name on a 2-byte channel is a factor-256 error, so
    /UMR: wins and the displayed name is kept only for reference.
    """
    lines = path.read_text(encoding="latin-1").splitlines()
    try:
        start = next(
            i for i, line in enumerate(lines) if "Pseudoadr.-Liste fuer VS20" in line
        )
    except StopIteration:
        raise SystemExit(f"could not find the VS20 list in {path}")

    entries: list[FrEntry] = []
    warnings: list[str] = []
    for line in lines[start : start + 600]:
        if "ABK" in line and "%VS20" in line:
            break
        match = _FR_LINE.match(line)
        if not match:
            continue
        name, desc, pseudo_hex, displayed, umr, fmt = match.groups()
        displayed = (displayed or "").strip()
        umr = (umr or "").strip()
        if not umr and displayed:
            umr = displayed
            warnings.append(f"{name}: no /UMR:, falling back to displayed [{displayed}]")
        entries.append(
            FrEntry(
                pseudo=int(pseudo_hex, 16),
                name=name.strip(),
                description=" ".join(desc.split()),
                umr=umr,
                umr_displayed=displayed,
                size=int(fmt) if fmt else 1,
            )
        )
    return entries, warnings


# ======================================================================= image


def resolve_addresses(
    image: Path, table_offset: int, entries: list[FrEntry]
) -> dict[int, int]:
    """Read the VS20 table out of the image: pseudo-address -> real RAM address."""
    buf = image.read_bytes()
    out: dict[int, int] = {}
    for entry in entries:
        index = entry.pseudo - 0x2000
        at = table_offset + 2 * index
        if index < 0 or at + 2 > len(buf):
            continue
        # 80C196 is little-endian.
        out[entry.pseudo] = struct.unpack_from("<H", buf, at)[0]
    return out


#: Where the Bosch part number sits in the fixed (unbanked) area of every 018 image
#: seen so far, as both a file offset and a CPU data address -- below 0xC000 the two
#: are the same. The McMESS responder serves any address, so this doubles as a live
#: fingerprint: read 11 bytes here from a connected ECU and you know the variant.
#: Confirmed at the same offset in the 018CG and 018AQ images.  [RE]
PART_NUMBER_ADDR = 0xBF0A
PART_NUMBER_LEN = 11
PART_NUMBER_RE = re.compile(rb"06A ?906 ?018 ?[A-Z]{1,2}")


def read_part_number(image: Path) -> str | None:
    """The ECU part number out of an image, preferring the known fixed offset."""
    buf = image.read_bytes()
    at = PART_NUMBER_ADDR
    window = buf[at : at + PART_NUMBER_LEN]
    if PART_NUMBER_RE.fullmatch(window):
        return window.decode("ascii")
    # Fall back to a scan: a variant that moved it is still worth identifying, but
    # say so, because PART_NUMBER_ADDR is what the live fingerprint check reads.
    found = PART_NUMBER_RE.search(buf)
    if not found:
        return None
    print(
        f"        NOTE: part number at 0x{found.start():05X}, not the expected "
        f"0x{PART_NUMBER_ADDR:05X} -- the live fingerprint check will not match"
    )
    return found.group().decode("ascii")


def find_vs20_table(image: Path, entries: list[FrEntry]) -> int:
    """Locate the VS20 table in an image, so a new variant needs no hand-found offset.

    Scoring candidates on "do these look like RAM addresses" is useless: the region
    is dense plausible little-endian u16s and a wide plateau of offsets ties. The
    structural constraint separates cleanly instead -- the FR lists each 16-bit
    channel as a pair, "X" and "X+1", and only at the true offset does every "+1"
    entry resolve to exactly one past its base. Both known images score a perfect
    12/12 at their real offset and at most 7/12 anywhere else.
    """
    buf = image.read_bytes()
    by_name = {e.name: e.pseudo for e in entries}
    pairs = [
        (by_name[name[:-2]], pseudo)
        for name, pseudo in by_name.items()
        if name.endswith("+1") and name[:-2] in by_name
    ]
    if not pairs:
        raise SystemExit("no '+1' word pairs in the FR list; cannot locate the table")

    def at(off: int, pseudo: int) -> int | None:
        pos = off + 2 * (pseudo - 0x2000)
        if pos < 0 or pos + 2 > len(buf):
            return None
        return struct.unpack_from("<H", buf, pos)[0]

    best, best_score = -1, 0
    for off in range(0, min(len(buf), 0x8000), 2):
        hits = 0
        for base_pseudo, high_pseudo in pairs:
            base, high = at(off, base_pseudo), at(off, high_pseudo)
            if base and high == base + 1:
                hits += 1
        if hits > best_score:
            best, best_score = off, hits
    if best_score < len(pairs):
        raise SystemExit(
            f"could not locate the VS20 table in {image.name}: best offset "
            f"0x{best:04X} satisfies only {best_score}/{len(pairs)} word pairs. "
            "Pass --vs20-table explicitly if you have found it by hand."
        )
    return best


# ============================================================ word-order evidence


def check_word_order(
    ump: dict[str, UmpVariable], entries: list[FrEntry], args: argparse.Namespace
) -> dict:
    """Re-derive the FMT:2 byte-order rule from the DAMOS, every build.

    The DAMOS gives each variable's true base address, but for its OWN image -- so the
    comparison must use that image's VS20 table, not the 018CG's. With --damos-image
    pointing at the A4 bin, this confirms:

        FR name without "+1"  ->  VS20 address == base
        FR name with "+1"     ->  VS20 address == base + 1

    Kept as a build-time check so that swapping in a different DAMOS or FR revision
    cannot silently invalidate the rule the runtime relies on.
    """
    result = {
        "base": 0,
        "plus_one": 0,
        "multiword": 0,
        "conflicts": 0,
        "unmatched": 0,
        "details": [],
    }
    if not args.damos_image or not args.damos_image.is_file():
        result["details"].append("skipped: no --damos-image given")
        return result

    buf = args.damos_image.read_bytes()
    for entry in entries:
        if entry.size != 2:
            continue
        index = entry.pseudo - 0x2000
        at = args.damos_vs20_table + 2 * index
        if index < 0 or at + 2 > len(buf):
            continue
        vs_addr = struct.unpack_from("<H", buf, at)[0]
        if vs_addr == 0:
            continue
        stem = entry.name[:-2] if entry.name.endswith("+1") else entry.name
        variable = ump.get(stem)
        if variable is None:
            result["unmatched"] += 1
            continue
        expected_base = vs_addr - 1 if entry.name.endswith("+1") else vs_addr
        delta = expected_base - variable.addr
        if delta != 0:
            # Some DAMOS variables are wider than one word -- QAD spans 0xC3F4..0xC3F7
            # (QADLL starts at 0xC3F8) -- so a VS20 entry can legitimately address the
            # upper word of one. That is an offset into a known variable, not a
            # contradiction of the rule.
            if 0 < delta <= 6 and delta % 2 == 0:
                result["multiword"] += 1
                result["details"].append(
                    f"{entry.name}: word base 0x{expected_base:04X} is +{delta} into "
                    f"{variable.name} at 0x{variable.addr:04X} (multi-word variable)"
                )
            else:
                result["conflicts"] += 1
                result["details"].append(
                    f"CONFLICT {entry.name}: VS20 0x{vs_addr:04X} implies base "
                    f"0x{expected_base:04X} but DAMOS says 0x{variable.addr:04X}"
                )
        elif entry.name.endswith("+1"):
            result["plus_one"] += 1
        else:
            result["base"] += 1
    return result


# ======================================================================== build


def _plausible_name(name: str) -> bool:
    return bool(name) and not name.isdigit() and any(c.isalpha() for c in name)


def build(args: argparse.Namespace) -> int:
    print(f"DAMOS : {args.dam}")
    conversions, dam_meta = parse_dam(args.dam)
    ump = parse_ump(args.dam)
    print(f"        {len(conversions)} conversions, {len(ump)} variable records")
    print(f"        EPK {dam_meta.get('epk', '?')}")

    corrected = apply_clock_correction(conversions)
    if corrected:
        print(
            f"\n        {len(corrected)} te-quantisation(s) scaled by "
            f"{CLOCK_CORRECTION} for this ECU's clock: {', '.join(sorted(corrected))}"
        )

    print("\nvalidating formulas against their declared ranges")
    stats = validate_conversions(conversions, verbose=args.report)
    print(
        f"        {stats['validated']} validated "
        f"({stats['by_width'][8]} 8-bit, {stats['by_width'][16]} 16-bit), "
        f"{stats['unverified']} unverified, {stats['no_range']} with no declared range"
    )

    print(f"\nFR    : {args.fr}")
    entries, fr_warnings = parse_fr_vs20(args.fr)
    print(f"        {len(entries)} VS20 entries")
    mismatched = [e for e in entries if e.umr_displayed and e.umr != e.umr_displayed]
    print(
        f"        {len(mismatched)} entries where the displayed [UMR] differs from "
        "/UMR: (using /UMR:)"
    )

    print(f"\nimage : {args.image}")
    part = read_part_number(args.image)
    print(f"        part number: {part or 'NOT FOUND'}")
    if args.vs20_table is None:
        args.vs20_table = find_vs20_table(args.image, entries)
        print(f"        VS20 table located at 0x{args.vs20_table:04X}")
    else:
        print(f"        VS20 table at 0x{args.vs20_table:04X} (given on the command line)")
    addresses = resolve_addresses(args.image, args.vs20_table, entries)

    if args.out is None:
        if not part:
            raise SystemExit(
                "no part number in the image and no --out given: cannot name the "
                "output database. Pass --out explicitly."
            )
        args.out = DATA_DIR / f"m38_{part[-5:].lower()}.json"

    channels = []
    rejected = []
    seen: set[str] = set()
    for entry in entries:
        addr = addresses.get(entry.pseudo)
        if addr is None:
            rejected.append([entry.name, "pseudo-address outside the table"])
            continue
        if addr == 0x0000:
            rejected.append([entry.name, "unresolved VS20 entry (0x0000) - not fitted"])
            continue
        if not _plausible_name(entry.name):
            rejected.append([entry.name or "<unnamed>", "no usable name in the FR list"])
            continue
        if entry.name in seen:
            rejected.append([entry.name, "duplicate name"])
            continue
        seen.add(entry.name)
        row = {
            "name": entry.name,
            "pseudo": entry.pseudo,
            "addr": addr,
            "size": entry.size,
            "umr": entry.umr,
            "umr_displayed": entry.umr_displayed,
            "description": entry.description,
        }
        if entry.size == 2:
            # The FR's "+1" entries point at the second byte of the word; see the
            # word-order block in mcmess/channels.py for the evidence.
            row["word_base"] = addr - 1 if entry.name.endswith("+1") else addr
        channels.append(row)

    print(f"\n        {len(channels)} channels kept, {len(rejected)} rejected")

    word_evidence = check_word_order(ump, entries, args)
    print(
        f"        word order: {word_evidence['base']} plain names at base, "
        f"{word_evidence['plus_one']} '+1' names at base+1, "
        f"{word_evidence['multiword']} into multi-word vars, "
        f"{word_evidence['conflicts']} conflicts"
    )
    if word_evidence["conflicts"]:
        print("        WARNING: the FMT:2 byte-order rule does not hold for:")
        for line in word_evidence["details"]:
            if line.startswith("CONFLICT"):
                print(f"          {line}")

    # Which channels end up with a usable conversion?
    used = {c["umr"] for c in channels if c["umr"]}
    missing = sorted(n for n in used if n not in conversions)
    unvalidated = sorted(
        n for n in used if n in conversions and not conversions[n].validated
    )
    with_formula = [c for c in channels if c["umr"] in conversions]
    print(
        f"        {len(with_formula)}/{len(channels)} channels have a conversion "
        f"({len(used) - len(missing)}/{len(used)} distinct conversions resolved)"
    )
    if missing:
        print(f"        no formula for: {', '.join(missing)}")
    if unvalidated:
        print(f"        unvalidated formulas in use: {', '.join(unvalidated)}")

    # Size agreement: does the conversion's inferred width match the FR's FMT?
    #
    # A disagreement is a real trap, not a parser glitch. DVL_SYNA+1 is FMT:2 but uses
    # the 8-bit UQ5 voltage conversion, and its FR description reads
    # "Speicher DVL_SYN + DDVLSYN" -- it is two packed 8-bit values sharing one entry,
    # not a 16-bit number. Assembling it as a word and scaling it would produce a
    # confident, wrong reading. So such channels are tagged and the runtime reports
    # them raw instead of applying the conversion.
    width_conflicts = []
    for channel in channels:
        conv = conversions.get(channel["umr"])
        if conv is None or conv.width is None or conv.kind != "formula":
            continue
        expected = channel["size"] * 8
        if conv.width == expected:
            continue
        if conv.width == 8 and channel["size"] == 2:
            why = (
                f"FMT:2 but {channel['umr']} is an 8-bit conversion -- likely two "
                "packed 8-bit values in one entry, not a 16-bit word"
            )
        else:
            why = (
                f"FMT:{channel['size']} but {channel['umr']} is a {conv.width}-bit "
                "conversion"
            )
        channel["suspect"] = why
        width_conflicts.append(f"{channel['name']} ({channel['umr']}): {why}")
    print(
        f"        {len(width_conflicts)} channels tagged suspect "
        "(FMT disagrees with the conversion width)"
    )

    payload = {
        "meta": {
            "target": f"{part or 'unknown'} (Bosch M3.8, VW/Audi 1.8T)",
            "part_number": part,
            "generated_by": "tools/build_database.py",
            # Basenames, not full paths: these are the operator's own local copies of
            # Bosch/VAG files, and the generated JSON is committed to a public repo.
            "sources": {
                "conversions": args.dam.name,
                "channels": args.fr.name,
                "addresses": args.image.name,
                "vs20_table_offset": args.vs20_table,
            },
            # How a connected ECU identifies itself. The McMESS responder serves any
            # address, so the logger reads these bytes at connect and refuses to log
            # through a database built for a different variant.
            "fingerprint": {
                "addr": PART_NUMBER_ADDR,
                "length": PART_NUMBER_LEN,
                "text": part,
            },
            "damos": dam_meta,
            "caveat": (
                f"There is no DAMOS for the {part or 'target'}. Conversion formulas are "
                "taken from 8D0907558E (Audi A4, M3.8.2, DAMOS 30N) while the channel "
                "list comes from FR 30O and the addresses from this ECU's own image. "
                "Conversion names are shared across the M3.8 family, so the formulas "
                "are expected to apply, but this is a cross-image inference and has "
                "not been measured on the bench."
            ),
            "counts": {
                "conversions": len(conversions),
                "conversions_validated": stats["validated"],
                "channels": len(channels),
                "channels_rejected": len(rejected),
                "channels_with_conversion": len(with_formula),
            },
            "conversions_missing": missing,
            "conversions_unvalidated_in_use": unvalidated,
            "width_conflicts": width_conflicts,
            "fr_warnings": fr_warnings,
            "word_order": {
                "rule": (
                    "FMT:2 entries occupy (base, base+1). An FR name ending in '+1' "
                    "has its listed address at base+1; all others at base. Byte order "
                    "within the word is little-endian (80C196)."
                ),
                "evidence": word_evidence,
            },
        },
        "conversions": {name: conv.to_json() for name, conv in sorted(conversions.items())},
        "channels": channels,
        "rejected": rejected,
    }

    merge_extra(payload, args.extra, part)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    # newline="" stops Python's text mode translating \n to \r\n on Windows. Without
    # it a rebuild rewrites all 4000-odd lines with CRLF, which leaves the working
    # tree dirty against the LF-normalised copy in git even though nothing changed.
    args.out.write_text(
        json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8", newline=""
    )
    print(f"\nwritten: {args.out}  ({args.out.stat().st_size / 1024:.0f} KB)")

    if args.report:
        _print_report(conversions, channels)
    return 0


def _print_report(conversions: dict[str, Conversion], channels: list[dict]) -> None:
    print()
    print(RULE)
    print("Conversions used by the 018CG channel list")
    print(RULE)
    used = sorted({c["umr"] for c in channels if c["umr"]})
    print(f"  {'name':<10} {'w':<3} {'unit':<16} formula / note")
    for name in used:
        conv = conversions.get(name)
        if conv is None:
            print(f"  {name:<10} {'?':<3} {'':<16} NO FORMULA - reported raw")
            continue
        width = str(conv.width or "?")
        linear = conv.scale_offset
        if conv.kind == "text":
            shape = "text: " + ", ".join(f"{k}={v}" for k, v in conv.text[:3])
        elif conv.kind == "table":
            shape = f"table of {len(conv.table)} points"
        elif linear:
            scale, offset = linear
            offset = offset + 0.0 if offset else 0.0  # normalise -0.0
            signed = conv.coeffs[5]
            sign_note = f", signed/{int(signed)}" if signed else ""
            sign = "-" if offset < 0 else "+"
            shape = f"{scale:g} * raw {sign} {abs(offset):g}{sign_note}"
        else:
            shape = f"rational, coeffs {conv.coeffs}"
        flag = "" if conv.validated else "  [UNVALIDATED]"
        print(f"  {name:<10} {width:<3} {conv.unit[:16]:<16} {shape}{flag}")


def merge_extra(payload: dict, path: Path, part: str | None) -> None:
    """Fold hand-found channels and their conversions into the generated database.

    The VS20 pseudo-address table is a *naming* table, not a list of what the ECU will
    let you read -- the responder serves any RAM address. Channels located by reading
    code therefore have no VS20 entry and the generator cannot rediscover them, so they
    live in their own file and are merged here. Keeping them out of the generator means
    a rebuild cannot silently drop them and their provenance stays visible: each entry
    carries a 'source' naming the instruction its address was read from.

    Collisions are fatal rather than merged. An extra channel that shadows a VS20 name
    or address almost certainly means the VS20 table already had it and the hand entry
    is redundant or wrong, which is worth stopping for.

    Each entry is pinned to the ECUs it was read out of, via an "ecus" list of part
    numbers, and is skipped for any other. These addresses came from one image's
    instruction stream, and RAM is NOT laid out the same across the family: 121 of the
    249 VS20 channels sit at different addresses in 018CG and 018AQ. Carrying a
    hand-found 0xCAxx address across variants would silently read the wrong cell --
    the worst failure this tool can have, because the number still looks reasonable.
    """
    if not path.exists():
        print(f"\n        no extra-channel file at {path}")
        return

    doc = json.loads(path.read_text(encoding="utf-8"))
    convs = payload["conversions"]
    channels = payload["channels"]
    names = {c["name"] for c in channels}
    addrs = {c["addr"] for c in channels}

    for name, conv in doc.get("conversions", {}).items():
        if name in convs:
            raise SystemExit(f"extra conversion {name!r} already exists in the DAMOS set")
        convs[name] = conv
    payload["conversions"] = dict(sorted(convs.items()))

    added = []
    skipped = []
    for row in doc.get("channels", []):
        row = {k: v for k, v in row.items() if not k.startswith("_")}
        only = row.get("ecus")
        if only and part not in only:
            skipped.append((row["name"], only))
            continue
        if row["name"] in names:
            raise SystemExit(f"extra channel {row['name']!r} collides with a VS20 name")
        if row["addr"] in addrs:
            clash = next(c["name"] for c in channels if c["addr"] == row["addr"])
            raise SystemExit(
                f"extra channel {row['name']!r} is at 0x{row['addr']:04X}, "
                f"already covered by {clash}"
            )
        if row.get("umr") and row["umr"] not in payload["conversions"]:
            raise SystemExit(
                f"extra channel {row['name']!r} names conversion {row['umr']!r}, "
                "which is neither in the DAMOS nor in the extra file"
            )
        channels.append(row)
        names.add(row["name"])
        addrs.add(row["addr"])
        added.append(row)

    payload["meta"]["counts"]["channels"] = len(channels)
    payload["meta"]["counts"]["channels_extra"] = len(added)
    payload["meta"]["counts"]["conversions"] = len(payload["conversions"])
    payload["meta"]["counts"]["channels_with_conversion"] = sum(
        1 for c in channels if c.get("umr") in payload["conversions"]
    )
    payload["meta"]["extra_channels"] = (
        f"{len(added)} channels merged from {path.name}. These are not in the VS20 "
        "table; they were read out of the code in Ghidra and none has been checked "
        "against a running engine. See each channel's 'source' field."
    )

    print(f"\n        {len(added)} extra channels merged from {path.name}")
    for name, only in skipped:
        print(f"          skipped {name}: address read from {', '.join(only)}, not {part}")
    for row in added:
        print(f"          {row['name']:<8} 0x{row['addr']:04X}  {row.get('source', '')}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dam", type=Path, default=DEFAULT_DAM)
    parser.add_argument("--fr", type=Path, default=DEFAULT_FR)
    parser.add_argument("--image", type=Path, default=DEFAULT_IMAGE)
    parser.add_argument(
        "--vs20-table",
        type=lambda s: int(s, 0),
        default=None,
        help="offset of the VS20 table in the image; located automatically if omitted",
    )
    parser.add_argument(
        "--extra",
        type=Path,
        default=DEFAULT_EXTRA,
        help="channels found by reading code, merged into the generated set",
    )
    parser.add_argument(
        "--damos-image",
        type=Path,
        default=DEFAULT_DAMOS_IMAGE,
        help="the image the DAMOS belongs to, used only to re-verify the byte-order rule",
    )
    parser.add_argument(
        "--damos-vs20-table",
        type=lambda s: int(s, 0),
        default=DEFAULT_DAMOS_VS20_TABLE,
        help="VS20 table offset in --damos-image",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="output JSON; defaults to mcmess/data/m38_<part suffix>.json",
    )
    parser.add_argument("--report", action="store_true", help="dump every conversion used")
    args = parser.parse_args(argv)

    for label, path in (("DAMOS", args.dam), ("FR", args.fr), ("image", args.image)):
        if not path.is_file():
            print(f"ERROR: {label} not found: {path}")
            return 1
    return build(args)


if __name__ == "__main__":
    sys.exit(main())
