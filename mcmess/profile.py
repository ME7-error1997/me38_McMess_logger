"""Logging profiles -- "presets" in the window: what to log, what to draw, and how.

A profile is a small JSON file::

    {
      "format": "mcmess-profile/1",
      "ecu": "06A906018CG",
      "log":   ["N40", "TL", "LDSHK", "KR_CYL1", "KLINFO"],
      "graph": ["N40", "TL", "LDSHK", "KR_CYL1"],
      "style": {
        "N40": {"color": "#3987e5", "range": [0, 8000]},
        "TL":  {"range": [0, 10]}
      }
    }

Two lists, because they answer different questions. ``log`` is what goes into the
CSV: everything you might want afterwards, since a channel you did not log is a
pull you have to drive again. ``graph`` is what is worth looking at while it
happens. They are not the same set -- KLINFO has to be logged for the per-cylinder
knock columns to exist, and drawn as a trace it is a cylinder counter stepping
0-1-2-3 forty times a second, which reads as noise and flattens everything that
shares a chart with it.

``style`` is how a channel looks whenever it is drawn: its colour, and a fixed Y
range in place of auto-fit. Both are optional and per channel. They live in the
profile because they are part of the same habit -- rpm is blue and runs 0 to
8000 every time, not only until the window is closed -- and a channel only has
an entry once somebody has chosen something for it.

Names are channel names from the ECU's database, or the names of computed
columns (``KR_CYL1``, ``ML_GS``). The inputs a computed column needs are added by
the logger when it connects; they do not have to be listed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .logenums import enum_for

FORMAT = "mcmess-profile/1"

#: Where profiles live unless a path says otherwise: profiles/ beside the code.
PROFILE_DIR = Path(__file__).resolve().parent.parent / "profiles"

#: The profile loaded when nothing else is asked for.
DEFAULT_PROFILE = PROFILE_DIR / "tuning.json"

#: Units that mark a channel as a code or a status byte rather than a quantity.
CODE_UNITS = ("dez", "raw", "hex", "bin")


class ProfileError(ValueError):
    """A profile file that cannot be used. The message is one sentence for a human."""


@dataclass
class Style:
    """How one channel is drawn. Either part may be left to the program."""

    #: "#rrggbb", or "" to take the next free colour of the palette.
    color: str = ""
    #: A fixed Y range, or None to fit what is visible.
    range: tuple[float, float] | None = None

    @property
    def empty(self) -> bool:
        return not self.color and self.range is None

    def to_json(self) -> dict:
        blob: dict = {}
        if self.color:
            blob["color"] = self.color
        if self.range is not None:
            blob["range"] = [self.range[0], self.range[1]]
        return blob

    @classmethod
    def from_json(cls, blob) -> "Style":
        """Lenient: a hand-edited file with a bad entry loses that entry, not the lot."""
        if not isinstance(blob, dict):
            return cls()
        color = blob.get("color")
        color = color.strip() if isinstance(color, str) else ""
        if not (len(color) == 7 and color.startswith("#")):
            color = ""
        span = blob.get("range")
        try:
            low, high = float(span[0]), float(span[1])
            span = (low, high) if high > low else None
        except (TypeError, ValueError, IndexError):
            span = None
        return cls(color=color.lower(), range=span)


@dataclass
class Profile:
    """Two channel lists, and how the channels in them are drawn."""

    log: list[str] = field(default_factory=list)
    graph: list[str] = field(default_factory=list)
    #: The ECU the names belong to. Informational: the logger verifies the ECU
    #: itself on connect, but a profile for another box deserves a warning first.
    ecu: str = ""
    #: Where it was loaded from or last saved to. None for one built in memory.
    path: Path | None = None
    #: Channel name -> its colour and Y range. Not limited to the log list: a
    #: wideband on its own port, or an input read only for a computed column,
    #: can have a look of its own too.
    style: dict[str, Style] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.path.stem if self.path else "unsaved"

    def copy(self) -> "Profile":
        return Profile(
            list(self.log), list(self.graph), self.ecu, self.path,
            {n: Style(s.color, s.range) for n, s in self.style.items()},
        )

    def normalised(self) -> "Profile":
        """No duplicates, nothing on the graph that is not logged, no empty styles."""
        log = list(dict.fromkeys(n.strip() for n in self.log if n and n.strip()))
        known = set(log)
        graph = [
            n for n in dict.fromkeys(g.strip() for g in self.graph if g)
            if n in known
        ]
        style = {
            n: Style(s.color, s.range) for n, s in self.style.items() if not s.empty
        }
        return Profile(log, graph, self.ecu, self.path, style)

    # -- style ---------------------------------------------------------------

    def color_of(self, name: str) -> str:
        style = self.style.get(name)
        return style.color if style else ""

    def range_of(self, name: str) -> tuple[float, float] | None:
        style = self.style.get(name)
        return style.range if style else None

    def set_color(self, name: str, color: str) -> bool:
        """Returns whether anything changed, so the caller knows to save."""
        style = self.style.setdefault(name, Style())
        changed = style.color != color
        style.color = color
        if style.empty:
            del self.style[name]
        return changed

    def set_range(self, name: str, span: tuple[float, float] | None) -> bool:
        style = self.style.setdefault(name, Style())
        span = (float(span[0]), float(span[1])) if span is not None else None
        changed = style.range != span
        style.range = span
        if style.empty:
            del self.style[name]
        return changed


def belongs_on_graph(name: str, unit: str = "") -> bool:
    """Whether a channel is worth a trace by default.

    A code is not: KLINFO packs a cylinder number and a flag into one byte, and
    a status byte is a set of bits. Both are worth logging and worth reading as
    a decoded value at the cursor, and both are noise as a line.
    """
    if enum_for(name) is not None:
        return False
    return unit.strip().lower() not in CODE_UNITS


def default_graph(channels) -> list[str]:
    """The channels to draw when a profile does not say: every quantity."""
    return [
        c.name for c in channels
        if belongs_on_graph(c.name, getattr(c, "unit_label", ""))
    ]


def load_profile(path: str | Path) -> Profile:
    """Read a profile. Raises ProfileError with one sentence about what is wrong."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProfileError(f"cannot read {path.name}: {exc.strerror or exc}") from exc
    try:
        blob = json.loads(text)
    except ValueError as exc:
        raise ProfileError(f"{path.name} is not valid JSON: {exc}") from exc
    if not isinstance(blob, dict) or not isinstance(blob.get("log"), list):
        raise ProfileError(
            f'{path.name} is not a logging profile: it has no "log" list of '
            "channel names"
        )
    log = [str(n) for n in blob["log"] if isinstance(n, str)]
    if not log:
        raise ProfileError(f'{path.name} has an empty "log" list: nothing to log')
    graph = blob.get("graph")
    if not isinstance(graph, list):
        # An older or hand-written file with only a log list: draw the lot and
        # let the reader untick what they do not want.
        graph = [n for n in log if belongs_on_graph(n)]
    style = blob.get("style")
    profile = Profile(
        log=log,
        graph=[str(n) for n in graph if isinstance(n, str)],
        ecu=str(blob.get("ecu") or ""),
        path=path,
        style={
            str(name): Style.from_json(entry) for name, entry in style.items()
        } if isinstance(style, dict) else {},
    )
    return profile.normalised()


def save_profile(profile: Profile, path: str | Path) -> Path:
    """Write a profile. One name per line, so a diff of two profiles is readable."""
    path = Path(path)
    clean = profile.normalised()
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = {
        "format": FORMAT,
        "ecu": clean.ecu,
        "log": clean.log,
        "graph": clean.graph,
    }
    if clean.style:
        blob["style"] = {name: clean.style[name].to_json() for name in clean.style}
    path.write_text(
        json.dumps(blob, indent=2)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def split_known(names: list[str], known) -> tuple[list[str], list[str]]:
    """(names the database or the computed columns know, names they do not)."""
    good = [n for n in names if n in known]
    bad = [n for n in names if n not in known]
    return good, bad
