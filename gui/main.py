"""McMESS logger and log viewer: one window.

    py gui/main.py --port COM3              # log from the car
    py gui/main.py logs/432.csv             # read a log
    py gui/main.py --demo                   # simulated ECU, no hardware
    py gui/main.py --replay logs/432.csv    # a real pull, played back as if live

The window is gui/window.py; this file is only the command line around it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pyqtgraph as pg  # noqa: E402
from PySide6 import QtCore, QtWidgets  # noqa: E402

from mcmess.channels import load_channels  # noqa: E402
from mcmess.wideband import add_arguments as add_wideband_args  # noqa: E402

# Absolute, not relative: this file is meant to run directly as
# `py gui/main.py`, where there is no parent package. The sys.path insert above
# makes the repo root importable, so `gui` resolves either way -- as a script or
# as `py -m gui.main`.
from gui import theme  # noqa: E402
from gui.window import REPLAY_PORT, MainWindow  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="McMESS logger and log viewer")
    parser.add_argument("log", nargs="?", help="a log to open straight away")
    parser.add_argument("--port", help="preselect this serial port, e.g. COM3")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="offer a simulated ECU with a running engine model, so the window "
        "can be used with no hardware",
    )
    parser.add_argument(
        "--replay",
        metavar="LOG",
        help="offer a recorded log as a port: connecting plays it back at the "
        "pace it was recorded, so the live view can be seen on a real pull",
    )
    parser.add_argument(
        "--replay-speed",
        type=float,
        default=1.0,
        help="play a --replay faster or slower than it was recorded (default 1)",
    )
    parser.add_argument("--database", help="override the channel/conversion JSON")
    parser.add_argument(
        "--profile",
        help="the preset to start with: which channels are logged, which are "
        "drawn, and their colours and Y ranges (default: the last one used, "
        "else profiles/tuning.json)",
    )
    parser.add_argument(
        "--channels",
        help="comma-separated channel names to log instead of a profile, e.g. "
        "N40,TL,FR. Every quantity among them is drawn",
    )
    parser.add_argument(
        "--select",
        action="store_true",
        help="open the channel window as soon as the program starts, to edit "
        "the preset first; its Connect button then connects",
    )
    parser.add_argument(
        "--connect",
        action="store_true",
        help="connect as soon as the window opens, with the preset as it is",
    )
    parser.add_argument(
        "--knock-base",
        default="auto",
        help=(
            "whether KLINFO counts cylinders 0..3 or 1..4 (0, 1, or auto). "
            "auto waits for a 0 or a 4, which settles it without guessing"
        ),
    )
    parser.add_argument(
        "--log-dir",
        default="logs",
        help="where recordings are written (default: logs/)",
    )
    parser.add_argument(
        "--tag",
        default="",
        help="appended to the auto-generated log filename, e.g. --tag pull2",
    )
    parser.add_argument(
        "--ask-filename",
        action="store_true",
        help="open a save dialog instead of naming the log automatically",
    )
    parser.add_argument(
        "--no-autorecord",
        dest="autorecord",
        action="store_false",
        help="do not start recording on connect; wait for the Record button "
        "(by default a CSV is written from the moment the link comes up)",
    )
    add_wideband_args(parser)
    parser.add_argument(
        "--smooth",
        action="store_true",
        help="antialias the traces. Prettier, but roughly halves the sample "
        "rate -- painting competes with the sampling thread",
    )
    parser.add_argument(
        "--opengl",
        action="store_true",
        help="render plots through OpenGL. Measured a further ~1.5x sample "
        "rate on the bench, but it is driver-dependent -- check the traces "
        "actually draw on your machine before trusting a log taken with it",
    )
    parser.add_argument(
        "--screenshot", help="render the window to this PNG and exit (for checking)"
    )
    parser.add_argument(
        "--screenshot-after",
        type=float,
        default=None,
        help="seconds to wait before --screenshot (default 9 when connecting, "
        "1.2 otherwise)",
    )
    parser.add_argument(
        "--disconnect-after",
        type=float,
        default=None,
        help="disconnect this many seconds after connecting (for checking what "
        "the window does when a session ends)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        channels = load_channels(args.database) if args.database else load_channels()
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}")
        return 1

    # Painting is the logger's main throughput cost, not the serial link.
    # Measured on a real 018CG, six channels, window visible:
    #     antialias on                 219 B/s   27 Hz
    #     antialias off                451 B/s   56 Hz
    #     antialias off + OpenGL       537 B/s   67 Hz
    # against 735 B/s with the window hidden, so painting -- not the K-line --
    # is the limit. Antialiasing is therefore off by default. OpenGL is NOT:
    # it is faster again, but it is driver-dependent and a grab() of the window
    # came back with an empty plot area, so it is opt-in via --opengl until it
    # has been eyeballed on the machine in question.
    pg.setConfigOptions(
        antialias=args.smooth,
        useOpenGL=args.opengl,
        background=theme.SURFACE,
        foreground=theme.TEXT_SECONDARY,
    )

    app = QtWidgets.QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    app.setStyleSheet(theme.STYLESHEET)

    window = MainWindow(channels, args, path=args.log)
    wanted = REPLAY_PORT if args.replay and not args.port else args.port
    if wanted:
        index = window.port_combo.findData(wanted)
        if index >= 0:
            window.port_combo.setCurrentIndex(index)
        elif args.connect or args.select:
            print(
                f"warning: {wanted} is not in the port list, so there is nothing "
                "to connect to. Press Refresh once the cable is plugged in."
            )
    window.show()
    if window._startup_note:
        window.statusBar().showMessage(window._startup_note, 10000)

    connecting = args.connect or args.select
    if args.connect:
        # Shortly after the window is up, not before: the handshake blocks its
        # thread, and starting it inside main() would show an unpainted window
        # for the second or two the init pulse and ack take.
        QtCore.QTimer.singleShot(400, window.connect)
    elif args.select:
        QtCore.QTimer.singleShot(400, window.choose_then_connect)
    if args.disconnect_after is not None:
        QtCore.QTimer.singleShot(
            int(400 + args.disconnect_after * 1000), window._disconnect
        )

    if args.screenshot:
        path = Path(args.screenshot)
        path.parent.mkdir(parents=True, exist_ok=True)
        wait = args.screenshot_after
        if wait is None:
            wait = 9.0 if connecting else 1.2

        def shoot() -> None:
            window.grab().save(str(path))
            print(f"screenshot: {path.resolve()}")
            window.close()
            app.quit()

        QtCore.QTimer.singleShot(int(wait * 1000), shoot)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
