"""Tests unitaires pour modules/homeassistant.py — client REST Home Assistant."""

from unittest.mock import MagicMock, patch

import pytest

import modules.homeassistant as ha


CFG = {
    "enabled": True, "url": "http://ha.local:8123/", "token": "tok",
    "poele_entity_id": "climate.poele", "clim_entity_id": "climate.clim",
    "shelly_temp_entity_id": "sensor.temp", "shelly_humidity_entity_id": "sensor.hum",
}
STATES = [
    {"entity_id": "climate.poele", "state": "heat"},
    {"entity_id": "climate.clim", "state": "off"},
    {"entity_id": "sensor.temp", "state": "19.5"},
    {"entity_id": "sensor.hum", "state": "unavailable"},
    {"entity_id": "person.a", "state": "not_home"},
    {"entity_id": "person.b", "state": "Nearby"},
]


def _resp(payload):
    r = MagicMock()
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r


@pytest.fixture
def session(monkeypatch):
    ha.invalidate_states()
    s = MagicMock()
    s.request.side_effect = lambda method, url, **kw: _resp(STATES if method == "GET" else [])
    monkeypatch.setattr(ha, "_session", s)
    return s


def _gets(session):
    return [c for c in session.request.call_args_list if c.args[0] == "GET"]


def test_one_http_call_for_many_reads(session):
    assert ha.get_state(CFG)["state"] == "heat"
    assert ha.get_clim_state(CFG)["state"] == "off"
    assert ha.get_indoor_climate(CFG) == {"temperature": 19.5, "humidity": None}
    assert ha.get_presence_extended(CFG, ["person.a", "person.b"], "nearby") == "nearby"
    assert len(_gets(session)) == 1
    assert _gets(session)[0].args[1] == "http://ha.local:8123/api/states"


def test_snapshot_expires(session):
    with patch("time.monotonic", return_value=1000.0):
        ha.get_state(CFG)
    with patch("time.monotonic", return_value=1000.0 + ha.STATES_TTL_SECONDS + 1):
        ha.get_state(CFG)
    assert len(_gets(session)) == 2


def test_command_invalidates_snapshot(session):
    ha.get_state(CFG)
    assert ha.turn_off(CFG) is True
    ha.get_state(CFG)
    assert len(_gets(session)) == 2
    post = [c for c in session.request.call_args_list if c.args[0] == "POST"][0]
    assert post.args[1] == "http://ha.local:8123/api/services/climate/turn_off"
    assert post.kwargs["json"] == {"entity_id": "climate.poele"}
    assert post.kwargs["timeout"] == ha.SERVICE_TIMEOUT


def test_unknown_entity_returns_none(session):
    assert ha.get_entity_state(CFG, "switch.inconnu") is None


def test_ha_down_returns_none_and_false(session):
    session.request.side_effect = ha.requests.ConnectionError("down")
    assert ha.get_state(CFG) is None
    assert ha.turn_on(CFG) is False
    assert ha.get_presence(CFG, ["person.a"]) is None


def test_bearer_token_sent(session):
    ha.get_state(CFG)
    assert _gets(session)[0].kwargs["headers"] == {"Authorization": "Bearer tok"}


class TestGetEntities:
    def test_returns_known_entities_only(self, session):
        assert ha.get_entities(CFG, ["person.a", "person.inconnu"]) == {"person.a": STATES[4]}

    def test_not_configured(self):
        assert ha.get_entities({"enabled": False}, ["person.a"]) is None


def test_find_energy_sensor_same_device(session):
    states = STATES + [
        {"entity_id": "sensor.io_5_energy", "state": "4251000",
         "attributes": {"unit_of_measurement": "Wh", "device_class": "energy", "state_class": "total_increasing"}},
        {"entity_id": "sensor.io_6_other", "state": "3", "attributes": {"unit_of_measurement": "kWh"}},
        {"entity_id": "sensor.io_2_temp", "state": "19", "attributes": {"unit_of_measurement": "°C"}},
    ]

    def request(method, url, **kw):
        return _resp(states)

    def post(url, **kw):
        r = MagicMock()
        r.text = '["climate.i2g_actuator_4", "sensor.io_2_temp", "sensor.io_6_other", "sensor.io_5_energy"]'
        r.raise_for_status.return_value = None
        session.template = (url, kw["json"]["template"])
        return r

    session.request.side_effect = request
    session.post.side_effect = post
    assert ha.find_energy_sensor(CFG, "climate.i2g_actuator_4") == ["sensor.io_5_energy", "sensor.io_6_other"]
    url, template = session.template
    assert url == "http://ha.local:8123/api/template"
    assert 'device_id("climate.i2g_actuator_4")' in template


def test_find_energy_sensor_rejects_bad_entity(session):
    with pytest.raises(ValueError):
        ha.find_energy_sensor(CFG, 'climate.x") }}{{ states')
    session.post.assert_not_called()


class TestUnknownPresence:
    @pytest.fixture
    def states(self, session):
        states = list(STATES) + [
            {"entity_id": "person.home", "state": "home"},
            {"entity_id": "person.lost", "state": "unknown"},
            {"entity_id": "person.off", "state": "unavailable"},
        ]
        session.request.side_effect = lambda method, url, **kw: _resp(states)
        return states

    @pytest.mark.parametrize("lost", ["person.lost", "person.off"])
    def test_unknown_with_others_away_is_indeterminate(self, states, lost):
        assert ha.get_presence_extended(CFG, ["person.a", lost], "nearby") is None
        assert ha.get_presence(CFG, ["person.a", lost]) is None

    def test_unknown_with_someone_nearby_is_indeterminate(self, states):
        assert ha.get_presence_extended(CFG, ["person.b", "person.lost"], "nearby") is None

    def test_someone_home_wins_over_unknown(self, states):
        assert ha.get_presence_extended(CFG, ["person.lost", "person.home"], "nearby") == "home"
        assert ha.get_presence(CFG, ["person.lost", "person.home"]) is True

    def test_all_away_still_away(self, states):
        assert ha.get_presence_extended(CFG, ["person.a"], "nearby") == "away"
        assert ha.get_presence(CFG, ["person.a"]) is False


class TestGetPeopleStatus:
    def test_status_per_person(self, session):
        states = STATES + [
            {"entity_id": "person.c", "state": "home", "attributes": {"friendly_name": "Carine"}},
            {"entity_id": "person.d", "state": "Travail"},
            {"entity_id": "person.e", "state": "unavailable"},
        ]
        session.request.side_effect = lambda method, url, **kw: _resp(states)
        people = ha.get_people_status(
            CFG, ["person.a", "person.b", "person.c", "person.d", "person.e", "person.jean_guy"], "nearby")
        assert [(p["name"], p["status"], p["zone"]) for p in people] == [
            ("A", "away", None),
            ("B", "nearby", None),
            ("Carine", "home", None),
            ("D", "away", "Travail"),
            ("E", "unknown", None),
            ("Jean Guy", "unknown", None),
        ]
        assert len(_gets(session)) == 1

    def test_without_nearby_zone(self, session):
        assert ha.get_people_status(CFG, ["person.b"])[0]["status"] == "away"

    def test_ha_down(self, session):
        session.request.side_effect = ha.requests.ConnectionError("down")
        assert ha.get_people_status(CFG, ["person.a"]) is None

    def test_not_configured(self):
        assert ha.get_people_status({"enabled": False}, ["person.a"]) is None
        assert ha.get_people_status(CFG, []) is None
