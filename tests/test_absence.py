"""Tests pour la fonctionnalité Mode Absence (/absence).

Couvre :
- get_vacation / set_vacation / clear_vacation (persistance)
- is_on_vacation : bornes, absence future, absence passée, dates invalides
- Cycle complet : programmer → état → annuler
- Validation API : logique set (start > end, format invalide, champs manquants)
- check_and_apply : absence future ne bloque pas le thermostat
"""

import json
import os
from datetime import datetime
from unittest.mock import patch, MagicMock
from freezegun import freeze_time

import pytest

from modules.thermostat import (
    get_vacation,
    set_vacation,
    clear_vacation,
    is_on_vacation,
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
    # État initial minimal
    with open(state_file, "w") as f:
        json.dump({"state": "off", "active_system": None}, f)
    return state_file


# ── get_vacation ─────────────────────────────────────────────────


class TestGetVacation:

    def test_returns_none_when_no_vacation_set(self):
        vac = get_vacation()
        assert vac == {"start": None, "end": None}

    def test_returns_dates_after_set(self):
        set_vacation("2026-07-01", "2026-07-15")
        vac = get_vacation()
        assert vac["start"] == "2026-07-01"
        assert vac["end"] == "2026-07-15"

    def test_preserves_other_state_fields(self, isolated_state):
        # get_vacation ne doit pas effacer les autres champs d'état
        _save_state({"state": "on", "active_system": "poele",
                     "vacation_start": "2026-07-01", "vacation_end": "2026-07-15"})
        get_vacation()
        state = _load_state()
        assert state["state"] == "on"
        assert state["active_system"] == "poele"


# ── set_vacation ─────────────────────────────────────────────────


class TestSetVacation:

    def test_persists_start_and_end(self):
        set_vacation("2026-12-20", "2027-01-05")
        state = _load_state()
        assert state["vacation_start"] == "2026-12-20"
        assert state["vacation_end"] == "2027-01-05"

    def test_overwrites_previous_vacation(self):
        set_vacation("2026-07-01", "2026-07-15")
        set_vacation("2026-08-01", "2026-08-20")
        vac = get_vacation()
        assert vac["start"] == "2026-08-01"
        assert vac["end"] == "2026-08-20"

    def test_does_not_erase_other_state_fields(self):
        _save_state({"state": "on", "active_system": "poele",
                     "suspended_until": "2026-04-15T12:00:00"})
        set_vacation("2026-07-01", "2026-07-15")
        state = _load_state()
        assert state["state"] == "on"
        assert state["suspended_until"] == "2026-04-15T12:00:00"


# ── clear_vacation ────────────────────────────────────────────────


class TestClearVacation:

    def test_clears_both_dates(self):
        set_vacation("2026-07-01", "2026-07-15")
        clear_vacation()
        vac = get_vacation()
        assert vac["start"] is None
        assert vac["end"] is None

    def test_clear_on_empty_state_is_noop(self):
        clear_vacation()  # ne doit pas lever d'exception
        vac = get_vacation()
        assert vac == {"start": None, "end": None}

    def test_does_not_erase_other_state_fields(self):
        _save_state({"state": "on", "active_system": "poele",
                     "vacation_start": "2026-07-01", "vacation_end": "2026-07-15"})
        clear_vacation()
        state = _load_state()
        assert state["state"] == "on"
        assert state["active_system"] == "poele"


# ── is_on_vacation : cas limites ─────────────────────────────────


class TestIsOnVacationEdgeCases:

    @freeze_time("2026-07-15")
    def test_on_end_date_returns_true(self):
        set_vacation("2026-07-01", "2026-07-15")
        assert is_on_vacation() is True

    @freeze_time("2026-07-16")
    def test_day_after_end_returns_false(self):
        set_vacation("2026-07-01", "2026-07-15")
        assert is_on_vacation() is False

    @freeze_time("2026-06-30")
    def test_day_before_start_returns_false(self):
        set_vacation("2026-07-01", "2026-07-15")
        assert is_on_vacation() is False

    @freeze_time("2026-04-15")
    def test_future_vacation_not_yet_active(self):
        """Absence programmée mais future → is_on_vacation = False."""
        set_vacation("2026-07-01", "2026-07-15")
        assert is_on_vacation() is False

    @freeze_time("2026-04-15")
    def test_past_vacation_returns_false(self):
        set_vacation("2026-01-01", "2026-01-10")
        assert is_on_vacation() is False

    def test_invalid_date_format_returns_false(self):
        _save_state({"vacation_start": "not-a-date", "vacation_end": "also-not-a-date"})
        assert is_on_vacation() is False

    def test_only_start_set_returns_false(self):
        _save_state({"vacation_start": "2026-07-01", "vacation_end": None})
        assert is_on_vacation() is False

    def test_only_end_set_returns_false(self):
        _save_state({"vacation_start": None, "vacation_end": "2026-07-15"})
        assert is_on_vacation() is False

    @freeze_time("2026-07-08")
    def test_single_day_vacation(self):
        """start == end → actif uniquement ce jour-là."""
        set_vacation("2026-07-08", "2026-07-08")
        assert is_on_vacation() is True

    @freeze_time("2026-07-09")
    def test_single_day_vacation_day_after(self):
        set_vacation("2026-07-08", "2026-07-08")
        assert is_on_vacation() is False


# ── Cycle complet ─────────────────────────────────────────────────


class TestVacationFullCycle:

    @freeze_time("2026-07-10")
    def test_programme_puis_actif_puis_annule(self):
        # 1. Aucune absence
        assert is_on_vacation() is False
        assert get_vacation() == {"start": None, "end": None}

        # 2. Programmer une absence qui couvre aujourd'hui
        set_vacation("2026-07-01", "2026-07-20")
        assert is_on_vacation() is True
        assert get_vacation()["start"] == "2026-07-01"

        # 3. Annuler
        clear_vacation()
        assert is_on_vacation() is False
        assert get_vacation() == {"start": None, "end": None}

    @freeze_time("2026-04-15")
    def test_programme_future_puis_annule(self):
        set_vacation("2026-07-01", "2026-07-20")
        assert is_on_vacation() is False  # pas encore active
        vac = get_vacation()
        assert vac["start"] == "2026-07-01"

        clear_vacation()
        assert get_vacation() == {"start": None, "end": None}


# ── Validation API (logique extraite) ────────────────────────────


def _validate_vacation_dates(start: str, end: str):
    """
    Réplique la validation de api_thermostat_vacation_set.
    Accepte des datetime ISO complets (YYYY-MM-DDTHH:MM).
    Retourne (True, None) si valide, (False, message) sinon.
    """
    if not start or not end:
        return False, "Dates invalides (format YYYY-MM-DDTHH:MM attendu)"
    try:
        s = datetime.fromisoformat(start)
        e = datetime.fromisoformat(end)
        if s >= e:
            return False, "Le départ doit être avant le retour"
        return True, None
    except Exception:
        return False, "Dates invalides (format YYYY-MM-DDTHH:MM attendu)"


class TestVacationApiValidation:

    def test_valid_datetimes_pass(self):
        ok, err = _validate_vacation_dates("2026-07-01T08:00", "2026-07-15T18:00")
        assert ok is True
        assert err is None

    def test_same_day_different_times_passes(self):
        """Départ et retour le même jour avec des heures différentes."""
        ok, err = _validate_vacation_dates("2026-07-08T08:00", "2026-07-08T20:00")
        assert ok is True

    def test_start_after_end_rejected(self):
        ok, err = _validate_vacation_dates("2026-07-20T08:00", "2026-07-01T18:00")
        assert ok is False
        assert "retour" in err or "avant" in err

    def test_start_equals_end_rejected(self):
        """Même datetime de départ et de retour → durée nulle, rejeté."""
        ok, err = _validate_vacation_dates("2026-07-08T14:00", "2026-07-08T14:00")
        assert ok is False

    def test_invalid_format_rejected(self):
        ok, err = _validate_vacation_dates("2026/07/01", "2026/07/15")
        assert ok is False

    def test_empty_start_rejected(self):
        ok, err = _validate_vacation_dates("", "2026-07-15T18:00")
        assert ok is False

    def test_empty_end_rejected(self):
        ok, err = _validate_vacation_dates("2026-07-01T08:00", "")
        assert ok is False

    def test_none_start_rejected(self):
        ok, err = _validate_vacation_dates(None, "2026-07-15T18:00")
        assert ok is False

    def test_garbage_values_rejected(self):
        ok, err = _validate_vacation_dates("not-a-date", "also-not")
        assert ok is False


# ── is_on_vacation : tests avec horaires (datetime complet) ─────


class TestIsOnVacationWithDatetime:
    """Tests du mode vacances avec heures de départ et de retour.

    Scénario de référence : départ 23/04 à 14h00, retour 25/04 à 18h00.
    Vérifie les bornes exactes et les cas limites à l'heure près.
    """

    START = "2026-04-23T14:00"
    END   = "2026-04-25T18:00"

    @freeze_time("2026-04-23 15:00:00")
    def test_during_vacation_returns_true(self):
        set_vacation(self.START, self.END)
        assert is_on_vacation() is True

    @freeze_time("2026-04-23 14:00:00")
    def test_exactly_at_start_returns_true(self):
        set_vacation(self.START, self.END)
        assert is_on_vacation() is True

    @freeze_time("2026-04-25 18:00:00")
    def test_exactly_at_end_returns_true(self):
        set_vacation(self.START, self.END)
        assert is_on_vacation() is True

    @freeze_time("2026-04-23 13:59:00")
    def test_one_minute_before_start_returns_false(self):
        """Le thermostat doit fonctionner jusqu'à l'heure de départ exacte."""
        set_vacation(self.START, self.END)
        assert is_on_vacation() is False

    @freeze_time("2026-04-25 18:01:00")
    def test_one_minute_after_end_returns_false(self):
        """Le thermostat reprend dès l'heure de retour passée."""
        set_vacation(self.START, self.END)
        assert is_on_vacation() is False

    @freeze_time("2026-04-23 00:00:00")
    def test_morning_of_departure_day_returns_false(self):
        """Le matin du jour de départ, avant l'heure, pas encore en vacances."""
        set_vacation(self.START, self.END)
        assert is_on_vacation() is False

    @freeze_time("2026-04-25 23:59:00")
    def test_evening_of_return_day_after_end_returns_false(self):
        """Le soir du jour de retour, après l'heure, vacances terminées."""
        set_vacation(self.START, self.END)
        assert is_on_vacation() is False

    @freeze_time("2026-04-24 12:00:00")
    def test_middle_day_returns_true(self):
        """Journée entière entre départ et retour → vacances actives."""
        set_vacation(self.START, self.END)
        assert is_on_vacation() is True

    @freeze_time("2026-04-23 14:30:00")
    def test_same_day_vacation_active(self):
        """Vacances départ et retour le même jour."""
        set_vacation("2026-04-23T14:00", "2026-04-23T20:00")
        assert is_on_vacation() is True

    @freeze_time("2026-04-23 20:01:00")
    def test_same_day_vacation_after_end_returns_false(self):
        set_vacation("2026-04-23T14:00", "2026-04-23T20:00")
        assert is_on_vacation() is False


# ── Rétrocompatibilité : anciennes valeurs sans heure ────────────


class TestIsOnVacationBackwardCompat:
    """Les dates stockées sans heure (ancienne version) restent fonctionnelles.

    Règle de conversion appliquée dans is_on_vacation :
    - date seule (start) → 00:00 (actif depuis le début du jour)
    - date seule (end)   → 23:59 (actif jusqu'à la fin du jour)
    """

    @freeze_time("2026-07-10 12:00:00")
    def test_date_only_active_during_range(self):
        set_vacation("2026-07-01", "2026-07-15")
        assert is_on_vacation() is True

    @freeze_time("2026-07-15 23:00:00")
    def test_date_only_end_of_last_day_still_active(self):
        """Fin de journée du dernier jour → toujours actif (end traité à 23:59)."""
        set_vacation("2026-07-01", "2026-07-15")
        assert is_on_vacation() is True

    @freeze_time("2026-07-16 00:00:00")
    def test_date_only_day_after_end_returns_false(self):
        set_vacation("2026-07-01", "2026-07-15")
        assert is_on_vacation() is False

    @freeze_time("2026-07-01 00:00:00")
    def test_date_only_start_of_first_day_active(self):
        """Début de journée du premier jour → actif (start traité à 00:00)."""
        set_vacation("2026-07-01", "2026-07-15")
        assert is_on_vacation() is True


# ── check_and_apply : absence future ne bloque pas le thermostat ─


class TestCheckAndApplyWithVacation:

    @freeze_time("2026-04-15 08:00:00")
    @patch("modules.thermostat.is_on_vacation", return_value=False)
    @patch("modules.thermostat._load_state")
    @patch("modules.thermostat._save_state")
    @patch("modules.homeassistant.get_indoor_climate", return_value={"temperature": 18.0, "humidity": 50.0})
    @patch("modules.homeassistant.get_state", return_value={"state": "off"})
    @patch("modules.homeassistant.turn_on", return_value=True)
    @patch("modules.ntfy_push.send")
    def test_future_vacation_does_not_block_thermostat(
        self, mock_ntfy, mock_ha_on, mock_ha_state, mock_indoor,
        mock_save, mock_load, mock_vacation,
        base_thermostat_cfg, base_ha_cfg
    ):
        """Absence future (is_on_vacation=False) → thermostat fonctionne normalement."""
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

    @patch("modules.thermostat.is_on_vacation", return_value=True)
    @patch("modules.thermostat._load_state")
    @patch("modules.thermostat._save_state")
    @patch("modules.homeassistant.turn_off", return_value=True)
    @patch("modules.ntfy_push.send")
    def test_active_vacation_turns_off_heating(
        self, mock_ntfy, mock_ha_off, mock_save, mock_load, mock_vacation,
        base_thermostat_cfg, base_ha_cfg
    ):
        """Absence active → éteindre le poêle si allumé."""
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
