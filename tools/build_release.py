"""Build the release: a folder with an exe in it, zipped, that runs with no Python.

    .venv\\Scripts\\python.exe tools\\build_release.py

or double-click ``build_release.bat``. The result is

    dist/McMessLogger-<version>-win64.zip

which unpacks to one folder anyone can put anywhere:

    McMessLogger-<version>-win64/
        McMessLogger.exe          the window: logger and log viewer
        Log.bat                   connect on a port at once
        profiles/tuning.json      the preset it starts with -- the user's to edit
        examples/                 a real log to open before there is a car to log
        README.txt                how to use it
        LICENSE.txt, THIRD-PARTY.txt
        _internal/                Python, Qt and the program itself

Three decisions worth knowing about:

**One folder, not one file.** PyInstaller can also pack everything into a single
exe, which unpacks itself to a temp directory at every start. That costs several
seconds each time, trips more antivirus heuristics, and -- the deciding one --
statically bundles Qt, whereas the LGPL that Qt for Python is under expects the
Qt libraries to stay replaceable. In a folder they are plain DLLs.

**Nothing of the user's is inside _internal.** Presets and logs sit beside the
exe (mcmess/paths.py), so unzipping a newer version over an old one cannot eat
a preset somebody spent an afternoon on.

**The build is run before it is zipped.** A PyInstaller bundle that builds is
not a bundle that starts: a missing data file or hidden import only shows when
the exe is launched. So the script launches it against the simulated ECU, makes
it draw, and refuses to produce a zip if that fails.

This is packaging, not compilation. The program is in the bundle as Python
bytecode, which is not readable in an editor but can be turned back into
something close to the source by anyone determined to. It keeps a recipient
from needing Python; it does not keep the code secret.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mcmess import __version__  # noqa: E402

APP = "McMessLogger"
NAME = f"{APP}-{__version__}-win64"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
ASSETS = ROOT / "tools" / "release"
EXAMPLES = ROOT / "examples"

#: Things PyInstaller would otherwise drag in because something installed in the
#: virtualenv mentions them. None is used; together they are most of the size.
EXCLUDED = (
    "tkinter", "matplotlib", "scipy", "IPython", "PIL", "pytest",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel", "PySide6.QtWebSockets", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets",
    "PySide6.Qt3DCore", "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning", "PySide6.QtLocation",
    "PySide6.QtSql", "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtUiTools",
    "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtSerialBus",
    "PySide6.QtTextToSpeech", "PySide6.QtRemoteObjects", "PySide6.QtScxml",
    "PySide6.QtStateMachine", "PySide6.QtSpatialAudio", "PySide6.QtHttpServer",
)


def say(text: str) -> None:
    print(f"\n== {text}", flush=True)


def fail(text: str) -> "int":
    print(f"\nBUILD FAILED: {text}", flush=True)
    return 1


def folder_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def build() -> Path | None:
    """Run PyInstaller. Returns the folder it made, or None."""
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print(
            "PyInstaller is not installed in this environment. Once:\n"
            "    .venv\\Scripts\\python.exe -m pip install -r requirements-build.txt"
        )
        return None

    for stale in (BUILD, DIST / APP, DIST / NAME):
        shutil.rmtree(stale, ignore_errors=True)
        if stale.exists():
            # Windows will not delete a folder something has open: the exe
            # still running, an Explorer window, a terminal sitting in it.
            print(
                f"Cannot remove the previous build at {stale}.\n"
                "Close the logger if it is running from there, and any window "
                "or terminal open in that folder, then build again."
            )
            return None

    command = [
        sys.executable, "-m", "PyInstaller",
        str(ROOT / "gui" / "main.py"),
        "--name", APP,
        "--noconfirm", "--clean",
        "--onedir",
        # No console window: this is started by double-click. The price is that
        # a command-line mistake exits silently, which is why Log.bat exists.
        "--windowed",
        "--paths", str(ROOT),
        "--distpath", str(DIST),
        "--workpath", str(BUILD),
        "--specpath", str(BUILD),
        # The channel databases and the English descriptions. Kept at the same
        # relative path as in the source tree, so the code that finds them
        # (mcmess/channels.py) is the same code in both.
        "--add-data", f"{ROOT / 'mcmess' / 'data'};mcmess/data",
        # Imported inside a function, where static analysis usually still finds
        # them -- named anyway, because "usually" is how a release ships
        # without its simulator.
        "--hidden-import", "mcmess.simulator",
        "--hidden-import", "mcmess.identify",
        # --opengl is opt-in, but it should not be broken in the release.
        "--hidden-import", "PySide6.QtOpenGL",
        "--hidden-import", "PySide6.QtOpenGLWidgets",
    ]
    for module in EXCLUDED:
        command += ["--exclude-module", module]

    say("PyInstaller")
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode != 0:
        return None
    made = DIST / APP
    return made if (made / f"{APP}.exe").exists() else None


#: Qt files PyInstaller collects because a plugin links against them, not because
#: the program uses them: the virtual keyboard pulls in all of Qt Quick and QML,
#: the PDF image-format plugin pulls in QtPdf, and opengl32sw.dll is a 20 MB
#: software renderer for machines with no OpenGL at all -- which matters only
#: with --opengl, and that flag is opt-in and documented as driver-dependent.
#: Removing them is safe because nothing imports them, and it is CHECKED rather
#: than assumed: the smoke test runs on the folder after this has been done.
PRUNED = (
    "PySide6/opengl32sw.dll",
    "PySide6/Qt6Quick.dll",
    "PySide6/Qt6Qml.dll",
    "PySide6/Qt6QmlModels.dll",
    "PySide6/Qt6QmlMeta.dll",
    "PySide6/Qt6QmlWorkerScript.dll",
    "PySide6/Qt6Pdf.dll",
    "PySide6/Qt6VirtualKeyboard.dll",
    "PySide6/plugins/platforminputcontexts",
    "PySide6/plugins/imageformats/qpdf.dll",
    "PySide6/translations",
)


def prune(release: Path) -> None:
    before = folder_size(release)
    for name in PRUNED:
        target = release / "_internal" / name
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)
    print(f"   pruned {(before - folder_size(release)) / 1e6:.0f} MB of unused Qt")


def assemble(made: Path) -> Path:
    """Add the files a user needs beside the exe, and name the folder."""
    say("Assembling the release folder")
    release = DIST / NAME
    made.rename(release)
    prune(release)

    (release / "profiles").mkdir()
    shutil.copy2(ROOT / "profiles" / "tuning.json", release / "profiles" / "tuning.json")
    # Real logs, so the window has something to show on a machine that has
    # never been near the car. In a folder of their own rather than in logs/:
    # that one is the user's, and "the newest log" should be one they made.
    (release / "examples").mkdir()
    for log in sorted(EXAMPLES.glob("*.csv")):
        shutil.copy2(log, release / "examples" / log.name)
    shutil.copy2(ROOT / "LICENSE", release / "LICENSE.txt")
    for name in ("README.txt", "THIRD-PARTY.txt", "Log.bat"):
        text = (ASSETS / name).read_text(encoding="utf-8").replace("@VERSION@", __version__)
        # Windows line endings: Notepad on an older machine shows LF-only text
        # as one line, and cmd can misparse an LF-only batch file.
        (release / name).write_bytes(
            text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8")
        )
    return release


def smoke_test(release: Path) -> str:
    """Start the built exe and make it work. Returns '' or what went wrong.

    Against the simulated ECU, so it exercises everything the car would except
    the cable: the channel database loads, the preset beside the exe is found,
    the recorder thread samples, the chart draws, a CSV is written to the logs
    folder beside the exe and re-read when the session ends.
    """
    say("Running the built exe against the simulated ECU")
    exe = release / f"{APP}.exe"
    shot = BUILD / "smoke.png"
    shot.unlink(missing_ok=True)
    began = time.monotonic()
    try:
        result = subprocess.run(
            [
                str(exe), "--demo", "--port", "SIM", "--connect", "--knock-base", "0",
                # Named outright: on the build machine "the preset last used"
                # is the developer's own, somewhere else entirely.
                "--profile", str(release / "profiles" / "tuning.json"),
                "--tag", "buildcheck", "--disconnect-after", "6",
                "--screenshot", str(shot), "--screenshot-after", "8",
            ],
            cwd=BUILD,           # NOT the release folder: paths must not depend on it
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return "the exe did not exit within two minutes"
    print(f"   exited {result.returncode} after {time.monotonic() - began:.1f} s")
    if result.returncode != 0:
        return f"the exe exited with code {result.returncode}"
    if not shot.exists() or shot.stat().st_size < 20_000:
        return "the exe ran but drew nothing (no screenshot, or an empty one)"

    logs = sorted((release / "logs").glob("*buildcheck*.csv"))
    if not logs:
        return "no CSV was written to the logs folder beside the exe"
    rows = [
        line for line in logs[-1].read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    print(f"   recorded {len(rows) - 1} samples to logs/{logs[-1].name}")
    if len(rows) < 10:
        return f"the recorded CSV has only {len(rows) - 1} samples"
    if (BUILD / "logs").exists():
        return "logs were written relative to the working directory, not the exe"
    # The release ships clean: no logs, and the preset exactly as committed.
    shutil.rmtree(release / "logs")

    # The other half of the program: reading a log, with nothing connected.
    # On the shipped example, so a release cannot go out with an example its
    # own exe will not open.
    examples = sorted((release / "examples").glob("*.csv"))
    if not examples:
        return "the release has no example log in its examples folder"
    opened = BUILD / "smoke-example.png"
    opened.unlink(missing_ok=True)
    try:
        result = subprocess.run(
            [str(exe), str(examples[0]), "--screenshot", str(opened),
             "--screenshot-after", "3"],
            cwd=BUILD, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return "the exe did not exit within two minutes of opening the example log"
    print(f"   opened examples/{examples[0].name}, exited {result.returncode}")
    if result.returncode != 0:
        return f"opening the example log exited with code {result.returncode}"
    if not opened.exists() or opened.stat().st_size < 20_000:
        return "the exe opened the example log but drew nothing"
    if (release / "logs").exists():
        return "opening a log created a logs folder; the release would ship with it"
    forget_buildcheck()
    shipped = (release / "profiles" / "tuning.json").read_bytes()
    if shipped != (ROOT / "profiles" / "tuning.json").read_bytes():
        return "the smoke test changed the shipped preset"
    return ""


def forget_buildcheck() -> None:
    """Drop what the window remembered about the smoke test's log.

    The window keeps the drawn channels per log file in the user's settings.
    The recorded file is gone a moment later, and the example inside the
    release folder is not a file anybody on the build machine reads from
    there; neither entry should pile up, one per build.
    """
    try:
        from PySide6 import QtCore
    except ImportError:
        return
    settings = QtCore.QSettings("McMESS", "logviewer")
    for key in settings.allKeys():
        if "buildcheck" in key or f"/{NAME}/" in key:
            settings.remove(key)


def make_zip(release: Path) -> Path:
    say("Zipping")
    archive = DIST / f"{NAME}.zip"
    archive.unlink(missing_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for file in sorted(release.rglob("*")):
            if file.is_file():
                bundle.write(file, Path(NAME) / file.relative_to(release))
    return archive


def main() -> int:
    if sys.platform != "win32":
        return fail("the release is a Windows build; run this on Windows")
    made = build()
    if made is None:
        return fail("PyInstaller did not produce an exe")
    release = assemble(made)
    problem = smoke_test(release)
    if problem:
        return fail(problem)
    archive = make_zip(release)

    print(
        f"\nBuilt McMESS logger {__version__}\n"
        f"  folder  {release}   ({folder_size(release) / 1e6:.0f} MB)\n"
        f"  zip     {archive}   ({archive.stat().st_size / 1e6:.0f} MB)\n"
        f"  checked {BUILD / 'smoke.png'} and {BUILD / 'smoke-example.png'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
