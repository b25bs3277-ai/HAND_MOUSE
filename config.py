"""
config.py
---------
Loads and saves settings.json.

* DEFAULTS below is the full list of settings, with a comment for each.
* If settings.json is missing, broken, or has a wrong value, the default is used,
  so the app always starts.
* settings.json lives next to main.py (or next to HandMouse.exe). If that folder
  is read-only, it goes to %APPDATA%\\HandMouse instead.
"""

import copy
import json
import logging
import os
import sys
import threading

log = logging.getLogger(__name__)

APP_NAME = "HandMouse"

DEFAULTS = {
    # ---------- Camera ----------
    "camera_index": 0,            # 0 = first webcam, 1 = second, ...
    "camera_width": 640,
    "camera_height": 480,
    "low_res_mode": False,        # True = 320x240 + lighter model -> less CPU, a bit more jitter
    "target_fps": 30,             # we never process faster than this
    "mirror": True,               # selfie view: move right -> cursor goes right
    "low_light_boost": True,      # brighten dark frames before hand tracking

    # ---------- Hand tracking ----------
    "hand": "right",              # "right", "left" or "any"
    "swap_handedness": False,     # turn on if it tracks the wrong hand
    "max_hands": 1,               # 2 = finds your hand even when both hands are visible (more CPU)
    "tracker_backend": "auto",    # "auto", "legacy" (built-in model) or "tasks" (hand_landmarker.task)
    "model_complexity": 1,        # legacy only: 0 = fastest, 1 = most accurate
    "detection_confidence": 0.6,  # lower (0.4) helps in bad light, higher = fewer false hands
    "tracking_confidence": 0.5,

    # ---------- Screen + active box ----------
    "monitor": 1,                 # 0 = all monitors together, 1 = main monitor, 2+ = other monitors
    # The "active box" is the part of the camera image that maps to the whole screen.
    # Values are fractions of the camera image (0..1). Smaller box = smaller arm moves.
    "active_box": {"x": 0.30, "y": 0.45, "w": 0.40, "h": 0.30},   # low in the image: no need to raise the arm
    "cursor_point": "palm",       # what moves the cursor: "palm" (palm centre, steadiest) or "index" (fingertip)
    "lock_aspect": True,          # keep the box the same shape as the screen (round letters stay round)

    # ---------- Smoothing (One Euro filter + dead zone) ----------
    "min_cutoff": 0.6,            # LOWER = steadier when moving slowly (more lag)
    "beta": 0.015,                # HIGHER = less lag when moving fast
    "d_cutoff": 1.0,              # advanced, leave at 1.0
    "dead_zone_px": 4.0,          # tiny shakes smaller than this (pixels) are ignored

    # ---------- Gestures ----------
    "stable_frames": 4,           # a gesture must last this many frames (3-5) before it counts
    # Thumb touches. "gap" = thumb-tip to fingertip gap / hand size (shown live in the preview).
    # "Calibrate touch" in the control panel sets these 4 values for YOUR hand.
    "pinch_on": 0.20,             # thumb + INDEX: touch starts when the gap is below this
    "pinch_off": 0.30,            # ... and ends when the gap grows past this
    "middle_on": 0.20,            # thumb + MIDDLE (right click): touch starts below this
    "middle_off": 0.30,           # ... and ends above this
    "tap_frames": 2,              # a thumb tap must last this many frames (lower = quick taps count)
    "hold_ms": 400,               # thumb + index touch held this long -> left button stays DOWN (drag / select)
    "click_freeze_ms": 300,       # cursor stays still this long after a click (makes double-click easy)
    "click_lookback_ms": 200,     # click where the cursor was this long before the pinch (aim fix)
    "right_click_hold_s": 1.0,    # hold a fist this long for a right click
    "scroll_speed": 30,           # how fast the back-of-hand scroll goes
    "scroll_invert": False,
    "smooth_scroll": True,        # small smooth scroll steps (turn off if an old app scrolls too fast)
    "palm_mode": "toggle",        # "toggle" = open palm locks/unlocks, "hold" = only freezes while shown
    "palm_hold_s": 0.8,           # how long to hold the open palm to lock/unlock
    "lost_hand_grace_ms": 150,    # if the hand vanishes longer than this, the button is released

    # ---------- Feature switches (turn on one at a time while testing) ----------
    "enable_draw": True,          # index+middle = hold left button
    "enable_click": True,         # thumb + index tap = left click (2 quick taps = double-click)
    "enable_middle_right_click": True,  # thumb + middle finger touch = right click
    "back_action": "scroll",      # back of the hand: "scroll" (hold above/below start), "right_click" or "off"
    "enable_scroll": False,       # three fingers = scroll (old)
    "enable_fist_click": False,   # fist = left click (old)
    "enable_right_click": False,  # fist HELD 1 s = right click (old; only works if fist click is off)
    "enable_palm_lock": False,    # open palm = lock (old; if on, the open palm stops moving the cursor)

    # ---------- App ----------
    "autostart": True,            # start tracking as soon as the app opens
    "preview_visible": True,
    "preview_width": 360,
    "preview_topmost": True,
    "release_camera_when_stopped": True,  # camera light goes off when stopped (F8)

    # ---------- Hotkeys (restart the app after changing) ----------
    "hotkey_start_stop": "f8",
    "hotkey_preview": "f9",
    "hotkey_exit": "esc",
    "hotkey_overlay": "f10",

    # ---------- Drawing overlay ----------
    "overlay_pen_width": 4,
    "overlay_color": "#ff3b30",

    "settings_version": 5,        # used to update old settings files - do not change
}

