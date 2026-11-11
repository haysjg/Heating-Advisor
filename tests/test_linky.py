"""Tests unitaires pour modules/linky.py — courbe de charge MyElectricalData et comparatif des options."""

from datetime import date, datetime, timedelta

import pytest

import modules.linky as linky

CFG = {"enabled": True, "usage_point_id": "01234567890123", "token": "secret", "backfill_days": 28}
TEMPO_PRICES = {
    "BLUE": {"HP": 0.1654, "HC": 0.1356},
    "WHITE": {"HP": 0.1921, "HC": 0.1536},
    "RED": {"HP": 0.7295, "HC": 0.1615},
}
OPTIONS = {
    "kva": 12,
    "subscription_monthly": {"TEMPO": 23.50, "BASE": 23.76, "HPHC": 23.76},
    "BASE_PRICE": 0.1985,
    "HPHC_PRICES": {"HP": 0.2142, "HC": 0.1589},
}


@pytest.fixture(autouse=True)
def state_file(tmp_path, monkeypatch):
    monkeypatch.setattr(linky, "STATE_FILE", str(tmp_path / "linky_state.json"))


def _payload(start: date, end: date, watts: float = 1000, marks_end: bool = True, minutes: int = 30) -> dict:
    """Réponse MyElectricalData : une valeur par intervalle sur [start, end[."""
    readings = []
    t = datetime.combine(start, datetime.min.time())
    stop = datetime.combine(end, datetime.min.time())
    while t < stop:
        stamp = t + timedelta(minutes=minutes) if marks_end else t
        readings.append({"value": str(watts), "date": stamp.strftime("%Y-%m-%d %H:%M:%S"),
                         "interval_length": f"PT{minutes}M", "measure_type": "B"})
        t += timedelta(minutes=minutes)
    return {"meter_reading": {"usage_point_id": CFG["usage_point_id"], "start": start.isoformat(),
                              "end": end.isoformat(), "quality": "BRUT", "reading_type": {},
                              "interval_reading": readings}}


class TestParseLoadCurve:
    def test_end_stamped_intervals_shifted_to_start(self):
        rows = linky.parse_load_curve(_payload(date(2026, 11, 1), date(2026, 11, 2)), date(2026, 11, 1), date(2026, 11, 2))
        assert len(rows) == 48
        assert rows[0] == ("2026-11-01T00:00:00", 0.5)   # 1000 W pendant 30 min
        assert rows[-1][0] == "2026-11-01T23:30:00"

    def test_start_stamped_intervals_kept(self):
        rows = linky.parse_load_curve(_payload(date(2026, 11, 1), date(2026, 11, 2), marks_end=False),
                                      date(2026, 11, 1), date(2026, 11, 2))
        assert rows[0][0] == "2026-11-01T00:00:00"
        assert rows[-1][0] == "2026-11-01T23:30:00"

    def test_other_interval_length(self):
        rows = linky.parse_load_curve(_payload(date(2026, 11, 1), date(2026, 11, 2), watts=600, minutes=10),
                                      date(2026, 11, 1), date(2026, 11, 2))
        assert len(rows) == 144
        assert rows[0][1] == 0.1

    def test_error_payload_raises(self):
        with pytest.raises(linky.LinkyError, match="consent"):
            linky.parse_load_curve({"detail": "consentement expiré"}, date(2026, 11, 1), date(2026, 11, 2))


class TestCompareOptions:
    def test_night_belongs_to_previous_tempo_day(self):
        # 3 h du matin le 12 : jour Tempo du 11 (rouge), heure creuse
        curve = [("2026-11-12T03:00:00", 1.0)]
        colors = {"2026-11-11": "RED", "2026-11-12": "BLUE"}
        r = linky.compare_options(curve, colors, TEMPO_PRICES, OPTIONS, 6, 22)
        assert r["breakdown"]["RED"]["HC"] == 1.0
        assert r["energy"]["TEMPO"] == round(0.1615, 2)

    def test_red_peak_costs_more_in_tempo(self):
        curve = [("2026-12-01T18:00:00", 10.0)]
        r = linky.compare_options(curve, {"2026-12-01": "RED"}, TEMPO_PRICES, OPTIONS, 6, 22)
        assert r["energy"] == pytest.approx({"TEMPO": 7.295, "BASE": 1.985, "HPHC": 2.142}, abs=0.006)
        assert r["best"] == "BASE"

    def test_subscription_prorated_per_day(self):
        curve = [(f"2026-11-{d:02d}T12:00:00", 0.0) for d in range(1, 31)]
        colors = {f"2026-11-{d:02d}": "BLUE" for d in range(1, 31)}
        r = linky.compare_options(curve, colors, TEMPO_PRICES, OPTIONS, 6, 22)
        assert r["days"] == 30
        assert r["subscription"]["TEMPO"] == round(23.50 * 12 / 365 * 30, 2)
        assert r["savings"]["BASE"] == round((23.76 - 23.50) * 12 / 365 * 30, 2)

    def test_unknown_color_excluded_from_all_options(self):
        curve = [("2026-11-10T12:00:00", 2.0), ("2026-11-11T12:00:00", 3.0)]
        r = linky.compare_options(curve, {"2026-11-10": "WHITE"}, TEMPO_PRICES, OPTIONS, 6, 22)
        assert r["kwh"] == 2.0
        assert r["unpriced_kwh"] == 3.0
        assert r["breakdown"]["WHITE"]["HP"] == 2.0

    def test_monthly_rows(self):
        curve = [("2026-10-31T12:00:00", 1.0), ("2026-11-01T12:00:00", 1.0)]
        colors = {"2026-10-31": "BLUE", "2026-11-01": "BLUE"}
        r = linky.compare_options(curve, colors, TEMPO_PRICES, OPTIONS, 6, 22)
        assert [m["month"] for m in r["months"]] == ["2026-10", "2026-11"]
        assert r["first_day"] == "2026-10-31" and r["last_day"] == "2026-11-01"

    def test_empty(self):
        r = linky.compare_options([], {}, TEMPO_PRICES, OPTIONS, 6, 22)
        assert r["days"] == 0 and r["best"] is None


