"""Tests unitaires pour modules/tempo.py — couleur EDF Tempo et cache local."""

import io
import json
from unittest.mock import patch

from datetime import date

import pytest
from freezegun import freeze_time

import modules.tempo as tempo


@pytest.fixture(autouse=True)
def cache_file(tmp_path, monkeypatch):
    monkeypatch.setattr(tempo, "CACHE_FILE", str(tmp_path / "tempo_cache.json"))
    monkeypatch.setattr(tempo, "SEASON_FILE", str(tmp_path / "tempo_season.json"))


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


def _season_days(blue=0, white=0, red=0, unknown=0):
    codes = [1] * blue + [2] * white + [3] * red + [0] * unknown
    return [{"dateJour": f"d{i}", "codeJour": c} for i, c in enumerate(codes)]


class TestSeasonOf:
    def test_september_starts_new_season(self):
        assert tempo.season_of(date(2026, 9, 1)) == "2026-2027"

    def test_august_belongs_to_previous_season(self):
        assert tempo.season_of(date(2026, 8, 31)) == "2025-2026"

    def test_winter(self):
        assert tempo.season_of(date(2027, 1, 15)) == "2026-2027"


@freeze_time("2026-11-10 12:00:00")
class TestSeasonStats:
    def test_counts_and_remaining(self):
        payload = _season_days(blue=60, white=8, red=3, unknown=1)
        with patch("urllib.request.urlopen", return_value=_response(payload)) as mock:
            stats = tempo.get_season_stats()
        assert "periode=2026-2027" in mock.call_args[0][0].full_url
        assert stats["label"] == "2026-27"
        assert stats["colors"]["RED"] == {"used": 3, "total": 22, "left": 19}
        assert stats["colors"]["WHITE"] == {"used": 8, "total": 43, "left": 35}
        # 365 jours (sept. 2026 → août 2027) moins 22 rouges et 43 blancs
        assert stats["colors"]["BLUE"] == {"used": 60, "total": 300, "left": 240}

    def test_leap_season_has_one_more_blue_day(self):
        with freeze_time("2027-10-01"), \
             patch("urllib.request.urlopen", return_value=_response([])):
            stats = tempo.get_season_stats()
        assert stats["colors"]["BLUE"]["total"] == 301

    def test_cached_within_refresh_window(self):
        with patch("urllib.request.urlopen", return_value=_response(_season_days(red=2))):
            tempo.get_season_stats()
        with freeze_time("2026-11-10 14:00:00"), \
             patch("urllib.request.urlopen") as mock:
            stats = tempo.get_season_stats()
        mock.assert_not_called()
        assert stats["colors"]["RED"]["used"] == 2

    def test_refreshed_after_window(self):
        with patch("urllib.request.urlopen", return_value=_response(_season_days(red=2))):
            tempo.get_season_stats()
        with freeze_time("2026-11-10 15:30:00"), \
             patch("urllib.request.urlopen", return_value=_response(_season_days(red=3))):
            stats = tempo.get_season_stats()
        assert stats["colors"]["RED"]["used"] == 3

    def test_api_down_keeps_last_counts(self):
        with patch("urllib.request.urlopen", return_value=_response(_season_days(white=5))):
            tempo.get_season_stats()
        with freeze_time("2026-11-11 12:00:00"), \
             patch("urllib.request.urlopen", side_effect=OSError("down")):
            stats = tempo.get_season_stats()
        assert stats["colors"]["WHITE"]["used"] == 5

    def test_api_down_without_cache_returns_none(self):
        with patch("urllib.request.urlopen", side_effect=OSError("down")):
            assert tempo.get_season_stats() is None

    def test_new_season_ignores_previous_counts(self):
        with freeze_time("2026-08-31 12:00:00"), \
             patch("urllib.request.urlopen", return_value=_response(_season_days(red=22))):
            tempo.get_season_stats()
        with freeze_time("2026-09-01 12:00:00"), \
             patch("urllib.request.urlopen", side_effect=OSError("down")):
            assert tempo.get_season_stats() is None

    def test_left_never_negative(self):
        with patch("urllib.request.urlopen", return_value=_response(_season_days(red=25))):
            stats = tempo.get_season_stats()
        assert stats["colors"]["RED"]["left"] == 0
