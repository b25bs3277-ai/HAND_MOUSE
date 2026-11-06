"""
gestures.py
-----------
Turns 21 hand landmarks into ONE gesture name, and makes gestures "stable".

MediaPipe landmark numbers:
    0 = wrist
    thumb : 1  2  3  4(tip)
    index : 5  6  7  8(tip)      5 = knuckle (MCP), 6 = middle joint (PIP)
    middle: 9 10 11 12(tip)
    ring  :13 14 15 16(tip)
    pinky :17 18 19 20(tip)

Gestures:
    PALM   four fingers up, PALM to camera -> move cursor (or lock, if palm lock is on)
    POINT  index up                        -> move cursor
    DRAW   index + middle up, pinky down   -> left button held (writing)
    PINCH  thumb tip touches index tip     -> left click (2 quick taps = double-click)
    MIDDLE thumb tip touches middle tip    -> right click
    BACK   back of the hand to the camera  -> scroll (hold hand above / below start)
    SCROLL index + middle + ring up        -> scroll (old, off by default)
    FIST   all fingers tightly curled      -> left click (old, off by default)
    NONE   anything else (relaxed hand)    -> move cursor

How we decide a finger is "up" (extended):
  We measure how much the finger BENDS at its two middle joints, in 3D.
  A straight finger bends ~0-30 degrees in total, a curled one ~150+.
  We use two thresholds ("hysteresis"): it takes a clear straightening to
  count as up, and a clear bend to count as down. Half-bent fingers keep
  their old state, which stops flickering.

How we decide the thumb TOUCHES a fingertip (see touch_gaps):
  We measure the gap between the thumb's top bone (landmarks 3-4) and the
  finger's top bone (7-8 for index, 11-12 for middle). Touching ANYWHERE near
  the two tips counts, not only the exact tip points. The gap is measured in
  the camera image (most accurate from any angle, also from the side) and
  divided by the hand size, so your distance to the camera does not matter.
  The 3D shape is used as a safety check.
"""

import math

NONE = "none"
POINT = "point"
DRAW = "draw"
PINCH = "pinch"
MIDDLE = "middle"
SCROLL = "scroll"
PALM = "palm"
FIST = "fist"
BACK = "back"

WRIST, THUMB_TIP, INDEX_TIP, MIDDLE_MCP = 0, 4, 8, 9
FINGERS = {                     # knuckle, middle joint, top joint, tip
    "index": (5, 6, 7, 8),
    "middle": (9, 10, 11, 12),
    "ring": (13, 14, 15, 16),
    "pinky": (17, 18, 19, 20),
}

# Bend thresholds in degrees (sum of the two middle joints)
BEND_TO_EXTEND = 70     # straighter than this -> finger becomes "up"
BEND_TO_CURL = 100      # bent more than this  -> finger becomes "down"
BEND_SMOOTHING = 0.5    # 0..1, how much of the new bend to use each frame (calms 3D noise)

# Fist = fingertips tucked into the palm (so a relaxed, half-bent hand is not a fist).
# "reach" = (wrist->tip) / (wrist->middle joint): straight ~1.3, relaxed ~1.1, fist ~0.6-0.8
FIST_REACH_IN = 0.85
FIST_REACH_OUT = 1.0
FIST_MIN_BEND = 110

# Palm or back of the hand? "facing" goes from +1 (palm to camera) to -1 (back to camera).
FACING_SWITCH = 0.2     # must pass +0.2 / -0.2 to switch side (stops flicker when edge-on)

# 3D safety check: if the 3D shape says thumb and finger are this far apart
# (in palm lengths, ~5 cm), it is not a touch even if they overlap in the image.
TOUCH_3D_VETO = 0.6


# -- small math helpers (work for 2D and 3D points) ---------------------------
def _sub(a, b):
    return tuple(x - y for x, y in zip(a, b))


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _length(v):
    return math.sqrt(_dot(v, v))


def _dist(a, b):
    return _length(_sub(a, b))


def _bend(a, b, c):
    """Angle (degrees) the line a->b->c turns at b. 0 = perfectly straight."""
    u, v = _sub(b, a), _sub(c, b)
    lu, lv = _length(u), _length(v)
    if lu < 1e-9 or lv < 1e-9:
        return 0.0
    return math.degrees(math.acos(max(-1.0, min(1.0, _dot(u, v) / (lu * lv)))))


