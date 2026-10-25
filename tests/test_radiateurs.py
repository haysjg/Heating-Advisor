"""Tests unitaires pour modules/radiateurs.py — rallumage après jour rouge et restriction week-end."""

from datetime import datetime

import pytest

import modules.radiateurs as radiateurs
from modules.radiateurs import split_turn_back_on

COMBLES = "switch.prise_radiateur_combles_switch_0"
SALON = "climate.radiateur_salon"
SATURDAY = datetime(2026, 11, 7, 10, 0)
SUNDAY = datetime(2026, 11, 8, 10, 0)
MONDAY = datetime(2026, 11, 9, 10, 0)


class TestSplitTurnBackOn:
    def test_weekend_combles_postponed_other_radiators_turned_on(self):
        on, later = split_turn_back_on([COMBLES, SALON], [COMBLES, SALON], [COMBLES], SATURDAY, False)
        assert on == [SALON]
        assert later == [COMBLES]

    def test_sunday_also_postponed(self):
        assert split_turn_back_on([COMBLES], [COMBLES], [COMBLES], SUNDAY, False) == ([], [COMBLES])

    def test_monday_turned_back_on(self):
        assert split_turn_back_on([COMBLES], [COMBLES], [COMBLES], MONDAY, False) == ([COMBLES], [])

    def test_weekend_allowed_by_user(self):
        assert split_turn_back_on([COMBLES], [COMBLES], [COMBLES], SATURDAY, True) == ([COMBLES], [])

    def test_unconfigured_radiator_ignored(self):
        assert split_turn_back_on([COMBLES], [SALON], [COMBLES], MONDAY, False) == ([], [])


class TestWeekendSetting:
    @pytest.fixture(autouse=True)
    def setting_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(radiateurs, "WEEKEND_FILE", str(tmp_path / "radiateurs_weekend.json"))

    def test_refused_by_default(self):
        assert radiateurs.weekend_auto_allowed() is False

    def test_persisted(self):
        radiateurs.set_weekend_auto_allowed(True)
        assert radiateurs.weekend_auto_allowed() is True
        radiateurs.set_weekend_auto_allowed(False)
        assert radiateurs.weekend_auto_allowed() is False
