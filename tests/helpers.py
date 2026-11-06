"""
Test helpers: build MediaPipe-like hands WITHOUT a camera, and a fake mouse.

A hand is drawn in "hand space" (meters, wrist at 0,0,0, fingers pointing up = -y),
then placed in the camera image. Each finger is 3 bones; we give 3 angles that bend
it toward the palm (0 = straight up, 90 = pointing at the camera).
"""

import math
import random

from tracker import Hand

BONES = (0.045, 0.025, 0.020)                 # finger bone lengths in meters
KNUCKLES = {"index": (-0.030, -0.088), "middle": (-0.010, -0.090),
            "ring": (0.010, -0.086), "pinky": (0.030, -0.078)}
ORDER = ("index", "middle", "ring", "pinky")
STRAIGHT = (0, 5, 8)
CURLED = (60, 160, 230)
BENT_TO_THUMB = (20, 60, 90)                  # finger bent forward to meet the thumb

rng = random.Random(0)                        # reset by the autouse fixture in conftest.py


def _finger(name, angles):
    x, y = KNUCKLES[name]
    z = 0.0
    pts = [(x, y, z)]
    for length, a in zip(BONES, angles):
        r = math.radians(a)
        y -= length * math.cos(r)
        z += length * math.sin(r)
        pts.append((x, y, z))
    return pts


def hand(index=STRAIGHT, middle=STRAIGHT, ring=STRAIGHT, pinky=STRAIGHT, touch=None, gap=0.006,
         thumb_at=None, cx=0.5, cy=0.6, back=False, yaw_deg=0.0, noise=0.0, znoise=0.0,
         label="Right", scale=2.2):
    """touch = 8 (index tip) or 12 (middle tip) or 16 (ring tip): put the thumb tip there.
    thumb_at = (x, y, z) puts the thumb tip at any spot. back=True shows the back of the hand.
    yaw_deg turns the hand sideways. noise/znoise = landmark jitter (z is MediaPipe's weak axis)."""
    world = [(0.0, 0.0, 0.0)] + [None] * 20
    for i, name in enumerate(ORDER):
        angles = (index, middle, ring, pinky)[i]
        for j, p in enumerate(_finger(name, angles)):
            world[5 + 4 * i + j] = p
    world[1], world[2] = (0.03, -0.02, 0.01), (0.045, -0.045, 0.02)
    if touch is not None:
        t = world[touch]
        thumb_at = (t[0] + gap, t[1] + gap * 0.5, t[2] + gap * 0.3)
    if thumb_at is None:
        thumb_at = (0.075, -0.08, 0.0)          # thumb relaxed, out to the side
    world[4] = thumb_at
    world[3] = tuple((world[2][k] + thumb_at[k]) / 2 for k in range(3))
    if back:
        world = [(-x, y, z) for x, y, z in world]
    c, s = math.cos(math.radians(yaw_deg)), math.sin(math.radians(yaw_deg))
    world = [(x * c + z * s, y, -x * s + z * c) for x, y, z in world]
    points = [(cx + x * scale + rng.gauss(0, noise), cy + y * scale + rng.gauss(0, noise), z)
              for x, y, z in world]
    if noise or znoise:
        world = [(x + rng.gauss(0, noise), y + rng.gauss(0, noise), z + rng.gauss(0, znoise))
                 for x, y, z in world]
    return Hand(points, world, label, 0.95, 640, 480)


def open_hand(**kw):
    return hand(**kw)


def index_touch(**kw):
    return hand(index=BENT_TO_THUMB, touch=8, **kw)


def middle_touch(**kw):
    return hand(middle=BENT_TO_THUMB, touch=12, **kw)


def peace_sign(**kw):
    """Writing pose: index + middle up, ring + pinky curled, thumb resting on the ring finger."""
    return hand(ring=CURLED, pinky=CURLED, touch=16, **kw)


def fist(**kw):
    return hand(index=CURLED, middle=CURLED, ring=CURLED, pinky=CURLED,
                thumb_at=(0.0, -0.10, 0.045), **kw)


class FakeMouse:
    """Records what the app would do with the mouse (never touches your real mouse)."""

    def __init__(self):
        self.events = []
        self.left_is_down = False
        self.vx, self.vy, self.vw, self.vh = 0, 0, 1920, 1080
        self.last_pos = None

    def refresh_screen(self):
        pass

    def move(self, x, y):
        pos = (int(round(x)), int(round(y)))
        if pos != self.last_pos:
            self.last_pos = pos
            self.events.append(("move",) + pos)

    def _at(self, x, y):
        if x is not None:
            self.move(x, y)

    def left_down(self, x=None, y=None):
        self._at(x, y)
        self.left_is_down = True
        self.events.append(("left_down",) + self.last_pos)

    def left_up(self, x=None, y=None):
        self._at(x, y)
        self.left_is_down = False
        self.events.append(("left_up",) + self.last_pos)

    def click(self, x=None, y=None):
        self.left_down(x, y)
        self.left_up()

    def right_click(self, x=None, y=None):
        self._at(x, y)
        self.events.append(("right_click",) + self.last_pos)

    def scroll(self, amount):
        self.events.append(("scroll", int(amount)))

    def release_all(self):
        if self.left_is_down:
            self.left_up()

    def of(self, kind):
        return [e for e in self.events if e[0] == kind]


class Sim:
    """Feeds frames to the engine at 30 frames per second."""

    def __init__(self, engine):
        self.e = engine
        self.t = 100.0
        self.box = engine._effective_box((480, 640, 3))

    @property
    def mouse(self):
        return self.e.mouse

    def frames(self, make, n):
        """make(i) returns a Hand, or None for 'no hand in view'."""
        for i in range(n):
            self.t += 1 / 30
            h = make(i)
            if h is None:
                self.e._hand_missing(self.t)
            else:
                self.e._hand_seen(h, self.box, self.t)

    def still(self, n=12, **kw):
        self.frames(lambda i: open_hand(**kw), n)
        return self.e.cursor
