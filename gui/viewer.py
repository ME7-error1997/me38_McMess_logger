"""Open a log. The viewer is the logger's own window now; this is the old door.

    py gui/viewer.py                      # open empty, then Open log...
    py gui/viewer.py logs/432.csv         # straight into a log

There used to be a separate viewer window here. It and the logger turned out to
be the same job with the data arriving or not, so they were merged into
gui/window.py and this file is kept so that ``view.bat``, old shortcuts and
anything importing ``LogViewerWindow`` go on working. It adds nothing:
``py gui/viewer.py x.csv`` is ``py gui/main.py x.csv``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gui.main import main  # noqa: E402
from gui.window import (  # noqa: E402,F401  (re-exported)
    PRESETS,
    GridModel,
    MainWindow,
    pick_color,
)

#: The old name. One window, one class.
LogViewerWindow = MainWindow

if __name__ == "__main__":
    raise SystemExit(main())
