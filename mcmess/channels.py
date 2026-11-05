"""Channel definitions and physical conversions for the 018CG.

Everything is loaded from one generated JSON, mcmess/data/m38_018cg.json, built by
tools/build_database.py out of:

  * a DAMOS .DAM  -- the conversion formulas
  * the FR .FRM   -- the VS20 channel list (names, descriptions, conversion, width)
  * the ECU .bin  -- the VS20 table, which resolves pseudo-addresses to RAM addresses

Rebuild it with:

    py tools/build_database.py

PROVENANCE, worth keeping in mind when reading a log: there is no DAMOS for the 018CG.
The formulas come from 8D0907558E (Audi A4, M3.8.2, DAMOS 30N); the channel list and
addresses are the 018CG's own. Conversion names are shared across the M3.8 family so
the formulas are expected to apply, but that is an inference, not a measurement. The
caveat travels with the data in ChannelSet.meta["caveat"].
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent / "data" / "m38_018cg.json"


# --------------------------------------------------------------------- word order
#
# How a 2-byte (FMT:2) channel maps onto a pair of byte addresses. This was open
# question 6 and it is now RESOLVED from the data, not guessed.
#
# The A4 damos carries /UMP records giving every variable's real base address, and the
# FR's VS20 table in that same A4 image gives the address each entry resolves to.
# Comparing them across all 80 FMT:2 entries is unambiguous:
#
#     FR name without a "+1" suffix  ->  VS20 address == base       35 / 35
#     FR name with a "+1" suffix     ->  VS20 address == base + 1   45 / 45
#
# So the "+1" in a name like QSOLL+1 or FR2+1 is literal: that entry points at the
# SECOND byte of the word. Either way the word occupies (base, base+1), which makes
# this a per-channel property decided by the name -- not one global mode. The base is
# stored in the database as "word_base"; word_base_for() recomputes it if absent.
#
# Which byte is most significant follows from the CPU: the 80C196 is little-endian, so
# the base address holds the low byte. That one step is architecture, not measured
# data, so it stays overridable.

BYTE_ORDERS = ("little", "big")
_BYTE_ORDER = "little"


def set_byte_order(order: str) -> None:
    """Override which end of a 16-bit channel is the most significant byte.

    Defaults to "little", matching the 80C196. Only change this if bench data shows
    every 2-byte channel reading byte-swapped.
    """
    global _BYTE_ORDER
    if order not in BYTE_ORDERS:
        raise ValueError(f"byte order must be one of {BYTE_ORDERS}, got {order!r}")
    _BYTE_ORDER = order


def get_byte_order() -> str:
    return _BYTE_ORDER


def word_base_for(name: str, addr: int) -> int:
    """The base (lower) address of the 16-bit word an FR entry refers to."""
    return addr - 1 if name.endswith("+1") else addr


def word_value(base_byte: int, high_byte: int, order: str | None = None) -> int:
    """Combine the two bytes of a word, given base address byte first."""
    order = order or _BYTE_ORDER
    if order == "little":
        return (high_byte << 8) | base_byte
    return (base_byte << 8) | high_byte


# -------------------------------------------------------------------- conversions


@dataclass(frozen=True)
class Conversion:
    """A DAMOS conversion ("Umrechnung"): raw ECU byte(s) -> physical value.

    Three kinds:

    "formula"
        The DAMOS rational form, evaluated from six coefficients p1..p6:

            x    = raw, reinterpreted as two's complement modulo p6 when p6 != 0
            phys = (p2 - p4*(x - p5)) / (p3*(x - p5) - p1)

        Every record in this image is a degenerate case of it: plain linear (p3 = 0),
        reciprocal (p1 = p4 = 0), or identity. For linear ones `scale` and `offset`
        are precomputed for display, but apply() stays the authority.

    "table"
        A discrete raw -> value lookup. Not interpolated; an unlisted raw has no value.

    "text"
        A raw -> label mapping for flags and status words (0 = "Inaktiv", ...).
    """

    name: str
    kind: str
    unit: str
    description: str
    coeffs: tuple[float, ...] = ()
    table: tuple[tuple[int, float], ...] = ()
    text: tuple[tuple[int, str], ...] = ()
    width: int | None = None
    validated: bool = False
    scale: float | None = None
    offset: float | None = None
    signed_modulus: float = 0.0
    decl_min: float | None = None
    decl_max: float | None = None
    note: str = ""

    def apply(self, raw: int) -> float | None:
        """Convert a raw value to physical units, or None if this kind has no number."""
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
        if p6 and x >= p6 / 2.0:
            x -= p6
        x -= p5
        denominator = p3 * x - p1
        if denominator == 0:
            return None
        return (p2 - p4 * x) / denominator

    def label(self, raw: int) -> str | None:
        """The text label for a raw value, for flag/status conversions."""
        for key, text in self.text:
            if key == raw:
                return text
        return None

    @property
    def decimals(self) -> int:
        """A sensible number of decimal places for display, from the step size."""
        step = abs(self.scale) if self.scale else None
        if step is None or step == 0:
            return 2
        if step >= 1:
            return 0
        if step >= 0.1:
            return 1
        if step >= 0.01:
            return 2
        return 3

    def quantize(self, value: float) -> float:
        """Round to the displayed precision, collapsing -0.0 to 0.0.

        Without this a dimensionless factor sitting at zero renders as "-0.000".
        """
        rounded = round(value, self.decimals)
        return 0.0 if rounded == 0 else rounded

    def format(self, raw: int) -> str:
        """Human-readable value with unit, falling back to the raw number."""
        if self.kind == "text":
            return self.label(raw) or f"raw {raw}"
        value = self.apply(raw)
        if value is None:
            return f"raw {raw}"
        text = f"{self.quantize(value):.{self.decimals}f}"
        return f"{text} {self.unit}".strip()

    @classmethod
    def from_json(cls, name: str, blob: dict) -> "Conversion":
        return cls(
            name=name,
            kind=blob.get("kind", "formula"),
            unit=blob.get("unit", "") or "",
            description=blob.get("description", "") or "",
            coeffs=tuple(blob.get("coeffs", ()) or ()),
            table=tuple((int(k), float(v)) for k, v in blob.get("table", ()) or ()),
            text=tuple((int(k), str(v)) for k, v in blob.get("text", ()) or ()),
            width=blob.get("width"),
            validated=bool(blob.get("validated")),
            scale=blob.get("scale"),
            offset=blob.get("offset"),
            signed_modulus=float(blob.get("signed_modulus") or 0.0),
            decl_min=blob.get("min"),
            decl_max=blob.get("max"),
            note=blob.get("note", "") or "",
        )


#: Used when a channel's conversion is unknown or must not be applied.
RAW = Conversion(name="RAW", kind="raw", unit="", description="no conversion available")


# ----------------------------------------------------------------------- channels


@dataclass(frozen=True)
class Channel:
    """One loggable ECU variable."""

    name: str
    addr: int
    size: int
    umr: str
    description: str
    # None for a channel that has no VS20 entry because it was found by reading code
    # rather than by walking the pseudo-address table. See mcmess/data/extra_channels.json.
    pseudo: int | None = 0
    umr_displayed: str = ""
    suspect: str = ""
    #: Where the address came from, for channels that are not from the VS20 table.
    source: str = ""
    word_base: int | None = None
    _conversion: Conversion = field(default=RAW, repr=False, compare=False)

    @property
    def conversion(self) -> Conversion:
        return self._conversion

    @property
    def has_physical(self) -> bool:
        """Whether a physical value may be reported for this channel.

        False when no formula was found, or when the channel is tagged suspect --
        DVL_SYNA+1, for instance, is FMT:2 but carries an 8-bit conversion because it
        holds two packed bytes rather than one word. Scaling it would yield a
        confident, wrong number, so it is reported raw.
        """
        if self.suspect:
            return False
        return self._conversion.kind in ("formula", "table", "text")

    @property
    def unit(self) -> str:
        """Physical unit, or "" when there is none to report."""
        return self._conversion.unit if self.has_physical else ""

    @property
    def unit_label(self) -> str:
        """What to show beside the value.

        Distinguishes the two reasons a channel has no unit, which otherwise look
        identical and mislead: a converted but *dimensionless* quantity (a factor
        like the lambda integrator FR, whose values are physical and meaningful)
        reports "", while a channel with no usable conversion at all reports "raw".
        """
        if not self.has_physical:
            return "raw"
        return self._conversion.unit

    @property
    def base(self) -> int:
        """Base (lower) address of this channel's storage.

        Differs from `addr` only for the FR's "+1" entries, whose listed address is the
        second byte of the word.
        """
        if self.word_base is not None:
            return self.word_base
        return word_base_for(self.name, self.addr) if self.size == 2 else self.addr

    @property
    def byte_addresses(self) -> tuple[int, ...]:
        """The byte addresses this channel needs, lowest address first."""
        if self.size == 2:
            base = self.base
            return (base, base + 1)
        return (self.addr,)

    @property
    def page(self) -> int:
        return self.addr >> 8

    def assemble(self, values: dict[int, int]) -> int | None:
        """Combine raw bytes from a read result into this channel's raw value."""
        try:
            if self.size == 2:
                base = self.base
                return word_value(values[base], values[base + 1])
            return values[self.addr]
        except KeyError:
            return None

    def physical(self, raw: int) -> float | None:
        """The converted value, or None when none may be reported."""
        if not self.has_physical:
            return None
        return self._conversion.apply(raw)

    def format(self, raw: int) -> str:
        """Display string: physical value with unit, else the raw number."""
        if not self.has_physical:
            return f"raw {raw}"
        return self._conversion.format(raw)


