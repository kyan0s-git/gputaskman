"""A local crash log, so a failure in a window that closes (or a headless self-test) leaves its traceback behind.

Nothing here leaves the machine. The log lives in ``%LOCALAPPDATA%\\gputaskman\\crash.log`` on Windows and
``~/.cache/gputaskman/crash.log`` elsewhere, is capped at about 1 MB, and holds tracebacks only (which can include
local paths; review before sharing).
"""

from __future__ import annotations

import os
import sys
import time
import traceback
from pathlib import Path

MAX_BYTES = 1 << 20


def path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "gputaskman" / "crash.log"


def write(error: BaseException, context: str = "") -> Path | None:
    """Append a timestamped traceback; returns where it went, or None when the log cannot be written."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        v = version("gputaskman")
    except PackageNotFoundError:
        v = "?"
    target = path()
    text = (f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} gputaskman {v} on {sys.platform} "
            f"(Python {sys.version.split()[0]}){' - ' + context if context else ''}\n"
            + "".join(traceback.format_exception(type(error), error, error.__traceback__)) + "\n")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.stat().st_size > MAX_BYTES:
            target.write_text(target.read_text(encoding="utf-8", errors="replace")[-MAX_BYTES // 2:], encoding="utf-8")
        with target.open("a", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        return None
    return target
