"""Tests de sécurité des routes Flask (app.py)."""

import importlib
import threading
from unittest import mock

import pytest

pytest.importorskip("flask_limiter")
pytest.importorskip("apscheduler")


@pytest.fixture(scope="module")
def app_module():
    """Importe app.py sans démarrer le scheduler ni le premier calcul en arrière-plan."""
    from apscheduler.schedulers.background import BackgroundScheduler
    with mock.patch.object(BackgroundScheduler, "start"), mock.patch.object(threading.Thread, "start"):
        module = importlib.import_module("app")
    module.app.config["TESTING"] = True
    return module


@pytest.fixture
def client(app_module, monkeypatch):
    # Aucune écriture dans le vrai config_override.json
    monkeypatch.setattr(app_module, "_patch_override", lambda fn: fn({}))
    monkeypatch.setitem(app_module.config.AUTH, "password_hash", app_module.generate_password_hash("ancien-mdp"))
    monkeypatch.setitem(app_module.config.AUTH, "session_epoch", "")
    app_module.limiter.reset()
    return app_module.app.test_client()


def _login(client, password="ancien-mdp"):
    resp = client.post("/login", data={"password": password})
    assert resp.status_code == 302
    return client


FETCH = {"X-Requested-With": "fetch"}


# ── S-03 : un secret conservé ne part jamais vers un nouvel hôte ──────────

def test_config_refuse_changement_url_ha_sans_token(client, app_module, monkeypatch):
    monkeypatch.setitem(app_module.config.HOME_ASSISTANT, "url", "http://192.168.1.2:8123")
    monkeypatch.setitem(app_module.config.HOME_ASSISTANT, "token", "token-chiffre")
    _login(client)
    with mock.patch.object(app_module, "apply_overrides") as apply:
        resp = client.post("/api/config", json={"ha_url": "http://attaquant.example:8123", "ha_token": ""}, headers=FETCH)
    assert resp.status_code == 400
    assert "Home Assistant" in resp.get_json()["error"]
    apply.assert_not_called()
    assert app_module.config.HOME_ASSISTANT["url"] == "http://192.168.1.2:8123"


@pytest.mark.parametrize("payload", [
    {"ntfy_url": "https://autre.example", "ntfy_token": ""},
    {"smtp_host": "smtp.autre.example", "app_password": ""},
    {"imap_host": "imap.autre.example", "imap_password": ""},
])
def test_config_refuse_changement_hote_sans_secret(client, app_module, monkeypatch, payload):
    monkeypatch.setitem(app_module.config.NTFY, "url", "https://ntfy.sh")
    monkeypatch.setitem(app_module.config.NTFY, "token", "x")
    monkeypatch.setitem(app_module.config.EMAIL, "smtp_host", "smtp.gmail.com")
    monkeypatch.setitem(app_module.config.EMAIL, "app_password", "x")
    monkeypatch.setitem(app_module.config.ELECTRICITY, "imap_host", "imap.orange.fr")
    monkeypatch.setitem(app_module.config.ELECTRICITY, "imap_password", "x")
    assert app_module._secret_rebind_error(payload)


def test_config_accepte_meme_hote_ou_secret_ressaisi(app_module, monkeypatch):
    monkeypatch.setitem(app_module.config.HOME_ASSISTANT, "url", "http://192.168.1.2:8123")
    monkeypatch.setitem(app_module.config.HOME_ASSISTANT, "token", "token-chiffre")
    assert app_module._secret_rebind_error({"ha_url": "http://192.168.1.2:8123/", "ha_token": ""}) is None
    assert app_module._secret_rebind_error({"ha_url": "http://nouveau:8123", "ha_token": "nouveau-token"}) is None
    monkeypatch.setitem(app_module.config.HOME_ASSISTANT, "token", "")
    assert app_module._secret_rebind_error({"ha_url": "http://nouveau:8123", "ha_token": ""}) is None


# ── S-05 : pas de détail interne dans les réponses 500 ──────────────────

def test_erreur_interne_sans_detail(client, app_module, monkeypatch):
    def boom(hours):
        raise RuntimeError("/app/data/history.db : database is locked")
    monkeypatch.setattr(app_module.history_module, "get_history", boom)
    _login(client)
    resp = client.get("/api/statistics")
    assert resp.status_code == 500
    body = resp.get_json()
    assert "history.db" not in resp.get_data(as_text=True)
    assert body["error_id"] in body["error"]


def test_erreur_non_rattrapee_gestionnaire_global(client, app_module, monkeypatch):
    def boom():
        raise RuntimeError("/app/data/deliveries.json : permission refusée")
    # Route sans try/except : c'est le gestionnaire global qui répond
    monkeypatch.setattr(app_module, "_load_deliveries", boom)
    _login(client)
    resp = client.delete("/api/stock/delivery/0", headers=FETCH)
    assert resp.status_code == 500
    assert "deliveries.json" not in resp.get_data(as_text=True)
    assert resp.get_json()["error_id"]


def test_erreurs_http_non_interceptees(client):
    _login(client)
    assert client.get("/route-inexistante").status_code == 404


# ── S-06 : changer le mot de passe déconnecte les autres sessions ────────