def csv_number(value: float, digits: int = 6) -> str:
    """Format a physical value for a CSV cell.

    Collapses IEEE negative zero, which is not a cosmetic concern here: most
    conversions are rational forms with a negative denominator, so NQ40 is
    (0 - 40x) / (0 - 1) and returns -0.0 at raw 0. Left alone, every idle rpm,
    load and timing cell in a log reads "-0".
    """
    if value != value:  # NaN
        return ""
    if value == 0:
        return "0"
    return f"{value:.{digits}g}"


class ChannelSet:
    """All channels and conversions known for one image."""

    def __init__(
        self,
        channels: list[Channel],
        conversions: dict[str, Conversion] | None = None,
        rejected: list[tuple[str, str]] | None = None,
        meta: dict | None = None,
    ) -> None:
        self.channels = channels
        self.conversions = conversions or {}
        self.rejected = rejected or []
        self.meta = meta or {}
        self.by_name: dict[str, Channel] = {}
        self.duplicates: list[str] = []
        for channel in channels:
            if channel.name in self.by_name:
                self.duplicates.append(channel.name)
            else:
                self.by_name[channel.name] = channel

    def __len__(self) -> int:
        return len(self.channels)

    def __iter__(self):
        return iter(self.channels)

    def get(self, name: str) -> Channel:
        try:
            return self.by_name[name]
        except KeyError:
            raise KeyError(
                f"unknown channel {name!r}; {len(self.channels)} channels loaded "
                "-- run 'probe.py channels' to list them"
            ) from None

    def select(self, names: list[str]) -> list[Channel]:
        return [self.get(n) for n in names]

    def in_page(self, page: int) -> list[Channel]:
        return [c for c in self.channels if c.page == page]

    @property
    def without_conversion(self) -> list[Channel]:
        return [c for c in self.channels if not c.has_physical]

    @staticmethod
    def addresses_for(channels: list[Channel]) -> list[int]:
        addrs: list[int] = []
        for channel in channels:
            addrs.extend(channel.byte_addresses)
        return sorted(set(addrs))


