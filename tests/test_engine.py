"""What the mouse does for each gesture: clicks, double-click, hold-to-drag, right click,
writing, smooth scrolling, steady aiming. Runs the real engine with a fake mouse."""

import math

from helpers import (CURLED, Sim, hand, index_touch, middle_touch, open_hand, peace_sign)


def test_tap_is_one_click_when_you_let_go(sim):
    sim.still()
    sim.frames(lambda i: index_touch(), 4)               # touch...
    assert not sim.mouse.of("left_down"), "no click while still touching"
    sim.frames(lambda i: open_hand(), 6)                 # ...let go
    assert len(sim.mouse.of("left_down")) == 1 and len(sim.mouse.of("left_up")) == 1


def test_two_quick_taps_are_a_double_click_on_the_same_pixel(sim):
    sim.still()
    taps = [index_touch] * 3 + [open_hand] * 3 + [index_touch] * 3 + [open_hand] * 10
    times = []
    original = sim.mouse.left_down
    sim.mouse.left_down = lambda *a: (times.append(sim.t), original(*a))
    sim.frames(lambda i: taps[i](cx=0.5 + (0.004 if i > 4 else 0)), len(taps))
    downs = sim.mouse.of("left_down")
    assert len(downs) == 2
    assert downs[0][1:] == downs[1][1:], "both clicks on the same pixel"
    assert times[1] - times[0] < 0.4, "fast enough for Windows' double-click"


def test_touch_and_hold_keeps_the_button_down_to_drag(make_engine):
    sim = Sim(make_engine(hold_ms=400))
    sim.still()
    sim.frames(lambda i: index_touch(), 18)               # hold 0.6 s, hand still
    assert len(sim.mouse.of("left_down")) == 1 and not sim.mouse.of("left_up")
    assert sim.e.dragging
    n = len(sim.mouse.events)
    sim.frames(lambda i: index_touch(cx=0.5 + i * 0.004), 20)   # drag to the right
    dragged = [e for e in sim.mouse.events[n:] if e[0] == "move"]
    assert len(dragged) > 10 and dragged[-1][1] > dragged[0][1] + 150
    assert not sim.mouse.of("left_up"), "button stays down while you hold"
    sim.frames(lambda i: open_hand(cx=0.58), 8)           # let go
    assert len(sim.mouse.of("left_up")) == 1 and len(sim.mouse.of("left_down")) == 1
    assert not sim.e.dragging


def test_losing_the_hand_while_dragging_releases_the_button(sim):
    sim.still()
    sim.frames(lambda i: index_touch(), 20)
    assert sim.e.dragging
    sim.frames(lambda i: None, 8)
    assert len(sim.mouse.of("left_up")) == 1 and not sim.e.dragging


def test_click_lands_where_you_aimed_even_if_the_hand_drifts(sim):
    sim.frames(lambda i: open_hand(cx=0.45 + i * 0.003), 15)
    aim = sim.e.cursor

    def thumb_coming_in(i, n=6):
        t = min(1.0, (i + 1) / n)
        return hand(index=(20, 60, 90), touch=8, gap=0.06 * (1 - t) + 0.006 * t, cx=0.495 + i * 0.003)

    sim.frames(thumb_coming_in, 9)
    sim.frames(lambda i: open_hand(cx=0.522 + i * 0.003), 10)
    click = sim.mouse.of("left_down")[0]
    assert abs(click[1] - aim[0]) < 5, (click, aim)


def test_cursor_glides_back_instead_of_jumping_after_a_click(sim):
    sim.still()
    sim.frames(lambda i: index_touch(), 3)
    sim.frames(lambda i: open_hand(cx=0.5 + i * 0.004), 30)   # hand keeps moving after the tap
    moves = sim.mouse.events[sim.mouse.events.index(sim.mouse.of("left_up")[0]) + 1:]
    xs = [e[1] for e in moves if e[0] == "move"]
    steps = [b - a for a, b in zip(xs, xs[1:])]
    total = xs[-1] - xs[0]
    assert total > 300 and max(steps) < 0.35 * total, (max(steps), total)


