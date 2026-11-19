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

    def test_quota_exceeded_reports_retry_time(self, monkeypatch):
        retry = datetime(2026, 11, 11, 11, 0)

        def fake(cfg, start, end):
            raise linky.LinkyError("HTTP 429 : quota de la passerelle Enedis dépassé", 429, retry)

        r = self._run(monkeypatch, fake)
        assert r["status"] == "error" and r["retry_at"] == "2026-11-11T11:00:00"
        assert not r["backfill_done"]

        def ok(cfg, start, end):
            return linky.parse_load_curve(_payload(start, end), start, end)

        r = self._run(monkeypatch, ok)
        assert r["status"] == "ok" and "retry_at" not in r
        assert linky.get_state()["retry_at"] is None

    def test_server_error_retried_same_day_a_few_times(self, monkeypatch):
        monkeypatch.setattr(linky, "SERVER_RETRIES_PER_DAY", 2)
        retry = datetime(2026, 11, 11, 12, 0)

        def fake(cfg, start, end):
            raise linky.LinkyError("HTTP 500 : Technical error. Please try later.", 500, retry)

        for _ in range(2):
            r = self._run(monkeypatch, fake)
            assert r["status"] == "error" and r["retry_at"] == "2026-11-11T12:00:00"
            assert not r["backfill_done"]
        # Au-delà du plafond : plus de relance le jour même
        r = self._run(monkeypatch, fake)
        assert r["status"] == "error" and "retry_at" not in r

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


class TestNormalizePdl:
    @pytest.mark.parametrize("raw", ["01234567890123", " 0123 4567 8901 23 ", "0123-4567-8901-23", "0123.4567.8901.23",
                                     "0123 4567 890123"])
    def test_separators_removed(self, raw):
        assert linky.normalize_pdl(raw) == "01234567890123"


class TestAccessCheck:
    def test_missing_pdl(self):
        ok, message = linky.test_access({**CFG, "usage_point_id": ""})
        assert not ok and "non renseigné" in message

    def test_wrong_length_reports_count(self):
        ok, message = linky.test_access({**CFG, "usage_point_id": "1234"})
        assert not ok and "4 caractères" in message

    def test_valid_access(self, monkeypatch):
        monkeypatch.setattr(linky, "_get", lambda cfg, path: {
            "valid": True, "consent_expiration_date": "2029-11-11T00:00:00", "call_number": 3, "quota_limit": 50})
        ok, message = linky.test_access(CFG)
        assert ok and "11/11/2029" in message and "3/50" in message


def _day_curve(day: date, kwh_hp: float, kwh_hc: float) -> list:
    """Jour Tempo complet (6 h → 6 h le lendemain), 48 demi-heures, kWh répartis HP/HC."""
    rows = []
    t = datetime.combine(day, datetime.min.time()) + timedelta(hours=6)
    for _ in range(48):
        hp = 6 <= t.hour < 22
        rows.append((t.isoformat(timespec="seconds"), kwh_hp / 32 if hp else kwh_hc / 16))
        t += timedelta(minutes=30)
    return rows


class TestPerDayAndVerdict:
    def _compare(self, days):
        curve, colors = [], {}
        for i, (color, hp, hc) in enumerate(days):
            d = date(2026, 12, 1) + timedelta(days=i)
            colors[d.isoformat()] = color
            curve += _day_curve(d, hp, hc)
        return linky.compare_options(curve, colors, TEMPO_PRICES, OPTIONS, 6, 22)

    def test_days_and_per_day(self):
        r = self._compare([("BLUE", 16, 8)] * 4 + [("RED", 8, 8)] * 2)
        assert r["breakdown"]["BLUE"]["days"] == 4.0
        assert r["breakdown"]["BLUE"]["per_day"] == 24.0
        assert r["breakdown"]["RED"]["hp_per_day"] == 8.0

    def test_partial_day_counted_pro_rata(self):
        curve = _day_curve(date(2026, 12, 1), 16, 8)[:24]
        r = linky.compare_options(curve, {"2026-12-01": "BLUE"}, TEMPO_PRICES, OPTIONS, 6, 22)
        assert r["breakdown"]["BLUE"]["days"] == 0.5



