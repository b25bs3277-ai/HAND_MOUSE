"""Gesture recognition on synthetic hands (no camera needed)."""

import pytest

from gestures import (BACK, DRAW, FIST, MIDDLE, PALM, PINCH, Debouncer, GestureClassifier,
                      touch_gaps)
from helpers import (CURLED, fist, hand, index_touch, middle_touch, open_hand, peace_sign)


def recognised(make, frames=3):
    """Gesture after a few frames of the same pose (like a real, short video)."""
    c = GestureClassifier()
    for _ in range(frames):
        gesture, info = c.classify(make())
    return gesture, info


def rate(make, expected, runs=150):
    return sum(recognised(make)[0] == expected for _ in range(runs)) / runs


@pytest.mark.parametrize("name, make, expected", [
    ("open hand", lambda: open_hand(noise=0.0015), PALM),
    ("thumb + index tips", lambda: index_touch(noise=0.0015), PINCH),
    ("thumb + middle tips", lambda: middle_touch(noise=0.0015), MIDDLE),
    ("peace sign (writing)", lambda: peace_sign(noise=0.0015), DRAW),
    ("fist", lambda: fist(noise=0.0015), FIST),
    ("back of hand", lambda: hand(back=True, noise=0.0015), BACK),
    ("back of hand, pinky bent", lambda: hand(pinky=CURLED, back=True, noise=0.0015), BACK),
    ("left hand, palm (mirror image)", lambda: hand(back=True, label="Left"), PALM),
])
def test_each_pose_is_recognised(name, make, expected):
    assert rate(make, expected) >= 0.97, name


@pytest.mark.parametrize("yaw", [0, 30, 60, 75])
def test_touches_work_from_the_side(yaw):
    """Hand turned sideways + MediaPipe-like depth noise: touches are still found, open hand is not a touch."""
    noisy = dict(yaw_deg=yaw, noise=0.0015, znoise=0.015)
    assert rate(lambda: index_touch(**noisy), PINCH) >= 0.97
    assert rate(lambda: middle_touch(**noisy), MIDDLE) >= 0.97
    assert rate(lambda: open_hand(**noisy), PINCH) == 0.0


@pytest.mark.parametrize("gap_m", [0.02, 0.03])
def test_thumb_close_but_not_touching_is_not_a_click(gap_m):
    make = lambda: hand(index=(20, 60, 90), touch=8, gap=gap_m, noise=0.0015)
    assert rate(make, PINCH) <= 0.02


def test_fist_with_thumb_on_fingers_is_not_a_touch():
    gesture, info = recognised(lambda: fist())
    assert gesture not in (PINCH, MIDDLE)
    assert info["middle_gap"] < 0.3          # thumb lies right on the fingers - still no click


def test_touch_gap_numbers():
    touching = touch_gaps(index_touch())
    apart = touch_gaps(open_hand())
    assert touching[0] < 0.2 < 0.5 < apart[0]


def test_debouncer_needs_stable_frames():
    d = Debouncer()
    seen = []
    for g in ["point"] * 4 + ["draw", "point"] + ["draw"] * 4:
        d.update(g, 4)
        seen.append(d.stable)
    assert seen[:3] == ["none"] * 3 and seen[3] == "point"      # 4 frames in a row
    assert seen[4:9] == ["point"] * 5                           # 1-frame "draw" is ignored
    assert seen[9] == "draw"
