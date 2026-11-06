"""Smoothing filter and settings.json handling."""

import json
import math
import random

from config import DEFAULTS, Config
from smoothing import PointFilter


def test_filter_steadies_a_shaky_still_hand():
    rnd = random.Random(1)
    f = PointFilter(min_cutoff=0.6, beta=0.015, dead_zone_px=4.0)
    t, last, moves = 0.0, None, 0
    for i in range(300):
        t += 1 / 30
        out = f.update(500 + rnd.gauss(0, 6), 500 + rnd.gauss(0, 6), t)
        if last and i > 30 and math.dist(out, last) > 0.5:
            moves += 1
        last = out
    assert moves < 0.4 * 270


def test_filter_follows_a_writing_stroke_with_little_lag():
    f = PointFilter(min_cutoff=0.6, beta=0.015, dead_zone_px=4.0)
    t = 0.0
    for _ in range(60):
        t += 1 / 30
        x, _ = f.update(200 + 300 * t, 300, t)
    assert (200 + 300 * t) - x < 40          # pixels behind at 300 px/s


def test_broken_settings_fall_back_to_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"beta": "oops", "stable_frames": 99, "hand": "LEFT", "mirror": 1}')
    cfg = Config.load(str(path))
    assert cfg["beta"] == DEFAULTS["beta"] and cfg["stable_frames"] == 15
    assert cfg["hand"] == "left" and cfg["mirror"] is True


def test_missing_settings_file_is_created(tmp_path):
    path = tmp_path / "settings.json"
    Config.load(str(path))
    assert json.loads(path.read_text())["settings_version"] == DEFAULTS["settings_version"]


def test_upgrade_from_version_3_keeps_your_box(tmp_path):
    path = tmp_path / "settings.json"
    old = {k: v for k, v in DEFAULTS.items() if k not in ("middle_on", "middle_off", "hold_ms")}
    old.update(settings_version=3, pinch_on=0.34, pinch_off=0.45, enable_ring_right_click=True,
               tap_frames=2, active_box={"x": 0.3, "y": 0.5, "w": 0.4, "h": 0.5})
    path.write_text(json.dumps(old))
    cfg = Config.load(str(path))
    saved = json.loads(path.read_text())
    assert cfg["pinch_on"] == DEFAULTS["pinch_on"]          # new touch measure -> new numbers
    assert cfg["active_box"]["y"] == 0.5 and cfg["tap_frames"] == 2
    assert cfg["hold_ms"] == DEFAULTS["hold_ms"]
    assert saved["settings_version"] == DEFAULTS["settings_version"]
    assert "enable_ring_right_click" not in saved


def test_upgrade_from_version_4_keeps_your_touch_calibration(tmp_path):
    path = tmp_path / "settings.json"
    old = {k: v for k, v in DEFAULTS.items() if k not in ("hold_ms", "smooth_scroll")}
    old.update(settings_version=4, pinch_on=0.17, pinch_off=0.26)
    path.write_text(json.dumps(old))
    cfg = Config.load(str(path))
    assert cfg["pinch_on"] == 0.17 and cfg["pinch_off"] == 0.26
    cfg.set("pinch_on", 0.15)
    cfg.save()
    assert Config.load(str(path))["pinch_on"] == 0.15, "upgrade happens only once"
