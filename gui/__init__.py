"""PySide6 front end for the McMESS logger.

Run it with:

    py gui/main.py --port COM3
    py gui/main.py --demo        # simulated ECU, no hardware

The GUI only draws. Everything below it -- transport, session, scheduler,
conversions, buffering, CSV -- lives in the `mcmess` package and is exercised by
tests/test_recorder.py without Qt.
"""
