"""Tests unitaires pour modules/warmup.py (démarrage optimisé)."""

from datetime import datetime, timedelta

import pytest

import modules.history as history
import modules.warmup as warmup


def _readings(start: str, indoor: list, poele: list, outdoor=5.0, clim=None):
    """Lectures toutes les 10 min à partir de `start`."""
    t0 = datetime.fromisoformat(start)
    clim = clim or [None] * len(indoor)
    return [
        {"ts": (t0 + timedelta(minutes=10 * i)).isoformat(timespec="seconds"), "outdoor_temp": outdoor,
         "indoor_temp": t, "poele_state": p, "clim_state": c}
        for i, (t, p, c) in enumerate(zip(indoor, poele, clim))
    ]


# 05:40 arrêt à 18,0 °C, poêle en marche à 05:50 (démarrage estimé 05:45), +0,2 °C à 06:10, puis 1 °C/h jusqu'à 07:10
MORNING = _readings(
    "2026-11-09T05:40:00",
    [18.0, 18.0, 18.1, 18.2, 18.4, 18.5, 18.7, 18.9, 19.0, 19.2, 19.0],
    ["off", "on", "on", "on", "on", "on", "on", "on", "on", "on", "off"],
)


class TestExtractEvents:
    def test_morning_run(self):
        [e] = warmup.extract_events(MORNING)
        assert e["system"] == "poele"
        assert e["start_ts"] == "2026-11-09T05:45:00"
        assert e["lag_minutes"] == 25                  # 05:45 → 06:10 (18,2 °C)
        assert e["rate"] == pytest.approx(1.0, abs=0.01)  # 18,2 → 19,2 en 1 h
        assert (e["indoor_start"], e["indoor_peak"], e["outdoor_avg"]) == (18.0, 19.2, 5.0)

    def test_afternoon_run_ignored(self):
        afternoon = [{**r, "ts": r["ts"].replace("T05", "T14").replace("T06", "T15").replace("T07", "T16")}
                     for r in MORNING]
        assert warmup.extract_events(afternoon) == []

    def test_other_system_on_discards(self):
        mixed = [dict(r) for r in MORNING]
        mixed[5]["clim_state"] = "on"
        assert warmup.extract_events(mixed) == []

    def test_clim_run(self):
        clim = [dict(r, poele_state="off", clim_state="on" if r["poele_state"] == "on" else "off") for r in MORNING]
        [e] = warmup.extract_events(clim)
        assert e["system"] == "clim"

    def test_too_small_rise_discarded(self):
        flat = _readings("2026-11-09T05:40:00", [18.0, 18.0, 18.1, 18.2, 18.3, 18.3],
                         ["off", "on", "on", "on", "on", "off"])
        assert warmup.extract_events(flat) == []

    def test_implausible_rate_discarded(self):
        jump = _readings("2026-11-09T05:40:00", [18.0, 18.0, 18.3, 19.5, 21.0, 21.0],
                         ["off", "on", "on", "on", "on", "off"])
        assert warmup.extract_events(jump) == []


def _events(system="poele", temps=(4.0, 5.0, 6.0), rate=1.0, lag=15):
    return [{"start_ts": f"2026-11-0{i + 1}T05:45:00", "system": system, "outdoor_avg": t, "indoor_start": 18.0,
             "indoor_peak": 19.5, "lag_minutes": lag, "rate": rate} for i, t in enumerate(temps)]


class TestPredict:
    def test_duration(self):
        p = warmup.predict(_events(), "poele", 5.0, 1.5)
        assert p == {"minutes": 105, "rate": 1.0, "lag_minutes": 15, "events": 3}   # 15 + 1,5 / 1 × 60

    def test_not_enough_events(self):
        assert warmup.predict(_events(temps=(4.0, 5.0)), "poele", 5.0, 1.0) is None
        assert warmup.predict(_events(), "clim", 5.0, 1.0) is None
        # Loin des températures mesurées : pas d'extrapolation
        assert warmup.predict(_events(), "poele", -5.0, 1.0) is None
        assert warmup.predict(_events(), "poele", None, 1.0) is None

    def test_weighted_by_outdoor_distance(self):
        events = _events(temps=(0.0, 0.0, 0.0), rate=0.5) + _events(temps=(4.0, 4.0, 4.0), rate=1.5)
        p = warmup.predict(events, "poele", 1.0, 1.0)
        assert 0.5 < p["rate"] < 1.0   # plus proche des matins à 0 °C

    def test_progress(self):
        events = _events(temps=(4.0, 4.5, 5.0, 6.2)) + _events(system="clim", temps=(10.0,))
        p = warmup.progress(events, "poele")
        assert p["mornings"] == 4 and p["first_day"] == "2026-11-01"
        bands = {b["outdoor"]: b for b in p["bands"]}
        assert (bands[4]["events"], bands[6]["events"], bands[0]["events"]) == (2, 2, 0)   # [3,5[ et [5,7[
        assert bands[4]["ready"] and bands[6]["ready"] and not bands[0]["ready"]
        assert (p["ready_min"], p["ready_max"]) == (4, 6)
        empty = warmup.progress([], "clim")
        assert (empty["mornings"], empty["first_day"], empty["ready_min"]) == (0, None, None)

    def test_curve(self):
        # Matins à 4, 5 et 6 °C : la courbe ne couvre que les températures assez proches
        points = warmup.curve(_events(), "poele")
        assert [p["outdoor"] for p in points] == [4, 6]


