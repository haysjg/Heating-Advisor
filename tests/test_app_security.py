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
