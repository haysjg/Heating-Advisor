"""Tests de la récupération de la température extérieure (mesures de station, fallbacks)."""

import io
import json
import urllib.error
import urllib.parse
from unittest.mock import patch

import pytest
from freezegun import freeze_time

import modules.weather as weather

LOCATION = {"city": "X", "postal_code": "00000", "latitude": 48.7, "longitude": 2.2}
OBSERVATIONS = {
    "meteofrance_application_id": "app-id",
    "meteofrance_stations": ["91000001", "91000002"],
    "infoclimat_token": "ic-token",
    "infoclimat_stations": ["ME113", "00089"],
    "max_age_minutes": 90,
}


@pytest.fixture(autouse=True)
def reset_state():
    def _reset():
        weather._last_known = None
        weather._last_measured = None
        weather._mf_token = None
        weather._mf_station_names = {"91000001": "Station Un"}
        weather._paused_until.clear()
    _reset()
    yield
    _reset()


def _response(payload):
    return io.BytesIO(json.dumps(payload).encode())


def _http_error(code):
    return urllib.error.HTTPError("https://example.invalid", code, "err", {}, None)


def _obs(temp, observed_utc):
    return {"temperature": temp, "observed_at": weather._parse_utc(observed_utc)}


def _sources(mf=None, ic=None, openmeteo=None):
    """Simule les trois sources : mf = {station: obs|None}, ic = {station: obs}."""
    mf = mf or {}
    return (
        patch("modules.weather.get_temperature_meteofrance", side_effect=lambda sid, app_id: mf.get(sid)),
        patch("modules.weather.get_temperatures_infoclimat", return_value=ic or {}),
        patch("modules.weather.get_temperature_openmeteo", return_value=openmeteo),
    )


def _current(mf=None, ic=None, openmeteo=None, observations=OBSERVATIONS):
    p_mf, p_ic, p_om = _sources(mf, ic, openmeteo)
    with p_mf as mf_mock, p_ic, p_om:
        result = weather.get_current_temperature(LOCATION, observations)
    return result, mf_mock


class TestSourceOrder:
    @freeze_time("2026-11-01 08:00:00")
    def test_infoclimat_first(self):
        ic = {"ME113": {**_obs(6.1, "2026-11-01 07:50:00"), "name": "Massy"}}
        result, mf = _current(mf={"91000001": _obs(8.2, "2026-11-01T07:48:00Z")}, ic=ic, openmeteo=12.0)
        assert result["temperature"] == 6.1
        assert result["measured"] is True
        assert result["source"] == "Infoclimat Massy"
        assert result["observed_at"]
        mf.assert_not_called()  # Météo-France sollicité seulement si Infoclimat n'a rien donné

    @freeze_time("2026-11-01 08:00:00")
    def test_meteofrance_when_infoclimat_down(self):
        result, _ = _current(mf={"91000001": _obs(9.4, "2026-11-01T07:48:00Z")}, openmeteo=12.0)
        assert result["temperature"] == 9.4
        assert result["source"] == "Météo-France Station Un"

    @freeze_time("2026-11-01 08:00:00")
    def test_second_station_when_first_down(self):
        result, _ = _current(mf={"91000002": _obs(9.1, "2026-11-01T07:54:00Z")})
        assert result["source"] == "Météo-France 91000002"  # nom inconnu : identifiant

    @freeze_time("2026-11-01 08:00:00")
    def test_second_infoclimat_station_before_meteofrance(self):
        ic = {"00089": {**_obs(9.8, "2026-11-01 07:50:00"), "name": "Villemoisson-sur-Orge"}}
        result, _ = _current(mf={"91000001": _obs(8.2, "2026-11-01T07:48:00Z")}, ic=ic, openmeteo=12.0)
        assert result["temperature"] == 9.8
        assert result["measured"] is True
        assert result["source"] == "Infoclimat Villemoisson-sur-Orge"

    @freeze_time("2026-11-01 08:00:00")
    def test_stale_measurement_ignored(self):
        result, _ = _current(mf={"91000001": _obs(5.0, "2026-11-01T05:00:00Z"),
                                 "91000002": _obs(9.1, "2026-11-01T07:54:00Z")})
        assert result["temperature"] == 9.1

    @freeze_time("2026-11-01 08:00:00")
    def test_openmeteo_when_no_measurement(self):
        result, _ = _current(openmeteo=12.0)
        assert result["temperature"] == 12.0
        assert result["measured"] is False
        assert result["source"] == "Open-Meteo"

    @freeze_time("2026-11-01 08:00:00")
    def test_unconfigured_sources_skipped(self):
        result, mf = _current(openmeteo=12.0, observations={})
        assert result["source"] == "Open-Meteo"
        mf.assert_not_called()


