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