class TestRedDayVerdict:
    """Relais du poêle les jours rouges : jours suivis par l'appli, à température égale (kWh HP par degré-jour)."""

    def _verdict(self, days):
        """days : [(couleur, kWh HP, temp. ext., minutes poêle)] — un jour par entrée, lectures complètes."""
        curve, colors, hp_daily = [], {}, {}
        for i, (color, hp, outdoor, poele) in enumerate(days):
            d = date(2026, 12, 1) + timedelta(days=i)
            colors[d.isoformat()] = color
            curve += _day_curve(d, hp, 8)
            if outdoor is not None:
                hp_daily[d.isoformat()] = {"readings": 96, "outdoor_temp": outdoor, "poele_minutes": poele,
                                           "clim_minutes": 0, "radiator_minutes": 0}
        return linky.red_day_verdict(curve, colors, hp_daily, 6, 22)

    def test_warn_when_red_close_to_white_at_same_temperature(self):
        v = self._verdict([("WHITE", 20, 8, 0)] * 3 + [("RED", 19, 8, 480)] * 3)
        assert v["status"] == "warn"
        assert v["ratio"] == 0.95
        assert v["red"] == {"days": 3, "hp_per_day": 19.0, "outdoor_temp": 8.0, "kwh_per_dju": 1.9,
                            "share": {"poele": 50, "clim": 0, "radiators": 0}}

    def test_colder_red_days_judged_per_degree_day(self):
        # Même conso HP, mais jours rouges bien plus froids : le poêle a pris le relais
        v = self._verdict([("WHITE", 20, 10, 0)] * 3 + [("RED", 20, 2, 900)] * 3)
        assert v["status"] == "ok"
        assert v["ratio"] == 0.5

    def test_days_before_the_app_are_ignored(self):
        v = self._verdict([("WHITE", 20, None, 0)] * 3 + [("RED", 10, None, 0)] * 3 + [("WHITE", 20, 8, 0)] * 3)
        assert v == {"status": "pending", "red_days": 0, "white_days": 3, "min_days": 3}

    def test_mild_days_are_ignored(self):
        v = self._verdict([("WHITE", 20, 15, 0)] * 3 + [("RED", 19, 8, 0)] * 3)
        assert v["status"] == "pending"
        assert v["white_days"] == 0

    def test_partial_readings_are_ignored(self):
        v = self._verdict([("WHITE", 20, 8, 0)] * 3 + [("RED", 19, 8, 0)] * 3)
        assert v["status"] == "warn"
        curve = _day_curve(date(2026, 12, 1), 20, 8)
        hp_daily = {"2026-12-01": {"readings": 40, "outdoor_temp": 8, "poele_minutes": 0,
                                   "clim_minutes": 0, "radiator_minutes": 0}}
        assert linky.red_day_verdict(curve, {"2026-12-01": "WHITE"}, hp_daily, 6, 22)["white_days"] == 0


class TestDailyTotalsAndResume:
    def test_only_complete_days(self):
        rows = linky.parse_load_curve(_payload(date(2026, 11, 1), date(2026, 11, 3)), date(2026, 11, 1), date(2026, 11, 3))
        linky.store(rows[:-10])   # 2 novembre incomplet
        assert linky.get_daily_totals() == {"2026-11-01": 24.0}

    def test_spring_dst_day_counts_as_complete(self):
        rows = linky.parse_load_curve(_payload(date(2026, 3, 29), date(2026, 3, 30)), date(2026, 3, 29), date(2026, 3, 30))
        linky.store(rows[:46])    # 23 h de mesures
        assert "2026-03-29" in linky.get_daily_totals()

    def test_deeper_backfill_resumes(self, monkeypatch):
        calls = []

        def fake(cfg, start, end):
            calls.append(start)
            return linky.parse_load_curve(_payload(start, end), start, end)

        monkeypatch.setattr(linky, "fetch_load_curve", fake)
        now = datetime(2026, 11, 11)
        linky.sync({**CFG, "backfill_days": 14}, today=now.date(), sleep=lambda s: None, now=now)
        assert linky.get_state()["backfill_done_for"] == 14
        calls.clear()
        r = linky.sync({**CFG, "backfill_days": 28}, today=now.date(), sleep=lambda s: None, now=now)
        assert calls == [date(2026, 10, 21), date(2026, 10, 14)]
        assert r["backfill_done"] and linky.get_state()["backfill_done_for"] == 28


class TestNextAccessTime:
    BODY = ('{"detail": "{\\"code\\":\\"900804\\",\\"message\\":\\"Message throttled out\\",'
            '\\"nextAccessTime\\":\\"2026-Nov-14 10:00:00+0000 UTC\\"}"}')

    def test_parsed_as_local_time(self, monkeypatch):
        monkeypatch.setenv("TZ", "Europe/Paris")
        import time
        time.tzset()
        try:
            assert linky.next_access_time(self.BODY) == datetime(2026, 11, 14, 11, 0)
        finally:
            monkeypatch.undo()
            time.tzset()

    @pytest.mark.parametrize("text", ["", "Message throttled out", '"nextAccessTime":"2026-Xyz-14 10:00:00"'])
    def test_missing_or_invalid(self, text):
        assert linky.next_access_time(text) is None


def test_http_429_carries_retry_time(monkeypatch):
    import io
    import urllib.error

    def fake_urlopen(req, timeout):
        body = io.BytesIO(TestNextAccessTime.BODY.encode())
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, body)

    monkeypatch.setattr(linky, "decrypt_password", lambda v: v)
    monkeypatch.setattr(linky.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(linky.LinkyError) as exc:
        linky.fetch_load_curve(CFG, date(2026, 11, 7), date(2026, 11, 14))
    assert exc.value.status == 429 and exc.value.retryable
    assert exc.value.retry_at is not None and exc.value.retry_at.date() == date(2026, 11, 14)
    assert "quota" in str(exc.value)


def test_http_500_retried_one_hour_later(monkeypatch):
    import io
    import urllib.error

    def fake_urlopen(req, timeout):
        body = io.BytesIO(b'{"detail": "Technical error. Please try later."}')
        raise urllib.error.HTTPError(req.full_url, 500, "Internal Server Error", {}, body)

    monkeypatch.setattr(linky, "decrypt_password", lambda v: v)
    monkeypatch.setattr(linky.urllib.request, "urlopen", fake_urlopen)
    before = datetime.now()
    with pytest.raises(linky.LinkyError) as exc:
        linky.fetch_load_curve(CFG, date(2026, 11, 7), date(2026, 11, 14))
    assert exc.value.status == 500 and exc.value.retryable
    assert before + linky.SERVER_ERROR_PAUSE <= exc.value.retry_at <= datetime.now() + linky.SERVER_ERROR_PAUSE
    assert str(exc.value) == "HTTP 500 : Technical error. Please try later."
