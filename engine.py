"""
engine.py
---------
The heart of the app. It runs in a background thread:

    camera frame -> hand landmarks -> gesture -> smoothing -> mouse

Other parts of the app (hotkeys, the control panel, the settings window)
only flip simple switches on this object (running, preview_on, overlay_on,
exit_requested...). The loop checks those switches every frame.

Handwriting tricks used here:
  * Lossless stroke start: while "index + middle" is still being confirmed
    (debounce), we remember the fingertip path. When it is confirmed we press
    the button at the FIRST point and replay the path, so no ink is lost.
  * Clean stroke end: as soon as your fingers start closing, the cursor
    freezes, so the line does not get a little tail.
  * Steady clicks: the click lands where the cursor was just BEFORE your
    thumb and index started closing in.
"""

import logging
import math
import queue
import time
from collections import deque

import cv2
import numpy as np

from camera import Camera
from gestures import (BACK, DRAW, FIST, MIDDLE, NONE, PALM, PINCH, POINT, SCROLL,
                      Debouncer, GestureClassifier, touch_gaps)
from mouse_control import Mouse, list_monitors
from smoothing import PointFilter
from tracker import HandTracker, TrackerError

log = logging.getLogger(__name__)

PREVIEW_WINDOW = "HandMouse preview"

# Lines between landmarks, used to draw the hand skeleton in the preview
HAND_LINKS = [(0, 1), (1, 2), (2, 3), (3, 4), (0, 5), (5, 6), (6, 7), (7, 8),
              (5, 9), (9, 10), (10, 11), (11, 12), (9, 13), (13, 14), (14, 15),
              (15, 16), (13, 17), (17, 18), (18, 19), (19, 20), (0, 17)]

GESTURE_TEXT = {NONE: "MOVE", POINT: "MOVE", DRAW: "WRITE", PINCH: "TOUCH",
                SCROLL: "SCROLL", PALM: "OPEN PALM", FIST: "FIST", BACK: "BACK OF HAND",
                MIDDLE: "RIGHT CLICK"}
CLICK_GESTURES = (PINCH, MIDDLE)

TOUCH_HOLD_S = 0.6        # thumb near a fingertip -> cursor holds still (max this long)
SCROLL_DEAD_BAND = 0.035  # back-of-hand scroll: hand within this of the start = no scroll
SCROLL_MAX_NOTCHES = 25   # back-of-hand scroll: fastest speed (wheel notches per second)
SCROLL_EASE_S = 0.15      # back-of-hand scroll: speed changes smoothly over this time
GLIDE_S = 0.12            # after the cursor held still, it glides (not jumps) back to your hand

PALM_POINTS = (0, 5, 9, 13, 17)   # wrist + 4 knuckles: their average = palm centre
DOUBLE_CLICK_WINDOW_S = 1.0       # a 2nd click this soon after the 1st ...
DOUBLE_CLICK_SNAP_PX = 50         # ... and this close lands on the SAME pixel (= double-click)


