@echo off
rem ---------------------------------------------------------------------------
rem  Build the release zip: an exe in a folder, no Python needed to run it.
rem
rem  Double-click this file. The result is dist\McMessLogger-<version>-win64.zip
rem  The version number is __version__ in mcmess\__init__.py.
rem
rem  The build starts the exe once against the simulated ECU to check it works,
rem  so a logger window opens and closes by itself for a few seconds. That is
rem  the check, not a mistake.
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

".venv\Scripts\python.exe" -m pip install --quiet -r requirements-build.txt
".venv\Scripts\python.exe" tools\build_release.py
set "RC=%ERRORLEVEL%"

echo.
pause
endlocal
exit /b %RC%
