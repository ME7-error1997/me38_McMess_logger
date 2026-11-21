"""PySide6 front end for the McMESS logger.

Run it with:

    py gui/main.py --port COM3              # log from the car
    py gui/main.py logs/432.csv             # read a log
    py gui/main.py --demo                   # simulated ECU, no hardware
    py gui/main.py --replay logs/432.csv    # a real pull, played back as if live

One window does both jobs (gui/window.py). The GUI only draws. Everything below
it -- transport, session, scheduler, conversions, buffering, CSV -- lives in the
`mcmess` package and is exercised by tests/test_recorder.py without Qt.
"""
