"""pytest setup: run the app's modules from the project folder, with a fake mouse and one fixed screen."""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import engine as engine_module          # noqa: E402
import helpers                          # noqa: E402
from config import Config               # noqa: E402
from mouse_control import Monitor       # noqa: E402


@pytest.fixture(autouse=True)
def _same_random_numbers():
    helpers.rng.seed(1234)


@pytest.fixture
def make_engine(tmp_path, monkeypatch):
    """make_engine(**settings) -> Engine with a FakeMouse and a 1920x1080 screen."""
    monkeypatch.setattr(engine_module, "list_monitors", lambda: [Monitor(0, 0, 1920, 1080, True)])

    def _make(**settings):
        cfg = Config.load(str(tmp_path / "settings.json"))
        for key, value in settings.items():
            cfg.set(key, value)
        eng = engine_module.Engine(cfg)
        eng.mouse = helpers.FakeMouse()
        eng._update_screen(0)
        return eng

    return _make


@pytest.fixture
def sim(make_engine):
    return helpers.Sim(make_engine())
