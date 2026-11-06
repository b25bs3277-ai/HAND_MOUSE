"""
ui.py
-----
The small control panel window and the Settings window (Tkinter comes with Python).

Every change in Settings works IMMEDIATELY (the engine reads the settings every
frame). "Save" writes them to settings.json so they are kept next time.
"""

import logging
import tkinter as tk
from tkinter import messagebox, ttk

from mouse_control import get_virtual_screen, list_monitors

log = logging.getLogger(__name__)

FONT = ("Segoe UI", 10)
SMALL = ("Segoe UI", 8)


class App:
    """The control panel. Runs in the main thread."""

    def __init__(self, cfg, engine):
        self.cfg = cfg
        self.engine = engine
        self.overlay = None
        self.settings = None
        self._closed = False

        self.root = tk.Tk()
        self.root.title("HandMouse")
        self.root.resizable(False, False)
        self.root.report_callback_exception = self._on_tk_error
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self.root.after(100, self._poll)

    def _build(self):
        frame = ttk.Frame(self.root, padding=10)
        frame.grid(sticky="nsew")
        self.state_var = tk.StringVar(value="Starting...")
        self.info_var = tk.StringVar(value="")
        self.msg_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.state_var, font=("Segoe UI", 14, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(frame, textvariable=self.info_var, font=FONT).grid(
            row=1, column=0, columnspan=2, sticky="w")
        ttk.Label(frame, textvariable=self.msg_var, foreground="#666666", wraplength=340,
                  font=SMALL).grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 6))

        def key(name):
            return self.cfg[name].upper()

        buttons = [
            (f"Start / Stop  ({key('hotkey_start_stop')})", self.engine.toggle_running),
            (f"Preview on / off  ({key('hotkey_preview')})", self.engine.toggle_preview),
            ("Calibrate active box", self.engine.start_calibration),
            ("Calibrate touch (do once)", self.engine.start_touch_calibration),
            ("Settings...", self.open_settings),
            (f"Drawing overlay  ({key('hotkey_overlay')})", self.engine.toggle_overlay),
            (f"Exit  ({key('hotkey_exit')})", self.quit),
        ]
        for i, (text, command) in enumerate(buttons):
            ttk.Button(frame, text=text, command=command, width=26).grid(
                row=3 + i // 2, column=i % 2, padx=3, pady=3)

        tips = ("Open hand = move (palm centre)      Index + middle = write (button held)\n"
                "Thumb taps index tip = click, 2 taps = double-click, touch + hold = drag / select\n"
                "Thumb + middle tip = right click\n"
                "Back of hand: hold it higher / lower = scroll up / down      "
                f"Pause: lower your hand or {key('hotkey_start_stop')}")
        ttk.Label(frame, text=tips, foreground="#444444", font=SMALL, justify="left").grid(
            row=7, column=0, columnspan=2, sticky="w", pady=(8, 0))

    # ---- refresh the panel ~7 times a second ----
    def _poll(self):
        if self._closed:
            return
        engine = self.engine
        if engine.exit_requested:
            self._close_all()
            return
        st = engine.status
        self.state_var.set(st["state"])
        self.info_var.set(f"Gesture: {st['gesture']}     Hand: {st['hand']}     "
                          f"{st['fps']:.0f} FPS     Tracker: {st['backend']}")
        self.msg_var.set(st["message"])
        if engine.error:
            text, engine.error = engine.error, None
            messagebox.showerror("HandMouse", text, parent=self.root)
        self._sync_overlay()
        self.root.after(150, self._poll)

    def _sync_overlay(self):
        want = self.engine.overlay_on
        if want and (self.overlay is None or not self.overlay.visible):
            try:
                if self.overlay is None:
                    from overlay import Overlay
                    self.overlay = Overlay(self.root, self.engine, self.cfg)
                self.overlay.show()
            except Exception as e:
                log.exception("Overlay could not open")
                self.engine.overlay_on = False
                messagebox.showerror("HandMouse", f"The drawing overlay could not open:\n{e}",
                                     parent=self.root)
        elif not want and self.overlay is not None and self.overlay.visible:
            self.overlay.hide()

    def open_settings(self):
        if self.settings is not None and self.settings.top.winfo_exists():
            self.settings.top.lift()
            return
        self.settings = SettingsWindow(self)

    def _on_tk_error(self, exc_type, exc, tb):
        log.error("UI error", exc_info=(exc_type, exc, tb))
        messagebox.showerror("HandMouse", f"Something went wrong:\n{exc}\n\nSee handmouse.log.")

    def quit(self):
        self.engine.request_exit()
        self._close_all()

    def _close_all(self):
        if self._closed:
            return
        self._closed = True
        self.cfg.save()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def run(self):
        self.root.mainloop()


class SettingsWindow:
    def __init__(self, app):
        self.app = app
        self.cfg = app.cfg
        self.engine = app.engine
        self.rows = {}
        self.box_vars = {}
        self.top = tk.Toplevel(app.root)
        self.top.title("HandMouse settings")
        self.top.resizable(False, False)
        self.top.protocol("WM_DELETE_WINDOW", self.close)

        notebook = ttk.Notebook(self.top)
        notebook.pack(fill="both", expand=True, padx=8, pady=8)
        self._camera_tab(self._tab(notebook, "Camera & hand"))
        self._screen_tab(self._tab(notebook, "Screen & active box"))
        self._smoothing_tab(self._tab(notebook, "Smoothing"))
        self._gesture_tab(self._tab(notebook, "Gestures"))
        self._app_tab(self._tab(notebook, "App & keys"))

        bar = ttk.Frame(self.top)
        bar.pack(fill="x", padx=8, pady=(0, 8))
        self.saved_var = tk.StringVar(value="Changes work immediately. Save keeps them.")
        ttk.Label(bar, textvariable=self.saved_var, font=SMALL).pack(side="left")
        ttk.Button(bar, text="Close", command=self.close).pack(side="right")
        ttk.Button(bar, text="Reset to defaults", command=self.reset).pack(side="right", padx=4)
        ttk.Button(bar, text="Save", command=self.save).pack(side="right")
        self.top.after(500, self._refresh_box)

    # ---------------- small widget helpers ----------------
    def _tab(self, notebook, title):
        frame = ttk.Frame(notebook, padding=10)
        notebook.add(frame, text=title)
        return frame

    def _next_row(self, parent):
        row = self.rows.get(parent, 0)
        self.rows[parent] = row + 1
        return row

    def _help(self, parent, row, text):
        if text:
            ttk.Label(parent, text=text, foreground="#777777", font=SMALL, wraplength=250,
                      justify="left").grid(row=row, column=2, sticky="w", padx=(10, 0))

    def slider(self, parent, text, key, low, high, step, help_text="", box_part=None):
        row = self._next_row(parent)
        ttk.Label(parent, text=text).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=2)
        value = self.cfg["active_box"][box_part] if box_part else self.cfg[key]
        var = tk.DoubleVar(value=value)

        def changed(_value=None):
            if box_part:
                self.cfg.set_box(**{box_part: var.get()})
            else:
                self.cfg.set(key, var.get())

        scale = tk.Scale(parent, from_=low, to=high, resolution=step, orient="horizontal",
                         length=230, variable=var, command=changed)
        scale.grid(row=row, column=1, sticky="we")
        self._help(parent, row, help_text)
        if box_part:
            self.box_vars[box_part] = var
        return scale

    def check(self, parent, text, key, help_text="", on_change=None):
        row = self._next_row(parent)
        var = tk.BooleanVar(value=self.cfg[key])

        def changed():
            self.cfg.set(key, var.get())
            if on_change:
                on_change()

        ttk.Checkbutton(parent, text=text, variable=var, command=changed).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=2)
        self._help(parent, row, help_text)

    def choice(self, parent, text, key, options, help_text=""):
        """options = list of (value, label)."""
        row = self._next_row(parent)
        ttk.Label(parent, text=text).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=2)
        values = [v for v, _ in options]
        labels = [label for _, label in options]
        current = self.cfg[key]
        var = tk.StringVar(value=labels[values.index(current)] if current in values else labels[0])
        combo = ttk.Combobox(parent, values=labels, textvariable=var, state="readonly", width=32)
        combo.grid(row=row, column=1, sticky="w")

        def changed(_event=None):
            self.cfg.set(key, values[labels.index(var.get())])

        combo.bind("<<ComboboxSelected>>", changed)
        self._help(parent, row, help_text)

    def entry(self, parent, text, key, help_text=""):
        row = self._next_row(parent)
        ttk.Label(parent, text=text).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=2)
        var = tk.StringVar(value=self.cfg[key])
        box = ttk.Entry(parent, textvariable=var, width=20)
        box.grid(row=row, column=1, sticky="w")

        def changed(_event=None):
            self.cfg.set(key, var.get().strip().lower())

        box.bind("<FocusOut>", changed)
        box.bind("<Return>", changed)
        self._help(parent, row, help_text)

    # ---------------- tabs ----------------
    def _camera_tab(self, tab):
        self.slider(tab, "Camera number", "camera_index", 0, 5, 1,
                    "0 = built-in webcam. Try 1 if you use a USB camera.")
        self.check(tab, "Low-resolution mode (less CPU)", "low_res_mode",
                   "320x240 + fastest model. Use on slow laptops.")
        self.check(tab, "Mirror image (selfie view)", "mirror")
        self.check(tab, "Low-light boost", "low_light_boost", "Brightens dark rooms automatically.")
        self.slider(tab, "Max frames per second", "target_fps", 10, 60, 1, "30 is smooth and light.")
        self.choice(tab, "Hand to follow", "hand",
                    [("right", "Right hand"), ("left", "Left hand"), ("any", "Any hand")])
        self.check(tab, "Swap left/right labels", "swap_handedness",
                   "Turn on if the preview says 'Left' for your right hand.")
        self.slider(tab, "Hands to look for", "max_hands", 1, 2, 1,
                    "2 = finds the right hand even when both are visible (more CPU).")
        self.choice(tab, "Hand model", "tracker_backend",
                    [("auto", "Automatic"), ("legacy", "Built-in model (legacy)"),
                     ("tasks", "hand_landmarker.task (Tasks API)")])
        self.choice(tab, "Model accuracy (built-in model)", "model_complexity",
                    [(1, "Accurate (recommended)"), (0, "Fast")])
        self.slider(tab, "Detection confidence", "detection_confidence", 0.2, 0.9, 0.05,
                    "Lower (0.4-0.5) helps in bad light.")
        self.slider(tab, "Tracking confidence", "tracking_confidence", 0.2, 0.9, 0.05)

    def _screen_tab(self, tab):
        vx, vy, vw, vh = get_virtual_screen()
        options = [(0, f"All screens together ({vw}x{vh})")]
        for i, m in enumerate(list_monitors(), start=1):
            name = "Main screen" if m.primary else f"Screen {i}"
            options.append((i, f"{name} ({m.width}x{m.height})"))
        self.choice(tab, "Screen to control", "monitor", options,
                    "Pick the screen you write on (e.g. the projector).")
        self.choice(tab, "Cursor follows", "cursor_point",
                    [("palm", "Palm centre (steadiest)"), ("index", "Index fingertip")],
                    "Recalibrate the box after changing this.")
        self.check(tab, "Keep box the same shape as the screen", "lock_aspect",
                   "Recommended: circles stay round. The height then follows the width.",
                   on_change=self._update_height_state)
        self.slider(tab, "Box left", None, 0.0, 0.9, 0.01, "Green box in the preview.", box_part="x")
        self.slider(tab, "Box top", None, -0.3, 0.9, 0.01, box_part="y")
        self.slider(tab, "Box width", None, 0.1, 1.0, 0.01,
                    "Smaller = less arm movement, but more sensitive.", box_part="w")
        self.height_scale = self.slider(tab, "Box height", None, 0.1, 1.0, 0.01, box_part="h")
        self._update_height_state()
        row = self._next_row(tab)
        ttk.Button(tab, text="Calibrate with my hand...", command=self.engine.start_calibration).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self._help(tab, row, "3 s countdown, then trace the corners of a comfortable area for 5 s.")

    def _update_height_state(self):
        self.height_scale.configure(state="disabled" if self.cfg["lock_aspect"] else "normal")

    def _smoothing_tab(self, tab):
        self.slider(tab, "Slow-speed smoothing (min cutoff)", "min_cutoff", 0.1, 5.0, 0.05,
                    "LOWER = steadier handwriting, a bit more lag. Try 0.5-1.2.")
        self.slider(tab, "Speed response (beta)", "beta", 0.0, 0.1, 0.001,
                    "HIGHER = less lag on fast moves. Try 0.005-0.03.")
        self.slider(tab, "Dead zone (pixels)", "dead_zone_px", 0.0, 15.0, 0.5,
                    "Shakes smaller than this are ignored. 2-5 for writing.")
        self.slider(tab, "Speed smoothing (d cutoff)", "d_cutoff", 0.5, 5.0, 0.1,
                    "Advanced. Leave at 1.0.")

    def _gesture_tab(self, tab):
        self.check(tab, "Write: index + middle holds the left button", "enable_draw")
        self.check(tab, "Left click: thumb taps index tip", "enable_click",
                   "2 quick taps = double-click. Touch + hold = button held (drag / select).")
        self.check(tab, "Right click: thumb touches middle tip", "enable_middle_right_click")
        self.choice(tab, "Back of hand does", "back_action",
                    [("scroll", "Scroll (hold higher / lower)"), ("right_click", "Right click"),
                     ("off", "Nothing (just moves)")])
        self.check(tab, "Scroll: three fingers (old)", "enable_scroll")
        self.check(tab, "Left click: fist (old)", "enable_fist_click")
        self.check(tab, "Right click: fist held (old)", "enable_right_click",
                   "Only works when fist click is off.")
        self.check(tab, "Lock: open palm (old)", "enable_palm_lock",
                   "If on, the open palm locks instead of moving the cursor.")
        self.slider(tab, "Stable frames", "stable_frames", 1, 8, 1,
                    "Frames a gesture must last. 3-5. Higher = fewer accidents, slower.")
        self.slider(tab, "Hold to drag after (ms)", "hold_ms", 200, 1000, 50,
                    "Touch held longer than this = left button stays down.")
        self.slider(tab, "Tap frames", "tap_frames", 1, 6, 1,
                    "Frames a thumb tap must last. Lower = quick taps count, higher = fewer accidents.")
        row = self._next_row(tab)
        ttk.Button(tab, text="Calibrate touch...", command=self.engine.start_touch_calibration).grid(
            row=row, column=0, sticky="w", pady=(6, 2))
        self._help(tab, row, "Best way: sets the 4 numbers below for your hand (10 s).")
        self.slider(tab, "Index touch starts below", "pinch_on", 0.05, 0.60, 0.01,
                    "Watch 'index' in the preview while you touch.")
        self.slider(tab, "Index touch ends above", "pinch_off", 0.10, 0.90, 0.01,
                    "Must be bigger than 'starts below'.")
        self.slider(tab, "Middle touch starts below", "middle_on", 0.05, 0.60, 0.01,
                    "Watch 'middle' in the preview while you touch.")
        self.slider(tab, "Middle touch ends above", "middle_off", 0.10, 0.90, 0.01)
        self.slider(tab, "Click aim look-back (ms)", "click_lookback_ms", 0, 500, 10,
                    "Clicks where the cursor was this long before the pinch.")
        self.slider(tab, "Freeze after click (ms)", "click_freeze_ms", 0, 600, 10,
                    "Keeps the cursor still so a double pinch = double-click.")
        self.slider(tab, "Right-click fist hold (s)", "right_click_hold_s", 0.3, 3.0, 0.1)
        self.slider(tab, "Scroll speed", "scroll_speed", 5, 100, 1,
                    "Back of hand: further from the start line = faster.")
        self.check(tab, "Invert scroll direction", "scroll_invert")
        self.check(tab, "Smooth scrolling", "smooth_scroll",
                   "Turn off if an old app scrolls too fast (then: whole wheel steps).")
        self.choice(tab, "Open palm does", "palm_mode",
                    [("toggle", "Lock / unlock (hold palm)"), ("hold", "Freeze only while shown")])
        self.slider(tab, "Palm hold time (s)", "palm_hold_s", 0.3, 2.0, 0.1)
        self.slider(tab, "Lost-hand grace (ms)", "lost_hand_grace_ms", 0, 500, 10,
                    "Button is released if the hand is gone longer than this.")

    def _app_tab(self, tab):
        self.check(tab, "Start tracking when the app opens", "autostart")
        self.check(tab, "Preview always on top", "preview_topmost", "Applies when the preview reopens (F9).")
        self.slider(tab, "Preview width", "preview_width", 200, 640, 10)
        self.check(tab, "Turn camera off when stopped", "release_camera_when_stopped")
        self.slider(tab, "Overlay pen width", "overlay_pen_width", 1, 20, 1)
        self.entry(tab, "Start / stop key", "hotkey_start_stop", "Restart the app after changing keys.")
        self.entry(tab, "Preview key", "hotkey_preview", "Examples: f9, ctrl+shift+p")
        self.entry(tab, "Emergency exit key", "hotkey_exit",
                   "Esc also ends PowerPoint slide shows. Use e.g. ctrl+shift+q to avoid that.")
        self.entry(tab, "Overlay key", "hotkey_overlay")

    # ---------------- buttons ----------------
    def _refresh_box(self):
        """After a calibration, show the new box numbers."""
        if not self.top.winfo_exists():
            return
        box = self.cfg["active_box"]
        for part, var in self.box_vars.items():
            if abs(var.get() - box[part]) > 0.004:
                var.set(round(box[part], 2))
        self.top.after(500, self._refresh_box)

    def save(self):
        ok = self.cfg.save()
        self.saved_var.set("Saved to settings.json" if ok else "Could not save - see handmouse.log")

    def reset(self):
        if messagebox.askyesno("HandMouse", "Put every setting back to its default?", parent=self.top):
            self.cfg.reset_defaults()
            self.cfg.save()
            self.top.destroy()
            self.app.settings = None
            self.app.open_settings()

    def close(self):
        self.cfg.save()
        self.top.destroy()
        self.app.settings = None
