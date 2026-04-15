"""Tests pour les horaires d'absence récurrents.

Couvre :
- get_absence_schedules : liste vide, après ajout
- add_absence_schedule : persistance, tri des jours, préservation état
- remove_absence_schedule : suppression valide, index hors-bornes
- is_in_absence_schedule : dans la plage, avant, après, mauvais jour,
  bornes exactes, agenda corrompu, plusieurs créneaux
- Cycle complet : ajouter → actif → supprimer
- check_and_apply : horaire actif éteint le chauffage, horaire inactif ne bloque pas
"""

import json
from unittest.mock import patch
from freezegun import freeze_time

import pytest

from modules.thermostat import (
    get_absence_schedules,
    add_absence_schedule,
    remove_absence_schedule,
    is_in_absence_schedule,
    check_and_apply,
    _load_state,
    _save_state,
    STATE_FILE,
)


# ── Fixture : état isolé par test ────────────────────────────────


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Chaque test utilise son propre fichier d'état."""
    state_file = str(tmp_path / "thermostat_state.json")
    monkeypatch.setattr("modules.thermostat.STATE_FILE", state_file)
    with open(state_file, "w") as f:
        json.dump({"state": "off", "active_system": None}, f)
    return state_file


# ── get_absence_schedules ─────────────────────────────────────────


class TestGetAbsenceSchedules:

    def test_returns_empty_list_by_default(self):
        assert get_absence_schedules() == []

    def test_returns_schedules_after_add(self):
        add_absence_schedule(["mon", "fri"], "08:00", "18:00")
        schedules = get_absence_schedules()
        assert len(schedules) == 1
        assert schedules[0]["start"] == "08:00"
        assert schedules[0]["end"] == "18:00"

    def test_returns_multiple_schedules(self):
        add_absence_schedule(["mon"], "08:00", "12:00")
        add_absence_schedule(["sat", "sun"], "10:00", "14:00")
        assert len(get_absence_schedules()) == 2


# ── add_absence_schedule ──────────────────────────────────────────


class TestAddAbsenceSchedule:

    def test_persists_days_start_end(self):
        add_absence_schedule(["mon", "tue", "wed", "thu", "fri"], "07:00", "19:00")
        state = _load_state()
        schedules = state["absence_schedules"]
        assert len(schedules) == 1
        assert "mon" in schedules[0]["days"]
        assert schedules[0]["start"] == "07:00"
        assert schedules[0]["end"] == "19:00"

    def test_days_are_sorted_by_week_order(self):
        # Ajout dans le désordre
        add_absence_schedule(["fri", "mon", "wed"], "08:00", "18:00")
        schedules = get_absence_schedules()
        assert schedules[0]["days"] == ["mon", "wed", "fri"]

    def test_does_not_erase_other_state_fields(self):
        _save_state({"state": "on", "active_system": "poele",
                     "suspended_until": "2026-05-01T12:00:00"})
        add_absence_schedule(["mon"], "08:00", "18:00")
        state = _load_state()
        assert state["state"] == "on"
        assert state["suspended_until"] == "2026-05-01T12:00:00"

    def test_appends_without_overwriting_previous(self):
        add_absence_schedule(["mon"], "08:00", "12:00")
        add_absence_schedule(["tue"], "13:00", "17:00")
        schedules = get_absence_schedules()
        assert len(schedules) == 2
        assert schedules[0]["days"] == ["mon"]
        assert schedules[1]["days"] == ["tue"]


# ── remove_absence_schedule ───────────────────────────────────────


class TestRemoveAbsenceSchedule:

    def test_removes_correct_schedule(self):
        add_absence_schedule(["mon"], "08:00", "12:00")
        add_absence_schedule(["tue"], "13:00", "17:00")
        remove_absence_schedule(0)
        schedules = get_absence_schedules()
        assert len(schedules) == 1
        assert schedules[0]["days"] == ["tue"]

    def test_out_of_range_index_is_silent(self):
        add_absence_schedule(["mon"], "08:00", "12:00")
        remove_absence_schedule(5)  # ne doit pas lever d'exception
        assert len(get_absence_schedules()) == 1

    def test_negative_index_is_silent(self):
        add_absence_schedule(["mon"], "08:00", "12:00")
        remove_absence_schedule(-1)
        assert len(get_absence_schedules()) == 1

    def test_remove_on_empty_list_is_noop(self):
        remove_absence_schedule(0)
        assert get_absence_schedules() == []

    def test_does_not_erase_other_state_fields(self):
        _save_state({"state": "on", "active_system": "poele",
                     "absence_schedules": [{"days": ["mon"], "start": "08:00", "end": "18:00"}]})
        remove_absence_schedule(0)
        state = _load_state()
        assert state["state"] == "on"
        assert state["active_system"] == "poele"


