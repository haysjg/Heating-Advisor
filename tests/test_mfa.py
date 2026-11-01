"""Tests de la double authentification TOTP (modules/mfa.py et routes de connexion)."""

import importlib
import threading
import time
from unittest import mock

import pytest

import modules.mfa as mfa

pyotp = pytest.importorskip("pyotp")
pytest.importorskip("flask_limiter")
pytest.importorskip("apscheduler")

FETCH = {"X-Requested-With": "fetch"}


# ── modules/mfa.py ────────────────────────────────────────────────────────

def test_verify_totp_accepte_code_courant_et_refuse_rejeu():
    secret = mfa.generate_secret()
    code = pyotp.TOTP(secret).now()
    step = mfa.verify_totp(secret, code)
    assert step is not None
    assert mfa.verify_totp(secret, code, last_step=step) is None


def test_verify_totp_tolere_un_pas_de_decalage():
    secret = mfa.generate_secret()
    totp = pyotp.TOTP(secret)
    now = time.time()
    assert mfa.verify_totp(secret, totp.at(now - 30)) is not None
    assert mfa.verify_totp(secret, totp.at(now - 90)) is None


@pytest.mark.parametrize("code", ["", "12345", "abcdef", "1234567"])
def test_verify_totp_format_invalide(code):
    assert mfa.verify_totp(mfa.generate_secret(), code) is None


def test_code_de_secours_usage_unique():
    codes, hashes = mfa.generate_recovery_codes()
    assert len(codes) == mfa.RECOVERY_CODE_COUNT == len(set(codes))
    assert codes[0] not in hashes  # jamais stocké en clair
    remaining = mfa.consume_recovery_code(hashes, codes[0].upper().replace("-", " "))
    assert remaining is not None and len(remaining) == len(hashes) - 1
    assert mfa.consume_recovery_code(remaining, codes[0]) is None


def test_qr_svg():
    assert mfa.qr_svg(mfa.provisioning_uri(mfa.generate_secret())).startswith("<svg")


# ── Routes ────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def app_module():
    from apscheduler.schedulers.background import BackgroundScheduler
    with mock.patch.object(BackgroundScheduler, "start"), mock.patch.object(threading.Thread, "start"):
        module = importlib.import_module("app")
    module.app.config["TESTING"] = True
    return module