def test_thumb_and_middle_is_one_right_click_and_never_writes(sim):
    aim = sim.still()

    def middle_coming_in(i, n=8):
        t = min(1.0, (i + 1) / n)
        bend = tuple(a * t for a in (20, 60, 90))
        curl = tuple(a * t for a in CURLED)
        return hand(middle=bend, ring=curl, pinky=curl, touch=12, gap=0.07 * (1 - t) + 0.006 * t)

    sim.frames(middle_coming_in, 20)
    rights = sim.mouse.of("right_click")
    assert len(rights) == 1 and not sim.mouse.of("left_down")
    assert abs(rights[0][1] - aim[0]) < 5


def test_peace_sign_writes_one_stroke(sim):
    sim.still()
    sim.frames(lambda i: peace_sign(cx=0.5 + i * 0.003), 25)
    sim.frames(lambda i: hand(middle=CURLED, ring=CURLED, pinky=CURLED, cx=0.575), 8)
    assert len(sim.mouse.of("left_down")) == 1 and len(sim.mouse.of("left_up")) == 1
    assert not sim.mouse.of("right_click")


def test_back_of_hand_scrolls_smoothly(sim):
    sim.still()
    sim.frames(lambda i: hand(back=True, cy=0.60), 10)           # turn hand: start line here
    moves_before = len(sim.mouse.of("move"))
    sim.frames(lambda i: hand(back=True, cy=0.58), 30)           # inside the start band
    assert not sim.mouse.of("scroll")
    sim.frames(lambda i: hand(back=True, cy=0.50), 45)           # hold higher: scroll up
    up = sim.mouse.of("scroll")
    sim.frames(lambda i: hand(back=True, cy=0.70), 60)           # hold lower: scroll down
    down = sim.mouse.of("scroll")[len(up):]
    assert sum(a for _, a in up) > 300 and sum(a for _, a in down) < -300
    # smooth: many small steps (not a few big wheel jumps), speed eases in
    assert len(up) > 20 and max(abs(a) for _, a in up) <= 60
    assert abs(up[0][1]) <= abs(up[len(up) // 2][1])
    assert len(sim.mouse.of("move")) == moves_before, "cursor stays put while scrolling"


def test_scroll_whole_steps_option(make_engine):
    sim = Sim(make_engine(smooth_scroll=False))
    sim.still()
    sim.frames(lambda i: hand(back=True, cy=0.60), 10)
    sim.frames(lambda i: hand(back=True, cy=0.45), 45)
    amounts = [a for _, a in sim.mouse.of("scroll")]
    assert amounts and all(a % 120 == 0 for a in amounts)


def test_relaxed_thumb_never_clicks(sim):
    sim.frames(lambda i: hand(index=(10, 30, 40), thumb_at=(0.0, -0.06, 0.03), noise=0.0015,
                              cx=0.4 + (i % 60) * 0.003), 300)
    assert not sim.mouse.of("left_down") and not sim.mouse.of("right_click")
    assert len(sim.mouse.of("move")) > 250


def test_thumb_hovering_near_a_finger_does_not_make_the_cursor_stutter(sim):
    sim.still()
    n = len(sim.mouse.of("move"))

    def hover(i):
        g = 0.024 + 0.006 * math.sin(i / 3.0)
        return hand(index=(20, 60, 90), touch=8, gap=g, noise=0.0015, cx=0.4 + (i % 90) * 0.002)

    sim.frames(hover, 150)
    assert len(sim.mouse.of("move")) - n >= 120 and not sim.mouse.of("left_down")


def test_still_hand_means_still_cursor(sim):
    sim.frames(lambda i: open_hand(noise=0.0015), 20)
    n = len(sim.mouse.of("move"))
    sim.frames(lambda i: open_hand(noise=0.0015), 150)
    assert len(sim.mouse.of("move")) - n <= 15


def test_touch_calibration_fits_your_hand(sim):
    e = sim.e
    e._begin_touch_calibration()
    for make, seconds in [(open_hand, 2.6), (open_hand, 2.6), (index_touch, 3.1),
                          (middle_touch, 3.1), (open_hand, 4.2)]:
        for _ in range(int(seconds * 30)):
            sim.t += 1 / 30
            e._calibration_step(make(noise=0.0015), sim.t)
    assert not e.calibrating and e.calib_message.startswith("Saved")
    assert e.cfg["pinch_on"] < e.cfg["pinch_off"] < 0.5
    assert e.cfg["middle_on"] < e.cfg["middle_off"] < 0.5