class Engine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.mouse = Mouse()

        # ---- switches other threads may flip ----
        self.running = bool(cfg["autostart"])
        self.preview_on = bool(cfg["preview_visible"])
        self.overlay_on = False
        self.exit_requested = False
        self._calibration_requested = False
        self._touch_calibration_requested = False
        self.calib_kind = "box"

        # ---- read by the control panel ----
        self.status = {"state": "STARTING", "gesture": "-", "fps": 0.0,
                       "hand": "-", "message": "", "backend": "-"}
        self.error = None                     # a message the UI should pop up
        self.overlay_queue = queue.Queue()    # drawing events for the overlay window
        self.screen_rect = (0, 0, 1920, 1080)

        # ---- internal parts ----
        self.camera = None
        self._camera_params = None
        self._camera_retry_at = 0.0
        self.tracker = None
        self._tracker_params = None
        self.classifier = GestureClassifier()
        self.debouncer = Debouncer()
        self.filter = PointFilter()
        self.monitors = list_monitors()
        self._monitor_check_at = 0.0
        self._gamma_lut = np.array([((i / 255.0) ** 0.6) * 255 for i in range(256)], np.uint8)
        self._brightness = 255.0
        self._frame_count = 0
        self.dark = False
        self.fps = 0.0
        self._last_frame_time = None
        self._was_active = False
        self.preview_created = False
        self._idle_shown_at = 0.0
        self.calibrating = False
        self.frame_shape = (480, 640, 3)
        self.track_label, self.track_pos, self.track_time = None, (0.0, 0.0), 0.0
        self.flash_text, self.flash_until = "", 0.0
        self._reset_hand_state()
        self.locked = False

    # =====================================================================
    # Called from OTHER threads (hotkeys / buttons). Keep these tiny.
    # =====================================================================
    def toggle_running(self):
        self.running = not self.running

    def toggle_preview(self):
        self.preview_on = not self.preview_on

    def toggle_overlay(self):
        self.overlay_on = not self.overlay_on

    def start_calibration(self):
        self._calibration_requested = True

    def start_touch_calibration(self):
        self._touch_calibration_requested = True

    def request_exit(self):
        self.exit_requested = True

    # =====================================================================
    # Main loop
    # =====================================================================
    def run(self):
        log.info("Engine thread started")
        last_id = 0
        last_processed = 0.0
        try:
            while not self.exit_requested:
                if self._calibration_requested:
                    self._begin_calibration()
                if self._touch_calibration_requested:
                    self._begin_touch_calibration()

                if not (self.running or self.calibrating):
                    self._idle()
                    time.sleep(0.05)
                    continue

                if not self._was_active:          # we just (re)started
                    self._was_active = True
                    self.locked = False
                    self._reset_hand_state()

                if not self._ensure_tracker() or not self._ensure_camera():
                    self._release_everything()
                    self._show_message_preview(self.status["message"])
                    time.sleep(0.1)
                    continue

                frame, last_id = self.camera.read_latest(last_id, timeout=1.0)
                if frame is None:
                    if self.camera.broken:
                        self.status["message"] = "Camera lost - reconnecting..."
                        self.camera.close()
                        self.camera = None
                    self._hand_missing(time.monotonic())
                    continue

                now = time.monotonic()
                target_fps = self.cfg["target_fps"]
                if now - last_processed < 0.9 / target_fps:
                    continue                     # camera is faster than we need
                last_processed = now
                self._process(frame, now)
        except Exception as e:                   # never die with the button held down
            log.exception("Engine crashed")
            self.error = f"The tracking loop stopped because of an error:\n{e}\n\nSee handmouse.log."
        finally:
            self._release_everything()
            self.mouse.release_all()
            if self.camera:
                self.camera.close()
            if self.tracker:
                self.tracker.close()
            self._close_preview()
            log.info("Engine thread finished")

    # =====================================================================
    # Starting / stopping parts
    # =====================================================================
    def _ensure_camera(self):
        cfg = self.cfg
        if cfg["low_res_mode"]:
            width, height = 320, 240
        else:
            width, height = cfg["camera_width"], cfg["camera_height"]
        params = (cfg["camera_index"], width, height)
        if self.camera and self._camera_params == params and not self.camera.broken:
            return True
        if self.camera:                          # settings changed -> reopen
            self.camera.close()
            self.camera = None
        now = time.monotonic()
        if now < self._camera_retry_at:
            return False
        self.status["message"] = f"Opening camera {params[0]}..."
        cam = Camera(params[0], width, height, fps=30)
        if cam.open():
            self.camera = cam
            self._camera_params = params
            self.status["message"] = cam.description
            return True
        self._camera_retry_at = now + 2.0
        self.status["message"] = (f"Camera {params[0]} not available. Close Zoom/Teams/Camera app, "
                                  "check Windows camera privacy, or change the camera number in Settings.")
        return False

    def _ensure_tracker(self):
        cfg = self.cfg
        complexity = 0 if cfg["low_res_mode"] else cfg["model_complexity"]
        params = (cfg["tracker_backend"], cfg["max_hands"], complexity,
                  cfg["detection_confidence"], cfg["tracking_confidence"])
        if self.tracker and self._tracker_params == params:
            return True
        if self.tracker:
            self.tracker.close()
            self.tracker = None
        self.status["message"] = "Loading hand model..."
        try:
            self.tracker = HandTracker(*params)
        except TrackerError as e:
            self.error = str(e)
            self.running = False
            self.calibrating = False
            self.status["message"] = "Hand tracking could not start"
            return False
        self._tracker_params = params
        self.status["backend"] = self.tracker.backend
        return True

    def _idle(self):
        """Stopped (F8): let go of everything, optionally turn the camera off."""
        if self._was_active:
            self._was_active = False
            self._release_everything()
            self.locked = False
        if self.camera and self.cfg["release_camera_when_stopped"]:
            self.camera.close()
            self.camera = None
        self.status.update(state="STOPPED", gesture="-", fps=0.0, hand="-")
        hotkey = self.cfg["hotkey_start_stop"].upper()
        self._show_message_preview(f"STOPPED - press {hotkey} to start")

    def _reset_hand_state(self):
        """Forget everything about the current hand (new hand, restart, etc.)."""
        self.debouncer.reset(NONE)
        self.classifier.reset()
        self.filter.reset()
        self.raw_gesture = NONE
        self.info = None
        self.button_down = False
        self.pen_target = None          # "mouse" or "overlay" while writing
        self.pending_path = []          # fingertip path while DRAW is being confirmed
        self.cursor = None              # last position we sent to the mouse
        self.freeze_until = 0.0
        self.last_seen = None
        self.hand_gone = True
        self.approach_time = None       # when the thumb started closing in on the index
        self.turn_time = None           # when the hand started turning its back to the camera
        self.last_click = None          # (time, x, y) of the last left click
        self.history = deque(maxlen=45) # recent (time, x, y) cursor positions (~1.5 s)
        self.gap_history = deque(maxlen=20)  # recent (time, smallest thumb-to-fingertip gap)
        self.candidate_since = 0.0      # when the gesture now being confirmed first appeared
        self.fist_since = None
        self.fist_done = False
        self.palm_since = None
        self.palm_latch = False         # palm must be released before it can toggle again
        self.unlock_since = None
        self.non_palm_frames = 0
        self.scroll_y = None
        self.scroll_acc = 0.0
        self.scroll_ref = None          # back-of-hand scroll: where the hand was when it started
        self.scroll_t = 0.0
        self.scroll_rate = 0.0          # notches per second right now (+ up, - down)
        self.near_since = None          # when the thumb came close to a fingertip
        self.touch_hold = False         # True = cursor holds still for a click
        self.far_since = None           # when the thumb moved clearly away again
        self.tap_down_time = None       # when the thumb touched the index tip
        self.tap_aim = None             # where that touch was aimed
        self.dragging = False           # touch held -> left button is down (drag / select)
        self.glide = (0.0, 0.0)         # cursor offset that fades out (no jumps after a pause)
        self.glide_t = 0.0
        self.paused = False             # True = the cursor held still last frame
        self.scroll_y_smooth = None

    def _release_everything(self):
        if self.button_down:
            self._pen_up()
        self.mouse.release_all()
        self.dragging = False
        self.tap_down_time = self.tap_aim = None

    # =====================================================================
    # One frame
    # =====================================================================
    def _process(self, frame, now):
        cfg = self.cfg
        if cfg["mirror"]:
            frame = cv2.flip(frame, 1)
        frame = self._maybe_brighten(frame)

        try:
            hands = self.tracker.process(frame, now * 1000.0, mirrored=cfg["mirror"])
        except Exception:
            log.exception("Hand tracking failed on one frame")
            hands = []
        hand = self._pick_hand(hands, now)

        self._update_screen(now)
        self.frame_shape = frame.shape
        box = self._effective_box(frame.shape)

        if self.calibrating:
            self._calibration_step(hand, now)
        elif hand is not None:
            self._hand_seen(hand, box, now)
        else:
            self._hand_missing(now)
            if hands:
                self.status["message"] = (f"Ignoring {hands[0].label} hand (tracking: "
                                          f"{cfg['hand']}). Wrong? Turn on 'swap left/right'.")

        # frames per second (smoothed)
        if self._last_frame_time is not None:
            dt = now - self._last_frame_time
            if dt > 0:
                self.fps = 0.9 * self.fps + 0.1 * (1.0 / dt) if self.fps else 1.0 / dt
        self._last_frame_time = now
        self._update_status(hand)

        if self.preview_on or self.calibrating:
            self._draw_preview(frame, hand, box, now)
        else:
            self._close_preview()

    def _maybe_brighten(self, frame):
        """In a dark room, brighten the image so MediaPipe can still see the hand."""
        if not self.cfg["low_light_boost"]:
            self.dark = False
            return frame
        self._frame_count += 1
        if self._frame_count % 15 == 1:          # measuring every frame is not needed
            small = cv2.resize(frame, (64, 48), interpolation=cv2.INTER_AREA)
            self._brightness = float(small.mean())
        self.dark = self._brightness < 80
        return cv2.LUT(frame, self._gamma_lut) if self.dark else frame

    def _pick_hand(self, hands, now):
        want = self.cfg["hand"]
        best = None
        for h in hands:
            if self.cfg["swap_handedness"]:
                h.label = "Left" if h.label == "Right" else "Right"
            # Same hand as a moment ago (same place)? Keep its label. MediaPipe can
            # mix up left/right when you turn the BACK of your hand to the camera.
            if (self.track_label and now - self.track_time < 0.5
                    and math.dist(h.points[0][:2], self.track_pos) < 0.15):
                h.label = self.track_label
            if want == "any" or h.label.lower() == want:
                if best is None or h.score > best.score:
                    best = h
        if best is not None:
            self.track_label, self.track_pos, self.track_time = best.label, best.points[0][:2], now
        return best

    # ---------------------------------------------------------------------
    # Screen + active box
    # ---------------------------------------------------------------------
    def _update_screen(self, now):
        if now - self._monitor_check_at > 2.0:   # notice projectors being plugged in
            self._monitor_check_at = now
            self.monitors = list_monitors()
            self.mouse.refresh_screen()
        index = self.cfg["monitor"]
        if index == 0:
            m = self.mouse
            self.screen_rect = (m.vx, m.vy, m.vw, m.vh)
        else:
            mon = self.monitors[index - 1] if index <= len(self.monitors) else self.monitors[0]
            self.screen_rect = (mon.left, mon.top, mon.width, mon.height)

    def _effective_box(self, frame_shape):
        """The active box (x, y, w, h in 0..1). With lock_aspect its height is set
        from its width so that the box has the same shape as the screen."""
        b = self.cfg["active_box"]
        x, y, w, h = b["x"], b["y"], b["w"], b["h"]
        if self.cfg["lock_aspect"]:
            frame_h, frame_w = frame_shape[:2]
            _, _, screen_w, screen_h = self.screen_rect
            center_y = y + h / 2
            h = w * (frame_w / frame_h) * (screen_h / screen_w)
            y = center_y - h / 2
        return x, y, max(w, 0.05), max(h, 0.05)

    def _anchor(self, hand):
        """The point of the hand that moves the cursor (0..1 in the camera image)."""
        if self.cfg["cursor_point"] == "index":
            return hand.points[8][0], hand.points[8][1]
        xs = [hand.points[i][0] for i in PALM_POINTS]
        ys = [hand.points[i][1] for i in PALM_POINTS]
        return sum(xs) / len(xs), sum(ys) / len(ys)   # palm centre: barely moves when you make a fist

    def _moves_cursor(self, gesture):
        """Gestures during which moving the hand moves the cursor."""
        if gesture == PALM:
            return not self.cfg["enable_palm_lock"]   # open palm moves (unless it is the lock)
        if gesture == SCROLL:
            return not self.cfg["enable_scroll"]      # three fingers only move if they do not scroll
        if gesture == BACK:
            return self.cfg["back_action"] == "off"
        if gesture == PINCH:
            return self.dragging                       # touch held = drag: the cursor follows
        return gesture in (POINT, DRAW, NONE)

    def _to_screen(self, nx, ny, box):
        bx, by, bw, bh = box
        u = min(max((nx - bx) / bw, 0.0), 1.0)
        v = min(max((ny - by) / bh, 0.0), 1.0)
        left, top, width, height = self.screen_rect
        return left + u * (width - 1), top + v * (height - 1)

    # ---------------------------------------------------------------------
    # Hand present
    # ---------------------------------------------------------------------
    def _hand_seen(self, hand, box, now):
        cfg = self.cfg
        raw, info = self.classifier.classify(
            hand, cfg["pinch_on"], cfg["pinch_off"], allow_pinch=cfg["enable_click"],
            mirrored=cfg["mirror"], allow_middle=cfg["enable_middle_right_click"],
            middle_on=cfg["middle_on"], middle_off=cfg["middle_off"])
        self.raw_gesture, self.info = raw, info
        self.last_seen = now
        self.gap_history.append((now, min(info["index_gap"], info["middle_gap"])))
        if self.hand_gone:                       # hand just came back: start fresh
            self.hand_gone = False
            self.filter.reset()

        # Palm centre (or fingertip) -> screen -> smoothing
        ax, ay = self._anchor(hand)
        sx, sy = self._to_screen(ax, ay, box)
        self.filter.set_params(cfg["min_cutoff"], cfg["beta"], cfg["d_cutoff"], cfg["dead_zone_px"])
        fx, fy = self.filter.update(sx, sy, now)
        if self.cursor is None:
            self.cursor = (fx, fy)

        if self.locked:
            self._locked_frame(raw, now)
            return

        old = self.debouncer.stable
        old_candidate = self.debouncer.candidate
        needed = cfg["stable_frames"]
        if raw in CLICK_GESTURES:
            needed = min(needed, cfg["tap_frames"])   # taps are short: count them sooner
        if old in (FIST, BACK) + CLICK_GESTURES and not self.dragging:
            needed = min(needed, 2)      # letting go counts fast -> ready for the 2nd tap

        # Thumb close to a fingertip? Hold the cursor still, so the click lands
        # exactly where you aimed (at most TOUCH_HOLD_S, then it moves again).
        # It holds only ONCE per approach: the thumb must move clearly away before
        # it can hold again, so a thumb resting near a finger never makes the cursor stutter.
        g_i = info["index_gap"] if cfg["enable_click"] else 9.0
        g_m = info["middle_gap"] if cfg["enable_middle_right_click"] else 9.0
        close = (g_i < (cfg["pinch_on"] + cfg["pinch_off"]) / 2
                 or g_m < (cfg["middle_on"] + cfg["middle_off"]) / 2)
        far = g_i > cfg["pinch_off"] + 0.08 and g_m > cfg["middle_off"] + 0.08
        if self.near_since is None:
            if close:
                self.near_since = now
        elif far:                                # away for 0.25 s -> ready to hold again
            if self.far_since is None:
                self.far_since = now
            elif now - self.far_since > 0.25:
                self.near_since = self.far_since = None
        else:
            self.far_since = None
        self.touch_hold = self.near_since is not None and now - self.near_since < TOUCH_HOLD_S
        changed = self.debouncer.update(raw, needed)
        stable, candidate = self.debouncer.stable, self.debouncer.candidate
        if (candidate is not None and candidate != old_candidate) or (changed and old_candidate != stable):
            self.candidate_since = now           # a new gesture started appearing

        # Remember the fingertip path while DRAW waits to be confirmed
        path = None
        if changed and stable == DRAW:
            path = self.pending_path + [(fx, fy)]
            self.pending_path = []
        elif candidate == DRAW:
            if old_candidate != DRAW:
                self.pending_path = []
            self.pending_path.append((fx, fy))
        else:
            self.pending_path = []

        if changed:
            self._on_gesture_change(old, stable, path, hand, now)

        self._track_pinch_approach(info, stable, now)
        self._continuous_actions(stable, candidate, fx, fy, hand, now)
        self.history.append((now, self.cursor[0], self.cursor[1]))

    def _on_gesture_change(self, old, new, path, hand, now):
        cfg = self.cfg
        # ---- leaving the old gesture ----
        if old == DRAW and self.button_down:
            self._pen_up()
        if old == PINCH:
            self._finish_touch(now)              # short touch = click, held touch = end of drag
        if old in (PINCH, MIDDLE, FIST, BACK):   # keep still a moment after a click
            self.freeze_until = now + cfg["click_freeze_ms"] / 1000.0
        if old == PALM:
            self.palm_latch = False
        # ---- entering the new gesture ----
        if new == DRAW and cfg["enable_draw"]:
            self._pen_down(path)
        elif new in CLICK_GESTURES:
            # Click where the cursor was when the thumb STARTED moving toward the
            # fingertip (the cursor also holds still once the thumb is close).
            start = self._tap_start(now)
            lookback = 0.08 if cfg["cursor_point"] == "palm" else None
            if new == PINCH:
                # Do not click yet: a short touch clicks when you LET GO,
                # a long touch holds the button down (see _continuous_actions).
                self.tap_down_time = now
                self.tap_aim = self._aim_point(now, start, lookback)
                self.cursor = self.tap_aim
            elif cfg["enable_middle_right_click"]:
                self._right_click(now, start, lookback)
        elif new == SCROLL:
            self.scroll_y = hand.points[9][1]
            self.scroll_acc = 0.0
        elif new == BACK and cfg["back_action"] == "scroll":
            self.scroll_ref = self._anchor(hand)[1]   # scrolling starts from here
            self.scroll_y_smooth = self.scroll_ref
            self.scroll_t, self.scroll_acc, self.scroll_rate = now, 0.0, 0.0
        elif new == FIST:
            self.fist_since, self.fist_done = now, False
            if cfg["enable_fist_click"]:
                # every fist = one left click. The palm centre hardly moves when you
                # close your hand, so only look back a tiny bit (80 ms).
                self._left_click(now, lookback_s=0.08 if cfg["cursor_point"] == "palm" else None)
                self.fist_done = True           # (so the old fist-hold right click cannot fire too)
        elif new == BACK and cfg["back_action"] == "right_click":
            self._right_click(now, self.turn_time)
        elif new == PALM:
            self.palm_since = now

    def _continuous_actions(self, stable, candidate, fx, fy, hand, now):
        cfg = self.cfg
        # Touch HELD longer than hold_ms -> press the left button and keep it down (drag / select)
        if (stable == PINCH and not self.dragging and self.tap_down_time is not None
                and now - self.tap_down_time >= cfg["hold_ms"] / 1000.0):
            x, y = self.tap_aim
            self.mouse.left_down(x, y)
            self.cursor = (x, y)
            self.dragging = True
            self._flash("HOLD - DRAG", now)
        if self._moves_cursor(stable):
            if stable in (DRAW, PINCH):
                freeze = candidate is not None           # fingers opening -> no tail on the line
            else:                                        # a click gesture is starting -> hold still
                freeze = ((candidate is not None and candidate != DRAW
                           and not self._moves_cursor(candidate))
                          or self.touch_hold)           # thumb close to a fingertip
            if freeze or now < self.freeze_until:
                self.paused = True
            else:
                self._glide_to(fx, fy, now)
            return
        self.paused = True                       # this gesture does not move the cursor
        if stable == SCROLL and cfg["enable_scroll"]:
            self._scroll(hand)                   # move the hand up / down
        elif stable == BACK and cfg["back_action"] == "scroll":
            self._scroll_back_of_hand(hand, now)
        elif stable == FIST and cfg["enable_right_click"] and not cfg["enable_fist_click"]:
            if not self.fist_done and now - self.fist_since >= cfg["right_click_hold_s"]:
                self.fist_done = True
                self.mouse.right_click(*self.cursor)
                self._flash("RIGHT CLICK", now)
        elif stable == PALM and cfg["enable_palm_lock"] and cfg["palm_mode"] == "toggle":
            if not self.palm_latch and now - self.palm_since >= cfg["palm_hold_s"]:
                self._lock(now)

    # ---------------------------------------------------------------------
    # Mouse actions
    # ---------------------------------------------------------------------
    def _glide_to(self, fx, fy, now):
        """Move to the hand position. If the cursor was holding still (after a click,
        while the thumb came in...), slide back to the hand smoothly instead of jumping."""
        if self.paused:
            self.paused = False
            self.glide = (self.cursor[0] - fx, self.cursor[1] - fy)
        else:
            k = math.exp(-max(0.0, now - self.glide_t) / GLIDE_S)
            self.glide = (self.glide[0] * k, self.glide[1] * k)
            if abs(self.glide[0]) < 0.5 and abs(self.glide[1]) < 0.5:
                self.glide = (0.0, 0.0)
        self.glide_t = now
        self._move(fx + self.glide[0], fy + self.glide[1])

    def _finish_touch(self, now):
        """The thumb let go of the index tip."""
        if self.dragging:
            self.mouse.left_up()                 # end of drag / selection
            self.dragging = False
            self._flash("RELEASE", now)
        elif self.tap_aim is not None:
            self._left_click(now, aim=self.tap_aim)   # short touch = one click
        self.tap_down_time = self.tap_aim = None

    def _move(self, x, y):
        self.cursor = (x, y)
        self.mouse.move(x, y)
        if self.button_down and self.pen_target == "overlay":
            self.overlay_queue.put(("move", x, y))

    def _pen_down(self, path):
        """Start writing: press at the first point of the path, then replay it."""
        if not path:
            path = [self.cursor]
        x0, y0 = path[0]
        if self.overlay_on:
            self.pen_target = "overlay"
            self.mouse.move(x0, y0)
            self.overlay_queue.put(("start", x0, y0))
            for x, y in path[1:]:
                self.mouse.move(x, y)
                self.overlay_queue.put(("move", x, y))
        else:
            self.pen_target = "mouse"
            self.mouse.left_down(x0, y0)
            for x, y in path[1:]:
                self.mouse.move(x, y)
        self.button_down = True
        self.cursor = path[-1]

    def _pen_up(self):
        if self.pen_target == "overlay":
            self.overlay_queue.put(("end",))
        else:
            self.mouse.left_up()
        self.button_down = False
        self.pen_target = None

    def _track_pinch_approach(self, info, stable, now):
        """Remember WHEN the thumb started moving toward the index or middle fingertip."""
        gap = min(info["index_gap"], info["middle_gap"])
        if gap > max(self.cfg["pinch_off"], self.cfg["middle_off"]) + 0.25:
            self.approach_time = None
        elif self.approach_time is None and self._moves_cursor(stable):
            self.approach_time = now
        # ...and when the hand started turning its back to the camera (right click)
        if info["facing"] > 0.45:
            self.turn_time = None
        elif self.turn_time is None and self._moves_cursor(stable):
            self.turn_time = now

    def _tap_start(self, now):
        """When did the thumb start moving toward the fingertip? = the last moment
        (within 0.4 s) when the thumb gap was still at its widest."""
        recent = [(t, g) for t, g in self.gap_history if now - t <= 0.4]
        if len(recent) < 2:
            return None
        widest = max(g for _, g in recent)
        start = None
        for t, g in recent:
            if g >= widest - 0.05:
                start = t
        return start

    def _aim_point(self, now, start=None, lookback_s=None):
        """Where you were AIMING. Making a gesture moves the hand a little, so we look
        back in the cursor history (click_lookback_ms before the gesture appeared, or
        to `start` = when the hand began changing), but never more than 0.6 s."""
        seen = self.candidate_since or now
        if lookback_s is None:
            lookback_s = self.cfg["click_lookback_ms"] / 1000.0
        target = seen - lookback_s
        if start is not None and seen - start < 0.8:
            target = min(target, start)
        target = max(target, seen - 0.6)
        x, y = self.cursor
        for t, hx, hy in self.history:          # oldest -> newest
            if t <= target:
                x, y = hx, hy
            else:
                break
        return x, y

    def _left_click(self, now, start=None, lookback_s=None, aim=None):
        x, y = aim if aim is not None else self._aim_point(now, start, lookback_s)
        text = "CLICK"
        # A 2nd click soon after the 1st, close by, goes on the SAME pixel,
        # so Windows sees a double-click (it allows only ~4 px of movement).
        if self.last_click is not None:
            t0, x0, y0 = self.last_click
            if now - t0 < DOUBLE_CLICK_WINDOW_S and math.hypot(x - x0, y - y0) < DOUBLE_CLICK_SNAP_PX:
                x, y, text = x0, y0, "CLICK 2"
        self.mouse.click(x, y)
        self.cursor = (x, y)
        self.last_click = (now, x, y)
        self.approach_time = None
        self._flash(text, now)

    def _right_click(self, now, start=None, lookback_s=None):
        x, y = self._aim_point(now, start, lookback_s)
        self.mouse.right_click(x, y)
        self.cursor = (x, y)
        self.turn_time = None
        self._flash("RIGHT CLICK", now)

    def _scroll(self, hand):
        y = hand.points[9][1]                   # middle-finger knuckle: steady point
        if self.scroll_y is None:
            self.scroll_y = y
        smooth_y = 0.5 * self.scroll_y + 0.5 * y
        dy = self.scroll_y - smooth_y           # > 0 when the hand moves UP
        self.scroll_y = smooth_y
        if abs(dy) < 0.0015:                    # ignore tiny shakes
            return
        amount = dy * self.cfg["scroll_speed"] * 120
        if self.cfg["scroll_invert"]:
            amount = -amount
        self.scroll_acc += amount
        if abs(self.scroll_acc) >= 30:          # send in small steps for smooth scrolling
            step = int(self.scroll_acc)
            self.mouse.scroll(step)
            self.scroll_acc -= step

    def _scroll_back_of_hand(self, hand, now):
        """Like a joystick: hold the hand ABOVE where you started -> keeps scrolling up,
        BELOW -> keeps scrolling down. Further away = faster. Back near the start = stop.
        Smooth: the hand position is smoothed, the speed eases in and out, and the page
        moves in small steps every frame instead of jumpy wheel notches."""
        y = self._anchor(hand)[1]
        if self.scroll_y_smooth is None:
            self.scroll_y_smooth = y
        self.scroll_y_smooth += 0.35 * (y - self.scroll_y_smooth)      # calm the hand jitter
        if self.scroll_ref is None:
            self.scroll_ref, self.scroll_t = self.scroll_y_smooth, now
            return
        dt = min(max(now - self.scroll_t, 0.0), 0.1)
        self.scroll_t = now
        offset = self.scroll_ref - self.scroll_y_smooth  # > 0 when the hand is higher
        beyond = abs(offset) - SCROLL_DEAD_BAND
        target = 0.0
        if beyond > 0:
            # gentle just outside the start band, faster further away (notches per second)
            target = self.cfg["scroll_speed"] * (1.5 * beyond + 8.0 * beyond * beyond)
            target = min(target, SCROLL_MAX_NOTCHES) * (1 if offset > 0 else -1)
            if self.cfg["scroll_invert"]:
                target = -target
        self.scroll_rate += (target - self.scroll_rate) * min(1.0, dt / SCROLL_EASE_S)
        if target == 0.0 and abs(self.scroll_rate) < 0.2:
            self.scroll_rate, self.scroll_acc = 0.0, 0.0
            return
        self.scroll_acc += self.scroll_rate * 120.0 * dt                # 120 = one wheel notch
        step = 10 if self.cfg["smooth_scroll"] else 120
        amount = int(self.scroll_acc / step) * step
        if amount:
            self.mouse.scroll(amount)
            self.scroll_acc -= amount

    # ---------------------------------------------------------------------
    # Lock (open palm)
    # ---------------------------------------------------------------------
    def _lock(self, now):
        self.locked = True
        self.palm_latch = True
        self.unlock_since = None
        self._release_everything()
        self._flash("LOCKED", now)

    def _locked_frame(self, raw, now):
        cfg = self.cfg
        if not cfg["enable_palm_lock"] or cfg["palm_mode"] != "toggle":
            self.locked = False                 # lock was switched off in settings
            return
        if raw == PALM:
            self.non_palm_frames = 0
            if self.palm_latch:
                return                          # still the same palm that locked it
            if self.unlock_since is None:
                self.unlock_since = now
            elif now - self.unlock_since >= cfg["palm_hold_s"]:
                self.locked = False
                self.palm_latch = True
                self.debouncer.reset(PALM)      # stay calm until the palm closes
                self.palm_since = now
                self._flash("UNLOCKED", now)
        else:
            self.non_palm_frames += 1
            if self.non_palm_frames >= 3:       # palm really gone (not a 1-frame glitch)
                self.palm_latch = False
                self.unlock_since = None

    # ---------------------------------------------------------------------
    # Hand missing
    # ---------------------------------------------------------------------
    def _hand_missing(self, now):
        self.raw_gesture = NONE
        if self.last_seen is None or self.hand_gone:
            return
        if now - self.last_seen < self.cfg["lost_hand_grace_ms"] / 1000.0:
            return                              # short dropout: wait (cursor stays put)
        if self.button_down:
            self._pen_up()                      # never leave the button stuck down
        self.mouse.release_all()
        self.dragging = False
        self.tap_down_time = self.tap_aim = None
        self.debouncer.reset(NONE)
        self.classifier.reset()
        self.pending_path = []
        self.approach_time = None
        self.turn_time = None
        self.near_since = self.far_since = None
        self.history.clear()
        self.gap_history.clear()
        self.palm_latch = False
        self.unlock_since = None
        self.hand_gone = True

    # =====================================================================
    # Calibration of the active box
    # =====================================================================
    def _begin_calibration(self):
        self._calibration_requested = False
        self._release_everything()
        self.calibrating = True
        self.calib_kind = "box"
        self.calib_phase = "ready"
        self.calib_t0 = time.monotonic()
        self.calib_points = []
        self.calib_message = ""

    def _calibration_step(self, hand, now):
        if not self.calibrating:
            return
        if self.calib_kind == "touch":
            self._touch_calibration_step(hand, now)
            return
        t = now - self.calib_t0
        if self.calib_phase == "ready" and t >= 3.0:
            self.calib_phase, self.calib_t0 = "record", now
        elif self.calib_phase == "record":
            if hand is not None:
                self.calib_points.append(self._anchor(hand))
            if t >= 5.0:
                self._finish_calibration()
                self.calib_phase, self.calib_t0 = "done", now
        elif self.calib_phase == "done" and t >= 3.0:
            self.calibrating = False
            self._reset_hand_state()

    def _finish_calibration(self):
        if len(self.calib_points) < 20:
            self.calib_message = "Not enough hand data - try again in better light."
            return
        pts = np.array(self.calib_points)
        x0, x1 = np.percentile(pts[:, 0], [5, 95])
        y0, y1 = np.percentile(pts[:, 1], [5, 95])
        w, h = max(x1 - x0, 0.12), max(y1 - y0, 0.12)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        if self.cfg["lock_aspect"]:
            # Fit a screen-shaped box INSIDE what you reached, so every screen edge is easy.
            frame_h, frame_w = self.frame_shape[:2]
            _, _, screen_w, screen_h = self.screen_rect
            need = (frame_w / frame_h) * (screen_h / screen_w)   # h = w * need
            if w * need <= h:
                h = w * need
            else:
                w = h / need
        self.cfg.set_box(x=float(cx - w / 2), y=float(cy - h / 2), w=float(w), h=float(h))
        self.cfg.save()
        self.calib_message = "Saved! This is your new active box."
        log.info("Calibrated active box: %s", self.cfg["active_box"])

    # =====================================================================
    # Calibration of the thumb touches (sets the 4 touch numbers for YOUR hand)
    # =====================================================================
    TOUCH_STEPS = (("ready", 2.5), ("open", 2.5), ("index", 3.0), ("middle", 3.0), ("done", 4.0))

    def _begin_touch_calibration(self):
        self._touch_calibration_requested = False
        self._release_everything()
        self.calibrating = True
        self.calib_kind = "touch"
        self.calib_step = 0
        self.calib_t0 = None                 # starts with the first camera frame
        self.touch_samples = {"open": [], "index": [], "middle": []}
        self.calib_live = None
        self.calib_message = ""

    def _touch_calibration_step(self, hand, now):
        if self.calib_t0 is None:
            self.calib_t0 = now
        name, length = self.TOUCH_STEPS[self.calib_step]
        t = now - self.calib_t0
        if hand is not None:
            gaps = touch_gaps(hand)
            self.calib_live = gaps
            if name in self.touch_samples and t > 0.8:   # skip the moment you change your hand
                self.touch_samples[name].append(gaps)
        if t >= length:
            if name == "middle":
                self._finish_touch_calibration()
            self.calib_step += 1
            self.calib_t0 = now
            if self.calib_step >= len(self.TOUCH_STEPS):
                self.calibrating = False
                self.calib_kind = "box"
                self._reset_hand_state()

    def _finish_touch_calibration(self):
        s = self.touch_samples
        if min(len(v) for v in s.values()) < 15:
            self.calib_message = "Hand not seen enough. Try again."
            return

        def pct(values, q):                  # q = 0.85 -> 85 % of the values are below this
            values = sorted(values)
            return values[int(q * (len(values) - 1))]

        results = {}
        for name, k in (("index", 0), ("middle", 1)):
            touch = pct([g[k] for g in s[name]], 0.85)      # gap while touching
            apart = pct([g[k] for g in s["open"]], 0.15)    # gap with the hand open
            if apart - touch < 0.06:
                self.calib_message = f"{name.upper()} touch unclear. More light, try again."
                return
            # A touch starts just above your "touching" number (so the tips must really
            # touch) and ends a little higher (so it does not flicker).
            on = touch + max(0.04, min(0.40 * (apart - touch), 0.10))
            off = on + max(0.05, min(0.30 * (apart - touch), 0.10))
            results[name] = (on, off)
        self.cfg.set("pinch_on", results["index"][0])
        self.cfg.set("pinch_off", results["index"][1])
        self.cfg.set("middle_on", results["middle"][0])
        self.cfg.set("middle_off", results["middle"][1])
        self.cfg.save()
        self.calib_message = f"Saved! index<{results['index'][0]:.2f} middle<{results['middle'][0]:.2f}"
        log.info("Touch calibration: %s", results)

    # =====================================================================
    # Status + preview window
    # =====================================================================
    def _flash(self, text, now):
        self.flash_text, self.flash_until = text, now + 0.7

    def _update_status(self, hand):
        if self.calibrating:
            state = "CALIBRATING"
        elif self.locked:
            state = "LOCKED"
        else:
            state = "RUNNING"
        if self.overlay_on and state == "RUNNING":
            state = "RUNNING + OVERLAY"
        gesture = GESTURE_TEXT.get(self.debouncer.stable, "-") if hand else "no hand"
        self.status.update(state=state, gesture=gesture, fps=self.fps,
                           hand=hand.label if hand else "-")
        if hand is not None and self.camera is not None:
            self.status["message"] = self.camera.description + (" | low light boost" if self.dark else "")

    def _draw_preview(self, frame, hand, box, now):
        h, w = frame.shape[:2]
        pw = int(self.cfg["preview_width"])
        ph = int(h * pw / w)
        img = cv2.resize(frame, (pw, ph), interpolation=cv2.INTER_AREA)

        # active box
        bx, by, bw, bh = box
        box_color = (0, 170, 255) if self.locked else (0, 220, 0)
        cv2.rectangle(img, (int(bx * pw), int(by * ph)),
                      (int((bx + bw) * pw), int((by + bh) * ph)), box_color, 2)

        # hand skeleton
        if hand is not None:
            pts = [(int(x * pw), int(y * ph)) for x, y, _ in hand.points]
            for a, b in HAND_LINKS:
                cv2.line(img, pts[a], pts[b], (255, 255, 255), 1, cv2.LINE_AA)
            for p in pts:
                cv2.circle(img, p, 2, (0, 140, 255), -1)
            ax, ay = self._anchor(hand)
            cv2.circle(img, (int(ax * pw), int(ay * ph)), 7, (0, 255, 255), 2)   # cursor point

        # text
        def put(text, row, color=(255, 255, 255), scale=0.55):
            # dark label behind the text so it is readable on any background
            y = 22 + row * 22
            (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
            cv2.rectangle(img, (4, y - th - 5), (12 + tw, y + 5), (0, 0, 0), -1)
            cv2.putText(img, text, (8, y), cv2.FONT_HERSHEY_SIMPLEX, scale, color, 1, cv2.LINE_AA)

        if self.calibrating:
            self._draw_calibration(img, put, now, pw, ph)
        else:
            st = self.status
            put(f"{st['state']}   {st['fps']:.0f} FPS", 0, (0, 255, 0) if st["state"].startswith("RUN")
                else (0, 170, 255))
            put(f"Gesture: {st['gesture']}" + ("   [button DOWN]" if self.button_down or self.dragging
                                                else ""), 1)
            if self.locked:
                put("Hold open palm to unlock", 2, (0, 170, 255))
            if self.info is not None and hand is not None:
                near = self.info["near_touch"]
                put(f"index {self.info['index_gap']:.2f}  middle {self.info['middle_gap']:.2f}  "
                    f"side {self.info['facing']:+.2f}  {hand.label}", ph // 22 - 1,
                    (0, 255, 255) if near else (200, 200, 200), 0.45)
            if (self.debouncer.stable == BACK and self.cfg["back_action"] == "scroll"
                    and self.scroll_ref is not None and hand is not None):
                # scroll "joystick": line = start, band = no-scroll zone
                ry = self.scroll_ref * ph
                band = SCROLL_DEAD_BAND * ph
                cv2.line(img, (0, int(ry)), (pw, int(ry)), (255, 255, 0), 1)
                cv2.rectangle(img, (0, int(ry - band)), (pw, int(ry + band)), (255, 255, 0), 1)
                arrow = "UP" if self.scroll_rate > 0 else "DOWN" if self.scroll_rate < 0 else "-"
                put(f"SCROLL {arrow}  {abs(self.scroll_rate):.0f}/s", 2, (255, 255, 0))
            self._draw_hold_progress(img, now, pw)
            if now < self.flash_until:
                cv2.putText(img, self.flash_text, (pw // 2 - 70, ph // 2), cv2.FONT_HERSHEY_SIMPLEX,
                            0.9, (0, 255, 255), 2, cv2.LINE_AA)
        self._show(img)

    def _draw_hold_progress(self, img, now, pw):
        """A small filling circle while you hold a fist or an open palm."""
        progress = None
        stable = self.debouncer.stable
        if stable == PINCH and self.tap_down_time is not None and not self.dragging:
            progress = (now - self.tap_down_time) / (self.cfg["hold_ms"] / 1000.0)   # hold -> drag
        elif self.locked and self.unlock_since is not None:
            progress = (now - self.unlock_since) / self.cfg["palm_hold_s"]
        elif stable == FIST and self.fist_since and not self.fist_done and self.cfg["enable_right_click"]:
            progress = (now - self.fist_since) / self.cfg["right_click_hold_s"]
        elif (stable == PALM and self.palm_since and not self.palm_latch
              and self.cfg["enable_palm_lock"] and self.cfg["palm_mode"] == "toggle"):
            progress = (now - self.palm_since) / self.cfg["palm_hold_s"]
        if progress is not None:
            angle = int(360 * min(progress, 1.0))
            cv2.ellipse(img, (pw - 30, 30), (18, 18), -90, 0, angle, (0, 255, 255), 4)

    def _draw_calibration(self, img, put, now, pw, ph):
        t = now - (self.calib_t0 if self.calib_t0 is not None else now)
        if self.calib_kind == "touch":
            name, length = self.TOUCH_STEPS[min(self.calib_step, len(self.TOUCH_STEPS) - 1)]
            left = max(0.0, length - t)
            title, sub = {
                "ready": ("TOUCH CALIBRATION", f"Palm toward the camera  {left:.0f}"),
                "open": ("1/3  OPEN hand, thumb relaxed", f"Keep still  {left:.1f}s"),
                "index": ("2/3  TOUCH thumb + INDEX tips", f"Hold the touch  {left:.1f}s"),
                "middle": ("3/3  TOUCH thumb + MIDDLE tips", f"Hold the touch  {left:.1f}s"),
                "done": (self.calib_message, ""),
            }[name]
            put(title, 0, (0, 255, 255) if name != "done" else (0, 255, 0))
            if sub:
                put(sub, 1)
            if self.calib_live is not None:
                put(f"index {self.calib_live[0]:.2f}   middle {self.calib_live[1]:.2f}", 2, (200, 200, 200))
            return
        for x, y in self.calib_points:
            cv2.circle(img, (int(x * pw), int(y * ph)), 2, (255, 0, 255), -1)
        if self.calib_phase == "ready":
            put("CALIBRATION", 0, (0, 255, 255))
            put(f"Get ready... {3 - int(t)}", 1)
            put("Then move your hand to all 4", 2)
            put("corners of a COMFORTABLE area", 3)
        elif self.calib_phase == "record":
            put(f"RECORDING  {max(0.0, 5 - t):.1f}s", 0, (0, 0, 255))
            put("Trace a big rectangle - stay relaxed", 1)
        else:
            put(self.calib_message, 0, (0, 255, 0))

    def _show_message_preview(self, text):
        """While stopped or waiting, show a simple dark picture with a message."""
        if not self.preview_on:
            self._close_preview()
            return
        now = time.monotonic()
        if now - self._idle_shown_at < 0.1:     # 10 times a second is plenty
            return
        self._idle_shown_at = now
        pw = int(self.cfg["preview_width"])
        img = np.full((int(pw * 0.75), pw, 3), 40, np.uint8)
        words, line, row = text.split(), "", 0
        for word in words:                       # simple word wrap
            if len(line) + len(word) > 34:
                cv2.putText(img, line, (10, 30 + row * 24), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                            (255, 255, 255), 1, cv2.LINE_AA)
                line, row = "", row + 1
            line += word + " "
        cv2.putText(img, line, (10, 30 + row * 24), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                    (255, 255, 255), 1, cv2.LINE_AA)
        self._show(img)

    def _show(self, img):
        if self.preview_created:
            # Did you close the preview with its X button?
            try:
                visible = cv2.getWindowProperty(PREVIEW_WINDOW, cv2.WND_PROP_VISIBLE)
            except cv2.error:
                visible = 0
            if visible < 1:
                self.preview_created = False
                self.preview_on = False
                if self.calibrating:
                    self.calibrating = False
                return
        else:
            cv2.namedWindow(PREVIEW_WINDOW, cv2.WINDOW_AUTOSIZE)
            topmost = getattr(cv2, "WND_PROP_TOPMOST", None)   # missing in very old OpenCV
            if topmost is not None:
                try:
                    cv2.setWindowProperty(PREVIEW_WINDOW, topmost,
                                          1 if self.cfg["preview_topmost"] else 0)
                except cv2.error:
                    pass
            self.preview_created = True
        cv2.imshow(PREVIEW_WINDOW, img)
        cv2.waitKey(1)          # lets the window redraw

    def _close_preview(self):
        if self.preview_created:
            try:
                cv2.destroyWindow(PREVIEW_WINDOW)
                cv2.waitKey(1)
            except cv2.error:
                pass
            self.preview_created = False
