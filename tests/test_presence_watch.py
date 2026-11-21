"""Tests unitaires pour modules/presence_watch.py — alerte traqueurs de présence figés."""

from datetime import datetime, timezone

import pytest

import modules.presence_watch as pw

NOW = datetime(2026, 10, 29, 16, 0, tzinfo=timezone.utc)
TH_CFG = {"presence_enabled": True, "person_entities": ["person.jg", "person.carine"], "tracker_stale_hours": 3}


def _state(last_updated, state="home", name=None, source=None):
    attributes = {"friendly_name": name} if name else {}
    if source:
        attributes["source"] = source
    return {"state": state, "last_updated": last_updated, "attributes": attributes}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(pw, "ALERTS_FILE", str(tmp_path / "tracker_alerts.json"))
    sent = []
    monkeypatch.setattr(pw, "ntfy_send", lambda title, msg, cfg: sent.append(title))
    states = {}
    monkeypatch.setattr(pw.ha_client, "get_entities", lambda cfg, ids: {e: states[e] for e in ids if e in states})
    return sent, states


class TestStaleAges:
    def test_age_in_hours(self):
        ages = pw.stale_ages({"person.a": _state("2026-10-29T12:30:00+00:00")}, NOW)
        assert ages == {"person.a": pytest.approx(3.5)}

    def test_missing_or_invalid_date_ignored(self):
        assert pw.stale_ages({"person.a": {"state": "home"}, "person.b": _state("pas une date")}, NOW) == {}


    def test_tracker_last_reported_wins(self):
        person = _state("2026-10-29T01:00:00+00:00", source="device_tracker.tel")
        trackers = {"device_tracker.tel": {"last_updated": "2026-10-29T01:00:00+00:00",
                                           "last_reported": "2026-10-29T15:00:00+00:00"}}
        assert pw.stale_ages({"person.a": person}, NOW, trackers) == {"person.a": pytest.approx(1)}

    def test_person_date_kept_without_tracker(self):
        person = _state("2026-10-29T12:00:00+00:00", source="device_tracker.absent")
        assert pw.stale_ages({"person.a": person}, NOW, {}) == {"person.a": pytest.approx(4)}


class TestTransitions:
    def test_new_stale_and_recovered(self):
        newly, recovered = pw.transitions({"a": 5, "b": 1, "c": 1}, {"c": "x"}, 3)
        assert newly == ["a"] and recovered == ["c"]

    def test_already_alerted_not_repeated(self):
        assert pw.transitions({"a": 50}, {"a": "x"}, 3) == ([], [])


class TestCheck:
    def test_alert_once_then_recovery(self, env):
        sent, states = env
        states["person.jg"] = _state("2026-10-29T15:50:00+00:00")
        states["person.carine"] = _state("2026-10-21T02:19:00+00:00", name="Carine")

        pw.check({}, TH_CFG, {}, now=NOW)
        assert sent == ["📍 Traqueur figé — Carine"]

        pw.check({}, TH_CFG, {}, now=NOW)
        assert len(sent) == 1  # pas de répétition

        states["person.carine"] = _state("2026-10-29T15:55:00+00:00", name="Carine")
        pw.check({}, TH_CFG, {}, now=NOW)
        assert sent[-1] == "✅ Traqueur actif — Carine"

    def test_phone_still_at_home_overnight_no_alert(self, env):
        sent, states = env
        states["person.carine"] = _state("2026-10-29T01:00:00+00:00", source="device_tracker.tel_carine")
        states["device_tracker.tel_carine"] = {"state": "home", "last_updated": "2026-10-29T01:00:00+00:00",
                                               "last_reported": "2026-10-29T15:45:00+00:00"}
        pw.check({}, {**TH_CFG, "person_entities": ["person.carine"]}, {}, now=NOW)
        assert sent == []

    def test_ha_unreachable_no_alert(self, env, monkeypatch):
        sent, _ = env
        monkeypatch.setattr(pw.ha_client, "get_entities", lambda cfg, ids: None)
        pw.check({}, TH_CFG, {}, now=NOW)
        assert sent == []

    @pytest.mark.parametrize("override", [{"presence_enabled": False}, {"tracker_stale_hours": 0}, {"person_entities": []}])
    def test_disabled(self, env, override):
        sent, states = env
        states["person.carine"] = _state("2026-10-21T02:19:00+00:00")
        pw.check({}, {**TH_CFG, **override}, {}, now=NOW)
        assert sent == []