class TestFallbacks:
    def test_last_measurement_preferred_over_model(self):
        with freeze_time("2026-11-01 08:00:00"):
            _current(mf={"91000001": _obs(9.4, "2026-11-01T07:48:00Z")})
        with freeze_time("2026-11-01 08:30:00"):
            result, _ = _current(openmeteo=12.0)
        assert result["temperature"] == 9.4
        assert result["measured"] is True
        assert "dernière mesure" in result["source"]

    def test_model_after_one_hour_without_measurement(self):
        with freeze_time("2026-11-01 08:00:00"):
            _current(mf={"91000001": _obs(9.4, "2026-11-01T07:48:00Z")})
        with freeze_time("2026-11-01 09:10:00"):
            result, _ = _current(openmeteo=12.0)
        assert result["source"] == "Open-Meteo"

    def test_reuses_recent_value_when_all_sources_fail(self):
        with freeze_time("2026-10-31 10:07:00"):
            _current(openmeteo=10.4)
        with freeze_time("2026-10-31 10:27:00"):
            result, _ = _current()
        assert result["temperature"] == 10.4
        assert result["measured"] is False
        assert "dernière valeur" in result["source"]

    def test_stale_value_not_reused(self):
        with freeze_time("2026-10-31 06:00:00"):
            _current(openmeteo=8.0)
        with freeze_time("2026-10-31 09:30:00"):
            result, _ = _current()
        assert result["temperature"] is None
        assert result["error"]


