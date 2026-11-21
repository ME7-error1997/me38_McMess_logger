@echo off
rem ---------------------------------------------------------------------------
rem  Start the McMESS logger and connect at once.
rem
rem      Log.bat COM5                         connect on COM5
rem      Log.bat COM5 profiles\cruise.json    ...with that preset
rem      Log.bat                              just open the window
rem
rem  It uses the preset last used. A CSV starts by itself in logs\, named
rem  from the date, time and ECU. Nothing is ever overwritten.
rem
rem  Nothing here writes to the ECU -- its logging protocol is read-only.
rem ---------------------------------------------------------------------------

setlocal
cd /d "%~dp0"

set "PROFILE="
if not "%~2"=="" set "PROFILE=--profile "%~2""

rem KLINFO counts cylinders from 0 on these ECUs, so the per-cylinder knock
rem columns need no warm-up to decide which cylinder is which.
if "%~1"=="" (
    start "" "McMessLogger.exe" --knock-base 0
) else (
    start "" "McMessLogger.exe" --port %~1 --connect --knock-base 0 %PROFILE%
)

endlocal
