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


class TestIsHeating:
    def test_hvac_action_wins(self):
        assert radiateurs.is_heating({"state": "auto", "attributes": {"hvac_action": "heating"}})
        assert not radiateurs.is_heating({"state": "auto", "attributes": {"hvac_action": "off"}})
        assert not radiateurs.is_heating({"state": "heat", "attributes": {"hvac_action": "idle"}})

    def test_state_without_hvac_action(self):
        assert radiateurs.is_heating({"state": "on", "attributes": {}})
        assert not radiateurs.is_heating({"state": "off"})
        assert not radiateurs.is_heating(None)

    def test_climate_without_hvac_action_not_heating(self):
        # Sèche-serviettes Overkiz : « auto » en permanence, sans hvac_action
        assert not radiateurs.is_heating({"state": "auto", "attributes": {"preset_mode": "prog"}})
        assert not radiateurs.is_heating({"state": "heat"})


class TestHeatingKnown:
    def test_known(self):
        assert radiateurs.heating_known({"state": "auto", "attributes": {"hvac_action": "idle"}})
        assert radiateurs.heating_known({"state": "on"})
        assert radiateurs.heating_known({"state": "off"})

    def test_unknown(self):
        assert not radiateurs.heating_known({"state": "auto", "attributes": {"hvac_action": None}})
        assert not radiateurs.heating_known({"state": "heat"})
        assert not radiateurs.heating_known({"state": "unavailable"})
        assert not radiateurs.heating_known(None)


class TestMeterKwh:
    def test_units(self):
        assert radiateurs.meter_kwh({"state": "4251000", "attributes": {"unit_of_measurement": "Wh"}}) == 4251.0
        assert radiateurs.meter_kwh({"state": "12.5", "attributes": {"unit_of_measurement": "kWh"}}) == 12.5
        assert radiateurs.meter_kwh({"state": "4251000", "attributes": {}}) == 4251.0     # Wh par défaut

    def test_unreadable(self):
        assert radiateurs.meter_kwh({"state": "unavailable", "attributes": {"unit_of_measurement": "Wh"}}) is None
        assert radiateurs.meter_kwh({"state": "12", "attributes": {"unit_of_measurement": "W"}}) is None
        assert radiateurs.meter_kwh({"state": "-3", "attributes": {"unit_of_measurement": "kWh"}}) is None
        assert radiateurs.meter_kwh(None) is None


class TestResumeAt:
    def test_parse(self):
        assert radiateurs.parse_resume_at("") == ""
        assert radiateurs.parse_resume_at(None) == ""
        assert radiateurs.parse_resume_at("22:00") == "22:00"
        assert radiateurs.parse_resume_at(" 7:30 ") == "07:30"

    @pytest.mark.parametrize("raw", ["24:00", "22h", "22:60", "abc"])
    def test_parse_invalid(self, raw):
        with pytest.raises(ValueError):
            radiateurs.parse_resume_at(raw)

    def test_cut_minutes(self):
        assert radiateurs.cut_minutes("", 6) == 24 * 60
        assert radiateurs.cut_minutes("22:00", 6) == 16 * 60
        assert radiateurs.cut_minutes("02:30", 6) == 20 * 60 + 30
        assert radiateurs.cut_minutes("06:00", 6) == 24 * 60


LECTURE = "climate.radiateur_coin_lecture"
RED_MON = datetime(2026, 12, 7, 10, 0)   # lundi


def plan(now, color, radiators, managed=()):
    return radiateurs.plan_red_day(now, color, radiators, list(managed), 6, 22)


class TestPlanRedDay:
    def test_red_day_hp_cuts_all(self):
        assert plan(RED_MON, "RED", {LECTURE: "22:00", SALON: ""}) == ([LECTURE, SALON], [])

    def test_resume_time_turns_back_on_same_evening(self):
        now = datetime(2026, 12, 7, 22, 0)
        assert plan(now, "RED", {LECTURE: "22:00", SALON: ""}, [LECTURE, SALON]) == ([], [LECTURE])

    def test_before_resume_time_stays_off(self):
        now = datetime(2026, 12, 7, 21, 59)
        assert plan(now, "RED", {LECTURE: "22:00"}, [LECTURE]) == ([], [])

    def test_no_cut_in_hc_respects_manual_turn_on(self):
        now = datetime(2026, 12, 7, 23, 0)
        assert plan(now, "RED", {SALON: ""}) == ([], [])

    def test_night_resume_after_midnight(self):
        # Couleur du jour calendaire (mardi) avant 6h : sans effet, seul l'horaire compte
        assert plan(datetime(2026, 12, 8, 1, 0), "RED", {LECTURE: "02:00"}, [LECTURE]) == ([], [])
        assert plan(datetime(2026, 12, 8, 2, 0), "RED", {LECTURE: "02:00"}, [LECTURE]) == ([], [LECTURE])

    def test_next_red_day_cuts_again_at_6(self):
        now = datetime(2026, 12, 8, 6, 0)
        assert plan(now, "RED", {LECTURE: "22:00"}) == ([LECTURE], [])

    def test_day_mode_waits_for_blue_or_white(self):
        assert plan(datetime(2026, 12, 8, 5, 59), "BLUE", {SALON: ""}, [SALON]) == ([], [])
        assert plan(datetime(2026, 12, 8, 6, 0), "RED", {SALON: ""}, [SALON]) == ([], [])
        assert plan(datetime(2026, 12, 8, 6, 0), "WHITE", {SALON: ""}, [SALON]) == ([], [SALON])

    def test_blue_day_turns_on_even_before_resume_time(self):
        # Appli arrêtée la nuit : rallumage au premier passage d'un jour non rouge
        assert plan(datetime(2026, 12, 8, 10, 0), "BLUE", {LECTURE: "22:00"}, [LECTURE]) == ([], [LECTURE])

    def test_unknown_color_only_resume_time(self):
        assert plan(RED_MON, "UNKNOWN", {LECTURE: "22:00"}) == ([], [])
        assert plan(datetime(2026, 12, 7, 22, 5), "UNKNOWN", {LECTURE: "22:00"}, [LECTURE]) == ([], [LECTURE])
        assert plan(RED_MON, "UNKNOWN", {SALON: ""}, [SALON]) == ([], [])

    def test_resume_before_cut_hour_end(self):
        # Reprise à 20h : plus d'extinction après 20h même en HP
        assert plan(datetime(2026, 12, 7, 20, 30), "RED", {LECTURE: "20:00"}) == ([], [])


def test_scenario_chambres_19h_autres_jusquau_jour_bleu():
    chambre, sejour = "climate.chambre", "climate.sejour"
    rads = {chambre: "19:00", sejour: ""}
    # Lundi rouge 6h : tout est coupé
    assert plan(datetime(2026, 12, 7, 6, 0), "RED", rads) == ([chambre, sejour], [])
    # 19h : la chambre reprend, le séjour reste éteint
    assert plan(datetime(2026, 12, 7, 19, 0), "RED", rads, [chambre, sejour]) == ([], [chambre])
    # Mardi rouge 6h : chambre recoupée, séjour toujours éteint
    assert plan(datetime(2026, 12, 8, 6, 0), "RED", rads, [sejour]) == ([chambre], [])
    # Mercredi bleu 6h : tout ce que l'appli a éteint se rallume
    assert plan(datetime(2026, 12, 9, 6, 0), "BLUE", rads, [chambre, sejour]) == ([], [chambre, sejour])
