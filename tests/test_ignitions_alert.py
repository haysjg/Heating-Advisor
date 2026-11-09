"""Alerte ntfy sur les allumages répétés du poêle (app._notify_ignitions)."""

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


@pytest.fixture
def sent(app_module, monkeypatch):
    calls = []
    monkeypatch.setattr("modules.ntfy_push.send", lambda title, msg, cfg: calls.append((title, msg)))
    monkeypatch.setitem(app_module.config.POELE, "ignition_alert_daily", 4)
    return calls


def _stats(app_module, monkeypatch, today, avg=1.5):
    monkeypatch.setattr(app_module.history_module, "get_ignition_stats",
                        lambda: {"today": today, "avg_per_day_7d": avg, "avg_cycle_minutes_7d": 240,
                                 "season": 40, "since": "2026-10-01"})


def test_no_alert_below_threshold(app_module, monkeypatch, sent):
    _stats(app_module, monkeypatch, 3)
    app_module._notify_ignitions()
    assert sent == []


@pytest.mark.parametrize("today", [4, 5])
def test_alert_from_fourth_ignition(app_module, monkeypatch, sent, today):
    _stats(app_module, monkeypatch, today)
    app_module._notify_ignitions()
    assert len(sent) == 1
    assert f"{today}ᵉ allumage" in sent[0][0]
    assert "moyenne 1,5/jour" in sent[0][1]