def _segment_gap(p1, q1, p2, q2):
    """Shortest distance between segment p1-q1 and segment p2-q2 (2D or 3D)."""
    d1, d2, r = _sub(q1, p1), _sub(q2, p2), _sub(p1, p2)
    a, e, f = _dot(d1, d1), _dot(d2, d2), _dot(d2, r)
    clamp = lambda v: max(0.0, min(1.0, v))
    if a < 1e-12 and e < 1e-12:
        return _length(r)
    if a < 1e-12:
        s, t = 0.0, clamp(f / e)
    else:
        c = _dot(d1, r)
        if e < 1e-12:
            s, t = clamp(-c / a), 0.0
        else:
            b = _dot(d1, d2)
            denom = a * e - b * b
            s = clamp((b * f - c * e) / denom) if denom > 1e-12 else 0.0
            t = (b * s + f) / e
            if t < 0.0:
                s, t = clamp(-c / a), 0.0
            elif t > 1.0:
                s, t = clamp((b - c) / a), 1.0
    c1 = tuple(p + d * s for p, d in zip(p1, d1))
    c2 = tuple(p + d * t for p, d in zip(p2, d2))
    return _dist(c1, c2)


def touch_gaps(hand):
    """Gap between the thumb tip and (index tip, middle tip), in hand sizes.
    About 0.1-0.2 when touching, 0.5+ when apart (your numbers show in the preview)."""
    w, h = hand.img_w, hand.img_h
    img = [(x * w, y * h) for x, y, _ in hand.points]
    # hand size in the image: longest of these stays big when the hand turns sideways
    size = max(_dist(img[0], img[9]), _dist(img[0], img[5]), _dist(img[0], img[17]),
               1.25 * _dist(img[5], img[17]), 1e-6)
    p3 = hand.shape_points()
    size3 = max(_dist(p3[WRIST], p3[MIDDLE_MCP]), 1e-6)
    gaps = []
    for top_joint, tip in ((7, 8), (11, 12)):
        gap = _segment_gap(img[3], img[4], img[top_joint], img[tip]) / size
        gap3 = _segment_gap(p3[3], p3[4], p3[top_joint], p3[tip]) / size3
        if gap3 > TOUCH_3D_VETO:
            gap = max(gap, gap3)      # clearly apart in 3D -> not a touch
        gaps.append(gap)
    return gaps[0], gaps[1]


