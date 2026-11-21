"""Where the program keeps the things a user edits: presets and logs.

Run from source, that is the repository: ``profiles/`` and ``logs/`` beside the
code, which is where they have always been.

Run as a built release, the code is inside ``_internal/`` -- a folder the user
should never have to open, and one that is replaced wholesale by the next
version. So presets and logs live beside the exe instead, in the one folder the
user actually sees:

    McMessLogger/
        McMessLogger.exe
        profiles/tuning.json      <- theirs, survives an update
        logs/                     <- theirs
        _internal/                <- ours

The channel databases are the opposite case. They are part of the program, not
something to edit, so they stay with the code (``mcmess/data``) and are found
relative to it in both layouts.
"""

from __future__ import annotations

import sys
from pathlib import Path


def frozen() -> bool:
    """Whether this is a built release rather than a checkout."""
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """The folder presets and logs are kept in."""
    if frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


#: Fixed at import: nothing about it can change while the program runs.
APP_DIR = app_dir()


def in_app_dir(path: str | Path) -> Path:
    """A path the user gave, made absolute against the app folder.

    ``--log-dir logs`` means the logs folder of the program, not of whatever
    directory a shortcut happened to start it in. An absolute path is the
    user's own business and is returned as it is.
    """
    path = Path(path)
    return path if path.is_absolute() else APP_DIR / path
