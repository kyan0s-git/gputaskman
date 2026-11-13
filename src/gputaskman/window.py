"""Windows: make the console gputaskman runs in look like its own app window (``--window``).

The launcher (``GPU Task Manager.exe``) starts ``gputaskman.exe --window`` in a console of its own. This module
then gives that console a title, the app icon, a monospace font, a useful size, and turns off QuickEdit so mouse
clicks reach the TUI instead of starting a text selection. Every step is best effort: a failure leaves the console
as it was and never stops the app.
"""

from __future__ import annotations

import sys

TITLE = "GPU Task Manager"
COLUMNS, ROWS = 170, 52
FONT_HEIGHT = 16
FONTS = ("Cascadia Mono", "Consolas")


def _installed_font(names: tuple[str, ...]) -> str:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts") as key:
            installed = []
            i = 0
            while True:
                try:
                    installed.append(winreg.EnumValue(key, i)[0].lower())
                except OSError:
                    break
                i += 1
    except OSError:
        return names[-1]
    for name in names:
        if any(entry.startswith(name.lower()) for entry in installed):
            return name
    return names[-1]


def fit(want: tuple[int, int], largest: tuple[int, int]) -> tuple[int, int]:
    """The window size in character cells: what we want, no larger than the screen allows, never tiny."""
    cols = max(80, min(want[0], largest[0])) if largest[0] else want[0]
    rows = max(24, min(want[1], largest[1])) if largest[1] else want[1]
    return cols, rows


def setup_console() -> None:
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)

    class COORD(ctypes.Structure):
        _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

    class SMALL_RECT(ctypes.Structure):
        _fields_ = [("Left", ctypes.c_short), ("Top", ctypes.c_short), ("Right", ctypes.c_short), ("Bottom", ctypes.c_short)]

    class CONSOLE_FONT_INFOEX(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.ULONG), ("nFont", wintypes.DWORD), ("dwFontSize", COORD),
                    ("FontFamily", wintypes.UINT), ("FontWeight", wintypes.UINT), ("FaceName", wintypes.WCHAR * 32)]

    kernel32.GetStdHandle.restype = wintypes.HANDLE
    kernel32.GetConsoleWindow.restype = wintypes.HWND
    kernel32.GetLargestConsoleWindowSize.restype = COORD
    kernel32.GetLargestConsoleWindowSize.argtypes = [wintypes.HANDLE]
    kernel32.SetConsoleScreenBufferSize.argtypes = [wintypes.HANDLE, COORD]
    kernel32.SetConsoleWindowInfo.argtypes = [wintypes.HANDLE, wintypes.BOOL, ctypes.POINTER(SMALL_RECT)]
    kernel32.SetCurrentConsoleFontEx.argtypes = [wintypes.HANDLE, wintypes.BOOL, ctypes.POINTER(CONSOLE_FONT_INFOEX)]
    kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    shell32.ExtractIconExW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(wintypes.HICON),
                                       ctypes.POINTER(wintypes.HICON), wintypes.UINT]

    out = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
    inp = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
    try:
        kernel32.SetConsoleTitleW(TITLE)
    except OSError:
        pass
    try:
        font = CONSOLE_FONT_INFOEX()
        font.cbSize = ctypes.sizeof(CONSOLE_FONT_INFOEX)
        font.dwFontSize = COORD(0, FONT_HEIGHT)
        font.FontFamily = 0x36  # FF_MODERN | TMPF_TRUETYPE | TMPF_VECTOR: what conhost stores for a TrueType face
        font.FontWeight = 400
        font.FaceName = _installed_font(FONTS)
        kernel32.SetCurrentConsoleFontEx(out, False, ctypes.byref(font))
    except OSError:
        pass
    try:  # the font decides how many cells fit on screen, so size the window after it
        largest = kernel32.GetLargestConsoleWindowSize(out)
        cols, rows = fit((COLUMNS, ROWS), (largest.X, largest.Y))
        tiny = SMALL_RECT(0, 0, 0, 0)
        kernel32.SetConsoleWindowInfo(out, True, ctypes.byref(tiny))  # the window must fit the buffer at every step
        kernel32.SetConsoleScreenBufferSize(out, COORD(cols, rows))
        kernel32.SetConsoleWindowInfo(out, True, ctypes.byref(SMALL_RECT(0, 0, cols - 1, rows - 1)))
    except OSError:
        pass
    try:  # QuickEdit turns a click into a text selection and pauses output; the TUI wants the mouse
        mode = wintypes.DWORD()
        if kernel32.GetConsoleMode(inp, ctypes.byref(mode)):
            ENABLE_QUICK_EDIT_MODE, ENABLE_EXTENDED_FLAGS = 0x0040, 0x0080
            kernel32.SetConsoleMode(inp, (mode.value | ENABLE_EXTENDED_FLAGS) & ~ENABLE_QUICK_EDIT_MODE)
    except OSError:
        pass
    try:
        hwnd = kernel32.GetConsoleWindow()
        large, small = wintypes.HICON(), wintypes.HICON()
        if hwnd and shell32.ExtractIconExW(sys.executable, 0, ctypes.byref(large), ctypes.byref(small), 1):
            WM_SETICON = 0x0080
            if large:
                user32.SendMessageW(hwnd, WM_SETICON, 1, large.value)  # ICON_BIG
            if small:
                user32.SendMessageW(hwnd, WM_SETICON, 0, small.value)  # ICON_SMALL
    except OSError:
        pass


def hold_on_error(error: BaseException) -> None:
    """In its own window a crash would close the window before anyone could read why: show it and wait."""
    import traceback

    traceback.print_exception(error)
    try:
        input("\ngputaskman stopped with an error (above). Press Enter to close this window.")
    except (EOFError, KeyboardInterrupt):
        pass