# ── is_in_absence_schedule ────────────────────────────────────────


class TestIsInAbsenceSchedule:

    def test_returns_false_when_no_schedules(self):
        assert is_in_absence_schedule() is False

    @freeze_time("2026-04-15 10:00:00")  # mercredi 10h
    def test_returns_true_when_inside_range(self):
        add_absence_schedule(["wed"], "08:00", "18:00")
        assert is_in_absence_schedule() is True

    @freeze_time("2026-04-15 07:59:00")  # mercredi 07h59
    def test_returns_false_before_start(self):
        add_absence_schedule(["wed"], "08:00", "18:00")
        assert is_in_absence_schedule() is False

    @freeze_time("2026-04-15 18:01:00")  # mercredi 18h01
    def test_returns_false_after_end(self):
        add_absence_schedule(["wed"], "08:00", "18:00")
        assert is_in_absence_schedule() is False

    @freeze_time("2026-04-15 10:00:00")  # mercredi
    def test_returns_false_wrong_day(self):
        add_absence_schedule(["mon", "fri"], "08:00", "18:00")
        assert is_in_absence_schedule() is False

    @freeze_time("2026-04-15 08:00:00")  # borne début exacte
    def test_returns_true_on_exact_start_boundary(self):
        add_absence_schedule(["wed"], "08:00", "18:00")
        assert is_in_absence_schedule() is True

    @freeze_time("2026-04-15 18:00:00")  # borne fin exacte
    def test_returns_true_on_exact_end_boundary(self):
        add_absence_schedule(["wed"], "08:00", "18:00")
        assert is_in_absence_schedule() is True

    @freeze_time("2026-04-15 10:00:00")  # mercredi
    def test_matches_one_of_multiple_schedules(self):
        add_absence_schedule(["mon", "fri"], "08:00", "17:00")
        add_absence_schedule(["wed"], "09:00", "18:00")
        assert is_in_absence_schedule() is True

    @freeze_time("2026-04-15 10:00:00")  # mercredi
    def test_corrupted_schedule_entry_is_skipped(self):
        _save_state({"absence_schedules": [
            {"days": ["wed"], "start": "not-a-time", "end": "also-bad"},
            {"days": ["wed"], "start": "09:00", "end": "18:00"},
        ]})
        assert is_in_absence_schedule() is True  # la 2e entrée valide est trouvée

    @freeze_time("2026-04-19 10:00:00")  # samedi
    def test_weekend_schedule(self):
        add_absence_schedule(["sat", "sun"], "09:00", "14:00")
        assert is_in_absence_schedule() is True

    @freeze_time("2026-04-21 10:00:00")  # lundi (lendemain du dimanche)
    def test_schedule_does_not_bleed_to_next_day(self):
        add_absence_schedule(["sun"], "08:00", "20:00")
        assert is_in_absence_schedule() is False


# ── Cycle complet ─────────────────────────────────────────────────


class TestAbsenceScheduleFullCycle:

    @freeze_time("2026-04-15 10:30:00")  # mercredi 10h30
    def test_add_active_remove(self):
        # 1. Aucun horaire
        assert is_in_absence_schedule() is False
        assert get_absence_schedules() == []

        # 2. Ajouter un créneau qui couvre maintenant
        add_absence_schedule(["wed"], "08:00", "18:00")
        assert is_in_absence_schedule() is True
        assert len(get_absence_schedules()) == 1

        # 3. Supprimer
        remove_absence_schedule(0)
        assert is_in_absence_schedule() is False
        assert get_absence_schedules() == []

    @freeze_time("2026-04-15 07:00:00")  # mercredi 07h (hors créneau)
    def test_schedule_outside_range_not_active(self):
        add_absence_schedule(["wed"], "08:00", "18:00")
        assert is_in_absence_schedule() is False
        assert len(get_absence_schedules()) == 1


