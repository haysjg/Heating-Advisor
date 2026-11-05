"""Thème du fond animé : validation /config et attribut posé par base.html."""

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
        module = importlib.import_module("app")
    module.app.config["TESTING"] = True
    return module


@pytest.mark.parametrize("ui, expected", [
    ({"background_theme": None}, "aurora"),
    ({"background_theme": None, "background_animation": False}, "none"),
    ({"background_theme": "thermal", "background_animation": False}, "thermal"),
    ({"background_theme": "auto"}, "auto"),
])
def test_base_html_theme(app_module, monkeypatch, ui, expected):
    monkeypatch.setattr(app_module.config, "UI", ui)
    from flask import render_template_string
    with app_module.app.test_request_context("/"):
        out = render_template_string('{% extends "base.html" %}', config=app_module.config)
    assert f'data-bg-theme="{expected}"' in out
    assert 'data-lat="' in out and "/static/background.js" in out
    assert f'data-hp="{app_module.config.HP_START}-{app_module.config.HP_END}"' in out


def test_theme_validation(app_module, monkeypatch):
    monkeypatch.setattr(app_module.config, "UI", {"background_theme": "sky"})
    assert app_module._background_theme({}) == "sky"
    assert app_module._background_theme({"background_theme": "fireside"}) == "fireside"
    with pytest.raises(ValueError):
        app_module._background_theme({"background_theme": "disco"})


def test_legacy_disabled_kept(app_module, monkeypatch):
    monkeypatch.setattr(app_module.config, "UI", {"background_theme": None, "background_animation": False})
    assert app_module._background_theme({}) == "none"