# Version 4: thumb taps click, thumb+middle right click, new touch measurement
# (so the old touch numbers no longer fit). Older settings files get these values once.
UPGRADE_TO_V4 = {"enable_click": True, "enable_fist_click": False, "enable_scroll": False,
                 "enable_right_click": False, "enable_palm_lock": False,
                 "pinch_on": DEFAULTS["pinch_on"], "pinch_off": DEFAULTS["pinch_off"]}
OLD_DEFAULT_BOXES = [{"x": 0.25, "y": 0.20, "w": 0.50, "h": 0.50},
                     {"x": 0.25, "y": 0.30, "w": 0.50, "h": 0.50}]

# Safe ranges, so a typo in settings.json cannot break the app.
LIMITS = {
    "camera_index": (0, 10), "camera_width": (160, 1920), "camera_height": (120, 1080),
    "target_fps": (5, 60), "max_hands": (1, 2), "model_complexity": (0, 1),
    "detection_confidence": (0.1, 0.95), "tracking_confidence": (0.1, 0.95),
    "monitor": (0, 16), "min_cutoff": (0.01, 10.0), "beta": (0.0, 1.0),
    "d_cutoff": (0.1, 10.0), "dead_zone_px": (0.0, 40.0), "stable_frames": (1, 15),
    "pinch_on": (0.05, 1.0), "pinch_off": (0.06, 1.5), "click_freeze_ms": (0, 2000),
    "middle_on": (0.05, 1.0), "middle_off": (0.06, 1.5), "hold_ms": (150, 1500),
    "click_lookback_ms": (0, 600), "tap_frames": (1, 8),
    "right_click_hold_s": (0.2, 5.0), "scroll_speed": (1, 300), "palm_hold_s": (0.2, 5.0),
    "lost_hand_grace_ms": (0, 2000), "preview_width": (160, 1280), "overlay_pen_width": (1, 40),
    # parts of active_box
    "x": (0.0, 0.95), "y": (-0.5, 0.95), "w": (0.05, 1.0), "h": (0.05, 1.0),
}
CHOICES = {
    "hand": ("right", "left", "any"),
    "tracker_backend": ("auto", "legacy", "tasks"),
    "palm_mode": ("toggle", "hold"),
    "cursor_point": ("palm", "index"),
    "back_action": ("scroll", "right_click", "off"),
}


