@echo off
rem ---------------------------------------------------------------------------
rem  Play a recorded log back as if it were arriving from the car.
rem
rem  Double-click this file to replay the newest log in logs\, or:
rem      replay.bat                       the newest log in logs\
rem      replay.bat logs\432.csv          that one
rem      replay.bat logs\432.csv 4        ...four times as fast
rem
rem  You can also drag a CSV onto this file.
rem
rem  The window behaves exactly as it does on the car: the cursor rides on the
rem  newest sample and the view is locked until the log runs out or you press
rem  Disconnect. Nothing is written -- a replay does not record.
rem ---------------------------------------------------------------------------

setlocal

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

set "SPEED=%~2"
if "%SPEED%"=="" set "SPEED=1"

set "LOG=%~1"
if not defined LOG (
    for /f "delims=" %%F in ('dir /b /o-d "logs\*.csv" 2^>nul') do (
        if not defined LOG set "LOG=logs\%%F"
    )
)

if not defined LOG (
    echo.
    echo   No log given and none found in logs\ - there is nothing to replay.
    echo.
    pause
    exit /b 1
)

echo Replaying %LOG% at x%SPEED% ...
".venv\Scripts\python.exe" gui\main.py --replay "%LOG%" --replay-speed %SPEED% --connect
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo   The window exited with code %RC%.
    echo.
    pause
)

endlocal
exit /b %RC%
