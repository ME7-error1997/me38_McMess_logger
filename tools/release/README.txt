McMESS logger @VERSION@
Bosch M3.8 (VW/Audi 1.8T AGU) - 06A906018CG and 06A906018AQ
=====================================================================

Fast ECU logging over the K-line, and a viewer for the logs it makes.
It also opens VCDS, ME7Logger, OP-COM and plain CSV logs.

It only READS the ECU. This ECU's logging protocol has no write
command, so the program cannot change anything in it.


WHAT YOU NEED
-------------
  - Windows 10 or 11, 64-bit. Nothing to install: unzip the folder
    anywhere you can write to (not Program Files) and run it.
  - A plain FTDI K-line cable (FT232R based, "KKL 409.1" style).
    CH340 cables do not work reliably for this.
  - Ignition on.


BEFORE THE FIRST RUN: THE LATENCY TIMER
---------------------------------------
Set the cable's latency timer to 1 ms. This is not optional fine
tuning: at the default 16 ms the logger runs about twelve times
slower.

    Device Manager -> Ports (COM & LPT) -> your cable -> Properties
      -> Port Settings -> Advanced -> Latency Timer (msec) -> 1

It is remembered per USB port, so set it again if you move the cable
to another socket.


LOGGING
-------
  1. Start McMessLogger.exe.
  2. Pick the cable's port, and a preset.
  3. Press Connect.

A CSV starts by itself in the logs folder, named from the date, time
and ECU. Nothing is ever overwritten.

While data is arriving the cursor rides on the newest sample and the
values on the left are the live ones; the chart cannot be zoomed or
clicked. Press Disconnect (or Freeze view) and it becomes yours:
click to place the cursor, drag to zoom, double-click to see it all.
The session you just made is on screen as the log it has become.

Log.bat does steps 2 and 3 for you:

    Log.bat COM5          connect on COM5 with the preset last used

If the handshake fails, wait about three seconds and try again. The
ECU ignores a new connection attempt until the line has been quiet
that long after the previous one.


PRESETS
-------
A preset is what gets logged, what gets drawn, and each channel's
colour and Y range.

  - Channels...  edits the preset. Each channel has a "log" tick (it
    is recorded and shows its value) and a "graph" tick (it is also
    drawn). "Save as..." makes a new preset.
  - Double-click a channel on the left for its colour and Y range.
  - Tick or untick a channel on the left to draw or hide it.

Everything you change is saved to the preset's file straight away.
Presets are small text files in the profiles folder; copy one to give
it to somebody else.

Page 0x00 channels are the cheap ones: one byte each on the wire. The
more channels, and the more memory pages they are spread over, the
lower the sample rate. The channel window shows the cost as you tick.


READING A LOG
-------------
Open log... , or drag a CSV onto McMessLogger.exe.

    drag            zoom to the dragged span
    shift + drag    pan
    wheel           zoom at the mouse
    click           place the cursor
    double click    reset the zoom
    right click     colour, Y range, axis channel

Channels that share a unit share a Y scale, so load actual and load
requested can be compared directly. Scales of different units are
independent: the vertical distance between an rpm trace and a timing
trace means nothing. Read the numbers for those.


THINGS TO KNOW
--------------
  - The conversion formulas for the 018CG come from the DAMOS of a
    related ECU (Audi A4 M3.8.2), because none exists for the 018CG
    itself. Channel names are shared across the family, so they are
    expected to be right, but that is an inference.
  - It starts with the 06A906018CG channel list. For a 06A906018AQ
    make a shortcut to the exe and add   --database 06A906018AQ
    to its target. The program asks the ECU which one it is before
    logging and stops if the list does not match: addresses differ
    between ECUs, and the wrong list would give plausible, wrong
    numbers rather than an error.
  - A recording is only complete on disk once you stop recording or
    disconnect. Do not pull the plug on the laptop mid-pull.
  - Windows may warn that the program is from an unknown publisher.
    It is not signed. "More info" -> "Run anyway".


FILES
-----
    McMessLogger.exe     the program
    Log.bat              connect on a port straight away
    profiles\            your presets
    logs\                your recordings (made on the first one)
    _internal\           the program's own files - leave alone
    LICENSE.txt
    THIRD-PARTY.txt      the libraries inside and their licences

To update, unzip the new version into a new folder and copy your
profiles and logs folders across.
