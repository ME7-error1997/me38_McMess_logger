@echo off
rem ---------------------------------------------------------------------------
rem  Open a datalog. Same window as the logger, with nothing connected.
rem
rem  Double-click this file to open the newest log in logs\, or from a prompt:
rem      view.bat                       the newest log in logs\
rem      view.bat logs\432.csv          that one
rem
rem  You can also drag a CSV onto this file.
rem
rem  It reads this logger's own CSVs and other people's: VCDS measuring blocks
rem  and advanced measuring blocks, ME7Logger, OP-COM, and generic CSV from MHD,
rem  bootmod3 or anything else that writes text.
rem ---------------------------------------------------------------------------

setlocal

rem Run from this file's own folder, so double-clicking works wherever the
rem shortcut sits and the relative paths below still resolve.
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo   No virtualenv found at .venv\Scripts\python.exe
    echo.
    echo   Create it once with:
    echo       py -3.14 -m venv .venv
    echo       .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

rem An empty argument leaves LOG undefined, which is what "if not defined"
rem below tests. Deliberately no goto out of the loop: a goto from inside a
rem for inside an if is the kind of thing cmd parses differently on a whim.
set "LOG=%~1"
if not defined LOG (
    rem No file given: take the newest log, which is almost always the pull you
    rem have just driven. dir /o-d sorts newest first.
    for /f "delims=" %%F in ('dir /b /o-d "logs\*.csv" 2^>nul') do (
        if not defined LOG set "LOG=logs\%%F"
    )
)

if defined LOG (
    echo Opening %LOG% ...
    ".venv\Scripts\python.exe" gui\main.py "%LOG%"
) else (
    echo No log given and none found in logs\ - use Open log... in the window.
    ".venv\Scripts\python.exe" gui\main.py
)
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo   The window exited with code %RC%.
    echo.
    pause
)

endlocal
exit /b %RC%
