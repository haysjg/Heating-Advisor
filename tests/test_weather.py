"""Tests de la récupération de la température extérieure (fallbacks)."""

import urllib.error
from unittest.mock import patch

import pytest
from freezegun import freeze_time

import modules.weather as weather

LOCATION = {
    "city": "X", "postal_code": "00000", "latitude": 48.7, "longitude": 2.2,
    "meteociel_url": "https://example.invalid/obs",
}


@pytest.fixture(autouse=True)
def reset_state():
    weather._last_known = None
    weather._meteociel_blocked_until = None
    yield
    weather._last_known = None
    weather._meteociel_blocked_until = None


class TestLastKnownFallback:
    def test_reuses_recent_value_when_sources_fail(self):
        with freeze_time("2026-10-31 10:07:00"), \
             patch("modules.weather.get_temperature_meteociel", return_value=None), \
             patch("modules.weather.get_temperature_openmeteo", return_value=10.4):
            assert weather.get_current_temperature(LOCATION)["temperature"] == 10.4
        with freeze_time("2026-10-31 10:27:00"), \
             patch("modules.weather.get_temperature_meteociel", return_value=None), \
             patch("modules.weather.get_temperature_openmeteo", return_value=None):
            result = weather.get_current_temperature(LOCATION)
        assert result["temperature"] == 10.4
        assert "dernière valeur" in result["source"]

    def test_stale_value_not_reused(self):
        with freeze_time("2026-10-31 06:00:00"), \
             patch("modules.weather.get_temperature_meteociel", return_value=8.0):
            weather.get_current_temperature(LOCATION)
        with freeze_time("2026-10-31 09:30:00"), \
             patch("modules.weather.get_temperature_meteociel", return_value=None), \
             patch("modules.weather.get_temperature_openmeteo", return_value=None):
            result = weather.get_current_temperature(LOCATION)
        assert result["temperature"] is None
        assert result["error"]


class TestMeteocielBackoff:
    def test_429_pauses_scraping(self):
        err = urllib.error.HTTPError(LOCATION["meteociel_url"], 429, "Too Many Requests", {}, None)
        with freeze_time("2026-10-31 10:00:00"), \
             patch("urllib.request.urlopen", side_effect=err) as urlopen:
            assert weather.get_temperature_meteociel("X", "0", LOCATION["meteociel_url"]) is None
            assert weather.get_temperature_meteociel("X", "0", LOCATION["meteociel_url"]) is None
        assert urlopen.call_count == 1

    def test_scraping_resumes_after_pause(self):
        err = urllib.error.HTTPError(LOCATION["meteociel_url"], 429, "Too Many Requests", {}, None)
        with freeze_time("2026-10-31 10:00:00"), patch("urllib.request.urlopen", side_effect=err):
            weather.get_temperature_meteociel("X", "0", LOCATION["meteociel_url"])
        with freeze_time("2026-10-31 11:01:00"), \
             patch("urllib.request.urlopen", side_effect=err) as urlopen:
            weather.get_temperature_meteociel("X", "0", LOCATION["meteociel_url"])
        assert urlopen.call_count == 1