CFG = {
    "temp_on": 20.0,
    "check_interval_minutes": 10,
    "schedule": {"mon": {"start": "05:45", "end": "22:00", "comfort": "06:30"},
                 "sat": {"start": "07:00", "end": "22:00"}},
    "optimal_start": {"mode": "pilot", "safety_margin_minutes": 15},
}
MON = datetime(2026, 11, 9)   # lundi


class TestPlan:
    def test_window(self):
        assert warmup.morning_window(CFG, MON.replace(hour=5)) == (MON.replace(hour=5, minute=45),
                                                                    MON.replace(hour=6, minute=30))
        sat = datetime(2026, 11, 14)
        assert warmup.morning_window(CFG, sat)[1] == sat.replace(hour=7, minute=45)   # début + 45 min
        assert warmup.morning_window(CFG, datetime(2026, 11, 10)) is None             # pas de plage

    def test_outside_window(self):
        assert warmup.plan(CFG, MON.replace(hour=5, minute=30), 19.0, 5.0, "poele", _events()) is None
        assert warmup.plan(CFG, MON.replace(hour=6, minute=30), 19.0, 5.0, "poele", _events()) is None

    def test_delays_then_allows(self):
        # Écart 0,5 °C à 2 °C/h : 15 + 15 = 30 min + 15 min de marge → au plus tard 05:45
        p = warmup.plan(CFG, MON.replace(hour=5, minute=45), 19.5, 5.0, "poele", _events(rate=2.0))
        assert p["latest_start"] == MON.replace(hour=5, minute=45) and p["allow"]
        # Écart 0,1 °C : 15 + 3 + 15 → au plus tard 05:57, on attend
        p = warmup.plan(CFG, MON.replace(hour=5, minute=45), 19.9, 5.0, "poele", _events(rate=2.0))
        assert p["latest_start"] == MON.replace(hour=5, minute=57) and p["allow"] is False
        p = warmup.plan(CFG, MON.replace(hour=5, minute=50), 19.9, 5.0, "poele", _events(rate=2.0))
        assert p["allow"]   # 05:50 + 10 min de cycle dépasse 05:57 : on démarre

    def test_no_prediction_allows(self):
        p = warmup.plan(CFG, MON.replace(hour=5, minute=45), 19.9, 5.0, "poele", [])
        assert p["allow"] and p["prediction"] is None

    def test_no_need(self):
        p = warmup.plan(CFG, MON.replace(hour=5, minute=45), 20.5, 5.0, "poele", _events())
        assert p["allow"] and p["delta"] < 0

    def test_settings_defaults(self):
        assert warmup.settings({}) == {"mode": "simulation", "safety_margin_minutes": 15}
        assert warmup.settings({"optimal_start": {"mode": "bogus"}})["mode"] == "simulation"


