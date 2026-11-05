"""
Application Flask – Conseiller Chauffage
Déployable via Docker sur NAS Synology
"""

import hashlib
import hmac
import logging
import os
import secrets as _secrets
from datetime import date, datetime, timedelta
from flask import Flask, render_template, jsonify, request, session, redirect, url_for
from itsdangerous import BadSignature, URLSafeTimedSerializer
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import config
from modules.weather import get_current_conditions, get_current_temperature, get_tomorrow_weather, get_hourly_forecast
from modules.tempo import get_tempo_info
from modules.analysis import DataCache, build_advisor_config, compute_analysis, tariff_slot
from modules.overrides import apply as apply_overrides, load as load_overrides, OVERRIDE_FILE, patch_override as _patch_override
from modules.crypto import decrypt_password, encrypt_password
from modules import mfa
import modules.homeassistant as ha_client
from modules import jsonstore
import modules.thermostat as thermostat_module
import modules.history as history_module
import modules.radiateurs as radiateurs_module
import modules.presence_watch as presence_watch
import modules.source_watch as source_watch
import modules.pellet_refill as pellet_refill
from modules.stock import compute_stock_stats
import modules.imap_client as imap_client
import modules.electricity as electricity_module
import modules.backup as backup_module

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

RADIATEURS_MANAGED_OFF_FILE = os.path.join(os.path.dirname(__file__), "data", "radiateurs_managed_off.json")
DELIVERIES_FILE = os.path.join(os.path.dirname(__file__), "data", "deliveries.json")

def _load_managed_off() -> list:
    data = jsonstore.load(RADIATEURS_MANAGED_OFF_FILE, [])
    return data if isinstance(data, list) else []


def _update_managed_off(add: list = (), remove: list = ()) -> None:
    """Ajoute/retire des radiateurs de la liste « éteints par l'appli » (lecture-écriture sous verrou)."""
    def _apply(managed):
        managed = managed if isinstance(managed, list) else []
        managed = [e for e in managed if e not in remove]
        managed += [e for e in add if e not in managed]
        return managed
    try:
        jsonstore.update(RADIATEURS_MANAGED_OFF_FILE, _apply, [])
    except Exception as e:
        logger.error("Sauvegarde managed_off échouée : %s", e)


def _suspend_thermostat_after_manual_off(entity_type: str) -> None:
    """Suspend thermostat for configured hours after manual off."""
    try:
        state = thermostat_module.get_state()
        suspend_hours = config.THERMOSTAT.get("manual_off_suspend_hours", 4)
        suspended_until = (datetime.now() + timedelta(hours=suspend_hours)).isoformat()

        # Capturer le système actif pour mettre à jour le bon timestamp
        active_sys = state.get("active_system", entity_type)
        new_state = {
            **state,
            "state": "off",
            "active_system": None,
            "suspended_until": suspended_until,
        }
        thermostat_module._update_system_timestamp(new_state, active_sys, "off")
        thermostat_module._save_state(new_state)
        logger.info("Manual off %s - thermostat suspended until %s", entity_type, suspended_until)
    except Exception as e:
        logger.error("Erreur suspension thermostat : %s", e)


def _cancel_thermostat_suspension(entity_type: str) -> None:
    """Cancel thermostat suspension on manual on (only if suspended)."""
    try:
        state = thermostat_module.get_state()
        # Seulement annuler la suspension, ne pas réactiver le thermostat
        if state.get("suspended_until"):
            new_state = {
                **state,
                "suspended_until": None,
            }
            thermostat_module._save_state(new_state)
            logger.info("Manual on %s - suspension cancelled", entity_type)
    except Exception as e:
        logger.error("Erreur annulation suspension thermostat : %s", e)

app = Flask(__name__)

# ── Clé secrète persistante (sessions) ───────────────────────
def _get_secret_key() -> str:
    key_file = os.path.join(os.path.dirname(__file__), "data", "secret.key")
    os.makedirs(os.path.dirname(key_file), exist_ok=True)
    if os.path.exists(key_file):
        key = open(key_file).read().strip()
        if key:
            return key
    key = _secrets.token_hex(32)
    with open(key_file, "w") as f:
        f.write(key)
    return key

app.secret_key = _get_secret_key()
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=24)
# Cookie de session non envoyé sur les requêtes POST/PUT/DELETE venant d'un autre site
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True

# ── Rate limiter (protection brute-force login) ───────────────
limiter = Limiter(get_remote_address, app=app, default_limits=[], storage_uri="memory://")

load_overrides(config)

# ── Erreurs internes ──────────────────────────────────────────
# Le détail d'une exception (chemins, URLs internes, messages SMTP/IMAP ou SQLite) reste
# dans les journaux : le client ne reçoit qu'un message neutre et une référence à y chercher.

def _log_internal_error(exc: BaseException) -> str:
    error_id = _secrets.token_hex(4)
    logger.error("Erreur interne [%s] sur %s %s", error_id, request.method, request.path, exc_info=exc)
    return error_id


def _internal_error_message(error_id: str) -> str:
    return f"Erreur interne (réf. {error_id}) — détails dans les journaux du serveur"


def _internal_error(exc: BaseException):
    """Réponse JSON 500 neutre pour une exception attrapée dans une route d'API."""
    error_id = _log_internal_error(exc)
    return jsonify({"error": _internal_error_message(error_id), "error_id": error_id}), 500


@app.errorhandler(Exception)
def handle_unexpected_error(exc):
    # Les erreurs HTTP (404, 405, 429…) gardent leur réponse habituelle
    if isinstance(exc, HTTPException):
        return exc
    if request.path.startswith("/api/"):
        return _internal_error(exc)
    return _internal_error_message(_log_internal_error(exc)), 500

# ── Authentification ─────────────────────────────────────────

def _uses_default_password() -> bool:
    return not config.AUTH.get("password_hash")


def _check_password(password: str) -> bool:
    stored = config.AUTH.get("password_hash", "")
    if not stored:
        return hmac.compare_digest(password.encode(), config.AUTH.get("default_password", "heating").encode())
    return check_password_hash(stored, password)


def _auth_fingerprint() -> str:
    """Empreinte des identifiants courants, gardée dans la session.

    Elle change avec le mot de passe ou « Déconnecter tous les appareils » : les cookies de
    session émis avant ne correspondent plus et sont rejetés.
    """
    material = (f"{config.AUTH.get('password_hash', '')}|{config.AUTH.get('session_epoch', '')}"
                f"|{config.AUTH.get('totp_secret', '')}")
    return hashlib.sha256(material.encode()).hexdigest()[:16]


# ── Double authentification (TOTP) ───────────────────────────
# Après le mot de passe, la session est seulement « en attente MFA » : elle ne devient
# authentifiée qu'avec un code TOTP ou un code de secours. Un appareil de confiance garde
# un cookie signé qui dispense du code pendant TRUSTED_DEVICE_DAYS ; il porte l'empreinte
# des identifiants, donc changer le mot de passe, la MFA ou « Déconnecter tous les
# appareils » révoque tous les appareils de confiance.

TRUSTED_DEVICE_COOKIE = "trusted_device"
TRUSTED_DEVICE_DAYS = 30
MFA_PENDING_MINUTES = 5
_trust_serializer = URLSafeTimedSerializer(app.secret_key, salt="mfa-trusted-device")


def _mfa_enabled() -> bool:
    return bool(config.AUTH.get("totp_secret"))


def _set_auth(**values) -> None:
    """Enregistre des clés AUTH dans config_override.json et en mémoire (None = suppression)."""
    def _apply(d):
        auth = d.setdefault("AUTH", {})
        for key, value in values.items():
            if value is None:
                auth.pop(key, None)
            else:
                auth[key] = value
    _patch_override(_apply)
    for key, value in values.items():
        if value is None:
            config.AUTH.pop(key, None)
        else:
            config.AUTH[key] = value


def _check_mfa_code(code: str) -> str | None:
    """Vérifie un code TOTP ou de secours (et le consomme). Renvoie "totp", "recovery" ou None."""
    if mfa.is_totp_format(code):
        secret = decrypt_password(config.AUTH.get("totp_secret", ""))
        step = mfa.verify_totp(secret, code, int(config.AUTH.get("totp_last_step") or 0))
        if step is None:
            return None
        _set_auth(totp_last_step=step)
        return "totp"
    remaining = mfa.consume_recovery_code(config.AUTH.get("recovery_codes", []), code)
    if remaining is None:
        return None
    _set_auth(recovery_codes=remaining)
    logger.warning("Code de secours MFA utilisé — %d restant(s)", len(remaining))
    return "recovery"


def _is_trusted_device() -> bool:
    token = request.cookies.get(TRUSTED_DEVICE_COOKIE)
    if not token:
        return False
    try:
        data = _trust_serializer.loads(token, max_age=TRUSTED_DEVICE_DAYS * 86400)
    except BadSignature:
        return False
    return isinstance(data, dict) and hmac.compare_digest(str(data.get("fp", "")), _auth_fingerprint())


def _remember_device(resp) -> None:
    token = _trust_serializer.dumps({"fp": _auth_fingerprint(), "id": _secrets.token_hex(8)})
    resp.set_cookie(TRUSTED_DEVICE_COOKIE, token, max_age=TRUSTED_DEVICE_DAYS * 86400,
                    httponly=True, samesite="Lax", secure=request.is_secure)


def _safe_next(next_url: str | None) -> str:
    next_url = next_url or "/"
    # "//hote" ou "/\\hote" seraient interprétés comme une autre origine
    if not next_url.startswith("/") or next_url.startswith(("//", "/\\")):
        return "/"
    return next_url


def _start_session() -> None:
    session.clear()
    session.permanent = True
    session["authenticated"] = True
    session["auth_fp"] = _auth_fingerprint()


