"""Tests unitaires pour modules/thermostat.py — thermostat automatique.

Ce module teste toute la logique de pilotage du poêle :
- Température ressentie (correction humidité)
- Vérification des plages horaires
- Mode vacances
- Logique complète d'allumage/extinction
- Détection des allumages/extinctions manuels
- Gestion des pannes de sonde
"""

import json
import os
from unittest.mock import patch, MagicMock, call
from datetime import datetime, timedelta

import pytest
from freezegun import freeze_time

from modules.thermostat import (
    felt_temperature,
    is_in_schedule,
    is_on_vacation,
    check_and_apply,
    _load_state,
    _save_state,
    STATE_FILE,
)


# ── felt_temperature ─────────────────────────────────────────────


class TestFeltTemperature:
    """Tests de la température ressentie avec correction d'humidité.

    Formule : temp_ressentie = temp + (humidité - ref) × facteur
    Par défaut : ref=50%, facteur=0.05 → ±1°C pour ±20% d'humidité
    """

    def test_disabled_returns_raw(self):
        cfg = {"use_felt_temperature": False}
        assert felt_temperature(20.0, 70.0, cfg) == 20.0

    def test_humidity_none_returns_raw(self):
        cfg = {"use_felt_temperature": True, "humidity_reference": 50.0, "humidity_correction_factor": 0.05}
        assert felt_temperature(20.0, None, cfg) == 20.0

    def test_high_humidity_increases_temp(self):
        cfg = {"use_felt_temperature": True, "humidity_reference": 50.0, "humidity_correction_factor": 0.05}
        # 20°C + (70 - 50) * 0.05 = 20 + 1.0 = 21.0
        assert felt_temperature(20.0, 70.0, cfg) == 21.0

    def test_low_humidity_decreases_temp(self):
        cfg = {"use_felt_temperature": True, "humidity_reference": 50.0, "humidity_correction_factor": 0.05}
        # 20°C + (30 - 50) * 0.05 = 20 - 1.0 = 19.0
        assert felt_temperature(20.0, 30.0, cfg) == 19.0

    def test_reference_humidity_no_change(self):
        cfg = {"use_felt_temperature": True, "humidity_reference": 50.0, "humidity_correction_factor": 0.05}
        assert felt_temperature(20.0, 50.0, cfg) == 20.0


# ── is_in_schedule ───────────────────────────────────────────────


class TestIsInSchedule:
    """Tests de la vérification de plage horaire.

    Vérifie que l'heure actuelle est bien dans la plage définie pour le jour
    de la semaine courant (schedule[mon|tue|wed|thu|fri|sat|sun]).
    Utilise freezegun pour figer le temps.
    """

    # Lundi = weekday 0
    @freeze_time("2025-01-06 10:00:00")  # lundi
    def test_in_schedule(self, base_thermostat_cfg):
        assert is_in_schedule(base_thermostat_cfg) is True

    @freeze_time("2025-01-06 23:00:00")  # lundi 23h
    def test_out_of_schedule(self, base_thermostat_cfg):
        assert is_in_schedule(base_thermostat_cfg) is False

    @freeze_time("2025-01-06 10:00:00")  # lundi
    def test_no_schedule_for_day(self):
        cfg = {"schedule": {}}
        assert is_in_schedule(cfg) is False

    @freeze_time("2025-01-06 05:44:00")  # lundi, 1 min avant start
    def test_just_before_start(self, base_thermostat_cfg):
        assert is_in_schedule(base_thermostat_cfg) is False

    @freeze_time("2025-01-06 05:45:00")  # lundi, exactement start
    def test_exactly_at_start(self, base_thermostat_cfg):
        assert is_in_schedule(base_thermostat_cfg) is True

    @freeze_time("2025-01-11 06:00:00")  # samedi, avant start (07:00)
    def test_weekend_before_start(self, base_thermostat_cfg):
        assert is_in_schedule(base_thermostat_cfg) is False

    @freeze_time("2025-01-11 10:00:00")  # samedi, dans la plage
    def test_weekend_in_schedule(self, base_thermostat_cfg):
        assert is_in_schedule(base_thermostat_cfg) is True


# ── is_on_vacation ───────────────────────────────────────────────


