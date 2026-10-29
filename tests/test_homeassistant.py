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
