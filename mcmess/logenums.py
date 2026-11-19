"""Meanings for the channels that log a code rather than a quantity.

A channel like this is a number on the chart and a word in the readout. Without
the word it is unreadable: "RF Max Index 5" tells you nothing, "intake
temperature, knock or driving-behaviour retard" tells you why the ECU stopped
filling the cylinder.

Two families so far.

**Bosch M3.8** -- what this logger itself writes:

    KLINFO      the cylinder the knock controller last acted on, with a knock
                flag in bit 7 (see mcmess/derived.py)
    KNK_CYLn    that flag, demultiplexed per cylinder

**BMW MG1** (B58 / S58, MG1CS003 / MG1CS201) as MHD logs it, so a customer's
MHD log opens with its limiter columns readable:

    RF Max Index          which cylinder-fill (load) limiter is active: one value
    Status Torque limiter which torque limiters are active: a 32-bit mask, so
    Torque lim. 1         several can be set at once
    TQ Max Index          the same table by bit number: 11 = NMAX, 18 = MD_MOTOR

The MG1 bit table and fill-limit list are EcuTek's "BMW Limit Flags & States" for
the same DME; MHD's channel names come from owners' logs. TQ Max Index being the
bit number rather than a mask is an assumption -- MHD documents it only as "the
active limiter".

Pure module: no imports beyond the standard library. Tested in
tests/test_logparse.py.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class LogEnum:
    """A code table. ``kind`` says how to read the number.

    ``value``  the number is a code: look it up.
    ``flags``  the number is a bit mask: every set bit names itself.
    ``packed`` the number holds more than one field; ``describe`` unpacks it.
    """

    kind: str
    #: value (or bit number, for flags) -> meaning
    values: dict[int, str] = field(default_factory=dict)
    #: Only for kind "packed".
    describe: Callable[[int], str | None] | None = None


# ------------------------------------------------------------- Bosch M3.8 --

#: KLINFO's low bits. The FR calls the variable "Aktuelle Zylindernummer,
#: klopfen ja/nein": the byte carries the cylinder AND whether it knocked.
#: Which end the count starts at is decided at run time by KnockPerCylinder,
#: so this table names the raw value, not a firing order.
M38_KNOCK_FLAG_BIT = 0x80


def _describe_klinfo(value: int) -> str | None:
    """"130" -> "cylinder 2 (raw), knock detected"."""
    if value < 0 or value > 0xFF:
        return None
    knocked = bool(value & M38_KNOCK_FLAG_BIT)
    raw = value & ~M38_KNOCK_FLAG_BIT
    if raw > 4:
        return f"raw {raw}: outside 0..4" + (", knock detected" if knocked else "")
    return f"cylinder {raw} (raw)" + (", knock detected" if knocked else ", no knock")


#: Cylinder fill limit reason (MG1 "rfmax" index).
MG1_FILL_LIMIT = {
    0: "Standard limitation: fill from the torque request only",
    1: "Condensate protection (low temperature)",
    2: "Turbo component protection: compressor pressure ratio",
    3: "High-pressure fuel pump at its limit",
    4: "Electric (in-tank) fuel pump capacity",
    5: "Intake temperature, knock or driving-behaviour retard",
    6: "Turbine EGT protection",
    7: "Particulate filter",
    8: "Injection duration window (not enough time to inject)",
}

#: Torque limit state, by bit number.
MG1_TORQUE_LIMIT_BITS = {
    0: "ASR: traction control, torque reduction",
    1: "MSR: engine drag control, torque increase",
    2: "GS_DEC: gearbox torque decrease (upshift)",
    3: "GS_INC: gearbox torque increase (downshift)",
    4: "NREG_DEC: speed control",
    5: "NREG_INC: speed control",
    6: "DCC_INC: cruise / longitudinal dynamics",
    7: "SELFSTAB: operating losses",
    8: "Fuel temperature",
    9: "Cooling",
    10: "VAR_UL: variant limit",
    11: "NMAX: rev limiter",
    12: "VMAX: vehicle speed limit",
    13: "ACC_CTL: acceleration control",
    14: "Parking brake",
    15: "HA_DIF: rear axle protection",
    16: "Emergency running manager (limp home)",
    17: "MD_RF: cylinder fill limit (see RF Max Index)",
    18: "MD_MOTOR: engine protection",
    19: "SLAVE_STG: second control unit",
    20: "GS_PERM: gearbox torque limit over CAN",
    21: "MD_RAD: drive axle load",
    22: "GS_AUTARK: gearbox self-protection",
    23: "Clutch slip protection (manual gearbox)",
    24: "GHAS: controlled rear axle lock",
    25: "unknown",
    26: "Powerhop",
    27: "SLD: speed limiter device",
    28: "FIGA_MIN: torque gradient limit (DSC)",
    29: "FIGA_MAX: torque gradient limit (DSC)",
    30: "not used",
    31: "unknown",
}

#: Our own 0/1 columns, so the readout says a word rather than "1".
M38_KNOCK_PER_CYLINDER = {0: "no knock", 1: "knock detected"}

_RULES: tuple[tuple[re.Pattern[str], LogEnum], ...] = (
    (re.compile(r"^klinfo\b", re.I),
     LogEnum(kind="packed", describe=_describe_klinfo)),
    (re.compile(r"^knk_cyl\d", re.I),
     LogEnum(kind="value", values=M38_KNOCK_PER_CYLINDER)),
    (re.compile(r"rf ?max ?index|fill ?limit|load ?limit ?(reason|index)|rfmax", re.I),
     LogEnum(kind="value", values=MG1_FILL_LIMIT)),
    (re.compile(r"tq ?max ?index|torque ?max ?index", re.I),
     LogEnum(kind="value", values=MG1_TORQUE_LIMIT_BITS)),
    (re.compile(r"status ?torque ?lim|torque ?lim|tq ?lim|torque ?limiter", re.I),
     LogEnum(kind="flags", values=MG1_TORQUE_LIMIT_BITS)),
)


def enum_for(label: str) -> LogEnum | None:
    """The enumeration a channel logs, by its name; None for a quantity."""
    for pattern, table in _RULES:
        if pattern.search(label):
            return table
    return None


def describe_value(table: LogEnum, value: float) -> str | None:
    """The meaning of one logged value.

    For a mask, every set bit's name joined with " + " ("none" for 0); for a
    code, its name; None when the table has no entry, so the raw number is shown
    alone rather than mislabelled.
    """
    if value is None or not math.isfinite(value):
        return None
    if table.kind == "packed":
        return table.describe(int(round(value))) if table.describe else None
    if table.kind == "value":
        return table.values.get(int(value)) if float(value).is_integer() else None
    v = int(round(value))
    if v == 0:
        return "none"
    names = [
        table.values.get(bit, f"bit {bit}")
        for bit in range(32)
        if (v >> bit) & 1
    ]
    return " + ".join(names) if names else None
