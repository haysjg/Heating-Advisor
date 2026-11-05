"""Tests unitaires pour modules/pellet_refill.py — compteur d'heures depuis le dernier remplissage."""

import pytest

import modules.pellet_refill as pr


@pytest.fixture
def sent(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "STATE_FILE", str(tmp_path / "pellet_refill.json"))
    out = []
    monkeypatch.setattr(pr, "ntfy_send", lambda title, msg, cfg: out.append(("ntfy", title)))
    monkeypatch.setattr(pr, "_send_sensor_alert", lambda cfg, title, msg, kicker: out.append(("email", title)))
    return out


def _run(n, on=True, cycle=15.0):
    for _ in range(n):
        pr.tick(on, cycle, {}, {})


def test_counts_only_when_on(sent):
    _run(3, on=True)
    _run(5, on=False)
    assert pr.status()["minutes"] == 30


def test_alert_at_14h_once_with_push_and_email(sent):
    _run(14 * 6 - 1)
    assert sent == []
    _run(1)
    assert pr.status()["minutes"] == 14 * 60
    assert [kind for kind, _ in sent] == ["ntfy", "email"]
    _run(3)
    assert len(sent) == 2  # pas de répétition dans le même cycle


def test_auto_reset_at_15h(sent):
    _run(15 * 6)
    st = pr.status()
    assert st["minutes"] == 0 and st["reset_by"] == "auto" and not st["alerted"]
    _run(14 * 6)
    assert len(sent) == 4  # nouveau cycle → nouvelle alerte


def test_manual_reset(sent):
    _run(14 * 6)
    pr.reset()
    st = pr.status()
    assert st["minutes"] == 0 and st["reset_by"] == "manual" and not st["alerted"]


def test_status_uses_cycle(sent):
    _run(6 * 6)
    st = pr.status(cycle_hours=12)
    assert st["hours"] == 6 and st["percent"] == 50 and st["alert_hours"] == 11


class TestMeasure:
    def test_first_click_starts_measure_without_cycle(self, sent):
        _run(30)
        pr.reset("manual", kg=15)
        st = pr.status()
        assert st["measuring"] and st["refills_count"] == 0 and st["estimate"] is None

    def test_auto_reset_keeps_measure_counter(self, sent):
        pr.reset("manual", kg=15)
        _run(16 * 6)  # passe la remise à 0 automatique de 15 h
        st = pr.status()
        assert st["minutes"] == 60 and st["refill_hours"] == 16

    def test_estimate_after_three_refills(self, sent):
        pr.reset("manual", kg=15)
        for hours in (16, 17, 15):
            _run(hours * 6, cycle=30)
            pr.reset("manual", kg=15)
        e = pr.status()["estimate"]
        assert e == {"cycles": 3, "kg_per_hour": pytest.approx(0.9375, abs=1e-3), "hours_per_bag": 16.0,
                     "min_hours_per_bag": 15.0, "max_hours_per_bag": 17.0}

    def test_partial_bag_normalized(self, sent):
        pr.reset("manual", kg=15)
        for kg in (7.5, 15, 30):
            _run(int(kg / 15 * 16 * 6), cycle=100)
            pr.reset("manual", kg=kg)
        assert pr.status()["estimate"]["hours_per_bag"] == 16.0

    def test_double_click_not_recorded(self, sent):
        pr.reset("manual", kg=15)
        _run(16 * 6, cycle=30)
        pr.reset("manual", kg=15)
        pr.reset("manual", kg=15)
        assert pr.status()["refills_count"] == 1

    def test_estimate_uses_recent_cycles(self):
        refills = [{"hours": 30, "kg": 15}] * 5 + [{"hours": 15, "kg": 15}] * pr.ESTIMATE_CYCLES
        assert pr.estimate(refills)["hours_per_bag"] == 15.0


# ── Routes Flask ──────────────────────────────────────────────

FETCH = {"X-Requested-With": "fetch"}


@pytest.fixture
def client(sent, monkeypatch):
    import importlib
    import threading
    from unittest import mock
    pytest.importorskip("flask_limiter")
    from apscheduler.schedulers.background import BackgroundScheduler
    with mock.patch.object(BackgroundScheduler, "start"), mock.patch.object(threading.Thread, "start"):
        app_module = importlib.import_module("app")
    app_module.app.config["TESTING"] = True
    patched = {}
    monkeypatch.setattr(app_module, "_patch_override", lambda fn: fn(patched))
    monkeypatch.setattr(app_module, "_poele_purchase", lambda: {"poids_sac": 15, "hours_per_bag": 15})
    monkeypatch.setitem(app_module.config.POELE, "consumption_kg_per_hour", 1.0)
    c = app_module.app.test_client()
    with c.session_transaction() as s:
        s["authenticated"] = True
        s["auth_fp"] = app_module._auth_fingerprint()
    return c, patched, app_module


def test_route_reset_records_bags(client):
    c, _, _ = client
    pr.reset("manual", kg=15)
    _run(8 * 6, cycle=30)
    resp = c.post("/api/poele/refill-reset", json={"bags": 0.5}, headers=FETCH)
    assert resp.status_code == 200 and resp.get_json()["refills_count"] == 1
    assert pr._load()["refills"][-1]["kg"] == 7.5


def test_route_reset_rejects_bad_bags(client):
    c, _, _ = client
    assert c.post("/api/poele/refill-reset", json={"bags": 0}, headers=FETCH).status_code == 400
    assert c.post("/api/poele/refill-reset", json={"bags": "x"}, headers=FETCH).status_code == 400


def test_route_apply(client):
    c, patched, app_module = client
    assert c.post("/api/poele/refill-apply", headers=FETCH).status_code == 400
    pr.reset("manual", kg=15)
    for _ in range(3):
        _run(16 * 6, cycle=30)
        pr.reset("manual", kg=15)
    resp = c.post("/api/poele/refill-apply", headers=FETCH)
    assert resp.status_code == 200
    assert patched["_poele_purchase"]["hours_per_bag"] == 16.0
    assert patched["POELE"]["consumption_kg_per_hour"] == pytest.approx(0.9375)
    assert app_module.config.POELE["consumption_kg_per_hour"] == pytest.approx(0.9375)