def load_channels(path: Path | str = DEFAULT_DB) -> ChannelSet:
    """Load the generated channel + conversion database."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(
            f"channel database not found: {path}\n"
            "Generate it with:  py tools/build_database.py"
        )
    blob = json.loads(path.read_text(encoding="utf-8"))

    conversions = {
        name: Conversion.from_json(name, body)
        for name, body in blob.get("conversions", {}).items()
    }

    channels: list[Channel] = []
    for row in blob.get("channels", []):
        umr = row.get("umr", "") or ""
        channels.append(
            Channel(
                name=row["name"],
                addr=int(row["addr"]),
                size=int(row.get("size", 1)),
                umr=umr,
                description=row.get("description", "") or "",
                pseudo=None if row.get("pseudo") is None else int(row["pseudo"]),
                umr_displayed=row.get("umr_displayed", "") or "",
                suspect=row.get("suspect", "") or "",
                source=row.get("source", "") or "",
                word_base=row.get("word_base"),
                _conversion=conversions.get(umr, RAW),
            )
        )

    rejected = [(str(a), str(b)) for a, b in blob.get("rejected", [])]
    return ChannelSet(channels, conversions, rejected, blob.get("meta", {}))


#: A cheap, high-rate starting set: every one of these lives in page 0x00, so the whole
#: group costs one wire byte per channel and zero parity switches.
PAGE0_STARTER = [
    "N40",      # 0xA4  RPM, 40 rpm/bit
    "TL",       # 0xA6  load signal, 0.05 ms/Umdr. per bit
    "DKPOT",    # 0xB9  throttle angle, 0.4335 deg/bit
    "ZWOUT",    # 0x7F  ignition angle, 72 - 0.75*raw deg
    "TMOT",     # 0x9B  coolant temp, 0.75*raw - 48 C
    "TANS",     # 0x9A  intake air temp, same scaling
    "UB",       # 0x9C  battery voltage, 0.0704 V/bit
    "NSOL",     # 0x5D  target RPM, 10 rpm/bit
    "MLUFT",    # 0xA5  air mass, 4 kg/h per bit
    "STATVS",   # 0x88  status cell
]
