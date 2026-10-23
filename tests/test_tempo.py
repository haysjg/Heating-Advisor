"""Tests unitaires pour modules/tempo.py — couleur EDF Tempo et cache local."""

import io
import json
from unittest.mock import patch

import pytest
from freezegun import freeze_time

import modules.tempo as tempo


@pytest.fixture(autouse=True)
def cache_file(tmp_path, monkeypatch):
    monkeypatch.setattr(tempo, "CACHE_FILE", str(tmp_path / "tempo_cache.json"))


def _response(payload):
    return io.BytesIO(json.dumps(payload).encode())


@freeze_time("2026-01-15 10:00:00")
class TestFetchDay:
    def test_published_color_is_cached(self):
        with patch("urllib.request.urlopen", return_value=_response({"dateJour": "2026-01-15", "codeJour": 3})) as net:
            assert tempo.get_today()["color"] == "RED"
            assert tempo.get_today()["color"] == "RED"
        assert net.call_count == 1

    def test_api_down_after_first_fetch_keeps_color(self):
        with patch("urllib.request.urlopen", return_value=_response({"dateJour": "2026-01-15", "codeJour": 3})):
            tempo.get_today()
        with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
            assert tempo.get_today()["color"] == "RED"

    def test_unpublished_tomorrow_not_cached(self):
        with patch("urllib.request.urlopen",
                   side_effect=lambda *a, **k: _response({"dateJour": "2026-01-16", "codeJour": 0})) as net:
            assert tempo.get_tomorrow()["color"] == "UNKNOWN"
            assert tempo.get_tomorrow()["color"] == "UNKNOWN"
        assert net.call_count == 2

    def test_api_down_without_cache_returns_unknown_after_retries(self):
        with patch("urllib.request.urlopen", side_effect=OSError("timeout")) as net:
            assert tempo.get_today()["color"] == "UNKNOWN"
        assert net.call_count == tempo.FETCH_ATTEMPTS

    def test_retry_succeeds(self):
        with patch("urllib.request.urlopen",
                   side_effect=[OSError("timeout"), _response({"dateJour": "2026-01-15", "codeJour": 2})]):
            assert tempo.get_today()["color"] == "WHITE"

    def test_color_for_other_date_not_cached(self):
        with patch("urllib.request.urlopen", return_value=_response({"dateJour": "2026-01-14", "codeJour": 3})):
            tempo.get_today()
        assert tempo._cached_color("2026-01-15") is None

    def test_old_entries_purged(self):
        tempo._store_color("2025-11-01", "BLUE")
        tempo._store_color("2026-01-15", "RED")
        assert tempo._cached_color("2025-11-01") is None
        assert tempo._cached_color("2026-01-15") == "RED"

    def test_tomorrow_cached_becomes_today(self):
        with patch("urllib.request.urlopen", return_value=_response({"dateJour": "2026-01-16", "codeJour": 3})):
            tempo.get_tomorrow()
        with freeze_time("2026-01-16 08:00:00"), patch("urllib.request.urlopen", side_effect=OSError("down")):
            assert tempo.get_today()["color"] == "RED"