class TestMeteoFrance:
    TOKEN = {"access_token": "jwt-1", "token_type": "Bearer", "expires_in": 3600}
    OBS = [{"validity_time": "2026-11-01T07:42:00Z", "t": 282.0},
           {"validity_time": "2026-11-01T07:48:00Z", "t": 282.55}]

    @freeze_time("2026-11-01 08:00:00")
    def test_parses_latest_kelvin_measurement(self):
        with patch("urllib.request.urlopen", side_effect=[_response(self.TOKEN), _response(self.OBS)]) as urlopen:
            obs = weather.get_temperature_meteofrance("91000001", "app-id")
        assert obs["temperature"] == 9.4
        assert obs["observed_at"].strftime("%H:%M") == "07:48"
        token_req, obs_req = (c.args[0] for c in urlopen.call_args_list)
        assert token_req.get_header("Authorization") == "Basic app-id"
        assert obs_req.get_header("Authorization") == "Bearer jwt-1"
        assert "id_station=91000001" in obs_req.full_url

    @freeze_time("2026-11-01 08:00:00")
    def test_token_reused_until_expiry(self):
        with patch("urllib.request.urlopen",
                   side_effect=[_response(self.TOKEN), _response(self.OBS), _response(self.OBS)]) as urlopen:
            weather.get_temperature_meteofrance("91000001", "app-id")
            weather.get_temperature_meteofrance("91000001", "app-id")
        assert urlopen.call_count == 3

    @freeze_time("2026-11-01 08:00:00")
    def test_token_renewed_on_401(self):
        renewed = {**self.TOKEN, "access_token": "jwt-2"}
        with patch("urllib.request.urlopen", side_effect=[
            _response(self.TOKEN), _http_error(401), _response(renewed), _response(self.OBS),
        ]) as urlopen:
            obs = weather.get_temperature_meteofrance("91000001", "app-id")
        assert obs["temperature"] == 9.4
        assert urlopen.call_args_list[-1].args[0].get_header("Authorization") == "Bearer jwt-2"

    @freeze_time("2026-11-01 08:00:00")
    def test_missing_temperature_returns_none(self):
        with patch("urllib.request.urlopen",
                   side_effect=[_response(self.TOKEN), _response([{"validity_time": "2026-11-01T07:48:00Z", "t": None}])]):
            assert weather.get_temperature_meteofrance("91000001", "app-id") is None

    def test_429_pauses_source(self):
        with freeze_time("2026-11-01 08:00:00"), \
             patch("urllib.request.urlopen", side_effect=[_response(self.TOKEN), _http_error(429)]) as urlopen:
            assert weather.get_temperature_meteofrance("91000001", "app-id") is None
            assert weather.get_temperature_meteofrance("91000002", "app-id") is None
        assert urlopen.call_count == 2
        with freeze_time("2026-11-01 09:01:00"), \
             patch("urllib.request.urlopen", side_effect=[_response(self.TOKEN), _response(self.OBS)]):
            assert weather.get_temperature_meteofrance("91000001", "app-id") is not None


    @freeze_time("2026-11-01 08:00:00")
    def test_station_names_read_once(self):
        weather._mf_station_names = None
        csv_body = "Id_station;Id_omm;Nom_usuel;Latitude;Longitude;Altitude;Date_ouverture;Pack\n" \
                   "78640001;07147;VILLACOUBLAY;48.77;2.20;174;1940-07-01;RADOME\n"
        with patch("urllib.request.urlopen",
                   side_effect=[_response(self.TOKEN), io.BytesIO(csv_body.encode("iso-8859-1"))]) as urlopen:
            assert weather.meteofrance_station_name("78640001", "app-id") == "Villacoublay"
            assert weather.meteofrance_station_name("99999999", "app-id") == "99999999"
        assert urlopen.call_count == 2


class TestInfoclimat:
    ANSWER = {
        "status": "OK", "errors": [],
        "stations": [{"id": "ME113", "name": "Massy - Lycée Parc de Vilgénis"}],
        "hourly": {
            "ME113": [
                {"id_station": "ME113", "dh_utc": "2026-11-01 07:40:00", "temperature": "9.6"},
                {"id_station": "ME113", "dh_utc": "2026-11-01 07:50:00", "temperature": "9.8"},
                {"id_station": "ME113", "dh_utc": "2026-11-01 08:00:00", "temperature": None},
            ],
            "00089": [],
            "_params": ["temperature"],
        },
    }

    @freeze_time("2026-11-01 08:05:00")
    def test_one_request_for_all_stations(self):
        with patch("urllib.request.urlopen", return_value=_response(self.ANSWER)) as urlopen:
            found = weather.get_temperatures_infoclimat(["ME113", "00089"], "secret-token")
        assert urlopen.call_count == 1
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(urlopen.call_args.args[0].full_url).query)
        assert query["stations[]"] == ["ME113", "00089"]
        assert query["start"] == ["2026-10-31"] and query["end"] == ["2026-11-01"]
        assert set(found) == {"ME113"}
        assert found["ME113"]["temperature"] == 9.8
        assert found["ME113"]["observed_at"].strftime("%H:%M") == "07:50"
        assert found["ME113"]["name"] == "Massy - Lycée Parc de Vilgénis"

    def test_api_error_returns_nothing(self):
        answer = {"errors": ["No account found for this token"]}
        with patch("urllib.request.urlopen", return_value=_response(answer)):
            assert weather.get_temperatures_infoclimat(["ME113"], "bad") == {}

    def test_token_never_logged(self, caplog):
        with patch("urllib.request.urlopen", side_effect=_http_error(400)):
            weather.get_temperatures_infoclimat(["ME113"], "secret-token")
        assert "secret-token" not in caplog.text
        assert "HTTP 400" in caplog.text


