"""
tracker.py
----------
Finds hands in a camera frame with MediaPipe and returns 21 landmarks per hand.

MediaPipe has two ways to do this, and this file supports both:

  "legacy"  mp.solutions.hands        - model files are INSIDE the mediapipe package,
                                         so it works offline with nothing to download.
                                         Available up to mediapipe 0.10.21 (our pinned version).
  "tasks"   HandLandmarker (Tasks API) - needs the file models/hand_landmarker.task
                                         (run download_model.py once). Works on new
                                         mediapipe versions (0.10.30+ and 1.x).

"auto" picks legacy when it exists, otherwise tasks.
"""

import logging
import os

import cv2

from config import find_resource

log = logging.getLogger(__name__)

MODEL_FILE = os.path.join("models", "hand_landmarker.task")


class TrackerError(RuntimeError):
    """Raised with a friendly message when hand tracking cannot start."""


class Hand:
    """One detected hand.

    points : 21 (x, y, z) - x and y are 0..1 across the camera image
    world  : 21 (x, y, z) in meters (real 3D shape of the hand), or None
    label  : "Right" or "Left" (the person's own hand)
    """

    def __init__(self, points, world, label, score, img_w, img_h):
        self.points = points
        self.world = world
        self.label = label
        self.score = score
        self.img_w = img_w
        self.img_h = img_h

    def shape_points(self):
        """Points for measuring finger bends: 3D meters if we have them, else pixels."""
        if self.world:
            return self.world
        return [(x * self.img_w, y * self.img_h, z * self.img_w) for x, y, z in self.points]


def find_model():
    return find_resource(MODEL_FILE) or find_resource("hand_landmarker.task")


class HandTracker:
    def __init__(self, backend="auto", max_hands=1, model_complexity=1,
                 detection_confidence=0.6, tracking_confidence=0.5):
        try:
            import mediapipe as mp
        except ImportError as e:
            raise TrackerError(
                "MediaPipe is not installed or failed to load.\n\n"
                f"Details: {e}\n\n"
                "Fix: run setup.bat again (needs Python 3.10 or 3.11, 64-bit). "
                "If you see 'DLL load failed', install the Microsoft Visual C++ "
                "Redistributable (x64).") from e

        self.mp = mp
        self._last_ts = -1
        has_legacy = hasattr(mp, "solutions") and hasattr(mp.solutions, "hands")
        model_path = find_model()

        if backend == "auto":
            backend = "legacy" if has_legacy else "tasks"
        if backend == "legacy" and not has_legacy:
            log.warning("This mediapipe has no legacy hands API - switching to tasks")
            backend = "tasks"
        if backend == "tasks" and model_path is None:
            if has_legacy:
                log.warning("hand_landmarker.task not found - using the legacy model instead")
                backend = "legacy"
            else:
                raise TrackerError(
                    "The hand model file is missing.\n\n"
                    "Run:  python download_model.py   (needs internet once)\n"
                    "It saves models\\hand_landmarker.task next to main.py.\n"
                    "Or install the pinned version:  pip install mediapipe==0.10.21")

        self.backend = backend
        if backend == "legacy":
            self.hands = mp.solutions.hands.Hands(
                static_image_mode=False,
                max_num_hands=max_hands,
                model_complexity=model_complexity,
                min_detection_confidence=detection_confidence,
                min_tracking_confidence=tracking_confidence,
            )
        else:
            self._create_tasks(model_path, max_hands, detection_confidence, tracking_confidence)
        log.info("Hand tracker ready (backend=%s, mediapipe %s)",
                 backend, getattr(mp, "__version__", "?"))

    def _create_tasks(self, model_path, max_hands, det_conf, track_conf):
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision

        # Reading the file ourselves avoids MediaPipe problems with non-English folder names.
        with open(model_path, "rb") as f:
            model_bytes = f.read()
        try:
            base = mp_python.BaseOptions(model_asset_buffer=model_bytes)
        except TypeError:
            base = mp_python.BaseOptions(model_asset_path=model_path)
        options = vision.HandLandmarkerOptions(
            base_options=base,
            running_mode=vision.RunningMode.VIDEO,
            num_hands=max_hands,
            min_hand_detection_confidence=det_conf,
            min_hand_presence_confidence=track_conf,
            min_tracking_confidence=track_conf,
        )
        self.landmarker = vision.HandLandmarker.create_from_options(options)

    def process(self, frame_bgr, timestamp_ms, mirrored=True):
        """Find hands in one BGR camera frame. Returns a list of Hand objects."""
        h, w = frame_bgr.shape[:2]
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        hands = []

        if self.backend == "legacy":
            rgb.flags.writeable = False
            res = self.hands.process(rgb)
            if not res.multi_hand_landmarks:
                return hands
            worlds = res.multi_hand_world_landmarks or []
            for i, lms in enumerate(res.multi_hand_landmarks):
                pts = [(p.x, p.y, p.z) for p in lms.landmark]
                world = [(p.x, p.y, p.z) for p in worlds[i].landmark] if i < len(worlds) else None
                cls = res.multi_handedness[i].classification[0]
                # Legacy assumes a MIRRORED (selfie) image. If ours is not mirrored, swap.
                label = cls.label if mirrored else _swap(cls.label)
                hands.append(Hand(pts, world, label, cls.score, w, h))
        else:
            # The Tasks API needs a timestamp that always goes up.
            ts = max(int(timestamp_ms), self._last_ts + 1)
            self._last_ts = ts
            image = self.mp.Image(image_format=self.mp.ImageFormat.SRGB, data=rgb)
            res = self.landmarker.detect_for_video(image, ts)
            worlds = res.hand_world_landmarks or []
            for i, lms in enumerate(res.hand_landmarks):
                pts = [(p.x, p.y, p.z) for p in lms]
                world = [(p.x, p.y, p.z) for p in worlds[i]] if i < len(worlds) else None
                cat = res.handedness[i][0]
                # The Tasks docs say a mirrored image swaps Left/Right, so undo that.
                label = _swap(cat.category_name) if mirrored else cat.category_name
                hands.append(Hand(pts, world, label, cat.score, w, h))
        return hands

    def close(self):
        try:
            if self.backend == "legacy":
                self.hands.close()
            else:
                self.landmarker.close()
        except Exception:  # closing should never crash the app
            pass


def _swap(label):
    return "Left" if label == "Right" else "Right"
