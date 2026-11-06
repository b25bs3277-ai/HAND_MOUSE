"""
mouse_control.py
----------------
Moves, clicks and scrolls the mouse on Windows using ctypes + SendInput.

Why not PyAutoGUI?
  PyAutoGUI waits a little after every call and is slower. SendInput is the
  same path a real USB mouse uses, so OneNote, PowerPoint, Epic Pen and
  Windows Ink treat our input exactly like a real mouse.

This file also:
  * turns on "DPI awareness" so sizes are real pixels (needed when Windows
    display scaling is 125% / 150%, which most laptops use),
  * lists your monitors so the hand can target one screen or all of them.

Nothing here needs to be installed: ctypes comes with Python.
"""

import ctypes
import sys
from collections import namedtuple

IS_WINDOWS = sys.platform == "win32"

# One monitor: position of its top-left corner, its size, and whether it is the main one.
Monitor = namedtuple("Monitor", "left top width height primary")


# ---------------------------------------------------------------------------
# Windows API setup (only runs on Windows)
# ---------------------------------------------------------------------------
if IS_WINDOWS:
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)

    INPUT_MOUSE = 0
    MOUSEEVENTF_MOVE = 0x0001
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    MOUSEEVENTF_RIGHTDOWN = 0x0008
    MOUSEEVENTF_RIGHTUP = 0x0010
    MOUSEEVENTF_WHEEL = 0x0800
    MOUSEEVENTF_VIRTUALDESK = 0x4000   # coordinates cover ALL monitors
    MOUSEEVENTF_ABSOLUTE = 0x8000      # coordinates are a position, not a nudge

    # These structures copy the layout Windows expects for SendInput.
    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    user32.SendInput.restype = wintypes.UINT
    user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
    user32.GetSystemMetrics.restype = ctypes.c_int

    class MONITORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
        ]

    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
        ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    user32.EnumDisplayMonitors.argtypes = (
        wintypes.HDC, ctypes.POINTER(wintypes.RECT), MONITORENUMPROC, wintypes.LPARAM)
    user32.GetMonitorInfoW.argtypes = (wintypes.HANDLE, ctypes.POINTER(MONITORINFO))


def set_dpi_awareness():
    """Ask Windows for real pixel sizes. Call this FIRST, before any window opens."""
    if not IS_WINDOWS:
        return
    try:  # Windows 10 1703+ : best mode ("per monitor v2")
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:  # Windows 8.1+
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except (AttributeError, OSError):
        pass
    try:  # very old Windows
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


def get_virtual_screen():
    """The rectangle that holds ALL monitors: (left, top, width, height)."""
    if not IS_WINDOWS:
        return (0, 0, 1920, 1080)
    gsm = user32.GetSystemMetrics
    return (gsm(76), gsm(77), max(gsm(78), 1), max(gsm(79), 1))


def list_monitors():
    """Return the monitors, main monitor first, then the rest from left to right."""
    if not IS_WINDOWS:
        return [Monitor(0, 0, 1920, 1080, True)]
    found = []

    def _callback(hmon, hdc, rect_ptr, lparam):
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            r = info.rcMonitor
            found.append(Monitor(r.left, r.top, r.right - r.left, r.bottom - r.top,
                                 bool(info.dwFlags & 1)))
        return True

    callback = MONITORENUMPROC(_callback)   # keep a reference while Windows calls it
    user32.EnumDisplayMonitors(None, None, callback, 0)
    found.sort(key=lambda m: (not m.primary, m.left, m.top))
    if not found:
        left, top, w, h = get_virtual_screen()
        found = [Monitor(left, top, w, h, True)]
    return found


def emergency_release():
    """Let go of BOTH mouse buttons, no matter what we think their state is."""
    if not IS_WINDOWS:
        return
    for flag in (MOUSEEVENTF_LEFTUP, MOUSEEVENTF_RIGHTUP):
        inp = INPUT(type=INPUT_MOUSE)
        inp.mi.dwFlags = flag
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


class Mouse:
    """Small, fast mouse controller. Positions are screen pixels (any monitor)."""

    def __init__(self):
        self.left_is_down = False
        self.right_is_down = False
        self.last_pos = None
        self.events = []          # only used for testing on non-Windows computers
        self.refresh_screen()

    def refresh_screen(self):
        """Re-read the size of the whole desktop (call again if a monitor is plugged in)."""
        self.vx, self.vy, self.vw, self.vh = get_virtual_screen()

    # -- internal helpers --------------------------------------------------
    def _to_absolute(self, x, y):
        # SendInput wants 0..65535 across the whole virtual desktop.
        # This formula lands exactly on the pixel we ask for.
        nx = ((int(x) - self.vx) * 65536 + self.vw - 1) // self.vw
        ny = ((int(y) - self.vy) * 65536 + self.vh - 1) // self.vh
        return max(0, min(65535, nx)), max(0, min(65535, ny))

    def _send(self, flags, x=None, y=None, data=0):
        if x is not None:
            x, y = int(round(x)), int(round(y))
            self.last_pos = (x, y)
        if not IS_WINDOWS:
            self.events.append((flags, x, y, data))
            return
        inp = INPUT(type=INPUT_MOUSE)
        if x is not None:
            inp.mi.dx, inp.mi.dy = self._to_absolute(x, y)
            flags |= MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
        inp.mi.mouseData = data & 0xFFFFFFFF
        inp.mi.dwFlags = flags
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    # -- public actions ----------------------------------------------------
    def move(self, x, y):
        if self.last_pos == (int(round(x)), int(round(y))):
            return  # already there, do not spam Windows
        self._send(0, x, y)

    def left_down(self, x=None, y=None):
        self._send(MOUSEEVENTF_LEFTDOWN if IS_WINDOWS else "LEFTDOWN", x, y)
        self.left_is_down = True

    def left_up(self, x=None, y=None):
        self._send(MOUSEEVENTF_LEFTUP if IS_WINDOWS else "LEFTUP", x, y)
        self.left_is_down = False

    def click(self, x=None, y=None):
        self.left_down(x, y)
        self.left_up()

    def right_click(self, x=None, y=None):
        self._send(MOUSEEVENTF_RIGHTDOWN if IS_WINDOWS else "RIGHTDOWN", x, y)
        self._send(MOUSEEVENTF_RIGHTUP if IS_WINDOWS else "RIGHTUP")

    def scroll(self, amount):
        """amount > 0 scrolls up. 120 = one wheel 'notch'."""
        self._send(MOUSEEVENTF_WHEEL if IS_WINDOWS else "WHEEL", data=int(amount))

    def release_all(self):
        if self.left_is_down:
            self.left_up()
        self.left_is_down = False
        self.right_is_down = False