@app.before_request
def require_login():
    if request.path.startswith("/static"):
        return
    if request.endpoint in ("login", "login_mfa", "logout"):
        return
    if session.get("authenticated") and not hmac.compare_digest(session.get("auth_fp", ""), _auth_fingerprint()):
        session.clear()  # session antérieure à un changement de mot de passe ou à « Déconnecter tous les appareils »
    # Mot de passe par défaut : il doit être changé avant toute autre action
    if session.get("must_change_password"):
        if not _uses_default_password():
            session.pop("must_change_password")
        elif request.endpoint not in ("change_password_page", "api_change_password"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Changez d'abord le mot de passe par défaut"}), 403
            return redirect(url_for("change_password_page"))
    if not session.get("authenticated"):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Non authentifié"}), 401
        return redirect(url_for("login", next=request.path))
    # Protection CSRF : toute requête qui modifie quelque chose doit venir du front de l'appli,
    # qui ajoute cet en-tête (base.html). Un site tiers ne peut pas l'envoyer sans pré-vol CORS.
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.headers.get("X-Requested-With") != "fetch":
        logger.warning("Requête %s %s refusée : en-tête anti-CSRF absent (%s)", request.method, request.path, request.remote_addr)
        return jsonify({"error": "Requête refusée : rechargez la page puis réessayez"}), 403


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def login():
    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        if _check_password(password):
            next_url = _safe_next(request.args.get("next"))
            if _uses_default_password():
                _start_session()
                session["must_change_password"] = True
                logger.warning("Connexion avec le mot de passe par défaut — changement exigé")
                return redirect(url_for("change_password_page"))
            if _mfa_enabled() and not _is_trusted_device():
                session.clear()
                session["mfa_pending_fp"] = _auth_fingerprint()
                session["mfa_pending_until"] = (datetime.now() + timedelta(minutes=MFA_PENDING_MINUTES)).timestamp()
                session["mfa_next"] = next_url
                return redirect(url_for("login_mfa"))
            _start_session()
            return redirect(next_url)
        error = "Mot de passe incorrect"
        logger.warning("Tentative de connexion échouée depuis %s", request.remote_addr)
    return render_template("login.html", error=error)


@app.route("/login/code", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
def login_mfa():
    pending_fp = session.get("mfa_pending_fp", "")
    if (not pending_fp or not hmac.compare_digest(pending_fp, _auth_fingerprint())
            or datetime.now().timestamp() > session.get("mfa_pending_until", 0)):
        session.clear()
        return redirect(url_for("login"))
    error = None
    if request.method == "POST":
        method = _check_mfa_code(request.form.get("code", ""))
        if method:
            next_url = _safe_next(session.get("mfa_next"))
            _start_session()
            resp = redirect(next_url)
            if request.form.get("remember"):
                _remember_device(resp)
            return resp
        error = "Code incorrect ou déjà utilisé"
        logger.warning("Code MFA refusé depuis %s", request.remote_addr)
    return render_template("login_mfa.html", error=error, remember_days=TRUSTED_DEVICE_DAYS)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/changer-mot-de-passe")
def change_password_page():
    return render_template("change_password.html", forced=_uses_default_password())


@app.route("/api/auth/change-password", methods=["POST"])
def api_change_password():
    data = request.get_json(force=True)
    current = data.get("current_password", "")
    new_pwd = data.get("new_password", "")
    if not _check_password(current):
        return jsonify({"error": "Mot de passe actuel incorrect"}), 403
    if len(new_pwd) < 8:
        return jsonify({"error": "Le nouveau mot de passe doit faire au moins 8 caractères"}), 400
    new_hash = generate_password_hash(new_pwd)
    try:
        _patch_override(lambda d: d.setdefault("AUTH", {}).__setitem__("password_hash", new_hash))
        config.AUTH["password_hash"] = new_hash
        session.pop("must_change_password", None)
        session["auth_fp"] = _auth_fingerprint()  # cette session reste connectée, les autres non
        logger.info("Mot de passe modifié — autres sessions déconnectées")
    except Exception as e:
        return _internal_error(e)
    return jsonify({"status": "ok"})


@app.route("/api/auth/logout-all", methods=["POST"])
def api_logout_all():
    """Déconnecte tous les appareils sauf celui qui fait la demande."""
    epoch = _secrets.token_hex(8)
    _patch_override(lambda d: d.setdefault("AUTH", {}).__setitem__("session_epoch", epoch))
    config.AUTH["session_epoch"] = epoch
    session["auth_fp"] = _auth_fingerprint()
    logger.info("Toutes les autres sessions ont été déconnectées")
    return jsonify({"status": "ok"})


@app.route("/api/auth/mfa")
def api_mfa_status():
    return jsonify({
        "enabled": _mfa_enabled(),
        "recovery_remaining": len(config.AUTH.get("recovery_codes", [])) if _mfa_enabled() else 0,
        "trusted_device": _is_trusted_device(),
    })


@app.route("/api/auth/mfa/setup", methods=["POST"])
@limiter.limit("10 per minute")
def api_mfa_setup():
    """Étape 1 : nouveau secret (pas encore actif), à scanner puis confirmer par un code."""
    if _mfa_enabled():
        return jsonify({"error": "Désactivez d'abord la double authentification en place"}), 400
    data = request.get_json(force=True)
    if not _check_password(data.get("password", "")):
        return jsonify({"error": "Mot de passe incorrect"}), 403
    secret = mfa.generate_secret()
    session["mfa_setup_secret"] = encrypt_password(secret)
    uri = mfa.provisioning_uri(secret)
    return jsonify({"secret": secret, "qr_svg": mfa.qr_svg(uri)})


@app.route("/api/auth/mfa/enable", methods=["POST"])
@limiter.limit("10 per minute")
def api_mfa_enable():
    """Étape 2 : le code prouve que l'application est bien configurée ; la MFA devient active."""
    data = request.get_json(force=True)
    pending = session.get("mfa_setup_secret")
    if not pending:
        return jsonify({"error": "Recommencez l'activation"}), 400
    step = mfa.verify_totp(decrypt_password(pending), data.get("code", ""))
    if step is None:
        return jsonify({"error": "Code incorrect — vérifiez l'heure du téléphone"}), 400
    codes, hashes = mfa.generate_recovery_codes()
    try:
        _set_auth(totp_secret=pending, totp_last_step=step, recovery_codes=hashes)
    except Exception as e:
        return _internal_error(e)
    session.pop("mfa_setup_secret", None)
    session["auth_fp"] = _auth_fingerprint()  # cette session reste connectée, les autres non
    logger.info("Double authentification activée — autres sessions déconnectées")
    return jsonify({"status": "ok", "recovery_codes": codes})


def _reauth_mfa_error(data: dict):
    """Désactiver la MFA ou refaire les codes de secours exige mot de passe + code."""
    if not _check_password(data.get("password", "")):
        return jsonify({"error": "Mot de passe incorrect"}), 403
    if not _check_mfa_code(data.get("code", "")):
        return jsonify({"error": "Code incorrect ou déjà utilisé"}), 403
    return None


@app.route("/api/auth/mfa/disable", methods=["POST"])
@limiter.limit("5 per minute")
def api_mfa_disable():
    if not _mfa_enabled():
        return jsonify({"status": "ok"})
    data = request.get_json(force=True)
    error = _reauth_mfa_error(data)
    if error:
        return error
    try:
        _set_auth(totp_secret=None, totp_last_step=None, recovery_codes=None)
    except Exception as e:
        return _internal_error(e)
    session["auth_fp"] = _auth_fingerprint()
    logger.warning("Double authentification désactivée")
    return jsonify({"status": "ok"})


@app.route("/api/auth/mfa/recovery-codes", methods=["POST"])
@limiter.limit("5 per minute")
def api_mfa_recovery_codes():
    if not _mfa_enabled():
        return jsonify({"error": "Double authentification inactive"}), 400
    data = request.get_json(force=True)
    error = _reauth_mfa_error(data)
    if error:
        return error
    codes, hashes = mfa.generate_recovery_codes()
    try:
        _set_auth(recovery_codes=hashes)
    except Exception as e:
        return _internal_error(e)
    logger.info("Codes de secours MFA régénérés")
    return jsonify({"status": "ok", "recovery_codes": codes})


# ── Base SQLite : migrations appliquées une seule fois, avant le démarrage des jobs ──
try:
    history_module.init_db()
except Exception as e:
    logger.error("Migration de la base échouée : %s", e)

# ── Scheduler de notification ─────────────────────────────────

_scheduler = BackgroundScheduler(timezone="Europe/Paris")
_scheduler.start()


def _run_notify():
    """Lance la notification email (appelé par le scheduler ou manuellement)."""
    from notify import main as notify_main
    success = notify_main()
    # Pilotage automatique HA si activé
    if config.HOME_ASSISTANT.get("auto_control"):
        try:
            data = get_analysis()
            system = data.get("tomorrow", {}).get("recommendation", {}).get("system", "none")
            ha_client.apply_recommendation(config.HOME_ASSISTANT, system)
        except Exception as e:
            logger.error("HA apply_recommendation échoué : %s", e)
    return success


def _reschedule_notify():
    """(Re)planifie le job de notification selon la config courante."""
    try:
        _scheduler.remove_job("notify_job")
    except Exception:
        pass
    if config.EMAIL.get("enabled"):
        hour = int(config.EMAIL.get("notify_hour", 20))
        minute = int(config.EMAIL.get("notify_minute", 0))
        _scheduler.add_job(
            _run_notify,
            CronTrigger(hour=hour, minute=minute, timezone="Europe/Paris"),
            id="notify_job",
            misfire_grace_time=300,
        )
        logger.info("Notification planifiée chaque jour à %02dh%02d", hour, minute)
    else:
        logger.info("Notifications email désactivées — aucun job planifié")


def _record_history():
    """Enregistre un point d'historique (températures + état chauffage + couleur Tempo)."""
    try:
        data = get_analysis()
        outdoor_temp = data.get("weather", {}).get("temperature")
        indoor_temp = data.get("indoor", {}).get("temperature") if data.get("indoor") else None
        th_state = thermostat_module.get_state()
        poele_ha = ha_client.get_state(config.HOME_ASSISTANT)
        heating_state = history_module.poele_state_for_history(poele_ha.get("state") if poele_ha else None, th_state)
        tempo_color = data.get("tempo", {}).get("today", {}).get("color")
        history_module.record(outdoor_temp, indoor_temp, heating_state, tempo_color)
        pellet_refill.tick(heating_state == "on", _pellet_cycle_hours(), config.EMAIL, config.NTFY)
    except Exception as e:
        logger.error("History record échoué : %s", e)


def _poele_purchase() -> dict:
    override = jsonstore.load(OVERRIDE_FILE, {})
    purchase = override.get("_poele_purchase", {}) if isinstance(override, dict) else {}
    return purchase if isinstance(purchase, dict) else {}


def _pellet_cycle_hours() -> float:
    """Durée d'un sac (heures de chauffe), réglée dans /config ; 15 h par défaut."""
    try:
        return float(_poele_purchase().get("hours_per_bag") or pellet_refill.DEFAULT_CYCLE_HOURS)
    except (TypeError, ValueError):
        return pellet_refill.DEFAULT_CYCLE_HOURS


def _pellet_bag_kg() -> float:
    """Poids d'un sac, réglé dans /config ; 15 kg par défaut."""
    try:
        return float(_poele_purchase().get("poids_sac") or pellet_refill.DEFAULT_BAG_KG)
    except (TypeError, ValueError):
        return pellet_refill.DEFAULT_BAG_KG


def _pellet_status() -> dict:
    return pellet_refill.status(_pellet_cycle_hours(), _pellet_bag_kg())


def _record_diagnose():
    """Enregistre un snapshot de diagnostic toutes les 10 min."""
    try:
        nearby_zone = config.THERMOSTAT.get("nearby_zone_name", "")
        if config.THERMOSTAT.get("presence_enabled"):
            if nearby_zone:
                presence = ha_client.get_presence_extended(
                    config.HOME_ASSISTANT, config.THERMOSTAT.get("person_entities", []), nearby_zone
                )
            else:
                raw = ha_client.get_presence(config.HOME_ASSISTANT, config.THERMOSTAT.get("person_entities", []))
                presence = "home" if raw else "away" if raw is False else None
        else:
            presence = None

        th_state = thermostat_module.get_state()
        indoor = ha_client.get_indoor_climate(config.HOME_ASSISTANT)
        ha_state = ha_client.get_state(config.HOME_ASSISTANT)
        indoor_temp = indoor.get("temperature") if indoor else None
        humidity = indoor.get("humidity") if indoor else None
        felt = thermostat_module.felt_temperature(indoor_temp, humidity, config.THERMOSTAT) if indoor_temp is not None else None
        poele_real = ha_state.get("state") if ha_state else None
        clim_ha_state = ha_client.get_clim_state(config.HOME_ASSISTANT)
        clim_real = clim_ha_state.get("state") if clim_ha_state else None
        data = get_analysis()
        recommendation = data.get("recommendation", {}).get("system")

        history_module.record_diagnose(
            presence_status=presence,
            poele_real_state=poele_real,
            thermostat_state=th_state.get("state", "off"),
            felt_temperature=felt,
            indoor_temp=indoor_temp,
            in_schedule=thermostat_module.is_in_schedule(config.THERMOSTAT),
            everyone_away=(presence == "away"),
            suspended_until=th_state.get("suspended_until"),
            recommendation=recommendation,
            clim_real_state=clim_real,
            active_system=th_state.get("active_system"),
        )
    except Exception as e:
        logger.error("Diagnose record échoué : %s", e)


def _run_thermostat():
    """Vérifie la température intérieure et pilote le poêle via le thermostat."""
    try:
        data = get_analysis()
        recommendation = data.get("recommendation", {}).get("system", "none")
        outdoor_temp = data.get("weather", {}).get("temperature")
        clim_fallback_ok = bool((data.get("clim") or {}).get("available")) and \
            not (data.get("recommendation") or {}).get("red_day_override")
        thermostat_module.check_and_apply(config.HOME_ASSISTANT, config.THERMOSTAT, recommendation, config.EMAIL,
                                          ntfy_cfg=config.NTFY, outdoor_temp=outdoor_temp,
                                          comfort_min_temp=config.CLIM.get("comfort_min_temp"),
                                          clim_fallback_ok=clim_fallback_ok)
    except Exception as e:
        logger.error("Thermostat check échoué : %s", e)


def _reschedule_thermostat():
    """(Re)planifie le job thermostat selon la config courante."""
    try:
        _scheduler.remove_job("thermostat_job")
    except Exception:
        pass
    if config.THERMOSTAT.get("enabled"):
        interval = int(config.THERMOSTAT.get("check_interval_minutes", 10))
        _scheduler.add_job(
            _run_thermostat,
            "interval",
            minutes=interval,
            id="thermostat_job",
            misfire_grace_time=60,
        )
        logger.info("Thermostat planifié toutes les %d min", interval)
    else:
        logger.info("Thermostat désactivé — aucun job planifié")


def _run_radiateurs_tempo_rouge():
    """Vérifie et pilote les radiateurs lors des jours Tempo Rouge (HP uniquement)."""
    cfg = config.RADIATEURS_TEMPO_ROUGE
    if not cfg.get("enabled"):
        return
    raw_entities = cfg.get("entities", [])
    # Supporte l'ancien format (liste de strings) et le nouveau (liste de dicts)
    # Construit une liste de (entity_id, display_name)
    entities = []
    for e in raw_entities:
        if isinstance(e, dict) and e.get("enabled", True) and e.get("entity_id"):
            eid = e["entity_id"]
            label = e.get("name") or eid.split(".")[-1].replace("_", " ")
            entities.append((eid, label))
        elif isinstance(e, str) and e:
            entities.append((e, e.split(".")[-1].replace("_", " ")))
    entity_ids = [eid for eid, _ in entities]
    label_map  = {eid: lbl for eid, lbl in entities}

    if not entity_ids or not config.HOME_ASSISTANT.get("enabled"):
        return

    now = datetime.now()
    if not (config.HP_START <= now.hour < config.HP_END):
        return

    try:
        tempo = get_tempo_info(config.HP_START, config.HP_END)
        today_color = tempo.get("today", {}).get("color", "UNKNOWN")
    except Exception as e:
        logger.error("Radiateurs tempo rouge : erreur récupération tempo : %s", e)
        return
    if today_color not in ("BLUE", "WHITE", "RED"):
        # Couleur inconnue (API indisponible) : ni extinction ni rallumage, on attend une couleur fiable
        logger.warning("Radiateurs tempo rouge : couleur du jour inconnue — aucune action")
        return

    from modules.ntfy_push import send as ntfy_send

    if today_color == "RED":
        turned_off = []
        for entity_id in entity_ids:
            state = ha_client.get_entity_state(config.HOME_ASSISTANT, entity_id)
            if state and state.get("state") not in ("off", "unavailable", "unknown"):
                if ha_client.turn_off_entity(config.HOME_ASSISTANT, entity_id):
                    turned_off.append(entity_id)
        if turned_off:
            _update_managed_off(add=turned_off)
            names = ", ".join(label_map.get(e, e) for e in turned_off)
            ntfy_send("🔴 Jour Rouge — Radiateurs éteints", f"Extinction automatique : {names}", config.NTFY)
            logger.info("Radiateurs éteints (jour rouge) : %s", turned_off)
    else:
        to_turn_on, postponed = radiateurs_module.split_turn_back_on(
            _load_managed_off(), entity_ids, cfg.get("weekend_restricted_entities", []),
            now, radiateurs_module.weekend_auto_allowed(),
        )
        if postponed:
            logger.info("Radiateurs : rallumage reporté (week-end, gestion auto non autorisée) : %s", postponed)
        turned_on = []
        for entity_id in to_turn_on:
            if ha_client.turn_on_entity(config.HOME_ASSISTANT, entity_id):
                turned_on.append(entity_id)
        if turned_on:
            _update_managed_off(remove=turned_on)
            names = ", ".join(label_map.get(e, e) for e in turned_on)
            ntfy_send("🟢 Radiateurs rallumés", f"Jour non-rouge : {names}", config.NTFY)
            logger.info("Radiateurs rallumés (jour non-rouge) : %s", turned_on)


def _reschedule_radiateurs():
    """(Re)planifie le job radiateurs Tempo Rouge selon la config courante."""
    try:
        _scheduler.remove_job("radiateurs_job")
    except Exception:
        pass
    if config.RADIATEURS_TEMPO_ROUGE.get("enabled"):
        _scheduler.add_job(
            _run_radiateurs_tempo_rouge,
            "interval",
            minutes=50,
            id="radiateurs_job",
            misfire_grace_time=120,
        )
        logger.info("Radiateurs Tempo Rouge planifiés toutes les 50 min")
    else:
        logger.info("Radiateurs Tempo Rouge désactivés — aucun job planifié")


def _run_electricity_fetch():
    """Synchronise les nouveaux emails 'flash élec' (appelé par le scheduler)."""
    result = electricity_module.fetch_new_readings(config.ELECTRICITY)
    logger.info("Électricité : fetch terminé — %s", result)


def _reschedule_electricity_fetch():
    """(Re)planifie le job de suivi électricité selon la config courante."""
    try:
        _scheduler.remove_job("electricity_job")
    except Exception:
        pass
    if config.ELECTRICITY.get("enabled") and imap_client.is_connected(config.ELECTRICITY):
        hours = int(config.ELECTRICITY.get("check_interval_hours", 24))
        _scheduler.add_job(
            _run_electricity_fetch,
            "interval",
            hours=hours,
            id="electricity_job",
            misfire_grace_time=600,
        )
        logger.info("Suivi électricité planifié toutes les %d h", hours)
    else:
        logger.info("Suivi électricité désactivé ou non connecté — aucun job planifié")


_reschedule_notify()
_reschedule_thermostat()
_reschedule_radiateurs()
_reschedule_electricity_fetch()

# ── Historique des températures ───────────────────────────
_scheduler.add_job(
    _record_history,
    "interval",
    minutes=10,
    id="history_job",
    misfire_grace_time=60,
)
_scheduler.add_job(
    _record_diagnose,
    "interval",
    minutes=10,
    id="diagnose_job",
    misfire_grace_time=60,
)
# Surveillance des traqueurs de présence figés (alerte ntfy)
_scheduler.add_job(
    lambda: presence_watch.check(config.HOME_ASSISTANT, config.THERMOSTAT, config.NTFY),
    "interval",
    minutes=30,
    id="presence_watch_job",
    misfire_grace_time=300,
)
# Surveillance des sources de température mesurée + rappel abonnement Météo-France (alerte ntfy)
_scheduler.add_job(
    lambda: source_watch.check(config.OBSERVATIONS, config.NTFY),
    "interval",
    hours=source_watch.CHECK_INTERVAL_HOURS,
    next_run_time=datetime.now() + timedelta(minutes=2),
    id="source_watch_job",
    misfire_grace_time=600,
)
# Purge hebdomadaire des données > 30 jours
_scheduler.add_job(
    lambda: history_module.purge_old(30),
    CronTrigger(day_of_week="mon", hour=3, minute=0, timezone="Europe/Paris"),
    id="history_purge_job",
    misfire_grace_time=300,
)
# Purge hebdomadaire des diagnostics > 7 jours
_scheduler.add_job(
    lambda: history_module.purge_diagnose_old(7),
    CronTrigger(day_of_week="mon", hour=3, minute=10, timezone="Europe/Paris"),
    id="diagnose_purge_job",
    misfire_grace_time=300,
)
# Agrégation mensuelle quotidienne à 2h30 (avant la purge)
def _run_monthly_aggregation():
    """Agrège le mois en cours et le mois précédent dans monthly_reports."""
    try:
        cfg = _build_config_dict()
        now = datetime.now()
        current_month = now.strftime("%Y-%m")
        history_module.aggregate_month(current_month, cfg)
        # Mois précédent (au cas où des données non purgées restent)
        prev = now.replace(day=1) - timedelta(days=1)
        prev_month = prev.strftime("%Y-%m")
        history_module.aggregate_month(prev_month, cfg)
    except Exception as e:
        logger.error("Agrégation mensuelle échouée : %s", e)

_scheduler.add_job(
    _run_monthly_aggregation,
    CronTrigger(hour=2, minute=30, timezone="Europe/Paris"),
    id="monthly_aggregation_job",
    misfire_grace_time=300,
)
logger.info("Historique : enregistrement toutes les 10 min, agrégation mensuelle à 2h30")


# Sauvegarde locale nocturne de data/ (rotation 7 jours) à 3h30, après agrégation et purges
def _run_backup():
    try:
        backup_module.run()
    except Exception as e:
        logger.error("Sauvegarde nocturne échouée : %s", e)

_scheduler.add_job(
    _run_backup,
    CronTrigger(hour=3, minute=30, timezone="Europe/Paris"),
    id="backup_job",
    misfire_grace_time=3600,
)


def _build_config_dict() -> dict:
    """Configuration du moteur de décision (partagée avec notify.py)."""
    return build_advisor_config(config)


def _indoor_with_felt() -> dict | None:
    """Climat intérieur depuis HA, enrichi de la température ressentie si activée."""
    indoor = ha_client.get_indoor_climate(config.HOME_ASSISTANT)
    if indoor and (config.THERMOSTAT.get("use_felt_temperature")
                   and indoor.get("temperature") is not None
                   and indoor.get("humidity") is not None):
        indoor["felt_temperature"] = thermostat_module.felt_temperature(
            indoor["temperature"], indoor["humidity"], config.THERMOSTAT
        )
    return indoor


def _fetch_external_data() -> dict:
    """Récupère météo, Tempo et prévisions (appels réseau, potentiellement lents)."""
    tomorrow_weather = get_tomorrow_weather({
        **config.LOCATION,
        "hp_start": config.HP_START,
        "hp_end": config.HP_END,
    })
    return {
        "fetched_at": datetime.now().isoformat(),
        "weather": get_current_temperature(config.LOCATION, config.OBSERVATIONS),
        "tomorrow_weather": tomorrow_weather,
        "tempo": get_tempo_info(config.HP_START, config.HP_END),
        "hourly_forecast": get_hourly_forecast(config.LOCATION["latitude"], config.LOCATION["longitude"], hours=48),
        "indoor": _indoor_with_felt(),
    }


# Cache des données externes uniquement : la décision est recalculée à chaque lecture.
# Périmé après REFRESH_INTERVAL_MINUTES, à chaque bascule HP/HC et au changement de jour.
_data_cache = DataCache(
    fetch=_fetch_external_data,
    ttl=lambda: config.REFRESH_INTERVAL_MINUTES * 60,
    slot=lambda: tariff_slot(config.HP_START, config.HP_END),
)


def get_analysis(force_refresh: bool = False) -> dict:
    """Retourne l'analyse courante.

    Les données externes viennent du cache (stale-while-revalidate : un résultat périmé est
    servi pendant que le rafraîchissement part en arrière-plan). La décision est recalculée
    à chaque appel avec l'heure courante : période HP/HC et tarif toujours ceux en vigueur
    (05:50 → HC, 06:00 → HP), même si les données datent d'avant la bascule.
    """
    just_fetched = force_refresh or _data_cache.data is None
    raw = _data_cache.get(force=force_refresh)
    if not just_fetched:
        # Température intérieure toujours à jour (HA local, rapide)
        indoor = _indoor_with_felt()
        if indoor:
            raw["indoor"] = indoor
    return compute_analysis(raw, _build_config_dict())


@app.route("/")
def index():
    try:
        data = get_analysis()
        thermostat_state = thermostat_module.get_state()
        if config.THERMOSTAT.get("presence_enabled"):
            nearby_zone = config.THERMOSTAT.get("nearby_zone_name", "")
            if nearby_zone:
                presence = ha_client.get_presence_extended(config.HOME_ASSISTANT, config.THERMOSTAT.get("person_entities", []), nearby_zone)
            else:
                raw = ha_client.get_presence(config.HOME_ASSISTANT, config.THERMOSTAT.get("person_entities", []))
                presence = "home" if raw else "away" if raw is False else None
            thermostat_state["everyone_away"] = presence == "away"
            thermostat_state["presence_status"] = presence
        else:
            thermostat_state["everyone_away"] = False
            thermostat_state["presence_status"] = None
        next_start = thermostat_module.next_schedule_start(config.THERMOSTAT) if config.THERMOSTAT.get("enabled") else None

        # État réel du poêle depuis Home Assistant
        poele_state_value = None
        if ha_client.is_configured(config.HOME_ASSISTANT):
            poele_ha = ha_client.get_state(config.HOME_ASSISTANT)
            if poele_ha:
                poele_state_value = poele_ha.get("state")

        # Statut radiateurs pour l'affichage index avec état réel depuis HA
        managed_off = _load_managed_off()
        radiateurs_info = []
        if config.RADIATEURS_TEMPO_ROUGE.get("enabled"):
            for e in config.RADIATEURS_TEMPO_ROUGE.get("entities", []):
                entity_id = e["entity_id"] if isinstance(e, dict) else e
                name = (e.get("name") or entity_id.split(".")[-1].replace("_", " ")) if isinstance(e, dict) else entity_id.split(".")[-1].replace("_", " ")
                enabled = e.get("enabled", True) if isinstance(e, dict) else True
                is_managed_off = entity_id in managed_off

                # Récupérer l'état réel depuis Home Assistant
                state = None
                if config.HOME_ASSISTANT.get("enabled"):
                    ha_state = ha_client.get_entity_state(config.HOME_ASSISTANT, entity_id)
                    if ha_state:
                        state = ha_state.get("state", "unknown")

                radiateurs_info.append({
                    "entity_id": entity_id,
                    "name": name,
                    "enabled": enabled,
                    "managed_off": is_managed_off,
                    "state": state,
                    "weekend_restricted": entity_id in config.RADIATEURS_TEMPO_ROUGE.get("weekend_restricted_entities", []),
                })
        return render_template("index.html", data=data, config=config, thermostat_state=thermostat_state,
                               next_schedule_start=next_start, radiateurs_info=radiateurs_info,
                               weekend_auto_allowed=radiateurs_module.weekend_auto_allowed(),
                               poele_state=poele_state_value, ajax_interval=config.AJAX_REFRESH_INTERVAL,
                               next_check=_next_thermostat_check(),
                               pellet=_pellet_status())
    except Exception as e:
        error = _internal_error_message(_log_internal_error(e))
        return render_template("index.html", data=None, error=error, config=config, thermostat_state={},
                               next_schedule_start=None, radiateurs_info=[], poele_state=None,
                               ajax_interval=config.AJAX_REFRESH_INTERVAL)


@app.route("/api/data")
def api_data():
    try:
        data = get_analysis()
        return jsonify(data)
    except Exception as e:
        return _internal_error(e)


@app.route("/api/refresh")
def api_refresh():
    try:
        data = get_analysis(force_refresh=True)
        return jsonify({"status": "ok", "timestamp": data["timestamp"]})
    except Exception as e:
        return _internal_error(e)


@app.route("/api/dashboard/refresh")
def api_dashboard_refresh():
    """Endpoint AJAX pour rafraîchir uniquement les données du dashboard sans recharger la page."""
    try:
        # Récupérer les données cachées (météo, Tempo)
        data = get_analysis()

        # Vérifier que les données sont valides
        if data is None:
            return jsonify({"error": "Impossible de récupérer les données"}), 500

        # Récupérer l'état du thermostat
        thermostat_state = thermostat_module.get_state()
        if config.THERMOSTAT.get("presence_enabled"):
            nearby_zone = config.THERMOSTAT.get("nearby_zone_name", "")
            if nearby_zone:
                presence = ha_client.get_presence_extended(
                    config.HOME_ASSISTANT,
                    config.THERMOSTAT.get("person_entities", []),
                    nearby_zone
                )
            else:
                raw = ha_client.get_presence(
                    config.HOME_ASSISTANT,
                    config.THERMOSTAT.get("person_entities", [])
                )
                presence = "home" if raw else "away" if raw is False else None
            thermostat_state["everyone_away"] = presence == "away"
            thermostat_state["presence_status"] = presence
        else:
            thermostat_state["everyone_away"] = False
            thermostat_state["presence_status"] = None

        # Récupérer les radiateurs
        managed_off = _load_managed_off()
        radiateurs_info = []
        if config.RADIATEURS_TEMPO_ROUGE.get("enabled"):
            for e in config.RADIATEURS_TEMPO_ROUGE.get("entities", []):
                entity_id = e["entity_id"] if isinstance(e, dict) else e
                name = (e.get("name") or entity_id.split(".")[-1].replace("_", " ")) if isinstance(e, dict) else entity_id.split(".")[-1].replace("_", " ")
                enabled = e.get("enabled", True) if isinstance(e, dict) else True
                is_managed_off = entity_id in managed_off

                # État réel depuis Home Assistant
                state = None
                if config.HOME_ASSISTANT.get("enabled"):
                    ha_state = ha_client.get_entity_state(config.HOME_ASSISTANT, entity_id)
                    if ha_state:
                        state = ha_state.get("state", "unknown")

                radiateurs_info.append({
                    "entity_id": entity_id,
                    "name": name,
                    "enabled": enabled,
                    "managed_off": is_managed_off,
                    "state": state,
                    "weekend_restricted": entity_id in config.RADIATEURS_TEMPO_ROUGE.get("weekend_restricted_entities", []),
                })

        # État clim
        clim_state_value = None
        if ha_client.is_clim_configured(config.HOME_ASSISTANT):
            clim_ha = ha_client.get_clim_state(config.HOME_ASSISTANT)
            if clim_ha:
                clim_state_value = clim_ha.get("state")

        # État poêle
        poele_state_value = None
        if ha_client.is_configured(config.HOME_ASSISTANT):
            poele_ha = ha_client.get_state(config.HOME_ASSISTANT)
            if poele_ha:
                poele_state_value = poele_ha.get("state")

        # Construire la réponse JSON
        # Utiliser 'or {}' pour gérer le cas où les valeurs sont None au lieu de dict
        rec = data.get("recommendation") or {}
        weather = data.get("weather") or {}
        tempo = data.get("tempo") or {}
        tempo_today = tempo.get("today") or {}
        tempo_tomorrow = tempo.get("tomorrow") or {}
        tomorrow = data.get("tomorrow") or {}
        tomorrow_rec = tomorrow.get("recommendation") or {}

        response = {
            "timestamp": data.get("timestamp", ""),
            "indoor": data.get("indoor", {}),
            "outdoor": {
                "temperature": weather.get("temperature"),
                "source": weather.get("source"),
                "measured": weather.get("measured", False),
                "observed_at": weather.get("observed_at"),
            },
            "recommendation": {
                "system": rec.get("system"),
                "title": rec.get("title", ""),
                "explanation": rec.get("explanation", ""),
                "level": rec.get("level", "info"),
                "savings_per_hour": rec.get("savings_per_hour")
            },
            "tempo": {
                "current_period": tempo.get("current_period"),
                "today": {
                    "color": tempo_today.get("color"),
                    "label": tempo_today.get("label", ""),
                    "emoji": tempo_today.get("emoji", "")
                },
                "tomorrow": {
                    "color": tempo_tomorrow.get("color"),
                    "label": tempo_tomorrow.get("label", ""),
                    "emoji": tempo_tomorrow.get("emoji", "")
                }
            },
            "thermostat": {
                "state": thermostat_state.get("state"),
                "active_system": thermostat_state.get("active_system"),
                "enabled": thermostat_state.get("enabled", False),
                "in_schedule": thermostat_state.get("in_schedule", False),
                "suspended_until": thermostat_state.get("suspended_until"),
                "everyone_away": thermostat_state.get("everyone_away", False),
                "presence_status": thermostat_state.get("presence_status"),
                "last_turned_on": thermostat_state.get("last_turned_on"),
                "system_history": thermostat_state.get("system_history", {
                    "poele": {"last_turned_on": None, "last_turned_off": None},
                    "clim": {"last_turned_on": None, "last_turned_off": None}
                })
            },
            "clim_state": clim_state_value,
            "poele_state": poele_state_value,
            "pellet_refill": _pellet_status(),
            "radiateurs": radiateurs_info,
            "tomorrow": {
                "recommendation": {
                    "system": tomorrow_rec.get("system"),
                    "title": tomorrow_rec.get("title", ""),
                    "explanation": tomorrow_rec.get("explanation", ""),
                    "level": tomorrow_rec.get("level", "info")
                },
                "tempo_unknown": tomorrow.get("tempo_unknown", False)
            }
        }

        return jsonify(response)
    except Exception as e:
        return _internal_error(e)


@app.route("/api/poele/refill-reset", methods=["POST"])
def api_poele_refill_reset():
    """Clic « Trémie remplie » : remise à 0 et enregistrement du cycle (sacs versés)."""
    data = request.get_json(silent=True) or {}
    try:
        bags = float(data.get("bags", 1))
    except (TypeError, ValueError):
        return jsonify({"error": "Nombre de sacs invalide"}), 400
    if not 0 < bags <= 10:
        return jsonify({"error": "Nombre de sacs invalide"}), 400
    try:
        pellet_refill.reset("manual", kg=bags * _pellet_bag_kg())
        return jsonify(_pellet_status())
    except Exception as e:
        return _internal_error(e)


@app.route("/api/poele/refill-apply", methods=["POST"])
def api_poele_refill_apply():
    """Applique la durée par sac mesurée : heures par sac (/config), débit du poêle, stock, alerte."""
    try:
        bag_kg = _pellet_bag_kg()
        est = pellet_refill.status(bag_kg=bag_kg)["estimate"]
        if not est:
            return jsonify({"error": "Pas assez de remplissages mesurés"}), 400
        hours = est["hours_per_bag"]
        consumption = round(bag_kg / hours, 4)

        def _apply(d):
            d.setdefault("_poele_purchase", {})["hours_per_bag"] = hours
            d.setdefault("POELE", {})["consumption_kg_per_hour"] = consumption
        _patch_override(_apply)
        config.POELE["consumption_kg_per_hour"] = consumption
        logger.info("Poêle : durée par sac mesurée appliquée — %.1f h (%.3f kg/h)", hours, consumption)
        return jsonify(_pellet_status())
    except Exception as e:
        return _internal_error(e)


@app.route("/api/notify-test", methods=["POST"])
def api_notify_test():
    try:
        success = _run_notify()
        if success:
            return jsonify({"status": "ok"})
        return jsonify({"error": "Envoi échoué — vérifiez les logs et la configuration SMTP"}), 500
    except Exception as e:
        return _internal_error(e)


@app.route("/api/radiateurs/status")
def api_radiateurs_status():
    cfg = config.RADIATEURS_TEMPO_ROUGE
    managed_off = _load_managed_off()
    entities_state = []
    if cfg.get("enabled") and config.HOME_ASSISTANT.get("enabled"):
        for e in cfg.get("entities", []):
            entity_id = e["entity_id"] if isinstance(e, dict) else e
            enabled = e.get("enabled", True) if isinstance(e, dict) else True
            state = ha_client.get_entity_state(config.HOME_ASSISTANT, entity_id)
            entities_state.append({
                "entity_id": entity_id,
                "enabled": enabled,
                "state": state.get("state") if state else "unavailable",
                "managed_off": entity_id in managed_off,
            })
    return jsonify({
        "enabled": cfg.get("enabled", False),
        "entities": entities_state,
        "managed_off": managed_off,
    })


@app.route("/api/radiateurs/turn_on/<path:entity_id>", methods=["POST"])
def api_radiateurs_turn_on(entity_id):
    if not config.HOME_ASSISTANT.get("enabled"):
        return jsonify({"error": "Home Assistant non configuré"}), 400

    # Vérifier que entity_id est configuré
    cfg = config.RADIATEURS_TEMPO_ROUGE
    configured = []
    for e in cfg.get("entities", []):
        eid = e["entity_id"] if isinstance(e, dict) else e
        configured.append(eid)

    if entity_id not in configured:
        return jsonify({"error": "Radiateur non configuré"}), 404

    # Retirer de managed_off si présent
    _update_managed_off(remove=[entity_id])

    success = ha_client.turn_on_entity(config.HOME_ASSISTANT, entity_id)
    return jsonify({"status": "ok" if success else "error"})


@app.route("/api/radiateurs/turn_off/<path:entity_id>", methods=["POST"])
def api_radiateurs_turn_off(entity_id):
    if not config.HOME_ASSISTANT.get("enabled"):
        return jsonify({"error": "Home Assistant non configuré"}), 400

    # Vérifier que entity_id est configuré
    cfg = config.RADIATEURS_TEMPO_ROUGE
    configured = []
    for e in cfg.get("entities", []):
        eid = e["entity_id"] if isinstance(e, dict) else e
        configured.append(eid)

    if entity_id not in configured:
        return jsonify({"error": "Radiateur non configuré"}), 404

    # Extinction manuelle : le radiateur sort de la liste « éteints par la règle jour rouge »,
    # sinon le job radiateurs le rallumerait au prochain passage d'un jour non rouge.
    _update_managed_off(remove=[entity_id])

    success = ha_client.turn_off_entity(config.HOME_ASSISTANT, entity_id)
    return jsonify({"status": "ok" if success else "error"})


@app.route("/api/radiateurs/weekend", methods=["POST"])
def api_radiateurs_weekend():
    """Autorise ou non la gestion automatique le week-end des radiateurs restreints (combles)."""
    allowed = bool(request.get_json(force=True).get("allowed", False))
    radiateurs_module.set_weekend_auto_allowed(allowed)
    logger.info("Radiateurs : gestion automatique le week-end → %s", "autorisée" if allowed else "interdite")
    return jsonify({"status": "ok", "allowed": allowed})


@app.route("/api/ntfy-test", methods=["POST"])
def api_ntfy_test():
    from modules.ntfy_push import send as ntfy_send
    try:
        ntfy_cfg = config.NTFY
        if not ntfy_cfg.get("enabled"):
            return jsonify({"error": "Notifications ntfy désactivées"}), 400
        ntfy_send("🔔 Heating Advisor", "Notification de test — configuration OK !", ntfy_cfg)
        return jsonify({"status": "ok"})
    except Exception as e:
        return _internal_error(e)


@app.route("/api/ha/turn_on", methods=["POST"])
def api_ha_turn_on():
    if not ha_client.is_configured(config.HOME_ASSISTANT):
        return jsonify({"error": "Home Assistant non configuré"}), 400

    success = ha_client.turn_on(config.HOME_ASSISTANT)

    # Si action manuelle, annuler suspension thermostat
    if success and request.args.get("manual") == "true":
        _cancel_thermostat_suspension("poele")

    return jsonify({"status": "ok" if success else "error"})


@app.route("/api/ha/turn_off", methods=["POST"])
def api_ha_turn_off():
    if not ha_client.is_configured(config.HOME_ASSISTANT):
        return jsonify({"error": "Home Assistant non configuré"}), 400

    success = ha_client.turn_off(config.HOME_ASSISTANT)

    # Si action manuelle, suspendre thermostat 4h
    if success and request.args.get("manual") == "true":
        _suspend_thermostat_after_manual_off("poele")

    return jsonify({"status": "ok" if success else "error"})


@app.route("/api/ha/auto_control", methods=["POST"])
def api_ha_auto_control():
    """Active ou désactive le pilotage automatique sans toucher au reste de la config."""
    data = request.get_json(force=True)
    enabled = bool(data.get("enabled", False))
    config.HOME_ASSISTANT["auto_control"] = enabled
    try:
        _patch_override(lambda d: d.setdefault("HOME_ASSISTANT", {}).__setitem__("auto_control", enabled))
    except Exception as e:
        logger.error("Sauvegarde auto_control échouée : %s", e)
    logger.info("HA auto_control → %s", enabled)
    return jsonify({"status": "ok", "auto_control": enabled})


@app.route("/api/ha/clim/turn_on", methods=["POST"])
def api_ha_clim_turn_on():
    if not ha_client.is_clim_configured(config.HOME_ASSISTANT):
        return jsonify({"error": "Clim non configurée"}), 400
    target_temp = config.THERMOSTAT.get("temp_off", 22.9)
    success = ha_client.turn_on_clim(config.HOME_ASSISTANT, target_temp)
    if success:
        thermostat_module.mark_clim_setpoint_managed()

    # Si action manuelle, annuler suspension thermostat
    if success and request.args.get("manual") == "true":
        _cancel_thermostat_suspension("clim")

    return jsonify({"status": "ok" if success else "error"})


@app.route("/api/ha/clim/turn_off", methods=["POST"])
def api_ha_clim_turn_off():
    if not ha_client.is_clim_configured(config.HOME_ASSISTANT):
        return jsonify({"error": "Clim non configurée"}), 400
    success = ha_client.turn_off_clim(config.HOME_ASSISTANT)

    # Si action manuelle, suspendre thermostat 4h
    if success and request.args.get("manual") == "true":
        _suspend_thermostat_after_manual_off("clim")

    return jsonify({"status": "ok" if success else "error"})


@app.route("/api/ha/clim/state")
def api_ha_clim_state():
    if not ha_client.is_clim_configured(config.HOME_ASSISTANT):
        return jsonify({"error": "Clim non configurée"}), 400
    state = ha_client.get_clim_state(config.HOME_ASSISTANT)
    if state is None:
        return jsonify({"error": "Impossible de récupérer l'état"}), 500
    return jsonify(state)


@app.route("/api/ha/state")
def api_ha_state():
    if not ha_client.is_configured(config.HOME_ASSISTANT):
        return jsonify({"error": "Home Assistant non configuré"}), 400
    state = ha_client.get_state(config.HOME_ASSISTANT)
    if state is None:
        return jsonify({"error": "Impossible de récupérer l'état"}), 500
    return jsonify(state)


@app.route("/api/thermostat/diagnose")
def api_thermostat_diagnose():
    from modules.thermostat import get_state, is_in_schedule
    from zoneinfo import ZoneInfo
    state = get_state()
    indoor = ha_client.get_indoor_climate(config.HOME_ASSISTANT)
    ha_state = ha_client.get_state(config.HOME_ASSISTANT)
    job = _scheduler.get_job("thermostat_job")
    next_run = None
    if job and job.next_run_time:
        next_run = job.next_run_time.astimezone(ZoneInfo("Europe/Paris")).strftime("%H:%M:%S")
    clim_ha_state = ha_client.get_clim_state(config.HOME_ASSISTANT)
    return jsonify({
        "thermostat_enabled": config.THERMOSTAT.get("enabled", False),
        "in_schedule": is_in_schedule(config.THERMOSTAT),
        "indoor": indoor,
        "poele_real_state": ha_state.get("state") if ha_state else None,
        "clim_real_state": clim_ha_state.get("state") if clim_ha_state else None,
        "clim_configured": ha_client.is_clim_configured(config.HOME_ASSISTANT),
        "active_system": state.get("active_system"),
        "thermostat_state": state,
        "temp_on": config.THERMOSTAT.get("temp_on"),
        "temp_off": config.THERMOSTAT.get("temp_off"),
        "min_on_minutes": config.THERMOSTAT.get("min_on_minutes"),
        "min_on_minutes_clim": config.THERMOSTAT.get("min_on_minutes_clim", 15),
        "grace_minutes": config.THERMOSTAT.get("end_of_schedule_grace_minutes"),
        "use_felt_temperature": config.THERMOSTAT.get("use_felt_temperature"),
        "felt_temperature": thermostat_module.felt_temperature(
            indoor["temperature"], indoor.get("humidity"), config.THERMOSTAT
        ) if indoor and indoor.get("temperature") is not None else None,
        "next_check": next_run,
        "suspended_until": state.get("suspended_until"),
        "presence_enabled": config.THERMOSTAT.get("presence_enabled", False),
        "nearby_zone_name": config.THERMOSTAT.get("nearby_zone_name", ""),
        "nearby_no_ignition_after": config.THERMOSTAT.get("nearby_no_ignition_after", 20),
        "nearby_grace_minutes": config.THERMOSTAT.get("nearby_grace_minutes", 20),
        "presence_status": (
            ha_client.get_presence_extended(config.HOME_ASSISTANT, config.THERMOSTAT.get("person_entities", []), config.THERMOSTAT.get("nearby_zone_name", ""))
            if config.THERMOSTAT.get("presence_enabled") and config.THERMOSTAT.get("nearby_zone_name")
            else ("away" if ha_client.get_presence(config.HOME_ASSISTANT, config.THERMOSTAT.get("person_entities", [])) is False else "home")
            if config.THERMOSTAT.get("presence_enabled")
            else None
        ),
        "everyone_away": (
            ha_client.get_presence_extended(config.HOME_ASSISTANT, config.THERMOSTAT.get("person_entities", []), config.THERMOSTAT.get("nearby_zone_name", "")) == "away"
            if config.THERMOSTAT.get("presence_enabled") and config.THERMOSTAT.get("nearby_zone_name")
            else ha_client.get_presence(config.HOME_ASSISTANT, config.THERMOSTAT.get("person_entities", [])) is False
            if config.THERMOSTAT.get("presence_enabled")
            else None
        ),
    })


def _next_thermostat_check() -> str | None:
    """Heure ISO (avec fuseau) du prochain passage du thermostat, ou None s'il n'est pas planifié."""
    job = _scheduler.get_job("thermostat_job")
    if not job or not job.next_run_time:
        return None
    return job.next_run_time.isoformat()


@app.route("/api/thermostat/state")
def api_thermostat_state():
    state = thermostat_module.get_state()
    return jsonify({
        **state,
        "next_check": _next_thermostat_check(),
        "enabled": config.THERMOSTAT.get("enabled", False),
        "in_schedule": thermostat_module.is_in_schedule(config.THERMOSTAT),
        "temp_on": config.THERMOSTAT.get("temp_on"),
        "temp_off": config.THERMOSTAT.get("temp_off"),
    })


@app.route("/api/thermostat/check-now", methods=["POST"])
def api_thermostat_check_now():
    """Avance le prochain passage du thermostat à maintenant (bouton « Évaluer maintenant »)."""
    if not _scheduler.get_job("thermostat_job"):
        return jsonify({"error": "Thermostat non planifié"}), 409
    _scheduler.modify_job("thermostat_job", next_run_time=datetime.now(_scheduler.timezone))
    logger.info("Thermostat : évaluation forcée manuellement")
    return jsonify({"status": "ok"})


@app.route("/api/thermostat/vacation", methods=["GET"])
def api_thermostat_vacation_get():
    vac = thermostat_module.get_vacation()
    return jsonify({**vac, "active": thermostat_module.is_on_vacation()})


@app.route("/api/thermostat/vacation", methods=["POST"])
def api_thermostat_vacation_set():
    data = request.get_json(force=True)
    start = str(data.get("start", "")).strip()
    end = str(data.get("end", "")).strip()
    try:
        s = datetime.fromisoformat(start)
        e = datetime.fromisoformat(end)
        if s >= e:
            return jsonify({"error": "Le départ doit être avant le retour"}), 400
    except Exception:
        return jsonify({"error": "Dates invalides (format YYYY-MM-DDTHH:MM attendu)"}), 400
    thermostat_module.set_vacation(start, end)
    logger.info("Mode vacances activé : %s → %s", start, end)
    return jsonify({"status": "ok"})


@app.route("/api/thermostat/vacation", methods=["DELETE"])
def api_thermostat_vacation_clear():
    thermostat_module.clear_vacation()
    logger.info("Mode vacances annulé")
    return jsonify({"status": "ok"})


@app.route("/api/thermostat/absent", methods=["POST"])
def api_thermostat_absent_set():
    thermostat_module.set_absent()
    logger.info("Mode absence immédiate activé")
    return jsonify({"status": "ok"})


@app.route("/api/thermostat/absent", methods=["DELETE"])
def api_thermostat_absent_clear():
    thermostat_module.clear_absent()
    logger.info("Mode absence immédiate désactivé")
    return jsonify({"status": "ok"})


VALID_DAY_KEYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}


@app.route("/api/thermostat/absence-schedules", methods=["GET"])
def api_absence_schedules_get():
    schedules = thermostat_module.get_absence_schedules()
    active = thermostat_module.is_in_absence_schedule()
    return jsonify({"schedules": schedules, "active": active})


@app.route("/api/thermostat/absence-schedules", methods=["POST"])
def api_absence_schedules_add():
    data = request.get_json(force=True)
    days = data.get("days", [])
    start = str(data.get("start", "")).strip()
    end = str(data.get("end", "")).strip()
    if not days or not isinstance(days, list):
        return jsonify({"error": "Sélectionnez au moins un jour"}), 400
    invalid = [d for d in days if d not in VALID_DAY_KEYS]
    if invalid:
        return jsonify({"error": f"Jours invalides : {invalid}"}), 400
    import re
    hhmm = re.compile(r"^\d{2}:\d{2}$")
    if not hhmm.match(start) or not hhmm.match(end):
        return jsonify({"error": "Horaires invalides (format HH:MM attendu)"}), 400
    try:
        sh, sm = map(int, start.split(":"))
        eh, em = map(int, end.split(":"))
        if not (0 <= sh <= 23 and 0 <= sm <= 59 and 0 <= eh <= 23 and 0 <= em <= 59):
            raise ValueError()
        if sh * 60 + sm >= eh * 60 + em:
            return jsonify({"error": "L'heure de début doit être avant l'heure de fin"}), 400
    except (ValueError, AttributeError):
        return jsonify({"error": "Horaires invalides (format HH:MM attendu)"}), 400
    thermostat_module.add_absence_schedule(days, start, end)
    logger.info("Horaire d'absence ajouté : %s %s–%s", days, start, end)
    return jsonify({"status": "ok"})


@app.route("/api/thermostat/absence-schedules/<int:idx>", methods=["DELETE"])
def api_absence_schedules_remove(idx):
    schedules = thermostat_module.get_absence_schedules()
    if idx < 0 or idx >= len(schedules):
        return jsonify({"error": "Index invalide"}), 400
    thermostat_module.remove_absence_schedule(idx)
    logger.info("Horaire d'absence supprimé (index %d)", idx)
    return jsonify({"status": "ok"})


@app.route("/api/thermostat/resume", methods=["POST"])
def api_thermostat_resume():
    """Annule la suspension du thermostat."""
    state = thermostat_module.get_state()
    state["suspended_until"] = None
    from modules.thermostat import _save_state
    _save_state(state)
    logger.info("Thermostat : suspension annulée manuellement")
    # Contrôle immédiat plutôt qu'au prochain passage (jusqu'à 10 min)
    try:
        _scheduler.modify_job("thermostat_job", next_run_time=datetime.now(_scheduler.timezone))
    except Exception:
        pass
    return jsonify({"status": "ok"})


@app.route("/api/thermostat/thresholds", methods=["POST"])
def api_thermostat_thresholds():
    """Modifie les seuils d'allumage/extinction du thermostat (widget page principale)."""
    data = request.get_json(force=True)
    try:
        temp_on, temp_off = thermostat_module.validate_thresholds(data.get("temp_on"), data.get("temp_off"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    config.THERMOSTAT["temp_on"] = temp_on
    config.THERMOSTAT["temp_off"] = temp_off
    try:
        _patch_override(lambda d: d.setdefault("THERMOSTAT", {}).update({"temp_on": temp_on, "temp_off": temp_off}))
    except Exception as e:
        logger.error("Sauvegarde seuils thermostat échouée : %s", e)
        return jsonify({"error": "Sauvegarde impossible"}), 500
    logger.info("Thermostat : seuils → allumage < %.1f°C, extinction ≥ %.1f°C", temp_on, temp_off)
    clim_setpoint = thermostat_module.sync_clim_setpoint(ha_client, config.HOME_ASSISTANT, temp_off)
    return jsonify({"status": "ok", "temp_on": temp_on, "temp_off": temp_off, "clim_setpoint": clim_setpoint})


@app.route("/api/thermostat/toggle", methods=["POST"])
def api_thermostat_toggle():
    data = request.get_json(force=True)
    enabled = bool(data.get("enabled", False))
    config.THERMOSTAT["enabled"] = enabled
    try:
        _patch_override(lambda d: d.setdefault("THERMOSTAT", {}).__setitem__("enabled", enabled))
    except Exception as e:
        logger.error("Sauvegarde thermostat enabled échouée : %s", e)
    _reschedule_thermostat()
    logger.info("Thermostat → %s", "activé" if enabled else "désactivé")
    return jsonify({"status": "ok", "enabled": enabled})


@app.route("/absence")
def absence_page():
    vacation = thermostat_module.get_vacation()
    on_vacation = thermostat_module.is_on_vacation()
    absent = thermostat_module.is_absent()
    absence_schedules = thermostat_module.get_absence_schedules()
    in_absence_schedule = thermostat_module.is_in_absence_schedule()

    def _fmt_vacation_dt(val):
        """Formate 'YYYY-MM-DDTHH:MM' → 'DD/MM/YYYY à HHhMM', ou date seule si pas d'heure."""
        if not val:
            return val
        try:
            dt = datetime.fromisoformat(val)
            if "T" in val:
                return dt.strftime("%d/%m/%Y à %Hh%M")
            return dt.strftime("%d/%m/%Y")
        except Exception:
            return val

    vacation_display = {
        "start": _fmt_vacation_dt(vacation.get("start")),
        "end":   _fmt_vacation_dt(vacation.get("end")),
    }

    return render_template(
        "absence.html",
        config=config,
        vacation=vacation_display,
        on_vacation=on_vacation,
        absent=absent,
        absence_schedules=absence_schedules,
        in_absence_schedule=in_absence_schedule,
    )


@app.route("/statistics")
def statistics_page():
    return render_template("statistics.html", config=config)


@app.route("/api/weather/conditions")
def api_weather_conditions():
    """État du ciel pour l'illustration de la carte météo (Open-Meteo, cache 15 min)."""
    data = get_current_conditions(config.LOCATION["latitude"], config.LOCATION["longitude"])
    if data is None:
        return jsonify({"error": "Conditions météo indisponibles"}), 503
    return jsonify(data)


@app.route("/api/statistics")
def api_statistics():
    try:
        hours = int(request.args.get("hours", 24))
        hours = min(max(hours, 1), 168)  # entre 1h et 7 jours
        data = history_module.get_history(hours)
        return jsonify(data)
    except Exception as e:
        return _internal_error(e)


@app.route("/api/statistics/daily")
def api_statistics_daily():
    try:
        days = int(request.args.get("days", 30))
        days = min(max(days, 1), 30)
        data = history_module.get_daily_summary(days)
        return jsonify(data)
    except Exception as e:
        return _internal_error(e)


@app.route("/reports")
def reports_page():
    return render_template("reports.html", config=config)


@app.route("/api/reports/monthly")
def api_reports_monthly():
    try:
        months = int(request.args.get("months", 24))
        months = min(max(months, 1), 60)
        data = history_module.get_monthly_reports(months)
        return jsonify(data)
    except Exception as e:
        return _internal_error(e)


@app.route("/api/reports/aggregate-now", methods=["POST"])
def api_reports_aggregate_now():
    """Déclenche manuellement l'agrégation du mois en cours et du mois précédent."""
    try:
        _run_monthly_aggregation()
        return jsonify({"status": "ok"})
    except Exception as e:
        return _internal_error(e)


@app.route("/api/thermostat/diagnose/history")
def api_diagnose_history():
    try:
        hours = int(request.args.get("hours", 24))
        hours = min(max(hours, 1), 168)
        data = history_module.get_diagnose_history(hours)
        return jsonify(data)
    except Exception as e:
        return _internal_error(e)


@app.route("/electricity")
def electricity_page():
    return render_template("electricity.html", config=config)


@app.route("/api/electricity/history")
def api_electricity_history():
    try:
        weeks_param = request.args.get("weeks", "52")
        if weeks_param == "all":
            data = electricity_module.get_readings(weeks=None)
        else:
            weeks = min(max(int(weeks_param), 1), 260)
            data = electricity_module.get_readings(weeks=weeks)
        return jsonify(data)
    except Exception as e:
        return _internal_error(e)


@app.route("/api/electricity/status")
def api_electricity_status():
    return jsonify({
        "enabled": config.ELECTRICITY.get("enabled", False),
        "connected": imap_client.is_connected(config.ELECTRICITY),
        "latest": electricity_module.get_latest_reading(),
    })


@app.route("/api/electricity/sync-now", methods=["POST"])
def api_electricity_sync_now():
    if not config.ELECTRICITY.get("enabled"):
        return jsonify({"error": "Suivi électricité désactivé"}), 400
    try:
        result = electricity_module.fetch_new_readings(config.ELECTRICITY)
        return jsonify(result)
    except Exception as e:
        return _internal_error(e)


@app.route("/api/electricity/import-log")
def api_electricity_import_log():
    try:
        limit = int(request.args.get("limit", 50))
        limit = min(max(limit, 1), 200)
        data = electricity_module.get_import_log(limit=limit)
        return jsonify(data)
    except Exception as e:
        return _internal_error(e)


@app.route("/api/electricity/imap/test", methods=["POST"])
def api_electricity_imap_test():
    """Teste la connexion IMAP avec les identifiants actuellement enregistrés."""
    try:
        ok, message = imap_client.test_connection(config.ELECTRICITY)
        return jsonify({"status": "ok" if ok else "error", "message": message})
    except Exception as e:
        error_id = _log_internal_error(e)
        return jsonify({"status": "error", "message": _internal_error_message(error_id), "error_id": error_id}), 500


@app.route("/docs")
def docs_page():
    return render_template("docs.html", config=config)


@app.route("/config")
def config_page():
    override = jsonstore.load(OVERRIDE_FILE, {})
    purchase = override.get("_poele_purchase", {}) if isinstance(override, dict) else {}
    return render_template("config.html", config=config, purchase=purchase)


def _secret_rebind_error(data: dict) -> str | None:
    """Refuse de réutiliser un secret enregistré pour un hôte qui a changé.

    Un champ secret laissé vide conserve le secret existant : si l'hôte change en même temps,
    ce secret partirait vers le nouvel hôte. On exige alors qu'il soit ressaisi.
    """
    def _norm(value) -> str:
        return str(value or "").strip().rstrip("/").lower()

    services = (
        ("ha_url", config.HOME_ASSISTANT, "url", "ha_token", "token",
         "L'URL Home Assistant a changé : ressaisissez le token d'accès"),
        ("ntfy_url", config.NTFY, "url", "ntfy_token", "token",
         "L'URL du serveur ntfy a changé : ressaisissez le token d'accès"),
        ("smtp_host", config.EMAIL, "smtp_host", "app_password", "app_password",
         "Le serveur SMTP a changé : ressaisissez le mot de passe d'application"),
        ("imap_host", config.ELECTRICITY, "imap_host", "imap_password", "imap_password",
         "Le serveur IMAP a changé : ressaisissez le mot de passe IMAP"),
    )
    for host_field, section, host_key, secret_field, secret_key, message in services:
        if host_field not in data or not section.get(secret_key):
            continue
        if str(data.get(secret_field, "")).strip():
            continue
        if _norm(data[host_field]) != _norm(section.get(host_key)):
            return f"{message} (le secret enregistré n'est jamais envoyé à un nouveau serveur)"
    return None


@app.route("/api/config", methods=["POST"])
def api_config_save():
    data = request.get_json(force=True)
    rebind_error = _secret_rebind_error(data)
    if rebind_error:
        return jsonify({"error": rebind_error}), 400
    try:
        nb_sacs = float(data.get("nb_sacs", 1))
        prix = float(data.get("prix_livraison", 0))
        poids = float(data.get("poids_sac", 1))
        hours_per_bag = float(data.get("hours_per_bag", 15))
        price_per_kg = round(prix / max(nb_sacs * poids, 0.001), 6)
        consumption_kg_per_hour = round(poids / max(hours_per_bag, 0.1), 4)

        # Mot de passe SMTP : ne mettre à jour que si un nouveau est fourni, puis chiffrer
        new_password = str(data.get("app_password", "")).strip()
        if new_password:
            final_password = encrypt_password(new_password)
        else:
            final_password = config.EMAIL.get("app_password", "")

        # Token HA : ne mettre à jour que si un nouveau est fourni, puis chiffrer
        new_ha_token = str(data.get("ha_token", "")).strip()
        if new_ha_token:
            final_ha_token = encrypt_password(new_ha_token)
        else:
            final_ha_token = config.HOME_ASSISTANT.get("token", "")

        # Token Ntfy : ne mettre à jour que si un nouveau est fourni, puis chiffrer
        new_ntfy_token = str(data.get("ntfy_token", "")).strip()
        if new_ntfy_token:
            final_ntfy_token = encrypt_password(new_ntfy_token)
        else:
            final_ntfy_token = config.NTFY.get("token", "")

        # Mot de passe IMAP électricité : ne mettre à jour que si un nouveau est fourni, puis chiffrer
        new_imap_password = str(data.get("imap_password", "")).strip()
        if new_imap_password:
            final_imap_password = encrypt_password(new_imap_password)
        else:
            final_imap_password = config.ELECTRICITY.get("imap_password", "")

        # Identifiants des stations de mesure : idem, ne remplacer que si fournis
        new_mf_app_id = str(data.get("meteofrance_application_id", "")).strip()
        if new_mf_app_id:
            final_mf_app_id = encrypt_password(new_mf_app_id)
        else:
            final_mf_app_id = config.OBSERVATIONS.get("meteofrance_application_id", "")
        new_ic_token = str(data.get("infoclimat_token", "")).strip()
        if new_ic_token:
            final_ic_token = encrypt_password(new_ic_token)
        else:
            final_ic_token = config.OBSERVATIONS.get("infoclimat_token", "")

        def _subscription_end(d):
            raw = str(d.get("meteofrance_subscription_end", config.OBSERVATIONS.get("meteofrance_subscription_end", ""))).strip()
            if raw:
                date.fromisoformat(raw)  # ValueError → 400
            return raw

        def _station_ids(field, key):
            raw = data.get(field, config.OBSERVATIONS.get(key, []))
            if isinstance(raw, str):
                raw = raw.split(",")
            return [str(s).strip() for s in raw if str(s).strip()]

        override = {
            "_poele_purchase": {"nb_sacs": nb_sacs, "prix_livraison": prix, "poids_sac": poids, "hours_per_bag": hours_per_bag},
            "TARGET_TEMP": float(data["target_temp"]),
            "SURFACE_M2": int(data["surface_m2"]),
            "REFRESH_INTERVAL_MINUTES": int(data["refresh_interval"]),
            "HP_START": int(data["hp_start"]),
            "HP_END": int(data["hp_end"]),
            "POELE": {
                "pellet_price_per_kg": price_per_kg,
                "consumption_kg_per_hour": consumption_kg_per_hour,
                "efficiency": float(data["efficiency"]),
                "thermal_output_kw": float(data["poele_thermal_output_kw"]),
            },
            "CLIM": {
                "nominal_cop": float(data["nominal_cop"]),
                "comfort_min_temp": float(data["comfort_min_temp"]),
                "nominal_capacity_kw": float(data["clim_capacity_kw"]),
            },
            "LOCATION": {
                "city": str(data["city"]),
                "postal_code": str(data["postal_code"]),
                "latitude": float(data["latitude"]),
                "longitude": float(data["longitude"]),
                "nas_ip": str(data["nas_ip"]),
                "nas_port": int(data["nas_port"]),
                "public_url": str(data.get("public_url", "")).strip(),
            },
            "OBSERVATIONS": {
                "meteofrance_application_id": final_mf_app_id,
                "meteofrance_stations": _station_ids("meteofrance_stations", "meteofrance_stations"),
                "infoclimat_token": final_ic_token,
                "infoclimat_stations": _station_ids("infoclimat_stations", "infoclimat_stations"),
                "max_age_minutes": int(data.get("observation_max_age", config.OBSERVATIONS.get("max_age_minutes", 90))),
                "meteofrance_subscription_end": _subscription_end(data),
            },
            "TEMPO_PRICES": {
                "BLUE":    {"HP": float(data["blue_hp"]),  "HC": float(data["blue_hc"])},
                "WHITE":   {"HP": float(data["white_hp"]), "HC": float(data["white_hc"])},
                "RED":     {"HP": float(data["red_hp"]),   "HC": float(data["red_hc"])},
                "UNKNOWN": {"HP": float(data["white_hp"]), "HC": float(data["white_hc"])},
            },
            "EMAIL": {
                "enabled": bool(data.get("email_enabled", False)),
                "smtp_login": str(data.get("smtp_login", config.EMAIL.get("smtp_login", ""))).strip(),
                "sender": str(data.get("email_sender", config.EMAIL.get("sender", ""))),
                "app_password": final_password,
                "recipients": [r.strip() for r in str(data.get("recipients", "")).split(",") if r.strip()],
                "smtp_host": str(data.get("smtp_host", config.EMAIL.get("smtp_host", "smtp.gmail.com"))),
                "smtp_port": int(data.get("smtp_port", config.EMAIL.get("smtp_port", 587))),
                "notify_hour": int(data.get("notify_hour", config.EMAIL.get("notify_hour", 20))),
                "notify_minute": int(data.get("notify_minute", config.EMAIL.get("notify_minute", 0))),
            },
            "UI": {
                "background_animation": bool(data.get("background_animation", config.UI.get("background_animation", True))),
            },
            "NTFY": {
                "enabled": bool(data.get("ntfy_enabled", False)),
                "url": str(data.get("ntfy_url", config.NTFY.get("url", ""))).strip().rstrip("/"),
                "topic": str(data.get("ntfy_topic", config.NTFY.get("topic", "heating-advisor"))).strip(),
                "token": final_ntfy_token,
            },
            "HOME_ASSISTANT": {
                "enabled": bool(data.get("ha_enabled", False)),
                "url": str(data.get("ha_url", config.HOME_ASSISTANT.get("url", "http://192.168.1.2:8123"))).strip().rstrip("/"),
                "token": final_ha_token,
                "poele_entity_id": str(data.get("ha_entity_id", config.HOME_ASSISTANT.get("poele_entity_id", ""))),
                "clim_entity_id": str(data.get("ha_clim_entity_id", config.HOME_ASSISTANT.get("clim_entity_id", ""))).strip(),
                "auto_control": config.HOME_ASSISTANT.get("auto_control", False),
            },
            "THERMOSTAT": {
                "enabled": config.THERMOSTAT.get("enabled", False),
                "temp_on": float(data.get("thermostat_temp_on", config.THERMOSTAT.get("temp_on", 20.0))),
                "temp_off": float(data.get("thermostat_temp_off", config.THERMOSTAT.get("temp_off", 22.9))),
                "min_on_minutes": int(data.get("thermostat_min_on", config.THERMOSTAT.get("min_on_minutes", 90))),
                "min_on_minutes_clim": int(data.get("thermostat_min_on_clim", config.THERMOSTAT.get("min_on_minutes_clim", 15))),
                "end_of_schedule_grace_minutes": int(data.get("thermostat_grace", config.THERMOSTAT.get("end_of_schedule_grace_minutes", 45))),
                "manual_off_suspend_hours": float(data.get("thermostat_suspend_hours", config.THERMOSTAT.get("manual_off_suspend_hours", 4))),
                "max_on_hours_without_sensor": max(0.0, float(data.get("thermostat_max_on_no_sensor", config.THERMOSTAT.get("max_on_hours_without_sensor", 3)))),
                "overheat_margin": max(0.5, float(data.get("thermostat_overheat_margin", config.THERMOSTAT.get("overheat_margin", 1.5)))),
                "presence_enabled": bool(data.get("thermostat_presence_enabled", config.THERMOSTAT.get("presence_enabled", False))),
                "person_entities": [e.strip() for e in data.get("person_entities", config.THERMOSTAT.get("person_entities", [])) if e.strip()],
                "nearby_zone_name": str(data.get("nearby_zone_name", config.THERMOSTAT.get("nearby_zone_name", "nearby"))).strip(),
                "nearby_no_ignition_after": int(data.get("nearby_no_ignition_after", config.THERMOSTAT.get("nearby_no_ignition_after", 20))),
                "nearby_grace_minutes": int(data.get("nearby_grace_minutes", config.THERMOSTAT.get("nearby_grace_minutes", 20))),
                "away_grace_minutes": int(data.get("away_grace_minutes", config.THERMOSTAT.get("away_grace_minutes", 5))),
                "tracker_stale_hours": float(data.get("tracker_stale_hours", config.THERMOSTAT.get("tracker_stale_hours", 3))),
                "use_felt_temperature": bool(data.get("thermostat_use_felt", config.THERMOSTAT.get("use_felt_temperature", True))),
                "humidity_reference": float(data.get("thermostat_humidity_ref", config.THERMOSTAT.get("humidity_reference", 50.0))),
                "humidity_correction_factor": float(data.get("thermostat_humidity_factor", config.THERMOSTAT.get("humidity_correction_factor", 0.05))),
                "schedule": data.get("thermostat_schedule", config.THERMOSTAT.get("schedule", {})),
            },
            "RADIATEURS_TEMPO_ROUGE": {
                "enabled": bool(data.get("radiateurs_enabled", False)),
                "entities": [
                    {"entity_id": e["entity_id"].strip(), "name": e.get("name", "").strip(), "enabled": bool(e.get("enabled", True))}
                    for e in (data.get("radiateurs_entities") or [])
                    if isinstance(e, dict) and e.get("entity_id", "").strip()
                ],
            },
            "ELECTRICITY": {
                "enabled": bool(data.get("electricity_enabled", False)),
                "imap_host": str(data.get("imap_host", config.ELECTRICITY.get("imap_host", "imap.orange.fr"))).strip(),
                "imap_port": int(data.get("imap_port", config.ELECTRICITY.get("imap_port", 993))),
                "imap_user": str(data.get("imap_user", config.ELECTRICITY.get("imap_user", ""))).strip(),
                "imap_password": final_imap_password,
                "sender_filter": str(data.get("electricity_sender", config.ELECTRICITY.get("sender_filter", "bonjour@lite.eco"))).strip(),
                "subject_filter": str(data.get("electricity_subject", config.ELECTRICITY.get("subject_filter", "flash élec"))).strip(),
                "check_interval_hours": int(data.get("electricity_interval_hours", config.ELECTRICITY.get("check_interval_hours", 24))),
                "lookback_days": int(data.get("electricity_lookback_days", config.ELECTRICITY.get("lookback_days", 90))),
            },
        }
        thermostat_override = override["THERMOSTAT"]
        thermostat_override["temp_on"], thermostat_override["temp_off"] = thermostat_module.validate_thresholds(
            thermostat_override["temp_on"], thermostat_override["temp_off"]
        )
        previous_temp_off = config.THERMOSTAT.get("temp_off")
        def _save(d):
            d.update(override)
            d.pop("COP_LEARNING", None)  # section de l'apprentissage COP, supprimé
        _patch_override(_save)
        apply_overrides(config, override)
        _data_cache.invalidate()
        _reschedule_notify()
        _reschedule_radiateurs()
        _reschedule_electricity_fetch()

        # Consigne clim alignée sur temp_off si elle chauffe déjà (sinon envoyée au prochain allumage)
        clim_setpoint = None
        if config.THERMOSTAT["temp_off"] != previous_temp_off:
            clim_setpoint = thermostat_module.sync_clim_setpoint(ha_client, config.HOME_ASSISTANT, config.THERMOSTAT["temp_off"])

        return jsonify({"status": "ok", "clim_setpoint": clim_setpoint})
    except (KeyError, ValueError) as e:
        return jsonify({"error": str(e)}), 400


# ── Stock granulés ────────────────────────────────────────────

def _migrate_deliveries_from_override() -> None:
    """Migration unique : déplace _deliveries de config_override.json vers deliveries.json."""
    if os.path.exists(DELIVERIES_FILE):
        return
    try:
        if not os.path.exists(OVERRIDE_FILE):
            return
        override = jsonstore.load(OVERRIDE_FILE, {})
        if "_deliveries" not in override:
            return
        deliveries = override["_deliveries"]
        jsonstore.save(DELIVERIES_FILE, deliveries)
        _patch_override(lambda d: d.pop("_deliveries", None))
        logger.info("Migration : %d livraison(s) déplacée(s) vers deliveries.json", len(deliveries))
    except Exception as e:
        logger.error("Migration livraisons échouée : %s", e)


def _load_deliveries() -> list:
    """Charge l'historique des livraisons depuis deliveries.json."""
    data = jsonstore.load(DELIVERIES_FILE, [])
    return data if isinstance(data, list) else []


def _save_deliveries(deliveries: list) -> None:
    """Persiste la liste des livraisons dans deliveries.json (atomique)."""
    jsonstore.save(DELIVERIES_FILE, deliveries)


def _compute_stock() -> dict:
    """Wrapper I/O → délègue le calcul pur à modules.stock.compute_stock_stats."""
    deliveries = _load_deliveries()
    if not deliveries:
        return {"configured": False}
    oldest_date = sorted(deliveries, key=lambda d: d["date"])[0]["date"]
    consumption_data = history_module.get_pellet_consumption_since(oldest_date)
    return compute_stock_stats(
        deliveries=deliveries,
        consumption_data=consumption_data,
        consumption_kg_per_hour=config.POELE.get("consumption_kg_per_hour", 1.0),
        now=datetime.now(),
    )


@app.route("/stock")
def stock_page():
    stock = _compute_stock()
    return render_template("stock.html", config=config, stock=stock)


@app.route("/api/stock")
def api_stock():
    return jsonify(_compute_stock())


@app.route("/api/stock/delivery", methods=["POST"])
def api_stock_delivery_add():
    data = request.get_json(force=True)
    try:
        delivery = {
            "date": str(data["date"]),
            "nb_sacs": int(data["nb_sacs"]),
            "poids_sac": float(data["poids_sac"]),
        }
        if not delivery["date"] or delivery["nb_sacs"] <= 0 or delivery["poids_sac"] <= 0:
            return jsonify({"error": "Données invalides"}), 400
        prix_raw = data.get("prix_total")
        if prix_raw not in (None, "", 0):
            prix = float(prix_raw)
            if prix > 0:
                delivery["prix_total"] = round(prix, 2)
        with jsonstore.lock_for(DELIVERIES_FILE):
            deliveries = _load_deliveries()
            deliveries.append(delivery)
            deliveries.sort(key=lambda d: d["date"])
            _save_deliveries(deliveries)
        logger.info("Stock : livraison ajoutée %s — %d sacs × %.1f kg", delivery["date"], delivery["nb_sacs"], delivery["poids_sac"])
        return jsonify({"status": "ok"})
    except (KeyError, ValueError) as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/stock/delivery/<int:idx>", methods=["PUT"])
def api_stock_delivery_update(idx: int):
    data = request.get_json(force=True)
    try:
        delivery = {
            "date": str(data["date"]),
            "nb_sacs": int(data["nb_sacs"]),
            "poids_sac": float(data["poids_sac"]),
        }
        if not delivery["date"] or delivery["nb_sacs"] <= 0 or delivery["poids_sac"] <= 0:
            return jsonify({"error": "Données invalides"}), 400
        prix_raw = data.get("prix_total")
        if prix_raw not in (None, "", 0):
            prix = float(prix_raw)
            if prix > 0:
                delivery["prix_total"] = round(prix, 2)
        with jsonstore.lock_for(DELIVERIES_FILE):
            deliveries = _load_deliveries()
            if idx < 0 or idx >= len(deliveries):
                return jsonify({"error": "Index invalide"}), 400
            deliveries[idx] = delivery
            deliveries.sort(key=lambda d: d["date"])
            _save_deliveries(deliveries)
        logger.info("Stock : livraison modifiée index %d → %s", idx, delivery["date"])
        return jsonify({"status": "ok"})
    except (KeyError, ValueError) as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/stock/delivery/<int:idx>", methods=["DELETE"])
def api_stock_delivery_delete(idx: int):
    with jsonstore.lock_for(DELIVERIES_FILE):
        deliveries = _load_deliveries()
        if idx < 0 or idx >= len(deliveries):
            return jsonify({"error": "Index invalide"}), 400
        removed = deliveries.pop(idx)
        _save_deliveries(deliveries)
    logger.info("Stock : livraison supprimée index %d (%s)", idx, removed.get("date"))
    return jsonify({"status": "ok"})


_migrate_deliveries_from_override()
_data_cache.refresh_in_background()  # premier chargement dès le démarrage, sans attendre une requête

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