class GestureClassifier:
    """Looks at ONE frame and names the gesture (no time smoothing here)."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.extended = {name: False for name in FINGERS}
        self.smooth_bend = {}
        self.pinching = False          # thumb touching index
        self.middle_touching = False   # thumb touching middle
        self.fist_closed = False
        self.back_facing = False

    def classify(self, hand, pinch_on=0.20, pinch_off=0.30, allow_pinch=True, mirrored=True,
                 allow_middle=True, middle_on=0.20, middle_off=0.30):
        """Returns (gesture_name, info_dict)."""
        p = hand.shape_points()
        palm = max(_dist(p[WRIST], p[MIDDLE_MCP]), 1e-6)   # palm length = our 3D ruler

        bends, reaches = {}, {}
        for name, (mcp, pip, dip, tip) in FINGERS.items():
            bend = _bend(p[mcp], p[pip], p[dip]) + _bend(p[pip], p[dip], p[tip])
            if name in self.smooth_bend:      # blend with last frame to calm the noise
                bend = BEND_SMOOTHING * bend + (1 - BEND_SMOOTHING) * self.smooth_bend[name]
            self.smooth_bend[name] = bend
            # "reach": is the tip further from the wrist than the middle joint?
            reach = _dist(p[WRIST], p[tip]) / max(_dist(p[WRIST], p[pip]), 1e-6)
            bends[name], reaches[name] = bend, reach
            if self.extended[name]:
                self.extended[name] = bend < BEND_TO_CURL and reach > 1.05
            else:
                self.extended[name] = bend < BEND_TO_EXTEND and reach > 1.15
        i, m, r, k = (self.extended[n] for n in ("index", "middle", "ring", "pinky"))

        # Fist: all fingertips tucked in (average over the 4 fingers = less noise)
        avg_reach = sum(reaches.values()) / 4
        avg_bend = sum(bends.values()) / 4
        if any((i, m, r, k)) or avg_reach > FIST_REACH_OUT:
            self.fist_closed = False
        elif avg_reach < FIST_REACH_IN and avg_bend > FIST_MIN_BEND:
            self.fist_closed = True

        # ---- thumb touches ----
        index_gap, middle_gap = touch_gaps(hand)
        # In a fist the thumb lies on the curled fingers: that is NOT a touch.
        # A finger that touches the thumb still sticks out past its knuckle (seen in
        # the image, which is reliable); a curled fist finger does not.
        img = [(x * hand.img_w, y * hand.img_h) for x, y, _ in hand.points]

        def sticks_out(tip, knuckle):
            return _dist(img[tip], img[WRIST]) > _dist(img[knuckle], img[WRIST])

        index_ok, middle_ok = sticks_out(8, 5), sticks_out(12, 9)
        was_i, was_m = self.pinching, self.middle_touching
        # The finger nearest to the thumb wins (a curled finger does not count).
        now_i = index_gap < pinch_off if was_i else (
            index_ok and index_gap < pinch_on and (index_gap <= middle_gap or not middle_ok))
        now_m = middle_gap < middle_off if was_m else (
            middle_ok and middle_gap < middle_on and (middle_gap < index_gap or not index_ok))
        if now_i and now_m:            # only one at a time: keep the one that came first
            if was_m and not was_i:
                now_i = False
            else:
                now_m = False
        self.pinching, self.middle_touching = now_i, now_m
        # thumb close to a fingertip (but maybe not touching yet)
        near_touch = ((allow_pinch and index_gap < pinch_off)
                      or (allow_middle and middle_gap < middle_off))

        # ---- palm or back? ----
        # Turn from (wrist -> index knuckle) to (wrist -> pinky knuckle) in the image:
        # it turns one way for the palm side, the other way for the back.
        # A left hand is a mirror image, so its sign is flipped (same for a non-mirrored camera).
        pts, w, h = hand.points, hand.img_w, hand.img_h
        ax, ay = (pts[5][0] - pts[0][0]) * w, (pts[5][1] - pts[0][1]) * h
        bx, by = (pts[17][0] - pts[0][0]) * w, (pts[17][1] - pts[0][1]) * h
        facing = (ax * by - ay * bx) / max(math.hypot(ax, ay) * math.hypot(bx, by), 1e-6)
        if hand.label == "Left":
            facing = -facing
        if not mirrored:
            facing = -facing
        if facing < -FACING_SWITCH:
            self.back_facing = True
        elif facing > FACING_SWITCH:
            self.back_facing = False

        if allow_pinch and self.pinching:
            gesture = PINCH
        elif allow_middle and self.middle_touching:
            gesture = MIDDLE
        elif self.back_facing and (i + m + r + k) >= 3:
            gesture = BACK                 # 3 of 4 fingers up is enough from the back
        elif i and m and r and k:
            gesture = PALM
        elif i and m and r:
            gesture = SCROLL
        elif i and m and not k and not near_touch:
            # Pinky down, thumb away from the fingertips: writing pose.
            # (While the thumb moves to a fingertip for a click, we must not start writing.)
            gesture = DRAW
        elif i:
            gesture = POINT
        elif self.fist_closed:
            gesture = FIST
        else:
            gesture = NONE

        info = {
            "fingers": (i, m, r, k),
            "index_gap": index_gap,
            "middle_gap": middle_gap,
            "pinch_ratio": index_gap,      # old name, kept for compatibility
            "near_touch": near_touch,
            "bends": bends,
            "fist_reach": avg_reach,
            "facing": facing,
        }
        return gesture, info


class Debouncer:
    """A new gesture only 'counts' after it was seen N frames in a row.

    stable    : the gesture we are acting on right now
    candidate : a different gesture we are currently seeing (not trusted yet)
    """

    def __init__(self):
        self.reset()

    def reset(self, gesture=NONE):
        self.stable = gesture
        self.candidate = None
        self.count = 0

    def update(self, raw, needed):
        """Feed this frame's raw gesture. Returns True when `stable` just changed."""
        if raw == self.stable:
            self.candidate = None
            self.count = 0
            return False
        if raw == self.candidate:
            self.count += 1
        else:
            self.candidate = raw
            self.count = 1
        if self.count >= max(1, needed):
            self.stable = raw
            self.candidate = None
            self.count = 0
            return True
        return False
