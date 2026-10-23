"""Tests unitaires pour modules/jsonstore.py et la préservation des clés utilisateur du thermostat."""

import json
import threading

import pytest

from modules import jsonstore
import modules.thermostat as thermostat


def test_save_then_load(tmp_path):
    path = str(tmp_path / "f.json")
    jsonstore.save(path, {"a": 1, "é": "ü"})
    assert jsonstore.load(path, None) == {"a": 1, "é": "ü"}


def test_missing_file_returns_default(tmp_path):
    assert jsonstore.load(str(tmp_path / "absent.json"), []) == []


def test_no_temporary_file_left(tmp_path):
    path = str(tmp_path / "f.json")
    jsonstore.save(path, {"a": 1})
    jsonstore.save(path, {"a": 2})
    assert sorted(p.name for p in tmp_path.iterdir()) == ["f.json", "f.json.bak"]


def test_corrupt_file_set_aside_and_restored_from_backup(tmp_path):
    path = tmp_path / "f.json"
    jsonstore.save(str(path), {"version": 1})
    jsonstore.save(str(path), {"version": 2})      # .bak contient la version 1
    path.write_text('{"version": 3', encoding="utf-8")  # écriture tronquée
    assert jsonstore.load(str(path), None) == {"version": 1}
    assert any(p.name.startswith("f.json.corrupt-") for p in tmp_path.iterdir())
    assert json.loads(path.read_text(encoding="utf-8")) == {"version": 1}


def test_corrupt_without_backup_returns_default(tmp_path):
    path = tmp_path / "f.json"
    path.write_text("{", encoding="utf-8")
    assert jsonstore.load(str(path), {"défaut": True}) == {"défaut": True}


def test_concurrent_updates_are_not_lost(tmp_path):
    path = str(tmp_path / "compteur.json")
    jsonstore.save(path, {"n": 0})

    def incr():
        for _ in range(50):
            jsonstore.update(path, lambda d: d.__setitem__("n", d["n"] + 1), {"n": 0})

    threads = [threading.Thread(target=incr) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert jsonstore.load(path, None) == {"n": 200}


class TestThermostatUserKeys:
    """Une sauvegarde du thermostat ne doit pas écraser une absence activée pendant le check."""

    @pytest.fixture(autouse=True)
    def state_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(thermostat, "STATE_FILE", str(tmp_path / "thermostat_state.json"))

    def test_absent_set_during_check_survives_thermostat_save(self):
        snapshot = thermostat._load_state()          # le thermostat lit l'état en début de check
        thermostat.set_absent()                      # l'utilisateur active l'absence pendant le check
        thermostat._save_state({**snapshot, "state": "on"})  # le thermostat sauvegarde sa copie
        assert thermostat.is_absent() is True
        assert thermostat._load_state()["state"] == "on"

    def test_vacation_and_schedules_survive_thermostat_save(self):
        snapshot = thermostat._load_state()
        thermostat.set_vacation("2026-12-20T08:00", "2026-12-27T18:00")
        thermostat.add_absence_schedule(["mon"], "09:00", "17:00")
        thermostat._save_state({**snapshot, "sensor_failures": 2})
        assert thermostat.get_vacation() == {"start": "2026-12-20T08:00", "end": "2026-12-27T18:00"}
        assert len(thermostat.get_absence_schedules()) == 1

    def test_user_can_clear_absent(self):
        thermostat.set_absent()
        thermostat.clear_absent()
        assert thermostat.is_absent() is False