class TestSync:
    TODAY = date(2026, 11, 11)

    def _run(self, monkeypatch, fake, cfg=CFG):
        monkeypatch.setattr(linky, "fetch_load_curve", fake)
        return linky.sync(cfg, today=self.TODAY, sleep=lambda s: None,
                          now=datetime.combine(self.TODAY, datetime.min.time()))

    def test_disabled(self):
        assert linky.sync({**CFG, "enabled": False})["status"] == "disabled"

    def test_first_run_fetches_recent_then_history(self, monkeypatch):
        calls = []

        def fake(cfg, start, end):
            calls.append((start, end))
            return linky.parse_load_curve(_payload(start, end), start, end)

        r = self._run(monkeypatch, fake)
        # Récent : 7 derniers jours, puis historique jusqu'à 28 jours en tranches de 7
        assert calls[0] == (date(2026, 11, 4), self.TODAY)
        assert calls[1:] == [(date(2026, 10, 28), date(2026, 11, 4)), (date(2026, 10, 21), date(2026, 10, 28)),
                             (date(2026, 10, 14), date(2026, 10, 21))]
        assert r["status"] == "ok" and r["backfill_done"]
        assert r["first_day"] == "2026-10-14" and r["last_day"] == "2026-11-10"

    def test_next_run_only_fetches_missing_days(self, monkeypatch):
        def fake(cfg, start, end):
            return linky.parse_load_curve(_payload(start, end), start, end)

        self._run(monkeypatch, fake)
        calls = []

        def fake2(cfg, start, end):
            calls.append((start, end))
            return linky.parse_load_curve(_payload(start, end), start, end)

        monkeypatch.setattr(linky, "fetch_load_curve", fake2)
        linky.sync(CFG, today=date(2026, 11, 13), sleep=lambda s: None, now=datetime(2026, 11, 13, 10, 30))
        assert calls == [(date(2026, 11, 11), date(2026, 11, 13))]

    def test_history_ends_when_enedis_has_no_more_data(self, monkeypatch):
        def fake(cfg, start, end):
            if start < date(2026, 11, 4):
                raise linky.LinkyError("HTTP 400 : aucune donnée", 400)
            return linky.parse_load_curve(_payload(start, end), start, end)

        r = self._run(monkeypatch, fake)
        assert r["status"] == "ok" and r["backfill_done"]
        assert r["first_day"] == "2026-11-04"

    def test_auth_error_aborts_without_ending_history(self, monkeypatch):
        def fake(cfg, start, end):
            raise linky.LinkyError("HTTP 401 : clé invalide", 401)

        r = self._run(monkeypatch, fake)
        assert r["status"] == "error" and "401" in r["error"]
        assert not r["backfill_done"]
        assert linky.get_state()["last_error"] == "HTTP 401 : clé invalide"

    def test_daily_call_budget(self, monkeypatch):
        monkeypatch.setattr(linky, "DAILY_CALL_BUDGET", 2)
        calls = []

        def fake(cfg, start, end):
            calls.append(start)
            return linky.parse_load_curve(_payload(start, end), start, end)

        r = self._run(monkeypatch, fake)
        assert len(calls) == 2 and not r["backfill_done"]
        # Même jour : budget épuisé, aucun appel
        r = self._run(monkeypatch, fake)
        assert len(calls) == 2


class TestValidPdl:
    def test_valid(self):
        assert linky.valid_pdl("01234567890123")

    @pytest.mark.parametrize("value", ["", "1234", "0123456789012a", None, "012345678901234"])
    def test_invalid(self, value):
        assert not linky.valid_pdl(value)