class TestErrorClassification:
    def _http_error_body(self, code, body):
        return urllib.error.HTTPError("https://example.invalid", code, "err", {}, io.BytesIO(body.encode()))

    def test_infoclimat_wrong_ip_is_auth(self):
        with patch("urllib.request.urlopen", return_value=io.BytesIO(b"Wrong ip address")):
            with pytest.raises(weather.SourceError) as exc:
                weather.fetch_infoclimat(["ME113"], "secret-token")
        assert exc.value.auth is True

    def test_infoclimat_unknown_token_is_auth_and_masked(self):
        err = self._http_error_body(400, '{"errors":["No account found for this token: \'secret-token\'"]}')
        with patch("urllib.request.urlopen", side_effect=err):
            with pytest.raises(weather.SourceError) as exc:
                weather.fetch_infoclimat(["ME113"], "secret-token")
        assert exc.value.auth is True
        assert "secret-token" not in str(exc.value)

    def test_infoclimat_server_error_is_not_auth(self):
        with patch("urllib.request.urlopen", side_effect=self._http_error_body(502, "Bad Gateway")):
            with pytest.raises(weather.SourceError) as exc:
                weather.fetch_infoclimat(["ME113"], "secret-token")
        assert exc.value.auth is False

    def test_meteofrance_rejected_app_id_is_auth(self):
        with patch("urllib.request.urlopen", side_effect=_http_error(401)):
            with pytest.raises(weather.SourceError) as exc:
                weather.fetch_meteofrance("91000001", "bad-id")
        assert exc.value.auth is True

    @freeze_time("2026-11-01 08:00:00")
    def test_meteofrance_forbidden_after_renew_is_auth(self):
        token = {"access_token": "jwt", "expires_in": 3600}
        with patch("urllib.request.urlopen", side_effect=[
            _response(token), _http_error(401), _response(token), _http_error(403),
        ]):
            with pytest.raises(weather.SourceError) as exc:
                weather.fetch_meteofrance("91000001", "app-id")
        assert exc.value.auth is True

    def test_timeout_is_not_auth(self):
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("timed out")):
            with pytest.raises(weather.SourceError) as exc:
                weather.fetch_meteofrance("91000001", "app-id")
        assert exc.value.auth is False


class TestProbeSources:
    def test_probes_backup_source_too(self):
        with patch("modules.weather.fetch_infoclimat", return_value={"ME113": {}}) as ic, \
             patch("modules.weather.fetch_meteofrance", side_effect=weather.SourceError("HTTP 403", auth=True)) as mf:
            results = weather.probe_sources(OBSERVATIONS)
        ic.assert_called_once()
        mf.assert_called_once_with("91000001", "app-id")
        assert results["Infoclimat"] is None
        assert results["Météo-France"].auth is True

    def test_unconfigured_sources_absent(self):
        assert weather.probe_sources({}) == {}


# ── Ciel actuel (illustration de la carte météo) ──────────────────

@pytest.fixture
def no_conditions_cache():
    weather._conditions_cache = None
    yield
    weather._conditions_cache = None


@pytest.mark.parametrize("code, sky", [
    (0, "clear"), (1, "clear"), (2, "partly"), (3, "cloudy"), (45, "fog"),
    (61, "rain"), (81, "rain"), (73, "snow"), (86, "snow"), (95, "storm"),
])
def test_type_de_ciel_selon_code_wmo(code, sky):
    assert weather._sky_kind(code) == sky


def test_conditions_mises_en_cache(no_conditions_cache):
    payload = {"current": {"weather_code": 61, "is_day": 0}}
    with patch("urllib.request.urlopen", return_value=_response(payload)) as urlopen:
        first = weather.get_current_conditions(48.7, 2.2)
        second = weather.get_current_conditions(48.7, 2.2)
    assert first == {"sky": "rain", "is_day": False, "code": 61}
    assert second == first
    assert urlopen.call_count == 1


def test_conditions_indisponibles_sans_cache(no_conditions_cache):
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("hs")):
        assert weather.get_current_conditions(48.7, 2.2) is None