class TestIsOnVacation:
    """Tests du mode vacances.

    Vérifie que la date du jour est bien dans la plage [vacation_start, vacation_end]
    stockée dans le fichier d'état.
    """

    @patch("modules.thermostat._load_state", return_value={})
    def test_no_dates_returns_false(self, _mock):
        assert is_on_vacation() is False

    @freeze_time("2025-02-15")
    @patch("modules.thermostat._load_state", return_value={
        "vacation_start": "2025-02-10",
        "vacation_end": "2025-02-20",
    })
    def test_within_range_returns_true(self, _mock):
        assert is_on_vacation() is True

    @freeze_time("2025-03-01")
    @patch("modules.thermostat._load_state", return_value={
        "vacation_start": "2025-02-10",
        "vacation_end": "2025-02-20",
    })
    def test_outside_range_returns_false(self, _mock):
        assert is_on_vacation() is False

    @freeze_time("2025-02-10")
    @patch("modules.thermostat._load_state", return_value={
        "vacation_start": "2025-02-10",
        "vacation_end": "2025-02-20",
    })
    def test_on_start_date_returns_true(self, _mock):
        assert is_on_vacation() is True


# ── check_and_apply ──────────────────────────────────────────────


class TestCheckAndApply:
    """Tests de la logique complète de pilotage du poêle.

    Scénarios testés :
    - Thermostat désactivé → ne fait rien
    - Mode vacances + poêle allumé → extinction
    - Sonde en panne → incrémente compteur d'échecs, pas de pilotage
    - **ALLUMAGE** : poêle OFF + dans horaire + temp < seuil + reco="poele"
    - **EXTINCTION** :
      - Temp ≥ temp_off (+ min_on atteint)
      - Hors horaire (+ grâce écoulée)
      - Recommandation changée (+ min_on atteint)
    - **min_on_minutes** : durée minimale avant extinction possible
    - **Détection manuelle** :
      - Allumage manuel → synchro état + clear suspension
      - Extinction manuelle → suspension N heures
    """

    def _make_state(self, state="off", last_on=None, last_off=None,
                    sensor_failures=0, suspended_until=None):
        return {
            "state": state,
            "last_turned_on": last_on,
            "last_turned_off": last_off,
            "sensor_failures": sensor_failures,
            "last_alert_sent": None,
            "suspended_until": suspended_until,
        }

    def test_thermostat_disabled_does_nothing(self, base_ha_cfg):
        cfg = {"enabled": False}
        # Ne doit rien faire, pas d'exception
        check_and_apply(base_ha_cfg, cfg, "poele")

    @freeze_time("2025-02-15 10:00:00")
    @patch("modules.thermostat.is_on_vacation", return_value=True)
    @patch("modules.thermostat._load_state")
    @patch("modules.thermostat._save_state")
    @patch("modules.homeassistant.turn_off", return_value=True)
    @patch("modules.ntfy_push.send")
    def test_vacation_turns_off_poele(self, mock_ntfy, mock_ha_off, mock_save,
                                      mock_load, _mock_vac, base_ha_cfg, base_thermostat_cfg):
        mock_load.return_value = self._make_state(state="on")
        check_and_apply(base_ha_cfg, base_thermostat_cfg, "poele")
        mock_ha_off.assert_called_once_with(base_ha_cfg)
        # Vérifier que l'état a été sauvegardé avec state="off"
        saved = mock_save.call_args[0][0]
        assert saved["state"] == "off"

    @freeze_time("2025-01-06 10:00:00")  # lundi
    @patch("modules.thermostat.is_on_vacation", return_value=False)
    @patch("modules.thermostat._load_state")
    @patch("modules.thermostat._save_state")
    @patch("modules.homeassistant.get_indoor_climate", return_value=None)
    @patch("modules.homeassistant.get_state", return_value={"state": "off"})
    @patch("modules.ntfy_push.send")
    def test_sensor_failure_increments_counter(self, mock_ntfy, _mock_ha_state,
                                                mock_indoor, mock_save, mock_load,
                                                _mock_vac, base_ha_cfg, base_thermostat_cfg):
        mock_load.return_value = self._make_state(sensor_failures=0)
        check_and_apply(base_ha_cfg, base_thermostat_cfg, "poele")
        # _handle_sensor_failure est appelé, qui sauvegarde l'état avec sensor_failures incrémenté
        saved = mock_save.call_args[0][0]
        assert saved["sensor_failures"] == 1

    @freeze_time("2025-01-06 10:00:00")  # lundi dans la plage
    @patch("modules.thermostat.is_on_vacation", return_value=False)
    @patch("modules.thermostat._load_state")
    @patch("modules.thermostat._save_state")
    @patch("modules.homeassistant.get_indoor_climate", return_value={"temperature": 18.0, "humidity": 50.0})
    @patch("modules.homeassistant.get_state", return_value={"state": "off"})
    @patch("modules.homeassistant.turn_on", return_value=True)
    @patch("modules.ntfy_push.send")
    def test_poele_off_cold_in_schedule_turns_on(self, mock_ntfy, mock_ha_on, mock_ha_state,
                                                  mock_indoor, mock_save, mock_load,
                                                  _mock_vac, base_ha_cfg, base_thermostat_cfg):
        mock_load.return_value = self._make_state(state="off")
        check_and_apply(base_ha_cfg, base_thermostat_cfg, "poele")
        mock_ha_on.assert_called_once_with(base_ha_cfg)

    @freeze_time("2025-01-06 10:00:00")  # lundi
    @patch("modules.thermostat.is_on_vacation", return_value=False)
    @patch("modules.thermostat._load_state")
    @patch("modules.thermostat._save_state")
    @patch("modules.homeassistant.get_indoor_climate", return_value={"temperature": 23.0, "humidity": 50.0})
    @patch("modules.homeassistant.get_state", return_value={"state": "heat"})
    @patch("modules.homeassistant.turn_off", return_value=True)
    @patch("modules.ntfy_push.send")
    def test_poele_on_temp_reached_min_on_met_turns_off(self, mock_ntfy, mock_ha_off, mock_ha_state,
                                                         mock_indoor, mock_save, mock_load,
                                                         _mock_vac, base_ha_cfg, base_thermostat_cfg):
        two_hours_ago = (datetime(2025, 1, 6, 8, 0, 0)).isoformat()
        mock_load.return_value = self._make_state(state="on", last_on=two_hours_ago)
        check_and_apply(base_ha_cfg, base_thermostat_cfg, "poele")
        mock_ha_off.assert_called_once_with(base_ha_cfg)

    @freeze_time("2025-01-06 23:00:00")  # lundi hors plage
    @patch("modules.thermostat.is_on_vacation", return_value=False)
    @patch("modules.thermostat._load_state")
    @patch("modules.thermostat._save_state")
    @patch("modules.homeassistant.get_indoor_climate", return_value={"temperature": 20.0, "humidity": 50.0})
    @patch("modules.homeassistant.get_state", return_value={"state": "heat"})
    @patch("modules.homeassistant.turn_off", return_value=True)
    @patch("modules.ntfy_push.send")
    def test_poele_on_out_of_schedule_grace_elapsed_turns_off(self, mock_ntfy, mock_ha_off, mock_ha_state,
                                                               mock_indoor, mock_save, mock_load,
                                                               _mock_vac, base_ha_cfg, base_thermostat_cfg):
        two_hours_ago = (datetime(2025, 1, 6, 21, 0, 0)).isoformat()
        mock_load.return_value = self._make_state(state="on", last_on=two_hours_ago)
        check_and_apply(base_ha_cfg, base_thermostat_cfg, "poele")
        mock_ha_off.assert_called_once()

    @freeze_time("2025-01-06 10:00:00")  # lundi
    @patch("modules.thermostat.is_on_vacation", return_value=False)
    @patch("modules.thermostat._load_state")
    @patch("modules.thermostat._save_state")
    @patch("modules.homeassistant.get_indoor_climate", return_value={"temperature": 20.0, "humidity": 50.0})
    @patch("modules.homeassistant.get_state", return_value={"state": "heat"})
    @patch("modules.homeassistant.turn_off", return_value=True)
    @patch("modules.ntfy_push.send")
    def test_reco_changed_min_on_met_turns_off(self, mock_ntfy, mock_ha_off, mock_ha_state,
                                                mock_indoor, mock_save, mock_load,
                                                _mock_vac, base_ha_cfg, base_thermostat_cfg):
        two_hours_ago = (datetime(2025, 1, 6, 8, 0, 0)).isoformat()
        mock_load.return_value = self._make_state(state="on", last_on=two_hours_ago)
        check_and_apply(base_ha_cfg, base_thermostat_cfg, "clim")  # reco changée
        mock_ha_off.assert_called_once()

    @freeze_time("2025-01-06 10:00:00")  # lundi
    @patch("modules.thermostat.is_on_vacation", return_value=False)
    @patch("modules.thermostat._load_state")
    @patch("modules.thermostat._save_state")
    @patch("modules.homeassistant.get_indoor_climate", return_value={"temperature": 20.0, "humidity": 50.0})
    @patch("modules.homeassistant.get_state", return_value={"state": "heat"})
    @patch("modules.homeassistant.turn_off")
    @patch("modules.ntfy_push.send")
    def test_min_on_not_met_stays_on(self, mock_ntfy, mock_ha_off, mock_ha_state,
                                      mock_indoor, mock_save, mock_load,
                                      _mock_vac, base_ha_cfg, base_thermostat_cfg):
        # Allumé il y a 30 min seulement (min_on=90)
        thirty_min_ago = (datetime(2025, 1, 6, 9, 30, 0)).isoformat()
        mock_load.return_value = self._make_state(state="on", last_on=thirty_min_ago)
        check_and_apply(base_ha_cfg, base_thermostat_cfg, "clim")  # reco changée mais min_on pas atteint
        mock_ha_off.assert_not_called()

    @freeze_time("2025-01-06 10:00:00")  # lundi
    @patch("modules.thermostat.is_on_vacation", return_value=False)
    @patch("modules.thermostat._load_state")
    @patch("modules.thermostat._save_state")
    @patch("modules.homeassistant.get_indoor_climate", return_value={"temperature": 20.0, "humidity": 50.0})
    @patch("modules.homeassistant.get_state", return_value={"state": "heat"})
    @patch("modules.ntfy_push.send")
    def test_manual_on_syncs_state(self, mock_ntfy, mock_ha_state, mock_indoor,
                                    mock_save, mock_load, _mock_vac,
                                    base_ha_cfg, base_thermostat_cfg):
        # État interne = off mais HA dit que le poêle est allumé → synchro
        mock_load.return_value = self._make_state(state="off")
        check_and_apply(base_ha_cfg, base_thermostat_cfg, "poele")
        # Doit avoir sauvegardé avec state="on" (synchro manuelle)
        # Chercher l'appel de save avec state="on" (peut y avoir plusieurs appels)
        found_sync = False
        for c in mock_save.call_args_list:
            if c[0][0].get("state") == "on":
                found_sync = True
                assert c[0][0].get("suspended_until") is None
                break
        assert found_sync, "State should have been synced to 'on'"

    @freeze_time("2025-01-06 10:00:00")  # lundi
    @patch("modules.thermostat.is_on_vacation", return_value=False)
    @patch("modules.thermostat._load_state")
    @patch("modules.thermostat._save_state")
    @patch("modules.homeassistant.get_indoor_climate", return_value={"temperature": 20.0, "humidity": 50.0})
    @patch("modules.homeassistant.get_state", return_value={"state": "off"})
    @patch("modules.ntfy_push.send")
    def test_manual_off_suspends(self, mock_ntfy, mock_ha_state, mock_indoor,
                                  mock_save, mock_load, _mock_vac,
                                  base_ha_cfg, base_thermostat_cfg):
        # État interne = on mais HA dit off → extinction manuelle → suspension
        mock_load.return_value = self._make_state(state="on")
        check_and_apply(base_ha_cfg, base_thermostat_cfg, "poele")
        # Doit avoir sauvegardé avec suspended_until set
        found_suspend = False
        for c in mock_save.call_args_list:
            if c[0][0].get("suspended_until") is not None:
                found_suspend = True
                assert c[0][0].get("state") == "off"
                break
        assert found_suspend, "Should have set suspended_until after manual off"