@pytest.fixture
def client(app_module, monkeypatch, tmp_path):
    import modules.crypto as crypto
    monkeypatch.setattr(crypto, "_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setattr(app_module, "_patch_override", lambda fn: fn({}))
    monkeypatch.setitem(app_module.config.AUTH, "password_hash", app_module.generate_password_hash("mdp-solide"))
    monkeypatch.setitem(app_module.config.AUTH, "session_epoch", "")
    for key in ("totp_secret", "totp_last_step", "recovery_codes"):
        monkeypatch.delitem(app_module.config.AUTH, key, raising=False)
    yield app_module.app.test_client()
    for key in ("totp_secret", "totp_last_step", "recovery_codes"):
        app_module.config.AUTH.pop(key, None)
    app_module.limiter.reset()


@pytest.fixture(autouse=True)
def _reset_limiter(request):
    if "app_module" in request.fixturenames:
        request.getfixturevalue("app_module").limiter.reset()


def _login(client, password="mdp-solide"):
    return client.post("/login", data={"password": password})


def _enable_mfa(client):
    """Active la MFA via l'API ; renvoie (secret, codes de secours)."""
    assert _login(client).status_code == 302
    resp = client.post("/api/auth/mfa/setup", json={"password": "mdp-solide"}, headers=FETCH)
    assert resp.status_code == 200
    secret = resp.get_json()["secret"]
    resp = client.post("/api/auth/mfa/enable", json={"code": pyotp.TOTP(secret).now()}, headers=FETCH)
    assert resp.status_code == 200
    return secret, resp.get_json()["recovery_codes"]


def _next_code(app_module, secret):
    """Code d'un pas postérieur au dernier utilisé (l'anti-rejeu refuse le code courant)."""
    step = int(app_module.config.AUTH["totp_last_step"]) + 1
    return pyotp.TOTP(secret).generate_otp(step)


def test_sans_mfa_connexion_directe(client):
    resp = _login(client)
    assert resp.headers["Location"] == "/"
    assert client.get("/api/auth/mfa").get_json()["enabled"] is False


def test_activation_deconnecte_les_autres_sessions(app_module, client):
    autre = app_module.app.test_client()
    _login(autre)
    _enable_mfa(client)
    assert autre.get("/api/thermostat/state").status_code == 401
    assert client.get("/api/thermostat/state").status_code != 401


def test_mot_de_passe_seul_ne_suffit_plus(app_module, client):
    secret, _ = _enable_mfa(client)
    autre = app_module.app.test_client()
    resp = _login(autre)
    assert resp.headers["Location"].endswith("/login/code")
    assert autre.get("/api/thermostat/state").status_code == 401

    resp = autre.post("/login/code", data={"code": "000000"})
    assert resp.status_code == 200 and "incorrect" in resp.get_data(as_text=True)

    resp = autre.post("/login/code", data={"code": _next_code(app_module, secret)})
    assert resp.status_code == 302
    assert autre.get("/api/thermostat/state").status_code != 401


def test_code_de_secours_a_la_connexion(app_module, client):
    _, codes = _enable_mfa(client)
    autre = app_module.app.test_client()
    _login(autre)
    assert autre.post("/login/code", data={"code": codes[0]}).status_code == 302
    assert len(app_module.config.AUTH["recovery_codes"]) == len(codes) - 1

    tiers = app_module.app.test_client()
    _login(tiers)
    assert tiers.post("/login/code", data={"code": codes[0]}).status_code == 200  # déjà consommé


def test_page_code_sans_mot_de_passe_renvoie_au_login(client):
    resp = client.get("/login/code")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/login")


def test_attente_mfa_expiree(app_module, client):
    secret, _ = _enable_mfa(client)
    autre = app_module.app.test_client()
    _login(autre)
    with autre.session_transaction() as sess:
        sess["mfa_pending_until"] = 0
    resp = autre.post("/login/code", data={"code": _next_code(app_module, secret)})
    assert resp.headers["Location"].endswith("/login")


def test_appareil_de_confiance(app_module, client):
    secret, _ = _enable_mfa(client)
    appareil = app_module.app.test_client()
    _login(appareil)
    resp = appareil.post("/login/code", data={"code": _next_code(app_module, secret), "remember": "1"})
    assert app_module.TRUSTED_DEVICE_COOKIE in resp.headers.get("Set-Cookie", "")

    appareil.get("/logout")
    assert _login(appareil).headers["Location"] == "/"  # plus de code demandé

    # « Déconnecter tous les appareils » révoque la confiance
    client.post("/api/auth/logout-all", headers=FETCH)
    appareil.get("/logout")
    assert _login(appareil).headers["Location"].endswith("/login/code")


def test_cookie_de_confiance_falsifie(app_module, client):
    _enable_mfa(client)
    autre = app_module.app.test_client()
    autre.set_cookie(app_module.TRUSTED_DEVICE_COOKIE, "faux.jeton.signe")
    assert _login(autre).headers["Location"].endswith("/login/code")


def test_activation_refuse_mauvais_code_et_mauvais_mot_de_passe(client):
    _login(client)
    assert client.post("/api/auth/mfa/setup", json={"password": "faux"}, headers=FETCH).status_code == 403
    client.post("/api/auth/mfa/setup", json={"password": "mdp-solide"}, headers=FETCH)
    assert client.post("/api/auth/mfa/enable", json={"code": "000000"}, headers=FETCH).status_code == 400
    assert client.get("/api/auth/mfa").get_json()["enabled"] is False


def test_setup_refuse_si_deja_active(client):
    _enable_mfa(client)
    resp = client.post("/api/auth/mfa/setup", json={"password": "mdp-solide"}, headers=FETCH)
    assert resp.status_code == 400


def test_desactivation_exige_mot_de_passe_et_code(app_module, client):
    secret, _ = _enable_mfa(client)
    resp = client.post("/api/auth/mfa/disable", json={"password": "mdp-solide", "code": "000000"}, headers=FETCH)
    assert resp.status_code == 403
    resp = client.post("/api/auth/mfa/disable",
                       json={"password": "mdp-solide", "code": _next_code(app_module, secret)}, headers=FETCH)
    assert resp.status_code == 200
    assert "totp_secret" not in app_module.config.AUTH
    assert client.get("/api/thermostat/state").status_code != 401


def test_regeneration_codes_de_secours(app_module, client):
    secret, anciens = _enable_mfa(client)
    resp = client.post("/api/auth/mfa/recovery-codes",
                       json={"password": "mdp-solide", "code": _next_code(app_module, secret)}, headers=FETCH)
    nouveaux = resp.get_json()["recovery_codes"]
    assert mfa.consume_recovery_code(app_module.config.AUTH["recovery_codes"], anciens[0]) is None
    assert mfa.consume_recovery_code(app_module.config.AUTH["recovery_codes"], nouveaux[0]) is not None


@pytest.mark.parametrize("target", ["//attaquant.example", "https://attaquant.example", "/\\attaquant.example"])
def test_redirection_externe_refusee(client, target):
    resp = client.post("/login", query_string={"next": target}, data={"password": "mdp-solide"})
    assert resp.headers["Location"] == "/"
