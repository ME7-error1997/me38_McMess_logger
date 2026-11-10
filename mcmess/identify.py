"""Check that the connected ECU is the one the loaded channel database describes.

Why this exists
---------------
The M3.8 family shares a channel *list* but not a RAM *layout*. Comparing the two
images on hand, 121 of the 249 VS20 channels resolve to different addresses in
06A906018AQ than in 06A906018CG -- the whole 0xC9xx-0xCFxx region is shifted, while
the page-0 block (rpm, load, throttle, both temperatures, battery) happens to agree.

That is the dangerous shape of failure. Logging an AQ through the CG database does
not raise, does not time out and does not produce obvious garbage: it reads the
neighbouring cell and returns a number in a believable range. Someone tuning from
that log would be reading fiction. So the ECU is asked who it is before any of it
is trusted.

It can be asked, because the McMESS responder serves any 16-bit address and below
0xC000 that address space is flash. Every 018 image seen so far carries its part
number as plain ASCII at 0xBF0A, which makes identification an 11-byte read over
the same link that does the logging -- no extra protocol, no extra hardware.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mcmess.channels import ChannelSet, database_for_part

#: What the check concluded.
#:   ok          the ECU says it is exactly what the database was built for
#:   mismatch    the ECU identified itself as a DIFFERENT ECU -- do not log
#:   unreadable  the fingerprint read failed or came back garbled
#:   unsupported the database carries no fingerprint (built before this existed)
STATUS = ("ok", "mismatch", "unreadable", "unsupported")


@dataclass(frozen=True)
class Identity:
    status: str
    expected: str | None = None
    found: str | None = None
    #: A database that DOES match what the ECU reported, when one exists on disk.
    suggestion: Path | None = None

    @property
    def safe_to_log(self) -> bool:
        """Only a clean match is safe. Everything else needs a human decision."""
        return self.status == "ok"

    def describe(self) -> str:
        if self.status == "ok":
            return f"ECU identifies as {self.found}, matching the loaded database"
        if self.status == "mismatch":
            lines = [
                f"ECU IDENTIFIES AS {self.found}, but the loaded database is for "
                f"{self.expected}.",
                "Channel addresses differ across the M3.8 family, so logging now "
                "would read the wrong RAM cells and still look plausible.",
            ]
            if self.suggestion:
                lines.append(f"Use --database {self.suggestion} instead.")
            else:
                lines.append(
                    "No database for that ECU has been generated. Build one with:  "
                    "py tools/build_database.py --image <that ECU's bin>"
                )
            return "\n".join(lines)
        if self.status == "unreadable":
            return (
                f"could not read the ECU identity (got {self.found!r}, expected "
                f"{self.expected!r}). The link is answering, so this is more likely "
                "a variant that stores its part number elsewhere than a bad cable."
            )
        return (
            "the loaded database carries no ECU fingerprint, so the connected ECU "
            "cannot be checked against it. Rebuild it with tools/build_database.py."
        )


def identify(session, channels: ChannelSet) -> Identity:
    """Read the ECU's part number and compare it with the loaded database.

    Costs one short block read (11 bytes) on an already-open session.
    """
    fingerprint = channels.fingerprint
    if fingerprint is None:
        return Identity("unsupported", expected=channels.part_number)

    addr, length, expected = fingerprint
    found = session.read_identity(addr, length)

    if found == expected:
        return Identity("ok", expected=expected, found=found)
    # A part number that reads cleanly but differs is a real mismatch. One that comes
    # back with unprintable bytes means we did not find a part number at all, which
    # is a different problem and gets a different message.
    if "?" in found or not found.strip():
        return Identity("unreadable", expected=expected, found=found)
    return Identity(
        "mismatch",
        expected=expected,
        found=found,
        suggestion=database_for_part(found),
    )