class TestJournal:
    @pytest.fixture(autouse=True)
    def db(self, tmp_path, monkeypatch):
        from freezegun import freeze_time
        monkeypatch.setattr(history, "DB_FILE", str(tmp_path / "history.db"))
        with freeze_time("2026-11-09 08:00:00"):   # get_log lit les 30 derniers jours
            yield

    def _tick(self, cfg, hour, minute, felt, actual=None):
        now = MON.replace(hour=hour, minute=minute)
        p = warmup.plan(cfg, now, felt, 5.0, "poele", _events(rate=2.0))
        warmup.observe(cfg, now, felt, 5.0, "poele", actual, 0.42, p)

    def test_simulation_morning(self):
        cfg = {**CFG, "optimal_start": {"mode": "simulation"}}
        self._tick(cfg, 5, 30, 19.8)                                  # avant la plage : ignoré
        self._tick(cfg, 5, 45, 19.8)                                  # besoin : prévu au plus tard 05:54
        ignition = MON.replace(hour=5, minute=46)
        self._tick(cfg, 5, 55, 19.7, ignition)                        # le thermostat a allumé à 05:46
        self._tick(cfg, 6, 15, 20.1, ignition)                        # consigne atteinte
        self._tick(cfg, 6, 35, 20.6, ignition)                        # heure de confort passée
        [e] = warmup.get_log(30)
        assert e["mode"] == "simulation"
        assert e["need_ts"] == "2026-11-09T05:45:00"
        assert e["predicted_minutes"] == 21                           # 15 + 0,2 / 2 × 60
        assert e["planned_start"] == "2026-11-09T05:54:00"            # 06:30 − 21 − 15
        assert e["actual_start"] == "2026-11-09T05:46:00"
        assert e["reached_ts"] == "2026-11-09T06:15:00"
        assert e["indoor_at_comfort"] == 20.6
        assert warmup.saved_minutes(e) == 8
        s = warmup.summarize([e], 20.0)
        assert (s["mornings"], s["saved_minutes"], s["comfort_missed"]) == (1, 8, 0)
        assert s["saved_eur"] == round(8 / 60 * 0.42, 2)

    def test_accuracy(self):
        base = {"mode": "simulation", "need_ts": "2026-11-09T05:45:00", "planned_start": None, "cost_per_hour": 0,
                "indoor_at_comfort": None, "actual_start": "2026-11-09T05:46:00"}
        log = [
            {**base, "predicted_minutes": 30, "reached_ts": "2026-11-09T06:20:00"},   # réel 34 min : juste
            {**base, "predicted_minutes": 30, "reached_ts": "2026-11-09T06:46:00"},   # réel 60 min : faux
            {**base, "predicted_minutes": None, "reached_ts": "2026-11-09T06:20:00"}, # pas de prévision
            {**base, "predicted_minutes": 30, "reached_ts": None},                    # consigne pas atteinte
        ]
        s = warmup.summarize(log, 20.0)
        assert (s["accuracy_checked"], s["accuracy_ok"]) == (2, 1)
        assert warmup.real_minutes(log[0]) == 34 and warmup.real_minutes(log[3]) is None

    def test_log_since_season_start(self):
        from datetime import date
        cfg = {**CFG, "optimal_start": {"mode": "simulation"}}
        self._tick(cfg, 5, 45, 19.8)
        assert len(warmup.get_log(since=date(2026, 9, 1))) == 1
        assert warmup.get_log(since=date(2026, 11, 10)) == []

    def test_pilot_saved_minutes(self):
        e = {"mode": "pilot", "need_ts": "2026-11-09T05:45:00", "planned_start": "2026-11-09T06:00:00",
             "actual_start": "2026-11-09T06:00:00"}
        assert warmup.saved_minutes(e) == 15

    def test_day_off(self):
        cfg = {**CFG, "optimal_start": {"mode": "pilot"},
               "schedule": {"mon": {"start": "05:45", "end": "22:00", "comfort": "05:45"}}}
        assert warmup.disabled_today(cfg, MON.replace(hour=5, minute=45))
        assert warmup.plan(cfg, MON.replace(hour=5, minute=45), 19.0, 5.0, "poele", _events()) is None
        self._tick(cfg, 5, 30, 19.0)                 # avant la plage : rien
        assert warmup.get_log(30) == []
        self._tick(cfg, 5, 45, 19.0)
        self._tick(cfg, 5, 55, 19.0)
        [e] = warmup.get_log(30)
        assert (e["mode"], e["need_ts"]) == ("day_off", None)
        assert warmup.summarize([e], 20.0)["mornings"] == 0

    def test_off_mode_writes_nothing(self):
        self._tick({**CFG, "optimal_start": {"mode": "off"}}, 5, 45, 19.8)
        assert warmup.get_log(30) == []

    def test_refresh_events_kept_after_purge(self):
        from freezegun import freeze_time
        for r in MORNING:
            with freeze_time(r["ts"]):
                history.record(r["outdoor_temp"], r["indoor_temp"], r["poele_state"], "BLUE", None)
        assert warmup.refresh_events() == 1
        assert warmup.refresh_events() == 0
        with freeze_time("2027-01-20 03:00:00"):
            history.purge_old(30)
        assert warmup.refresh_events() == 0
        assert len(warmup.get_events()) == 1
