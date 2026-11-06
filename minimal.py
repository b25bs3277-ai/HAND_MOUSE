"""
minimal.py  -  STAGE 1: the smallest working version
=====================================================

Only three things happen here:
  1. Your index fingertip moves the mouse.
  2. Index + middle finger up  -> left button is held down (write / drag).
     Fold the middle finger again -> button is released.
  3. Thumb + index pinch      -> one left click.

Press  Q  or  Esc  in the camera window to quit.

Run:   python minimal.py

If this works (camera opens, cursor moves, you can draw a line in Paint),
your setup is good and you can move on to the full app:  python main.py
"""

import math
import time

import cv2

from mouse_control import Mouse, list_monitors, set_dpi_awareness
from tracker import HandTracker

# ---- Easy settings to play with -------------------------------------------
CAMERA_INDEX = 0
BOX = (0.20, 0.15, 0.60, 0.60)  # active box in the camera image: left, top, width, height (0..1)
SMOOTH = 0.35                   # 0..1  lower = smoother but slower cursor
PINCH_RATIO = 0.30              # pinch when thumb-index gap < this x palm size
STABLE_FRAMES = 3               # the two-finger gesture must last this many frames


def distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def finger_is_up(px, tip, pip):
    """A finger is 'up' when its tip is higher on screen than its middle joint.
    (In images, y grows DOWNWARD, so 'higher' means a smaller y.)"""
    return px[tip][1] < px[pip][1]


def open_camera(index):
    cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)   # DirectShow opens fast on Windows
    if not cap.isOpened():
        cap = cv2.VideoCapture(index)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    return cap


def main():
    set_dpi_awareness()                 # must happen before any window opens
    screen = list_monitors()[0]         # main monitor
    mouse = Mouse()
    tracker = HandTracker(max_hands=1)
    cap = open_camera(CAMERA_INDEX)
    if not cap.isOpened():
        print("Camera did not open. Try CAMERA_INDEX = 1, and close Zoom/Teams/Camera app.")
        return

    cursor_x = cursor_y = None
    button_down = False
    two_count = one_count = 0
    pinch_ready = True

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                continue
            frame = cv2.flip(frame, 1)                  # mirror, like a selfie
            h, w = frame.shape[:2]
            hands = tracker.process(frame, int(time.monotonic() * 1000), mirrored=True)

            if hands:
                hand = hands[0]
                px = [(x * w, y * h) for x, y, _ in hand.points]   # landmarks in pixels

                # 1) MOVE: map the index tip from the active box to the screen
                tip_x, tip_y = hand.points[8][0], hand.points[8][1]
                u = min(max((tip_x - BOX[0]) / BOX[2], 0.0), 1.0)
                v = min(max((tip_y - BOX[1]) / BOX[3], 0.0), 1.0)
                target_x = screen.left + u * (screen.width - 1)
                target_y = screen.top + v * (screen.height - 1)
                if cursor_x is None:
                    cursor_x, cursor_y = target_x, target_y
                cursor_x += SMOOTH * (target_x - cursor_x)       # simple smoothing
                cursor_y += SMOOTH * (target_y - cursor_y)
                mouse.move(cursor_x, cursor_y)

                # 2) HOLD: index + middle up = button down
                two_up = finger_is_up(px, 8, 6) and finger_is_up(px, 12, 10)
                two_count = two_count + 1 if two_up else 0
                one_count = 0 if two_up else one_count + 1
                if two_count >= STABLE_FRAMES and not button_down:
                    mouse.left_down(cursor_x, cursor_y)
                    button_down = True
                if one_count >= STABLE_FRAMES and button_down:
                    mouse.left_up()
                    button_down = False

                # 3) CLICK: pinch thumb tip (4) to index tip (8)
                palm = max(distance(px[0], px[9]), 1.0)          # wrist -> middle knuckle
                gap = distance(px[4], px[8]) / palm
                if gap < PINCH_RATIO and pinch_ready and not button_down:
                    mouse.click()
                    pinch_ready = False
                elif gap > PINCH_RATIO * 1.5:
                    pinch_ready = True

                # draw a few dots so you can see what the computer sees
                for x, y in px:
                    cv2.circle(frame, (int(x), int(y)), 3, (0, 255, 0), -1)
                state = "WRITING" if button_down else "MOVING"
                cv2.putText(frame, f"{state}  pinch={gap:.2f}", (10, 30),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
            else:
                # Hand lost -> never leave the button stuck down
                if button_down:
                    mouse.left_up()
                    button_down = False
                two_count = one_count = 0

            # show the active box
            x0, y0 = int(BOX[0] * w), int(BOX[1] * h)
            x1, y1 = int((BOX[0] + BOX[2]) * w), int((BOX[1] + BOX[3]) * h)
            cv2.rectangle(frame, (x0, y0), (x1, y1), (255, 200, 0), 2)
            cv2.imshow("Stage 1 - press Q to quit", frame)
            key = cv2.waitKey(1) & 0xFF
            if key in (27, ord("q")):
                break
    finally:
        mouse.release_all()             # always let go of the button
        cap.release()
        cv2.destroyAllWindows()
        tracker.close()


if __name__ == "__main__":
    main()
