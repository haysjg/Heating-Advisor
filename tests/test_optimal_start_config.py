"""Validation des réglages du démarrage optimisé envoyés par /config."""

import importlib
import threading
from unittest import mock

import pytest

pytest.importorskip("flask_limiter")
pytest.importorskip("apscheduler")


@pytest.fixture(scope="module")
def app_module():
    from apscheduler.schedulers.background import BackgroundScheduler
    with mock.patch.object(BackgroundScheduler, "start"), mock.patch.object(threading.Thread, "start"):
        return importlib.import_module("app")


def test_comfort_optional(app_module):
    out = app_module._schedule_with_comfort({
        "mon": {"start": "05:45", "end": "22:00", "comfort": "06:30"},
        "tue": {"start": "05:45", "end": "22:00", "comfort": ""},
    })
    assert out["mon"]["comfort"] == "06:30"
    assert "comfort" not in out["tue"]


def test_comfort_equal_to_start_allowed(app_module):
    out = app_module._schedule_with_comfort({"mon": {"start": "05:45", "end": "22:00", "comfort": "05:45"}})
    assert out["mon"]["comfort"] == "05:45"


@pytest.mark.parametrize("comfort", ["05:30", "6h"])
def test_comfort_rejected(app_module, comfort):
    with pytest.raises(ValueError):
        app_module._schedule_with_comfort({"mon": {"start": "05:45", "end": "22:00", "comfort": comfort}})


def test_settings(app_module):
    assert app_module._optimal_start_settings({"optimal_start_mode": "pilot", "optimal_start_margin": "20"}) == {
        "mode": "pilot", "safety_margin_minutes": 20}
    with pytest.raises(ValueError):
        app_module._optimal_start_settings({"optimal_start_mode": "turbo"})
    with pytest.raises(ValueError):
        app_module._optimal_start_settings({"optimal_start_margin": 500})
