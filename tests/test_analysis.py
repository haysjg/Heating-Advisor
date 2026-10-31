"""Tests de modules/analysis.py : configuration partagée et cache données / décision."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from freezegun import freeze_time

from modules import analysis, tempo as tempo_module
from modules.thermostat import check_and_apply


@pytest.fixture
def settings(base_config):
    """Module `config` simulé (attributs), avec la règle de nuit activée."""
    return SimpleNamespace(**{**base_config, "TARGET_TEMP": 19.5, "NO_HEATING_AT_NIGHT": True})


def _day(color):
    return tempo_module._day_result(color, {})


@pytest.fixture
def raw():
    """Données externes en cache, récupérées à 05:30 (HC), jour Bleu, 5 °C dehors."""
    return {
        "fetched_at": "2026-01-15T05:30:00",
        "weather": {"temperature": 5.0},
        "tomorrow_weather": {"temperature": 4.0},
        "tempo": tempo_module.at_time({"today": _day("BLUE"), "tomorrow": _day("WHITE")}, 6, 22,
                                      datetime(2026, 1, 15, 5, 30)),
        "hourly_forecast": [],
        "indoor": {"temperature": 19.0},
    }


class TestBuildAdvisorConfig:
    def test_contains_target_temp(self, settings):
        cfg = analysis.build_advisor_config(settings)
        assert cfg["TARGET_TEMP"] == 19.5

    def test_target_temp_used_by_decision(self, settings, raw):
        """Avec 21 °C par défaut, 20 °C dehors chaufferait ; avec 19.5 °C configuré, non."""
        raw["weather"] = {"temperature": 20.0}
        cfg = analysis.build_advisor_config(settings)
        result = analysis.compute_analysis(raw, cfg, datetime(2026, 1, 15, 10, 0))
        assert result["recommendation"]["system"] == "none"

    def test_night_rule_not_passed(self, settings):
        cfg = analysis.build_advisor_config(settings)
        assert not cfg.get("NO_HEATING_AT_NIGHT")

    def test_notify_and_app_share_builder(self):
        import notify
        assert notify.build_advisor_config is analysis.build_advisor_config


class TestComputeAnalysis:
    def test_0550_is_hc_and_heating_allowed(self, settings, raw):
        """05:50 : tarif HC (en vigueur jusqu'à 06:00) et une vraie recommandation,
        pour que le thermostat puisse allumer dès le début de sa plage de 05:45."""
        cfg = analysis.build_advisor_config(settings)
        result = analysis.compute_analysis(raw, cfg, datetime(2026, 1, 15, 5, 50))
        assert result["tempo"]["current_period"] == "HC"
        assert result["clim"]["elec_price_kwh"] == cfg["TEMPO_PRICES"]["BLUE"]["HC"]
        assert result["recommendation"]["system"] in ("poele", "clim")

    def test_0600_is_hp(self, settings, raw):
        cfg = analysis.build_advisor_config(settings)
        result = analysis.compute_analysis(raw, cfg, datetime(2026, 1, 15, 6, 0))
        assert result["tempo"]["current_period"] == "HP"
        assert result["clim"]["elec_price_kwh"] == cfg["TEMPO_PRICES"]["BLUE"]["HP"]

    def test_timestamp_is_fetch_time(self, settings, raw):
        cfg = analysis.build_advisor_config(settings)
        result = analysis.compute_analysis(raw, cfg, datetime(2026, 1, 15, 7, 0))
        assert result["timestamp"] == "2026-01-15T05:30:00"
        assert result["indoor"] == {"temperature": 19.0}
        assert "tomorrow" in result and "hourly_forecast" in result

    def test_raw_tempo_not_mutated(self, settings, raw):
        cfg = analysis.build_advisor_config(settings)
        analysis.compute_analysis(raw, cfg, datetime(2026, 1, 15, 7, 0))
        assert raw["tempo"]["current_period"] == "HC"


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class TestDataCache:
    def _cache(self, raw, now_box, clock=None, ttl=1800):
        calls = []

        def fetch():
            calls.append(1)
            return raw

        cache = analysis.DataCache(
            fetch=fetch,
            ttl=lambda: ttl,
            slot=lambda: analysis.tariff_slot(6, 22, now_box["now"]),
            clock=clock or FakeClock(),
        )
        return cache, calls

    def test_decision_recomputed_hc_to_hp_without_refetch(self, settings, raw, monkeypatch):
        """Bascule HC → HP : la décision change aussitôt, sans attendre un nouvel appel API."""
        now_box = {"now": datetime(2026, 1, 15, 5, 50)}
        cache, calls = self._cache(raw, now_box)
        # Empêche le rafraîchissement en arrière-plan déclenché par la bascule
        monkeypatch.setattr(cache, "refresh_in_background", lambda: None)
        cfg = analysis.build_advisor_config(settings)

        before = analysis.compute_analysis(cache.get(), cfg, now_box["now"])
        now_box["now"] = datetime(2026, 1, 15, 6, 5)
        after = analysis.compute_analysis(cache.get(), cfg, now_box["now"])

        assert len(calls) == 1
        assert before["tempo"]["current_period"] == "HC"
        assert after["tempo"]["current_period"] == "HP"
        assert before["clim"]["elec_price_kwh"] != after["clim"]["elec_price_kwh"]

    def test_fresh_within_ttl_and_slot(self, raw):
        now_box = {"now": datetime(2026, 1, 15, 10, 0)}
        clock = FakeClock()
        cache, calls = self._cache(raw, now_box, clock)
        cache.get()
        clock.t += 600
        now_box["now"] = datetime(2026, 1, 15, 10, 10)
        assert cache.is_fresh()
        cache.get()
        assert len(calls) == 1

    def test_stale_after_ttl(self, raw):
        now_box = {"now": datetime(2026, 1, 15, 10, 0)}
        clock = FakeClock()
        cache, _ = self._cache(raw, now_box, clock)
        cache.get()
        clock.t += 1801
        assert not cache.is_fresh()

    @pytest.mark.parametrize("before,after", [
        (datetime(2026, 1, 15, 5, 59), datetime(2026, 1, 15, 6, 0)),     # HC → HP
        (datetime(2026, 1, 15, 21, 59), datetime(2026, 1, 15, 22, 0)),   # HP → HC
        (datetime(2026, 1, 15, 23, 59), datetime(2026, 1, 16, 0, 0)),    # changement de jour
    ])
    def test_stale_on_tariff_switch(self, raw, before, after):
        now_box = {"now": before}
        cache, _ = self._cache(raw, now_box)
        cache.get()
        assert cache.is_fresh()
        now_box["now"] = after
        assert not cache.is_fresh()

    def test_stale_served_while_refreshing_in_background(self, raw, monkeypatch):
        now_box = {"now": datetime(2026, 1, 15, 10, 0)}
        clock = FakeClock()
        cache, calls = self._cache(raw, now_box, clock)
        cache.get()
        triggered = []
        monkeypatch.setattr(cache, "refresh_in_background", lambda: triggered.append(1))
        clock.t += 3600
        assert cache.get() is raw
        assert triggered == [1]
        assert len(calls) == 1

    def test_refresh_resets_slot(self, raw):
        now_box = {"now": datetime(2026, 1, 15, 5, 59)}
        cache, calls = self._cache(raw, now_box)
        cache.get()
        now_box["now"] = datetime(2026, 1, 15, 6, 0)
        cache.refresh()
        assert cache.is_fresh()
        assert len(calls) == 2

    def test_force_and_invalidate(self, raw):
        now_box = {"now": datetime(2026, 1, 15, 10, 0)}
        cache, calls = self._cache(raw, now_box)
        cache.get()
        cache.get(force=True)
        assert len(calls) == 2
        cache.invalidate()
        assert cache.data is None
        cache.get()
        assert len(calls) == 3


class TestThermostatAt0545:
    """Bout en bout : la décision servie au thermostat à 05:50 (HC) permet l'allumage."""

    def _state(self):
        return {
            "state": "off", "active_system": None, "last_turned_on": None, "last_turned_off": None,
            "sensor_failures": 0, "last_alert_sent": None, "suspended_until": None,
            "system_history": {"poele": {"last_turned_on": None, "last_turned_off": None},
                               "clim": {"last_turned_on": None, "last_turned_off": None}},
        }

    def _run(self, cfg, raw, base_ha_cfg, base_thermostat_cfg):
        raw["weather"] = {"temperature": -2.0}  # sous le seuil de confort clim → poêle
        with freeze_time("2025-01-06 05:50:00"), \
                patch("modules.thermostat.is_on_vacation", return_value=False), \
                patch("modules.thermostat._load_state", return_value=self._state()), \
                patch("modules.thermostat._save_state"), \
                patch("modules.homeassistant.get_indoor_climate", return_value={"temperature": 18.0, "humidity": 50.0}), \
                patch("modules.homeassistant.get_state", return_value={"state": "off"}), \
                patch("modules.homeassistant.turn_on", return_value=True) as mock_on, \
                patch("modules.ntfy_push.send"):
            rec = analysis.compute_analysis(raw, cfg)["recommendation"]["system"]
            check_and_apply(base_ha_cfg, base_thermostat_cfg, rec)
        return rec, mock_on

    def test_shared_config_lets_thermostat_start_at_0550(self, settings, raw, base_ha_cfg, base_thermostat_cfg):
        rec, mock_on = self._run(analysis.build_advisor_config(settings), raw, base_ha_cfg, base_thermostat_cfg)
        assert rec == "poele"
        mock_on.assert_called_once_with(base_ha_cfg)

    def test_night_rule_would_block_0550(self, settings, raw, base_ha_cfg, base_thermostat_cfg):
        """Garde-fou : c'est bien la règle de nuit qui bloquerait l'allumage de 05:45."""
        cfg = {**analysis.build_advisor_config(settings), "NO_HEATING_AT_NIGHT": True}
        rec, mock_on = self._run(cfg, raw, base_ha_cfg, base_thermostat_cfg)
        assert rec == "none"
        mock_on.assert_not_called()
