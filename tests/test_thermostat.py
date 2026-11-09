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
                    sensor_failures=0, suspended_until=None, active_system=None):
        """Crée un état de thermostat avec la structure system_history."""
        # Par défaut, si state=on et pas de système spécifié, utiliser "poele"
        if state == "on" and active_system is None:
            active_system = "poele"

        # Initialiser system_history
        system_history = {
            "poele": {"last_turned_on": None, "last_turned_off": None},
            "clim": {"last_turned_on": None, "last_turned_off": None}
        }

        # Si un timestamp est fourni, le mettre dans le système approprié
        if last_on and active_system in ("poele", "clim"):
            system_history[active_system]["last_turned_on"] = last_on
        if last_off and active_system in ("poele", "clim"):
            system_history[active_system]["last_turned_off"] = last_off

        return {
            "state": state,
            "active_system": active_system if state == "on" else None,
            "last_turned_on": last_on,  # Champs legacy maintenus
            "last_turned_off": last_off,
            "sensor_failures": sensor_failures,
            "last_alert_sent": None,
            "suspended_until": suspended_until,
            "system_history": system_history
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

    @freeze_time("2025-01-06 10:00:00")  # lundi dans la plage
    @patch("modules.thermostat.is_on_vacation", return_value=False)
    @patch("modules.thermostat._load_state")
    @patch("modules.thermostat._save_state")
    @patch("modules.homeassistant.get_indoor_climate", return_value={"temperature": 18.0, "humidity": 50.0})
    @patch("modules.homeassistant.get_state", return_value={"state": "off"})
    @patch("modules.homeassistant.get_clim_state", return_value={"state": "off"})
    @patch("modules.homeassistant.is_clim_configured", return_value=True)
    @patch("modules.homeassistant.turn_on_clim", return_value=True)
    @patch("modules.ntfy_push.send")
    def test_clim_turned_on_by_thermostat_marks_setpoint_managed(self, mock_ntfy, mock_clim_on, _cfg, _clim_state,
                                                                 _ha_state, _indoor, mock_save, mock_load,
                                                                 _mock_vac, base_ha_cfg, base_thermostat_cfg):
        mock_load.return_value = self._make_state(state="off")
        check_and_apply(base_ha_cfg, base_thermostat_cfg, "clim")
        mock_clim_on.assert_called_once()
        assert mock_save.call_args[0][0]["clim_setpoint_managed"] is True

    @freeze_time("2025-01-06 10:00:00")  # lundi dans la plage
    @patch("modules.thermostat.is_on_vacation", return_value=False)
    @patch("modules.thermostat._load_state")
    @patch("modules.thermostat._save_state")
    @patch("modules.homeassistant.get_indoor_climate", return_value={"temperature": 21.0, "humidity": 50.0})
    @patch("modules.homeassistant.get_state", return_value={"state": "off"})
    @patch("modules.homeassistant.get_clim_state", return_value={"state": "off"})
    @patch("modules.homeassistant.is_clim_configured", return_value=True)
    @patch("modules.ntfy_push.send")
    def test_clim_seen_off_clears_setpoint_managed(self, mock_ntfy, _cfg, _clim_state, _ha_state, _indoor,
                                                   mock_save, mock_load, _mock_vac,
                                                   base_ha_cfg, base_thermostat_cfg):
        mock_load.return_value = {**self._make_state(state="off"), "clim_setpoint_managed": True}
        check_and_apply(base_ha_cfg, base_thermostat_cfg, "clim")
        assert mock_save.call_args[0][0]["clim_setpoint_managed"] is False

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
    @patch("modules.homeassistant.get_clim_state", return_value={"state": "off"})
    @patch("modules.homeassistant.is_clim_configured", return_value=True)
    @patch("modules.homeassistant.turn_on_clim", return_value=True)
    @patch("modules.homeassistant.turn_off", return_value=True)
    @patch("modules.ntfy_push.send")
    def test_reco_changed_min_on_met_switches_system(self, mock_ntfy, mock_ha_off, mock_clim_on, mock_clim_cfg,
                                                     mock_clim_state, mock_ha_state, mock_indoor,
                                                     mock_save, mock_load, _mock_vac,
                                                     base_ha_cfg, base_thermostat_cfg):
        two_hours_ago = (datetime(2025, 1, 6, 8, 0, 0)).isoformat()
        mock_load.return_value = self._make_state(state="on", last_on=two_hours_ago)
        check_and_apply(base_ha_cfg, base_thermostat_cfg, "clim")  # reco changée
        mock_ha_off.assert_called_once()
        mock_clim_on.assert_called_once()

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


# ── Régression : timestamp last_turned_on manquant ───────────────


class TestMissingTimestampRegression:
    """Régression : si last_turned_on est None (état migré/corrompu),
    on_minutes était 0 → ni la cible de température ni la fin de plage
    ne pouvaient éteindre le système. Doit maintenant utiliser 9999 min.
    """

    def _make_state_no_timestamp(self, active_system="clim"):
        """État ON mais sans timestamp last_turned_on."""
        return {
            "state": "on",
            "active_system": active_system,
            "last_turned_on": None,
            "last_turned_off": None,
            "sensor_failures": 0,
            "last_alert_sent": None,
            "suspended_until": None,
            "system_history": {
                "poele": {"last_turned_on": None, "last_turned_off": None},
                "clim": {"last_turned_on": None, "last_turned_off": None},
            },
        }

    @freeze_time("2025-01-06 10:00:00")  # lundi dans la plage
    @patch("modules.thermostat.is_on_vacation", return_value=False)
    @patch("modules.thermostat.is_in_absence_schedule", return_value=False)
    @patch("modules.thermostat._load_state")
    @patch("modules.thermostat._save_state")
    @patch("modules.homeassistant.get_indoor_climate", return_value={"temperature": 23.5, "humidity": 50.0})
    @patch("modules.homeassistant.get_clim_state", return_value={"state": "heat"})
    @patch("modules.homeassistant.get_state", return_value={"state": "off"})
    @patch("modules.homeassistant.turn_off_clim", return_value=True)
    @patch("modules.ntfy_push.send")
    def test_clim_on_temp_reached_no_timestamp_turns_off(
        self, mock_ntfy, mock_turn_off_clim, mock_ha_state, mock_clim_state,
        mock_indoor, mock_save, mock_load, mock_sched, mock_vac,
        base_ha_cfg, base_thermostat_cfg
    ):
        """Clim allumée sans timestamp, temp atteinte → doit s'éteindre (régression on_minutes=0)."""
        base_ha_cfg = {**base_ha_cfg, "clim_entity_id": "climate.test_clim"}
        mock_load.return_value = self._make_state_no_timestamp("clim")
        check_and_apply(base_ha_cfg, base_thermostat_cfg, "clim")
        mock_turn_off_clim.assert_called_once()

    @freeze_time("2025-01-06 23:00:00")  # lundi hors plage
    @patch("modules.thermostat.is_on_vacation", return_value=False)
    @patch("modules.thermostat.is_in_absence_schedule", return_value=False)
    @patch("modules.thermostat._load_state")
    @patch("modules.thermostat._save_state")
    @patch("modules.homeassistant.get_indoor_climate", return_value={"temperature": 20.0, "humidity": 50.0})
    @patch("modules.homeassistant.get_clim_state", return_value={"state": "heat"})
    @patch("modules.homeassistant.get_state", return_value={"state": "off"})
    @patch("modules.homeassistant.turn_off_clim", return_value=True)
    @patch("modules.ntfy_push.send")
    def test_clim_on_out_of_schedule_no_timestamp_turns_off(
        self, mock_ntfy, mock_turn_off_clim, mock_ha_state, mock_clim_state,
        mock_indoor, mock_save, mock_load, mock_sched, mock_vac,
        base_ha_cfg, base_thermostat_cfg
    ):
        """Clim allumée sans timestamp, hors plage → doit s'éteindre (régression on_minutes=0)."""
        base_ha_cfg = {**base_ha_cfg, "clim_entity_id": "climate.test_clim"}
        mock_load.return_value = self._make_state_no_timestamp("clim")
        check_and_apply(base_ha_cfg, base_thermostat_cfg, "clim")
        mock_turn_off_clim.assert_called_once()

    @freeze_time("2025-01-06 23:00:00")  # lundi hors plage
    @patch("modules.thermostat.is_on_vacation", return_value=False)
    @patch("modules.thermostat.is_in_absence_schedule", return_value=False)
    @patch("modules.thermostat._load_state")
    @patch("modules.thermostat._save_state")
    @patch("modules.homeassistant.get_indoor_climate", return_value={"temperature": 20.0, "humidity": 50.0})
    @patch("modules.homeassistant.get_state", return_value={"state": "heat"})
    @patch("modules.homeassistant.turn_off", return_value=True)
    @patch("modules.ntfy_push.send")
    def test_poele_on_out_of_schedule_no_timestamp_turns_off(
        self, mock_ntfy, mock_ha_off, mock_ha_state,
        mock_indoor, mock_save, mock_load, mock_sched, mock_vac,
        base_ha_cfg, base_thermostat_cfg
    ):
        """Poêle allumé sans timestamp, hors plage → doit s'éteindre (régression on_minutes=0)."""
        mock_load.return_value = self._make_state_no_timestamp("poele")
        check_and_apply(base_ha_cfg, base_thermostat_cfg, "poele")
        mock_ha_off.assert_called_once_with(base_ha_cfg)


class TestValidateThresholds:
    """validate_thresholds : contrôle des seuils saisis depuis le widget de la page principale."""

    def test_valid_values_rounded(self):
        from modules.thermostat import validate_thresholds
        assert validate_thresholds("19.54", 21) == (19.5, 21.0)

    @pytest.mark.parametrize("temp_on,temp_off", [
        (21, 21),      # égalité
        (22, 21),      # inversion
        (9.9, 21),     # allumage trop bas
        (25.5, 26),    # allumage trop haut
        (20, 30.5),    # extinction trop haute
        ("abc", 21),   # non numérique
        (None, 21),    # manquant
    ])
    def test_invalid_values_rejected(self, temp_on, temp_off):
        from modules.thermostat import validate_thresholds
        with pytest.raises(ValueError):
            validate_thresholds(temp_on, temp_off)


class TestSyncClimSetpoint:
    """sync_clim_setpoint : renvoi de la consigne à la clim après modification de temp_off."""

    @pytest.fixture(autouse=True)
    def managed_by_app(self):
        with patch("modules.thermostat._load_state", return_value={"clim_setpoint_managed": True}) as m:
            yield m

    def test_no_push_when_clim_started_outside_app(self, managed_by_app):
        from modules.thermostat import sync_clim_setpoint
        managed_by_app.return_value = {"clim_setpoint_managed": False}
        ha = self._ha()
        assert sync_clim_setpoint(ha, {}, 21.5) is None
        ha.set_clim_temperature.assert_not_called()

    def _ha(self, configured=True, state="heat", set_ok=True):
        ha = MagicMock()
        ha.is_clim_configured.return_value = configured
        ha.get_clim_state.return_value = {"state": state} if state else None
        ha.set_clim_temperature.return_value = set_ok
        return ha

    def test_pushes_setpoint_when_clim_heating(self):
        from modules.thermostat import sync_clim_setpoint
        ha = self._ha()
        assert sync_clim_setpoint(ha, {"cfg": 1}, 21.5) is True
        ha.set_clim_temperature.assert_called_once_with({"cfg": 1}, 21.5)

    @pytest.mark.parametrize("configured,state", [(False, "heat"), (True, "off"), (True, "cool"), (True, None)])
    def test_no_push_when_clim_not_heating(self, configured, state):
        from modules.thermostat import sync_clim_setpoint
        ha = self._ha(configured=configured, state=state)
        assert sync_clim_setpoint(ha, {}, 21.5) is None
        ha.set_clim_temperature.assert_not_called()

    def test_reports_failure(self):
        from modules.thermostat import sync_clim_setpoint
        assert sync_clim_setpoint(self._ha(set_ok=False), {}, 21.5) is False


# ── Échecs des commandes Home Assistant ──────────────────────────


@freeze_time("2025-01-06 10:00:00")  # lundi, dans la plage
class TestCommandFailures:
    """Une commande HA en échec ne doit jamais changer l'état ni créer de fausse suspension."""

    @pytest.fixture
    def store(self):
        data = {"state": {
            "state": "off", "active_system": None, "sensor_failures": 0, "suspended_until": None,
            "system_history": {"poele": {"last_turned_on": None, "last_turned_off": None},
                               "clim": {"last_turned_on": None, "last_turned_off": None}},
        }}
        with patch("modules.thermostat._load_state", side_effect=lambda: dict(data["state"])), \
             patch("modules.thermostat._save_state", side_effect=lambda s: data.__setitem__("state", s)), \
             patch("modules.thermostat.is_on_vacation", return_value=False), \
             patch("modules.thermostat.is_in_absence_schedule", return_value=False):
            yield data

    def _check(self, base_ha_cfg, base_thermostat_cfg, indoor_temp, poele_real, turn_on=True, turn_off=True):
        with patch("modules.homeassistant.get_indoor_climate", return_value={"temperature": indoor_temp, "humidity": 50.0}), \
             patch("modules.homeassistant.get_state", return_value={"state": poele_real}), \
             patch("modules.homeassistant.turn_on", return_value=turn_on) as on, \
             patch("modules.homeassistant.turn_off", return_value=turn_off) as off, \
             patch("modules.ntfy_push.send") as ntfy:
            check_and_apply(base_ha_cfg, base_thermostat_cfg, "poele", ntfy_cfg={"enabled": True})
        return on, off, ntfy

    def test_failed_ignition_keeps_state_off(self, store, base_ha_cfg, base_thermostat_cfg):
        on, _, ntfy = self._check(base_ha_cfg, base_thermostat_cfg, 18.0, "off", turn_on=False)
        on.assert_called_once()
        assert store["state"]["state"] == "off"
        assert store["state"]["command_failures"] == 1
        assert not any("allumé" in c[0][0] for c in ntfy.call_args_list)

    def test_failed_ignition_does_not_suspend_and_retries(self, store, base_ha_cfg, base_thermostat_cfg):
        self._check(base_ha_cfg, base_thermostat_cfg, 18.0, "off", turn_on=False)
        on, _, _ = self._check(base_ha_cfg, base_thermostat_cfg, 18.0, "off", turn_on=True)
        on.assert_called_once()
        assert store["state"]["suspended_until"] is None
        assert store["state"]["state"] == "on"
        assert store["state"]["command_failures"] == 0

    def test_alert_on_third_consecutive_failure(self, store, base_ha_cfg, base_thermostat_cfg):
        alerts = []
        for _ in range(4):
            _, _, ntfy = self._check(base_ha_cfg, base_thermostat_cfg, 18.0, "off", turn_on=False)
            alerts += [c for c in ntfy.call_args_list if "échec" in c[0][0]]
        assert len(alerts) == 1
        assert store["state"]["command_failures"] == 4

    def test_failed_turn_off_keeps_state_on(self, store, base_ha_cfg, base_thermostat_cfg):
        store["state"] = {**store["state"], "state": "on", "active_system": "poele",
                          "system_history": {"poele": {"last_turned_on": "2025-01-06T07:00:00", "last_turned_off": None},
                                             "clim": {"last_turned_on": None, "last_turned_off": None}}}
        _, off, ntfy = self._check(base_ha_cfg, base_thermostat_cfg, 23.5, "heat", turn_off=False)
        off.assert_called_once()
        assert store["state"]["state"] == "on"
        assert store["state"]["active_system"] == "poele"
        assert store["state"]["command_failures"] == 1
        assert not any("éteint" in c[0][0] for c in ntfy.call_args_list)


class TestTransitions:
    """Changement de recommandation en cours de chauffe : le nouveau système prend le relais
    tant que la cible (temp_off) n'est pas atteinte, même au-dessus du seuil d'allumage."""

    @pytest.fixture
    def store(self):
        data = {"state": {
            "state": "on", "active_system": None, "sensor_failures": 0, "suspended_until": None,
            "system_history": {"poele": {"last_turned_on": None, "last_turned_off": None},
                               "clim": {"last_turned_on": None, "last_turned_off": None}},
        }}
        with patch("modules.thermostat._load_state", side_effect=lambda: dict(data["state"])), \
             patch("modules.thermostat._save_state", side_effect=lambda s: data.__setitem__("state", s)), \
             patch("modules.thermostat.is_on_vacation", return_value=False), \
             patch("modules.thermostat.is_in_absence_schedule", return_value=False), \
             freeze_time("2025-01-06 10:00:00"):
            yield data

    def _run(self, store, base_ha_cfg, base_thermostat_cfg, active, recommendation, indoor_temp):
        store["state"]["active_system"] = active
        store["state"]["system_history"][active]["last_turned_on"] = "2025-01-06T07:00:00"
        poele_real = "heat" if active == "poele" else "off"
        clim_real = "heat" if active == "clim" else "off"
        with patch("modules.homeassistant.get_indoor_climate", return_value={"temperature": indoor_temp, "humidity": 50.0}), \
             patch("modules.homeassistant.get_state", return_value={"state": poele_real}), \
             patch("modules.homeassistant.is_clim_configured", return_value=True), \
             patch("modules.homeassistant.get_clim_state", return_value={"state": clim_real}), \
             patch("modules.homeassistant.turn_on", return_value=True) as poele_on, \
             patch("modules.homeassistant.turn_off", return_value=True) as poele_off, \
             patch("modules.homeassistant.turn_on_clim", return_value=True) as clim_on, \
             patch("modules.homeassistant.turn_off_clim", return_value=True) as clim_off, \
             patch("modules.ntfy_push.send") as ntfy:
            check_and_apply(base_ha_cfg, base_thermostat_cfg, recommendation, ntfy_cfg={"enabled": True})
        return poele_on, poele_off, clim_on, clim_off, ntfy

    def test_poele_to_clim_above_temp_on_below_target_starts_clim(self, store, base_ha_cfg, base_thermostat_cfg):
        # 20.5°C : au-dessus de temp_on (20.0) mais sous la cible (22.9)
        _, poele_off, clim_on, _, _ = self._run(store, base_ha_cfg, base_thermostat_cfg, "poele", "clim", 20.5)
        poele_off.assert_called_once()
        clim_on.assert_called_once()
        assert store["state"]["state"] == "on"
        assert store["state"]["active_system"] == "clim"

    def test_poele_to_clim_target_reached_turns_off(self, store, base_ha_cfg, base_thermostat_cfg):
        _, poele_off, clim_on, _, ntfy = self._run(store, base_ha_cfg, base_thermostat_cfg, "poele", "clim", 23.0)
        poele_off.assert_called_once()
        clim_on.assert_not_called()
        assert store["state"]["state"] == "off"
        assert "cible atteinte" in ntfy.call_args[0][1]

    def test_clim_to_poele_above_temp_on_below_target_starts_poele(self, store, base_ha_cfg, base_thermostat_cfg):
        poele_on, _, _, clim_off, _ = self._run(store, base_ha_cfg, base_thermostat_cfg, "clim", "poele", 20.5)
        poele_on.assert_called_once()
        clim_off.assert_not_called()  # chevauchement : clim maintenue pendant la montée en température
        assert store["state"]["active_system"] == "poele"
        assert store["state"]["clim_overlap_until"]

    def test_clim_to_poele_target_reached_turns_off_clim(self, store, base_ha_cfg, base_thermostat_cfg):
        poele_on, _, _, clim_off, ntfy = self._run(store, base_ha_cfg, base_thermostat_cfg, "clim", "poele", 23.0)
        poele_on.assert_not_called()
        clim_off.assert_called_once()
        assert store["state"]["state"] == "off"
        assert "cible atteinte" in ntfy.call_args[0][1]

    def test_unknown_recommendation_keeps_clim(self, store, base_ha_cfg, base_thermostat_cfg):
        # Température extérieure indisponible : pas de transition clim → poêle
        poele_on, _, _, clim_off, _ = self._run(store, base_ha_cfg, base_thermostat_cfg, "clim", "unknown", 20.2)
        poele_on.assert_not_called()
        clim_off.assert_not_called()
        assert store["state"]["active_system"] == "clim"

    def test_unknown_recommendation_keeps_poele(self, store, base_ha_cfg, base_thermostat_cfg):
        _, poele_off, clim_on, _, _ = self._run(store, base_ha_cfg, base_thermostat_cfg, "poele", "unknown", 20.2)
        poele_off.assert_not_called()
        clim_on.assert_not_called()
        assert store["state"]["active_system"] == "poele"


class TestComfortHysteresisAndLateWindow:
    """Va-et-vient clim ↔ poêle autour du seuil de confort (hystérésis) et pas d'allumage
    du poêle juste avant la fin de plage (cas réel du 03/11/2026 : 6,8 → 7,0 → 6,9 °C)."""

    @pytest.fixture
    def store(self):
        data = {"state": {
            "state": "off", "active_system": None, "sensor_failures": 0, "suspended_until": None,
            "system_history": {"poele": {"last_turned_on": None, "last_turned_off": None},
                               "clim": {"last_turned_on": None, "last_turned_off": None}},
        }}
        with patch("modules.thermostat._load_state", side_effect=lambda: dict(data["state"])), \
             patch("modules.thermostat._save_state", side_effect=lambda s: data.__setitem__("state", s)), \
             patch("modules.thermostat.is_on_vacation", return_value=False), \
             patch("modules.thermostat.is_in_absence_schedule", return_value=False):
            yield data

    def _run(self, store, base_ha_cfg, cfg, now, active, recommendation, outdoor_temp,
             indoor_temp=20.5, clim_fallback_ok=True, clim_configured=True):
        if active:
            store["state"]["state"] = "on"
            store["state"]["active_system"] = active
            store["state"]["system_history"][active]["last_turned_on"] = "2025-01-06T05:50:00"
        poele_real = "heat" if active == "poele" else "off"
        clim_real = "heat" if active == "clim" else "off"
        with freeze_time(now), \
             patch("modules.homeassistant.get_indoor_climate", return_value={"temperature": indoor_temp, "humidity": 50.0}), \
             patch("modules.homeassistant.get_state", return_value={"state": poele_real}), \
             patch("modules.homeassistant.is_clim_configured", return_value=clim_configured), \
             patch("modules.homeassistant.get_clim_state", return_value={"state": clim_real}), \
             patch("modules.homeassistant.turn_on", return_value=True) as poele_on, \
             patch("modules.homeassistant.turn_off", return_value=True) as poele_off, \
             patch("modules.homeassistant.turn_on_clim", return_value=True) as clim_on, \
             patch("modules.homeassistant.turn_off_clim", return_value=True) as clim_off, \
             patch("modules.ntfy_push.send") as ntfy:
            check_and_apply(base_ha_cfg, cfg, recommendation, ntfy_cfg={"enabled": True},
                            outdoor_temp=outdoor_temp, comfort_min_temp=7.0, clim_fallback_ok=clim_fallback_ok)
        return poele_on, poele_off, clim_on, clim_off, ntfy

    # ── Hystérésis ──

    def test_poele_kept_just_above_comfort_threshold(self, store, base_ha_cfg, base_thermostat_cfg):
        _, poele_off, clim_on, _, _ = self._run(store, base_ha_cfg, base_thermostat_cfg,
                                                "2025-01-06 10:00:00", "poele", "clim", 7.0)
        poele_off.assert_not_called()
        clim_on.assert_not_called()
        assert store["state"]["active_system"] == "poele"

    def test_poele_to_clim_once_above_hysteresis(self, store, base_ha_cfg, base_thermostat_cfg):
        _, poele_off, clim_on, _, _ = self._run(store, base_ha_cfg, base_thermostat_cfg,
                                                "2025-01-06 10:00:00", "poele", "clim", 8.0)
        poele_off.assert_called_once()
        clim_on.assert_called_once()
        assert store["state"]["active_system"] == "clim"

    def test_hysteresis_configurable(self, store, base_ha_cfg, base_thermostat_cfg):
        cfg = {**base_thermostat_cfg, "clim_comfort_hysteresis": 0.5}
        _, poele_off, clim_on, _, _ = self._run(store, base_ha_cfg, cfg,
                                                "2025-01-06 10:00:00", "poele", "clim", 7.6)
        clim_on.assert_called_once()

    def test_hysteresis_does_not_block_clim_ignition_from_off(self, store, base_ha_cfg, base_thermostat_cfg):
        poele_on, _, clim_on, _, _ = self._run(store, base_ha_cfg, base_thermostat_cfg,
                                               "2025-01-06 10:00:00", None, "clim", 7.2, indoor_temp=19.0)
        clim_on.assert_called_once()
        poele_on.assert_not_called()

    def test_hysteresis_ignored_without_outdoor_temp(self, store, base_ha_cfg, base_thermostat_cfg):
        _, poele_off, clim_on, _, _ = self._run(store, base_ha_cfg, base_thermostat_cfg,
                                                "2025-01-06 10:00:00", "poele", "clim", None)
        clim_on.assert_called_once()

    # ── Fin de plage (22:00) ──

    def test_no_clim_to_poele_transition_near_end(self, store, base_ha_cfg, base_thermostat_cfg):
        poele_on, _, _, clim_off, _ = self._run(store, base_ha_cfg, base_thermostat_cfg,
                                                "2025-01-06 21:54:00", "clim", "poele", 6.9)
        poele_on.assert_not_called()
        clim_off.assert_not_called()
        assert store["state"]["active_system"] == "clim"
        assert not store["state"].get("clim_overlap_until")

    def test_clim_to_poele_transition_before_window(self, store, base_ha_cfg, base_thermostat_cfg):
        poele_on, _, _, _, _ = self._run(store, base_ha_cfg, base_thermostat_cfg,
                                         "2025-01-06 20:50:00", "clim", "poele", 6.9)
        poele_on.assert_called_once()

    def test_late_ignition_uses_clim_instead_of_poele(self, store, base_ha_cfg, base_thermostat_cfg):
        poele_on, _, clim_on, _, _ = self._run(store, base_ha_cfg, base_thermostat_cfg,
                                               "2025-01-06 21:30:00", None, "poele", 5.0, indoor_temp=19.0)
        poele_on.assert_not_called()
        clim_on.assert_called_once()
        assert store["state"]["active_system"] == "clim"

    def test_late_ignition_nothing_when_clim_not_allowed(self, store, base_ha_cfg, base_thermostat_cfg):
        poele_on, _, clim_on, _, ntfy = self._run(store, base_ha_cfg, base_thermostat_cfg,
                                                  "2025-01-06 21:30:00", None, "poele", 5.0,
                                                  indoor_temp=19.0, clim_fallback_ok=False)
        poele_on.assert_not_called()
        clim_on.assert_not_called()
        assert store["state"]["state"] == "off"
        assert "fin de plage" in ntfy.call_args[0][1]

    def test_late_window_keeps_running_poele(self, store, base_ha_cfg, base_thermostat_cfg):
        _, poele_off, clim_on, _, _ = self._run(store, base_ha_cfg, base_thermostat_cfg,
                                                "2025-01-06 21:30:00", "poele", "poele", 5.0)
        poele_off.assert_not_called()
        clim_on.assert_not_called()
        assert store["state"]["active_system"] == "poele"

    def test_late_window_configurable(self, store, base_ha_cfg, base_thermostat_cfg):
        cfg = {**base_thermostat_cfg, "poele_no_start_before_end_minutes": 15}
        poele_on, _, _, _, _ = self._run(store, base_ha_cfg, cfg,
                                         "2025-01-06 21:30:00", None, "poele", 5.0, indoor_temp=19.0)
        poele_on.assert_called_once()


class TestTurnOffAll:
    """R-04 : une extinction « forte » coupe aussi la clim maintenue en chevauchement clim → poêle."""

    @pytest.fixture
    def store(self):
        data = {"state": {
            "state": "on", "active_system": "poele", "sensor_failures": 0, "suspended_until": None,
            "clim_overlap_until": "2025-01-06T10:15:00",
            "system_history": {"poele": {"last_turned_on": "2025-01-06T09:55:00", "last_turned_off": None},
                               "clim": {"last_turned_on": "2025-01-06T07:00:00", "last_turned_off": None}},
        }}
        with patch("modules.thermostat._load_state", side_effect=lambda: dict(data["state"])), \
             patch("modules.thermostat._save_state", side_effect=lambda s: data.__setitem__("state", s)), \
             freeze_time("2025-01-06 10:00:00"):
            yield data

    def _run(self, base_ha_cfg, base_thermostat_cfg, vacation=True, absence=False, clim_off_ok=True):
        with patch("modules.thermostat.is_on_vacation", return_value=vacation), \
             patch("modules.thermostat.is_in_absence_schedule", return_value=absence), \
             patch("modules.homeassistant.turn_off", return_value=True) as poele_off, \
             patch("modules.homeassistant.turn_off_clim", return_value=clim_off_ok) as clim_off, \
             patch("modules.ntfy_push.send") as ntfy:
            check_and_apply(base_ha_cfg, base_thermostat_cfg, "poele", ntfy_cfg={"enabled": True})
        return poele_off, clim_off, ntfy

    def test_vacation_during_overlap_turns_off_clim_too(self, store, base_ha_cfg, base_thermostat_cfg):
        poele_off, clim_off, ntfy = self._run(base_ha_cfg, base_thermostat_cfg)
        poele_off.assert_called_once()
        clim_off.assert_called_once()
        assert store["state"]["state"] == "off"
        assert store["state"]["active_system"] is None
        assert store["state"]["clim_overlap_until"] is None
        assert store["state"]["system_history"]["clim"]["last_turned_off"]
        assert store["state"]["system_history"]["poele"]["last_turned_off"]
        assert "vacances" in ntfy.call_args[0][1]

    def test_absence_schedule_during_overlap_turns_off_clim_too(self, store, base_ha_cfg, base_thermostat_cfg):
        poele_off, clim_off, _ = self._run(base_ha_cfg, base_thermostat_cfg, vacation=False, absence=True)
        poele_off.assert_called_once()
        clim_off.assert_called_once()
        assert store["state"]["state"] == "off"
        assert store["state"]["clim_overlap_until"] is None

    def test_vacation_overlap_clim_left_after_manual_poele_off(self, store, base_ha_cfg, base_thermostat_cfg):
        # Poêle éteint à la main pendant le chevauchement : la clim tourne encore
        store["state"] = {**store["state"], "state": "off", "active_system": None}
        poele_off, clim_off, _ = self._run(base_ha_cfg, base_thermostat_cfg)
        poele_off.assert_not_called()
        clim_off.assert_called_once()
        assert store["state"]["clim_overlap_until"] is None

    def test_failed_overlap_clim_off_keeps_state_and_retries(self, store, base_ha_cfg, base_thermostat_cfg):
        _, _, ntfy = self._run(base_ha_cfg, base_thermostat_cfg, clim_off_ok=False)
        assert store["state"]["state"] == "on"
        assert store["state"]["clim_overlap_until"] == "2025-01-06T10:15:00"
        assert store["state"]["command_failures"] == 1
        assert not any("éteint" in c[0][0] for c in ntfy.call_args_list)
        _, clim_off, _ = self._run(base_ha_cfg, base_thermostat_cfg)
        clim_off.assert_called_once()
        assert store["state"]["state"] == "off"
        assert store["state"]["clim_overlap_until"] is None

    def test_nothing_to_do_when_off_without_overlap(self, store, base_ha_cfg, base_thermostat_cfg):
        store["state"] = {**store["state"], "state": "off", "active_system": None, "clim_overlap_until": None}
        poele_off, clim_off, ntfy = self._run(base_ha_cfg, base_thermostat_cfg)
        poele_off.assert_not_called()
        clim_off.assert_not_called()
        ntfy.assert_not_called()


class TestSensorSafeguards:
    """F-05 : garde-fous quand la sonde est HS ou que la température s'envole."""

    @pytest.fixture
    def store(self):
        data = {"state": {
            "state": "on", "active_system": "poele", "sensor_failures": 0, "suspended_until": None,
            "system_history": {"poele": {"last_turned_on": "2025-01-06T07:00:00", "last_turned_off": None},
                               "clim": {"last_turned_on": None, "last_turned_off": None}},
        }}
        with patch("modules.thermostat._load_state", side_effect=lambda: dict(data["state"])), \
             patch("modules.thermostat._save_state", side_effect=lambda s: data.__setitem__("state", s)), \
             patch("modules.thermostat.is_on_vacation", return_value=False), \
             patch("modules.thermostat.is_in_absence_schedule", return_value=False), \
             freeze_time("2025-01-06 10:00:00"):  # lundi, dans la plage
            yield data

    def _run(self, base_ha_cfg, cfg, indoor_temp=None):
        indoor = {"temperature": indoor_temp, "humidity": 50.0} if indoor_temp is not None else None
        with patch("modules.homeassistant.get_indoor_climate", return_value=indoor), \
             patch("modules.homeassistant.is_clim_configured", return_value=False), \
             patch("modules.homeassistant.get_state", return_value={"state": "heat"}), \
             patch("modules.homeassistant.turn_off", return_value=True) as poele_off, \
             patch("modules.ntfy_push.send") as ntfy:
            check_and_apply(base_ha_cfg, cfg, "poele", ntfy_cfg={"enabled": True})
        return poele_off, ntfy

    def test_blind_ceiling_turns_off(self, store, base_ha_cfg, base_thermostat_cfg):
        store["state"]["sensor_failure_since"] = "2025-01-06T06:30:00"
        poele_off, ntfy = self._run(base_ha_cfg, base_thermostat_cfg)
        poele_off.assert_called_once()
        assert store["state"]["state"] == "off"
        assert any("sans mesure" in c[0][1] for c in ntfy.call_args_list)

    def test_blind_ceiling_counts_from_sensor_loss(self, store, base_ha_cfg, base_thermostat_cfg):
        # Allumé depuis 3 h mais sonde perdue depuis 1 h seulement : la régulation était valide avant
        store["state"]["sensor_failure_since"] = "2025-01-06T09:00:00"
        poele_off, _ = self._run(base_ha_cfg, base_thermostat_cfg)
        poele_off.assert_not_called()
        assert store["state"]["state"] == "on"

    def test_blind_ceiling_configurable_and_disableable(self, store, base_ha_cfg, base_thermostat_cfg):
        store["state"]["sensor_failure_since"] = "2025-01-06T06:30:00"
        poele_off, _ = self._run(base_ha_cfg, {**base_thermostat_cfg, "max_on_hours_without_sensor": 0})
        poele_off.assert_not_called()
        poele_off, _ = self._run(base_ha_cfg, {**base_thermostat_cfg, "max_on_hours_without_sensor": 4})
        poele_off.assert_not_called()
        poele_off, _ = self._run(base_ha_cfg, {**base_thermostat_cfg, "max_on_hours_without_sensor": 2})
        poele_off.assert_called_once()

    def test_first_failure_records_start_of_outage(self, store, base_ha_cfg, base_thermostat_cfg):
        self._run(base_ha_cfg, base_thermostat_cfg)
        assert store["state"]["sensor_failure_since"] == "2025-01-06T10:00:00"
        assert store["state"]["state"] == "on"

    def test_sensor_alert_sent_via_ntfy_and_recovery(self, store, base_ha_cfg, base_thermostat_cfg):
        alerts = []
        for _ in range(3):
            _, ntfy = self._run(base_ha_cfg, base_thermostat_cfg)
            alerts += [c for c in ntfy.call_args_list if "injoignable" in c[0][0]]
        assert len(alerts) == 1
        assert alerts[0][0][2] == {"enabled": True}
        _, ntfy = self._run(base_ha_cfg, base_thermostat_cfg, indoor_temp=21.0)
        assert any("de nouveau joignable" in c[0][0] for c in ntfy.call_args_list)
        assert store["state"]["sensor_failures"] == 0
        assert store["state"]["sensor_failure_since"] is None

    def test_overheat_turns_off_before_min_on(self, store, base_ha_cfg, base_thermostat_cfg):
        store["state"]["system_history"]["poele"]["last_turned_on"] = "2025-01-06T09:30:00"  # 30 min < 90
        poele_off, ntfy = self._run(base_ha_cfg, base_thermostat_cfg, indoor_temp=24.5)  # ≥ 22.9 + 1.5
        poele_off.assert_called_once()
        assert store["state"]["state"] == "off"
        assert "surchauffe" in ntfy.call_args[0][0]

    def test_no_overheat_below_margin_before_min_on(self, store, base_ha_cfg, base_thermostat_cfg):
        store["state"]["system_history"]["poele"]["last_turned_on"] = "2025-01-06T09:30:00"
        poele_off, _ = self._run(base_ha_cfg, base_thermostat_cfg, indoor_temp=24.0)
        poele_off.assert_not_called()
        assert store["state"]["state"] == "on"

    def test_overheat_margin_configurable(self, store, base_ha_cfg, base_thermostat_cfg):
        store["state"]["system_history"]["poele"]["last_turned_on"] = "2025-01-06T09:30:00"
        poele_off, _ = self._run(base_ha_cfg, {**base_thermostat_cfg, "overheat_margin": 1.0}, indoor_temp=24.0)
        poele_off.assert_called_once()


class TestOptimalStart:
    """Démarrage optimisé : en pilotage, l'allumage du matin est retardé ; en simulation, jamais."""

    @pytest.fixture
    def store(self):
        data = {"state": {
            "state": "off", "active_system": None, "sensor_failures": 0, "suspended_until": None,
            "system_history": {"poele": {"last_turned_on": None, "last_turned_off": None},
                               "clim": {"last_turned_on": None, "last_turned_off": None}},
        }}
        with patch("modules.thermostat._load_state", side_effect=lambda: dict(data["state"])), \
             patch("modules.thermostat._save_state", side_effect=lambda s: data.__setitem__("state", s)), \
             patch("modules.thermostat.is_on_vacation", return_value=False), \
             patch("modules.thermostat.is_in_absence_schedule", return_value=False), \
             patch("modules.warmup.get_events", return_value=[
                 {"start_ts": f"2025-01-0{i}T05:45:00", "system": "poele", "outdoor_avg": 5.0, "indoor_start": 18.0,
                  "indoor_peak": 19.5, "lag_minutes": 15, "rate": 2.0} for i in range(1, 4)]):
            yield data

    def _run(self, cfg, base_ha_cfg, now, indoor_temp):
        with freeze_time(now), \
             patch("modules.homeassistant.get_indoor_climate", return_value={"temperature": indoor_temp, "humidity": 50.0}), \
             patch("modules.homeassistant.get_state", return_value={"state": "off"}), \
             patch("modules.homeassistant.is_clim_configured", return_value=False), \
             patch("modules.homeassistant.turn_on", return_value=True) as poele_on, \
             patch("modules.ntfy_push.send"):
            check_and_apply(base_ha_cfg, cfg, "poele", ntfy_cfg={"enabled": True}, outdoor_temp=5.0,
                            cost_per_hour={"poele": 0.42, "clim": 0.2})
        return poele_on

    def _cfg(self, base_thermostat_cfg, mode):
        schedule = {**base_thermostat_cfg["schedule"], "mon": {"start": "05:45", "end": "22:00", "comfort": "06:30"}}
        return {**base_thermostat_cfg, "schedule": schedule, "optimal_start": {"mode": mode, "safety_margin_minutes": 15}}

    def test_pilot_delays_then_ignites(self, store, base_ha_cfg, base_thermostat_cfg):
        cfg = self._cfg(base_thermostat_cfg, "pilot")
        # Ressenti 19,9 °C : 15 + 3 min + 15 de marge → au plus tard 05:57
        self._run(cfg, base_ha_cfg, "2025-01-06 05:45:00", 19.9).assert_not_called()
        assert store["state"]["state"] == "off"
        self._run(cfg, base_ha_cfg, "2025-01-06 05:50:00", 19.9).assert_called_once()
        assert store["state"]["state"] == "on"

    def test_simulation_never_delays(self, store, base_ha_cfg, base_thermostat_cfg):
        cfg = self._cfg(base_thermostat_cfg, "simulation")
        self._run(cfg, base_ha_cfg, "2025-01-06 05:45:00", 19.9).assert_called_once()

    def test_pilot_outside_morning_window(self, store, base_ha_cfg, base_thermostat_cfg):
        cfg = self._cfg(base_thermostat_cfg, "pilot")
        self._run(cfg, base_ha_cfg, "2025-01-06 10:00:00", 19.9).assert_called_once()

    def test_error_never_blocks(self, store, base_ha_cfg, base_thermostat_cfg):
        cfg = self._cfg(base_thermostat_cfg, "pilot")
        with patch("modules.warmup.plan", side_effect=RuntimeError("boom")):
            self._run(cfg, base_ha_cfg, "2025-01-06 05:45:00", 19.9).assert_called_once()