# ---------------------------------------------------------------------------
# Folder helpers (work both as .py and as a PyInstaller .exe)
# ---------------------------------------------------------------------------
def app_dir():
    """Folder of HandMouse.exe, or of this .py file."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def bundle_dir():
    """Inside a --onefile exe, bundled files are unpacked to a temp folder (_MEIPASS)."""
    return getattr(sys, "_MEIPASS", app_dir())


def find_resource(relative_path):
    """Look for a file next to the app first, then inside the exe bundle."""
    for base in (app_dir(), bundle_dir()):
        path = os.path.join(base, relative_path)
        if os.path.exists(path):
            return path
    return None


def data_dir():
    """Writable folder for settings.json and the log file."""
    folder = app_dir()
    if os.access(folder, os.W_OK):
        return folder
    folder = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
    os.makedirs(folder, exist_ok=True)
    return folder


# ---------------------------------------------------------------------------
# Cleaning values
# ---------------------------------------------------------------------------
def _clean(key, default, value):
    """Return value if it has the right type/range, else the default."""
    if isinstance(default, dict):
        if not isinstance(value, dict):
            return copy.deepcopy(default)
        return {k: _clean(k, dv, value.get(k, dv)) for k, dv in default.items()}
    if isinstance(default, bool):
        return value if isinstance(value, bool) else default
    if isinstance(value, bool):          # True is an int in Python; do not accept it for numbers
        return default
    if isinstance(default, int):
        if not isinstance(value, (int, float)):
            return default
        value = int(round(value))
    elif isinstance(default, float):
        if not isinstance(value, (int, float)):
            return default
        value = float(value)
    elif isinstance(default, str):
        if not isinstance(value, str):
            return default
        if key in CHOICES and value.lower() not in CHOICES[key]:
            return default
        return value.lower() if key in CHOICES else value
    if key in LIMITS:
        lo, hi = LIMITS[key]
        value = max(lo, min(hi, value))
    return value


class Config:
    """A dict-like object: cfg["beta"], cfg.set("beta", 0.02), cfg.save()."""

    def __init__(self, data, path):
        self._data = data
        self.path = path
        self._lock = threading.Lock()

    @classmethod
    def load(cls, path=None):
        path = path or os.path.join(data_dir(), "settings.json")
        loaded = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if not isinstance(loaded, dict):
                    loaded = {}
            except (OSError, ValueError) as e:
                log.warning("settings.json could not be read (%s) - using defaults", e)
                loaded = {}
        data = {k: _clean(k, v, loaded.get(k, v)) for k, v in DEFAULTS.items()}
        upgraded = False
        old_version = loaded.get("settings_version", 1) if loaded else DEFAULTS["settings_version"]
        if old_version < 4:
            data.update(UPGRADE_TO_V4)           # new gesture scheme
            if data["active_box"] in OLD_DEFAULT_BOXES:     # your own box is kept
                data["active_box"] = copy.deepcopy(DEFAULTS["active_box"])
        if old_version < DEFAULTS["settings_version"]:
            data["settings_version"] = DEFAULTS["settings_version"]   # so it happens only once
            upgraded = True
            log.info("settings.json updated from version %s", old_version)
        cfg = cls(data, path)
        if upgraded or not os.path.exists(path):
            cfg.save()   # write the file so you can see and edit every setting
        return cfg

    def __getitem__(self, key):
        return self._data[key]

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        """Change one setting (type and range are checked)."""
        if key not in DEFAULTS:
            raise KeyError(key)
        with self._lock:
            self._data[key] = _clean(key, DEFAULTS[key], value)

    def set_box(self, **parts):
        """Change part of the active box, e.g. cfg.set_box(x=0.3, w=0.4)."""
        with self._lock:
            box = dict(self._data["active_box"])
            box.update(parts)
            self._data["active_box"] = _clean("active_box", DEFAULTS["active_box"], box)

    def reset_defaults(self):
        with self._lock:
            self._data = copy.deepcopy(DEFAULTS)

    def save(self):
        """Write settings.json safely (write to a temp file, then swap)."""
        with self._lock:
            text = json.dumps(self._data, indent=2)
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, self.path)
            return True
        except OSError as e:
            log.error("Could not save settings: %s", e)
            return False
