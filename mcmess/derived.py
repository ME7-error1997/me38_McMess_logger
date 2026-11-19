"""Columns computed from other channels rather than read from the ECU.

Two things the M3.8 does not hand you directly:

**Per-cylinder knock retard.** There is no DWKR1..DWKR4. Knock control works one
cylinder at a time and publishes a pair: `DWKR_Z`, the retard of whichever cylinder
it is processing right now, and `KLINFO`, which cylinder that is. Logged raw, the
DWKR_Z trace jumps between cylinders and tells you almost nothing. Demultiplexed
against KLINFO it becomes four traces you can actually read.

**Air mass in g/s.** `ML` is kg/h. Dividing by 3.6 in your head on every row is the
kind of thing that eventually gets done wrong.

Derived columns cost nothing on the K-line; their *inputs* do, and asking for one
automatically selects whatever it needs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .channels import Channel, ChannelSet

NAN = float("nan")

#: KLINFO packs the cylinder number and a knock indication into one byte. Bit 7 is
#: the indication; the low bits are the number.  [INF]
KNOCK_FLAG_BIT = 0x80
#: Everything below the flag. Deliberately NOT 0x07: masking to three bits would
#: turn a bogus 9 into cylinder 1 and quietly file a reading under the wrong
#: cylinder. Only bit 7 has a documented meaning, so only bit 7 is stripped, and
#: anything still out of range is counted as unmapped rather than guessed at.
CYLINDER_MASK = 0x7F


@dataclass(frozen=True)
class DerivedChannel(Channel):
    """A column computed from a sample, not read from memory.

    Like AuxChannel it is a Channel so the picker, plots, ring buffer and CSV need
    no special case, and like AuxChannel it carries no ECU address -- `addr` is -1
    and `assemble()` raises, so a bug that routed one into the read program fails
    loudly instead of quietly reading page 0xFF.
    """

    derived_unit: str = ""
    decimals: int = 2
    #: Which Derivation fills this column.
    derivation_key: str = ""

    @property
    def byte_addresses(self) -> tuple[int, ...]:
        return ()

    @property
    def has_physical(self) -> bool:
        return True

    @property
    def unit(self) -> str:
        return self.derived_unit

    @property
    def unit_label(self) -> str:
        return self.derived_unit or ""

    @property
    def page(self) -> int:
        return -1

    @property
    def is_derived(self) -> bool:
        return True

    def assemble(self, values: dict[int, int]) -> int | None:
        raise AssertionError(
            f"derived channel {self.name!r} is computed from a sample, not read "
            "from memory -- it has no ECU address"
        )


class Derivation:
    """Fills one or more derived columns from the physical values of one sample."""

    key: str = ""
    #: ECU channel names this needs. Selected automatically when an output is asked for.
    inputs: tuple[str, ...] = ()

    def channels(self) -> list[DerivedChannel]:
        raise NotImplementedError

    def compute(self, values: dict[str, float]) -> dict[str, float]:
        raise NotImplementedError

    def reset(self) -> None:
        """Forget any state. Called when sampling (re)starts."""

    def note(self) -> str:
        """One line about what this derivation resolved at runtime, or ''."""
        return ""


def _column(name: str, unit: str, description: str, key: str, decimals: int = 2):
    return DerivedChannel(
        name=name,
        addr=-1,
        size=0,
        umr="",
        description=description,
        pseudo=None,
        derived_unit=unit,
        derivation_key=key,
        decimals=decimals,
    )


# ------------------------------------------------------------------- air mass


class AirMassGramsPerSecond(Derivation):
    """ML (kg/h) as g/s. 1 kg/h = 1000 g / 3600 s."""

    key = "ml_gs"
    inputs = ("ML",)

    def channels(self) -> list[DerivedChannel]:
        return [
            _column(
                "ML_GS",
                "g/s",
                "Air mass flow in g/s, computed from ML (kg/h divided by 3.6)",
                self.key,
                decimals=2,
            )
        ]

    def compute(self, values: dict[str, float]) -> dict[str, float]:
        ml = values.get("ML", NAN)
        return {"ML_GS": NAN if ml is None or math.isnan(ml) else ml / 3.6}


# ---------------------------------------------------------------------- knock


@dataclass
class KnockPerCylinder(Derivation):
    """Demultiplex (KLINFO, DWKR_Z) into one retard column per cylinder.

    Each sample updates the cylinder KLINFO names and holds the other three at
    their last known value, which is what you want: retard persists between that
    cylinder's turns, so holding shows the current state rather than a comb.

    **The KLINFO encoding is not documented.** The FR calls it "Akt. Zyl.Nr. der
    Klopfreg." and gives it the plain DEZ conversion, which leaves open whether it
    counts 1..4 or 0..3. Rather than guess -- a wrong base silently relabels every
    cylinder, which is exactly the kind of confident-but-wrong output this project
    tries hard not to produce -- `base="auto"` waits for proof. A observed 0 proves
    0-based; an observed 4 proves 1-based. Neither can occur under the other
    convention, so the decision is sound rather than heuristic, and the columns
    report NaN for the fraction of a second it takes. Pass base=0 or base=1 to skip
    the wait once you know.
    """

    key: str = "knock_cyl"
    inputs: tuple[str, ...] = ("KLINFO", "DWKR_Z")
    cylinders: int = 4
    #: 0, 1, or "auto".
    base: int | str = "auto"

    _hold: dict[int, float] = field(default_factory=dict, repr=False)
    _flag: dict[int, float] = field(default_factory=dict, repr=False)
    _knocked: dict[int, int] = field(default_factory=dict, repr=False)
    _resolved: int | None = field(default=None, repr=False)
    _seen: set[int] = field(default_factory=set, repr=False)
    _unmapped: int = field(default=0, repr=False)

    def __post_init__(self) -> None:
        if self.base != "auto":
            self._resolved = int(self.base)

    def channels(self) -> list[DerivedChannel]:
        return [
            _column(
                f"KR_CYL{n}",
                "grad KW",
                f"Knock retard held for cylinder {n}, demultiplexed from DWKR_Z and KLINFO",
                self.key,
                decimals=2,
            )
            for n in range(1, self.cylinders + 1)
        ] + [
            _column(
                f"KNK_CYL{n}",
                "",
                f"Knock detected on cylinder {n} at the last knock-control event "
                f"(KLINFO bit 7)",
                self.key,
                decimals=0,
            )
            for n in range(1, self.cylinders + 1)
        ]

    def reset(self) -> None:
        self._hold.clear()
        self._flag.clear()
        self._knocked.clear()
        self._seen.clear()
        self._unmapped = 0
        if self.base == "auto":
            self._resolved = None

    def compute(self, values: dict[str, float]) -> dict[str, float]:
        info = values.get("KLINFO", NAN)
        retard = values.get("DWKR_Z", NAN)

        if info is not None and not math.isnan(info):
            word = int(round(info))
            # KLINFO is not just a cylinder number. The FR calls it "Aktuelle
            # Zylindernummer, klopfen ja/nein" -- number AND whether that cylinder
            # knocked -- and a car log caught 0x82: bit 7 set over cylinder 2.
            # Treating the whole byte as a number made those samples look
            # out-of-range and threw them away, which is precisely backwards: a
            # sample flagged as knocking is the one you least want to drop.  [INF]
            # on the bit position, which rests on that observation plus the FR text.
            knocked = bool(word & KNOCK_FLAG_BIT)
            raw = word & CYLINDER_MASK
            self._seen.add(word)
            if self._resolved is None:
                # Decide only on proof, never on the values seen so far being
                # "probably" one convention.
                if raw == 0:
                    self._resolved = 0
                elif raw == self.cylinders:
                    self._resolved = 1
            if self._resolved is not None:
                cylinder = raw - self._resolved + 1
                if 1 <= cylinder <= self.cylinders:
                    if retard is not None and not math.isnan(retard):
                        self._hold[cylinder] = retard
                    if knocked:
                        self._knocked[cylinder] = self._knocked.get(cylinder, 0) + 1
                        self._flag[cylinder] = 1.0
                    else:
                        self._flag[cylinder] = 0.0
                else:
                    self._unmapped += 1

        out: dict[str, float] = {
            f"KR_CYL{n}": self._hold.get(n, NAN)
            for n in range(1, self.cylinders + 1)
        }
        out.update(
            {f"KNK_CYL{n}": self._flag.get(n, NAN) for n in range(1, self.cylinders + 1)}
        )
        return out

    def note(self) -> str:
        if self._resolved is None:
            return (
                f"KLINFO base still undecided (seen {sorted(self._seen)}): "
                "no 0 and no 4 observed yet, so the cylinder columns stay blank"
            )
        convention = "0..3" if self._resolved == 0 else "1..4"
        line = f"KLINFO reads {convention} (values seen: {sorted(self._seen)})"
        if self._knocked:
            hits = ", ".join(f"cyl{n}x{c}" for n, c in sorted(self._knocked.items()))
            line += f"; knock flag caught on {hits}"
        if self._unmapped:
            line += f"; {self._unmapped} sample(s) outside that range were dropped"
        return line


#: Derivations available by name. Asking for any of a derivation's output columns
#: pulls in the derivation and its input channels.
DERIVATIONS: dict[str, type[Derivation]] = {
    AirMassGramsPerSecond.key: AirMassGramsPerSecond,
    KnockPerCylinder.key: KnockPerCylinder,
}


def _outputs_index() -> dict[str, str]:
    """Derived column name -> derivation key."""
    index: dict[str, str] = {}
    for key, cls in DERIVATIONS.items():
        for column in cls().channels():
            index[column.name] = key
    return index


DERIVED_COLUMNS = _outputs_index()


def expand_selection(
    channel_set: ChannelSet, names: list[str], knock_base: int | str = "auto"
) -> tuple[list[Channel], list[Derivation]]:
    """Resolve a channel list that may name derived columns.

    Returns the ECU channels to read (inputs of any derivation included, even when
    not asked for, since the column cannot be computed without them) followed by the
    derived columns in the order requested. A derivation appears once however many
    of its columns were asked for.
    """
    wanted_derived: list[str] = []
    ecu_names: list[str] = []
    for name in names:
        if name in DERIVED_COLUMNS:
            wanted_derived.append(name)
        else:
            ecu_names.append(name)

    derivations: dict[str, Derivation] = {}
    for column in wanted_derived:
        key = DERIVED_COLUMNS[column]
        if key not in derivations:
            cls = DERIVATIONS[key]
            derivations[key] = (
                cls(base=knock_base) if cls is KnockPerCylinder else cls()
            )
            for needed in derivations[key].inputs:
                if needed not in ecu_names:
                    ecu_names.append(needed)

    channels: list[Channel] = channel_set.select(ecu_names)
    by_name = {c.name: c for d in derivations.values() for c in d.channels()}
    channels += [by_name[n] for n in wanted_derived]
    return channels, list(derivations.values())


def apply(derivations: list[Derivation], values: dict[str, float]) -> dict[str, float]:
    """Run every derivation over one sample's physical values."""
    out: dict[str, float] = {}
    for derivation in derivations:
        out.update(derivation.compute(values))
    return out
