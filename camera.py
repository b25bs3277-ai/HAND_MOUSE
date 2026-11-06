"""
camera.py
---------
Opens the webcam and reads frames in a background thread.

Why a thread?  The webcam keeps a small queue of old frames. If we read
slowly, we would get OLD frames and the cursor would lag. This thread
reads all the time and keeps only the NEWEST frame.
"""

import logging
import sys
import threading
import time

import cv2

log = logging.getLogger(__name__)


class Camera:
    def __init__(self, index=0, width=640, height=480, fps=30):
        self.index = index
        self.width = width
        self.height = height
        self.fps = fps
        self.cap = None
        self.broken = False          # True if the camera stopped sending frames
        self.description = ""
        self._frame = None
        self._frame_id = 0
        self._cond = threading.Condition()
        self._running = False
        self._thread = None

    def _try_open(self, backend, use_mjpg):
        cap = cv2.VideoCapture(self.index, backend)
        if not cap.isOpened():
            cap.release()
            return None
        if use_mjpg:  # MJPG lets most webcams give 30 FPS at 640x480; must be set before the size
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        for _ in range(15):          # some cameras need a few tries to wake up
            ok, frame = cap.read()
            if ok and frame is not None:
                return cap
            time.sleep(0.05)
        cap.release()
        return None

    def open(self):
        """Try the fastest Windows camera drivers first. Returns True if it worked."""
        if sys.platform == "win32":
            attempts = [(cv2.CAP_DSHOW, True, "DirectShow+MJPG"),
                        (cv2.CAP_DSHOW, False, "DirectShow"),
                        (cv2.CAP_MSMF, False, "Media Foundation"),
                        (cv2.CAP_ANY, False, "default")]
        else:
            attempts = [(cv2.CAP_ANY, False, "default")]
        for backend, mjpg, name in attempts:
            cap = self._try_open(backend, mjpg)
            if cap is not None:
                self.cap = cap
                w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                self.description = f"camera {self.index} ({name}, {w}x{h})"
                log.info("Opened %s", self.description)
                self._running = True
                self._thread = threading.Thread(target=self._reader, name="camera", daemon=True)
                self._thread.start()
                return True
        log.warning("Camera %s could not be opened", self.index)
        return False

    def _reader(self):
        failures = 0
        while self._running:
            ok, frame = self.cap.read()
            if not ok or frame is None:
                failures += 1
                if failures > 50:            # about 1-2 seconds of nothing
                    log.warning("Camera stopped sending frames")
                    self.broken = True
                    with self._cond:
                        self._cond.notify_all()
                    return
                time.sleep(0.02)
                continue
            failures = 0
            with self._cond:
                self._frame = frame
                self._frame_id += 1
                self._cond.notify_all()

    def read_latest(self, last_id, timeout=1.0):
        """Wait for a frame newer than last_id. Returns (frame, id) or (None, last_id)."""
        with self._cond:
            if self._frame_id == last_id and not self.broken:
                self._cond.wait(timeout)
            if self._frame_id == last_id:
                return None, last_id
            return self._frame, self._frame_id

    def close(self):
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        if self.cap is not None:
            self.cap.release()
            self.cap = None
