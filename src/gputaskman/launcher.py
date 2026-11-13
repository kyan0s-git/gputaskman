"""``GPU Task Manager.exe``: the windowless Windows launcher (frozen builds only).

Double-clicking a console program opens it in whatever terminal Windows picks, often a shell window. The launcher
instead starts ``gputaskman.exe --window`` in a console host of its own (``conhost.exe``), so the app opens as its own
window with its own title and icon, and closing that window closes the app. ``--elevate`` asks Windows to run it as
administrator (UAC), which the sensor helper needs for CPU temperatures, fans and package power. Any other arguments
are passed through to gputaskman.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

APP_EXE = "gputaskman.exe"


def command(app: Path, args: list[str], system_root: str | None) -> list[str]:
    """conhost.exe gives the app a console of its own even when Windows Terminal is the default terminal."""
    conhost = Path(system_root or r"C:\Windows") / "System32" / "conhost.exe"
    target = [str(app), "--window", *args]
    return [str(conhost), *target] if conhost.is_file() else target


def _message(text: str) -> None:
    import ctypes

    ctypes.windll.user32.MessageBoxW(None, text, "GPU Task Manager", 0x10)  # MB_ICONERROR


def main() -> None:
    args = sys.argv[1:]
    here = Path(sys.executable).resolve().parent
    app = here / APP_EXE
    if not app.is_file():
        _message(f"{APP_EXE} was not found next to the launcher ({here}). Reinstall GPU Task Manager.")
        sys.exit(1)
    if "--elevate" in args:
        import ctypes

        rest = subprocess.list2cmdline([a for a in args if a != "--elevate"])
        # Relaunch this launcher elevated; it then opens the window as usual. A declined UAC prompt does nothing.
        ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, rest, str(here), 1)
        return
    cmd = command(app, args, os.environ.get("SystemRoot"))
    flags = 0 if cmd[0].lower().endswith("conhost.exe") else getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    try:
        subprocess.Popen(cmd, cwd=os.path.expanduser("~"), creationflags=flags, close_fds=True)
    except OSError as error:
        _message(f"Could not start {APP_EXE}: {error}")
        sys.exit(1)


if __name__ == "__main__":
    main()