# ── check_and_apply : horaire d'absence ──────────────────────────


class TestCheckAndApplyWithAbsenceSchedule:

    @freeze_time("2026-04-15 10:00:00")  # mercredi dans la plage
    @patch("modules.thermostat.is_on_vacation", return_value=False)
    @patch("modules.thermostat.is_in_absence_schedule", return_value=True)
    @patch("modules.thermostat._load_state")
    @patch("modules.thermostat._save_state")
    @patch("modules.homeassistant.turn_off", return_value=True)
    @patch("modules.ntfy_push.send")
    def test_active_schedule_turns_off_heating(
        self, mock_ntfy, mock_ha_off, mock_save, mock_load,
        mock_sched, mock_vac, base_thermostat_cfg, base_ha_cfg
    ):
        """Horaire d'absence actif → éteindre si allumé."""
        mock_load.return_value = {
            "state": "on",
            "active_system": "poele",
            "suspended_until": None,
            "everyone_away": False,
            "sensor_failures": 0,
            "system_history": {
                "poele": {"last_turned_on": "2026-04-15T06:00:00", "last_turned_off": None},
                "clim": {"last_turned_on": None, "last_turned_off": None},
            },
        }
        check_and_apply(base_ha_cfg, base_thermostat_cfg, "poele")
        mock_ha_off.assert_called_once()

    @freeze_time("2026-04-15 07:00:00")  # mercredi hors créneau
    @patch("modules.thermostat.is_on_vacation", return_value=False)
    @patch("modules.thermostat.is_in_absence_schedule", return_value=False)
    @patch("modules.thermostat._load_state")
    @patch("modules.thermostat._save_state")
    @patch("modules.homeassistant.get_indoor_climate", return_value={"temperature": 18.0, "humidity": 50.0})
    @patch("modules.homeassistant.get_state", return_value={"state": "off"})
    @patch("modules.homeassistant.turn_on", return_value=True)
    @patch("modules.ntfy_push.send")
    def test_inactive_schedule_does_not_block_thermostat(
        self, mock_ntfy, mock_ha_on, mock_ha_state, mock_indoor,
        mock_save, mock_load, mock_sched, mock_vac,
        base_thermostat_cfg, base_ha_cfg
    ):
        """Horaire d'absence inactif → thermostat fonctionne normalement."""
        mock_load.return_value = {
            "state": "off",
            "active_system": None,
            "suspended_until": None,
            "everyone_away": False,
            "sensor_failures": 0,
            "system_history": {
                "poele": {"last_turned_on": None, "last_turned_off": None},
                "clim": {"last_turned_on": None, "last_turned_off": None},
            },
        }
        check_and_apply(base_ha_cfg, base_thermostat_cfg, "poele")
        mock_ha_on.assert_called_once()

    @freeze_time("2026-04-15 10:00:00")
    @patch("modules.thermostat.is_on_vacation", return_value=False)
    @patch("modules.thermostat.is_in_absence_schedule", return_value=True)
    @patch("modules.thermostat._load_state")
    @patch("modules.thermostat._save_state")
    @patch("modules.homeassistant.turn_off", return_value=True)
    @patch("modules.ntfy_push.send")
    def test_active_schedule_does_not_turn_off_when_already_off(
        self, mock_ntfy, mock_ha_off, mock_save, mock_load,
        mock_sched, mock_vac, base_thermostat_cfg, base_ha_cfg
    ):
        """Horaire actif mais système déjà éteint → turn_off non appelé."""
        mock_load.return_value = {
            "state": "off",
            "active_system": None,
            "suspended_until": None,
            "everyone_away": False,
            "sensor_failures": 0,
            "system_history": {
                "poele": {"last_turned_on": None, "last_turned_off": None},
                "clim": {"last_turned_on": None, "last_turned_off": None},
            },
        }
        check_and_apply(base_ha_cfg, base_thermostat_cfg, "poele")
        mock_ha_off.assert_not_called()
