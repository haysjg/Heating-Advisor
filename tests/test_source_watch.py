"""Tests unitaires pour modules/source_watch.py — alertes sources de mesure et rappel d'abonnement."""

from datetime import date

import pytest

import modules.source_watch as sw
from modules.weather import SourceError

OBS = {
    "meteofrance_application_id": "app-id",
    "meteofrance_stations": ["78640001"],
    "infoclimat_token": "tok",
    "infoclimat_stations": ["ME113"],
    "meteofrance_subscription_end": "2028-11-01",
}
TODAY = date(2026, 11, 1)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(sw, "ALERTS_FILE", str(tmp_path / "source_alerts.json"))
    sent = []
    monkeypatch.setattr(sw, "ntfy_send", lambda title, msg, cfg: sent.append((title, msg)))
    results = {}
    monkeypatch.setattr(sw.weather, "probe_sources", lambda obs: dict(results))
    return sent, results


def _titles(sent):
    return [t for t, _ in sent]


class TestFailureAlerts:
    def test_auth_failure_alerts_immediately_once(self, env):
        sent, results = env
        results["Infoclimat"] = SourceError("clé refusée (Wrong ip address)", auth=True)
        sw.check(OBS, {}, TODAY)
        sw.check(OBS, {}, TODAY)
        assert _titles(sent) == ["🔑 Infoclimat refuse les identifiants"]
        assert "infoclimat.fr/opendata" in sent[0][1]

    def test_other_failure_alerts_after_two_checks(self, env):
        sent, results = env
        results["Météo-France"] = SourceError("HTTP 502")
        sw.check(OBS, {}, TODAY)
        assert sent == []
        sw.check(OBS, {}, TODAY)
        assert _titles(sent) == ["⚠️ Météo-France ne répond plus"]

    def test_transient_failure_forgotten(self, env):
        sent, results = env
        results["Météo-France"] = SourceError("timed out")
        sw.check(OBS, {}, TODAY)
        results["Météo-France"] = None
        sw.check(OBS, {}, TODAY)
        results["Météo-France"] = SourceError("timed out")
        sw.check(OBS, {}, TODAY)
        assert sent == []

    def test_recovery_notified_after_alert(self, env):
        sent, results = env
        results["Infoclimat"] = SourceError("clé refusée", auth=True)
        sw.check(OBS, {}, TODAY)
        results["Infoclimat"] = None
        sw.check(OBS, {}, TODAY)
        assert _titles(sent) == ["🔑 Infoclimat refuse les identifiants", "✅ Infoclimat répond de nouveau"]


class TestSubscriptionReminder:
    def test_nothing_before_30_days(self, env):
        sent, _ = env
        sw.check(OBS, {}, date(2028, 10, 1))
        assert sent == []

    def test_reminders_at_30_and_7_days_once_each(self, env):
        sent, _ = env
        for day in (date(2028, 10, 2), date(2028, 10, 3), date(2028, 10, 25), date(2028, 10, 26)):
            sw.check(OBS, {}, day)
        assert _titles(sent) == ["📅 Abonnement Météo-France à renouveler"] * 2
        assert "dans 30 j" in sent[0][1] and "dans 7 j" in sent[1][1]

    def test_late_start_sends_single_reminder(self, env):
        sent, _ = env
        sw.check(OBS, {}, date(2028, 10, 30))
        sw.check(OBS, {}, date(2028, 10, 31))
        assert len(sent) == 1

    def test_new_end_date_rearms_reminders(self, env):
        sent, _ = env
        sw.check(OBS, {}, date(2028, 10, 26))
        sw.check({**OBS, "meteofrance_subscription_end": "2030-11-01"}, {}, date(2030, 10, 2))
        assert len(sent) == 2

    def test_no_reminder_without_meteofrance(self, env):
        sent, _ = env
        sw.check({**OBS, "meteofrance_application_id": ""}, {}, date(2028, 10, 26))
        assert sent == []