def test_changement_mot_de_passe_invalide_les_autres_sessions(app_module, client):
    autre = _login(app_module.app.test_client())
    _login(client)
    assert autre.get("/api/thermostat/state").status_code != 401

    resp = client.post("/api/auth/change-password",
                       json={"current_password": "ancien-mdp", "new_password": "nouveau-mdp"}, headers=FETCH)
    assert resp.status_code == 200

    # L'ancien cookie de l'autre appareil est rejeté, la session qui a changé le mot de passe reste connectée
    assert autre.get("/api/thermostat/state").status_code == 401
    assert client.get("/api/thermostat/state").status_code != 401


def test_deconnecter_tous_les_appareils(app_module, client):
    autre = _login(app_module.app.test_client())
    _login(client)
    resp = client.post("/api/auth/logout-all", headers=FETCH)
    assert resp.status_code == 200
    assert autre.get("/api/thermostat/state").status_code == 401
    assert client.get("/api/thermostat/state").status_code != 401


# ── Compte à rebours du thermostat ────────────────────────────────────────

def test_etat_thermostat_donne_prochaine_evaluation(client, app_module, monkeypatch):
    from datetime import datetime, timezone
    job = mock.Mock(next_run_time=datetime(2026, 11, 1, 18, 20, tzinfo=timezone.utc))
    monkeypatch.setattr(app_module._scheduler, "get_job", lambda job_id: job if job_id == "thermostat_job" else None)
    _login(client)
    assert client.get("/api/thermostat/state").get_json()["next_check"] == "2026-11-01T18:20:00+00:00"

    monkeypatch.setattr(app_module._scheduler, "get_job", lambda job_id: None)
    assert client.get("/api/thermostat/state").get_json()["next_check"] is None


def test_evaluation_forcee_avance_le_passage(client, app_module, monkeypatch):
    calls = []
    monkeypatch.setattr(app_module._scheduler, "get_job", lambda job_id: mock.Mock() if job_id == "thermostat_job" else None)
    monkeypatch.setattr(app_module._scheduler, "modify_job", lambda job_id, **kw: calls.append((job_id, kw)))
    _login(client)
    resp = client.post("/api/thermostat/check-now", headers=FETCH)
    assert resp.status_code == 200
    assert calls and calls[0][0] == "thermostat_job" and "next_run_time" in calls[0][1]

    monkeypatch.setattr(app_module._scheduler, "get_job", lambda job_id: None)
    assert client.post("/api/thermostat/check-now", headers=FETCH).status_code == 409


# ── Linky : le test d'accès utilise le PDL et la clé saisis ──────────────

def test_linky_test_utilise_les_valeurs_du_formulaire(client, app_module, monkeypatch):
    seen = {}

    def fake_test_access(cfg):
        seen.update(cfg)
        return True, "ok"

    monkeypatch.setattr(app_module.linky_module, "test_access", fake_test_access)
    _login(client)
    resp = client.post("/api/linky/test", json={"pdl": "0123 4567 8901 23", "token": "cle-saisie"}, headers=FETCH)
    assert resp.get_json()["status"] == "ok"
    assert seen["usage_point_id"] == "01234567890123"
    assert seen["token"] == "cle-saisie"


# ── Flux d'énergie de l'accueil ───────────────────────────────────────────

def test_heating_flow_jour_rouge(app_module, monkeypatch):
    monkeypatch.setattr(app_module.history_module, "heating_today",
                        lambda cfg: {"since": {"poele": "2026-11-14T13:00:00", "clim": None},
                                     "today_cost": {"poele": 1.2, "clim": 0.0, "total": 1.2}})
    monkeypatch.setattr(app_module.ha_client, "is_clim_configured", lambda cfg: True)
    data = {"tempo": {"today": {"color": "RED", "label": "Rouge"}, "current_period": "HP"},
            "weather": {"temperature": 3.0}, "indoor": {"temperature": 20.1}}
    radiateurs = [{"entity_id": "switch.r1", "enabled": True, "state": "off", "managed_off": True}]
    f = app_module._heating_flow(data, {"state": "heat"}, {"state": "off"}, radiateurs)
    assert f["poele"]["on"] and f["poele"]["since"] == "2026-11-14T13:00:00"
    assert f["poele"]["cost_per_hour"] > 0 and f["cost_per_hour"] == f["poele"]["cost_per_hour"]
    assert f["clim"]["excluded"] and not f["clim"]["on"]
    assert f["radiateurs"] == {"count": 1, "on": 0, "cut": True}
    assert f["today_cost"] == 1.2 and f["indoor"] == 20.1


def test_heating_flow_ha_injoignable(app_module, monkeypatch):
    monkeypatch.setattr(app_module.history_module, "heating_today",
                        lambda cfg: {"since": {"poele": None, "clim": None},
                                     "today_cost": {"poele": 0, "clim": 0, "total": 0}})
    f = app_module._heating_flow({"tempo": {}, "weather": {}}, None, None, [])
    assert not f["poele"]["known"] and not f["poele"]["on"]
    assert f["cost_per_hour"] == 0 and f["tempo"]["color"] == "UNKNOWN"
