@echo off
rem ---------------------------------------------------------------------------
rem  Start the McMESS logger.
rem
rem  Double-click this file, or from a prompt:
rem      log.bat                              uses COM2
rem      log.bat COM5                         uses COM5
rem      log.bat COM5 profiles\cruise.json    ...with that profile
rem
rem  The channel window opens first: tick what to log and what to draw, or
rem  just press Enter to take the profile as it is. It then connects, and a CSV
rem  starts by itself in logs\, named from the date, time and ECU. Nothing is
rem  ever overwritten.
rem
rem  With no profile named it starts with the last one you used, and the first
rem  time with profiles\tuning.json.
rem
rem  Nothing here writes to the ECU -- the 018CG responder is read-only.
rem ---------------------------------------------------------------------------

setlocal

rem Run from this file's own folder, so double-clicking works wherever the
rem shortcut sits and the relative paths below still resolve.
cd /d "%~dp0"

set "PORT=%~1"
if "%PORT%"=="" set "PORT=COM2"

set "PROFILE="
if not "%~2"=="" set "PROFILE=--profile "%~2""

rem KLINFO was measured as 0-based on this ECU, so the knock columns need no
rem warm-up to decide which cylinder is which.
set "KNOCKBASE=0"

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

echo Starting the logger on %PORT% ...
echo.

".venv\Scripts\python.exe" gui\main.py --port %PORT% --select --knock-base %KNOCKBASE% %PROFILE%
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo   The logger exited with code %RC%.
    echo.
    echo   If the handshake failed: wait about 3 seconds and try again. The ECU
    echo   ignores a new init pulse until the line has been quiet that long after
    echo   a previous session.
    echo.
    pause
)

endlocal
exit /b %RC%
