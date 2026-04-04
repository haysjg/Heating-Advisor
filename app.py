"""
Application Flask – Conseiller Chauffage
Déployable via Docker sur NAS Synology
"""

import logging
import json
import os
import secrets as _secrets
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify, request, session, redirect, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import config
from modules.weather import get_current_temperature, get_tomorrow_weather, get_hourly_forecast
from modules.tempo import get_tempo_info
from modules.advisor import analyze, analyze_tomorrow
from modules.overrides import apply as apply_overrides, load as load_overrides, OVERRIDE_FILE
from modules.crypto import encrypt_password, is_configured
import modules.homeassistant as ha_client
import modules.thermostat as thermostat_module
import modules.history as history_module

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

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

# ── Rate limiter (protection brute-force login) ───────────────
limiter = Limiter(get_remote_address, app=app, default_limits=[], storage_uri="memory://")

load_overrides(config)

# ── Authentification ─────────────────────────────────────────

def _check_password(password: str) -> bool:
    stored = config.AUTH.get("password_hash", "")
    if not stored:
        return password == config.AUTH.get("default_password", "heating")
    return check_password_hash(stored, password)


@app.before_request
def require_login():
    if request.path.startswith("/static"):
        return
    if request.endpoint in ("login", "logout"):
        return
    if not session.get("authenticated"):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Non authentifié"}), 401
        return redirect(url_for("login", next=request.path))


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def login():
    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        if _check_password(password):
            session.permanent = True
            session["authenticated"] = True
            next_url = request.args.get("next") or "/"
            if not next_url.startswith("/"):
                next_url = "/"
            return redirect(next_url)
        error = "Mot de passe incorrect"
        logger.warning("Tentative de connexion échouée depuis %s", request.remote_addr)
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


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
        override = {}
        if os.path.exists(OVERRIDE_FILE):
            with open(OVERRIDE_FILE) as f:
                override = json.load(f)
        override.setdefault("AUTH", {})["password_hash"] = new_hash
        with open(OVERRIDE_FILE, "w") as f:
            json.dump(override, f, indent=2, ensure_ascii=False)
        config.AUTH["password_hash"] = new_hash
        logger.info("Mot de passe modifié")
    except Exception as e:
        logger.error("Erreur sauvegarde mot de passe : %s", e)
        return jsonify({"error": str(e)}), 500
    return jsonify({"status": "ok"})


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
    """Enregistre un point d'historique (températures + état poêle + couleur Tempo)."""
    try:
        data = get_analysis()
        outdoor_temp = data.get("weather", {}).get("temperature")
        indoor_temp = data.get("indoor", {}).get("temperature") if data.get("indoor") else None
        poele_state = thermostat_module.get_state().get("state", "off")
        tempo_color = data.get("tempo", {}).get("today", {}).get("color")
        history_module.record(outdoor_temp, indoor_temp, poele_state, tempo_color)
    except Exception as e:
        logger.error("History record échoué : %s", e)


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
        )
    except Exception as e:
        logger.error("Diagnose record échoué : %s", e)


def _run_thermostat():
    """Vérifie la température intérieure et pilote le poêle via le thermostat."""
    try:
        data = get_analysis()
        recommendation = data.get("recommendation", {}).get("system", "none")
        outdoor_temp = data.get("weather", {}).get("temperature")
        thermostat_module.check_and_apply(config.HOME_ASSISTANT, config.THERMOSTAT, recommendation, config.EMAIL,
                                          ntfy_cfg=config.NTFY, outdoor_temp=outdoor_temp)
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


_reschedule_notify()
_reschedule_thermostat()

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
logger.info("Historique : enregistrement toutes les 10 min")

# Cache simple en mémoire pour éviter de surcharger les APIs
_cache: dict = {"data": None, "expires_at": None}


def _build_config_dict() -> dict:
    return {
        "TEMPO_PRICES": config.TEMPO_PRICES,
        "CLIM": config.CLIM,
        "POELE": config.POELE,
        "HP_START": config.HP_START,
        "HP_END": config.HP_END,
    }


def _fetch_indoor() -> None:
    """Rafraîchit uniquement la température intérieure dans le cache (HA local, pas d'API externe)."""
    if not _cache["data"]:
        return
    indoor = ha_client.get_indoor_climate(config.HOME_ASSISTANT)
    if indoor:
        if (config.THERMOSTAT.get("use_felt_temperature")
                and indoor.get("temperature") is not None
                and indoor.get("humidity") is not None):
            indoor["felt_temperature"] = thermostat_module.felt_temperature(
                indoor["temperature"], indoor["humidity"], config.THERMOSTAT
            )
        _cache["data"]["indoor"] = indoor


def get_analysis(force_refresh: bool = False) -> dict:
    """Retourne l'analyse en cache ou en recharge une fraîche."""
    import time

    now = time.time()
    ttl = config.REFRESH_INTERVAL_MINUTES * 60

    if not force_refresh and _cache["data"] and _cache["expires_at"] and now < _cache["expires_at"]:
        logger.info("Retour depuis le cache — rafraîchissement température intérieure")
        _fetch_indoor()
        return _cache["data"]

    logger.info("Rafraîchissement des données…")
    cfg = _build_config_dict()

    weather = get_current_temperature(config.LOCATION)
    tomorrow_weather = get_tomorrow_weather({
        **config.LOCATION,
        "hp_start": config.HP_START,
        "hp_end": config.HP_END,
    })
    tempo = get_tempo_info(config.HP_START, config.HP_END)
    result = analyze(weather, tempo, cfg)
    result["tomorrow"] = analyze_tomorrow(tomorrow_weather, tempo, cfg)
    result["hourly_forecast"] = get_hourly_forecast(config.LOCATION["latitude"], config.LOCATION["longitude"], hours=48)

    indoor = ha_client.get_indoor_climate(config.HOME_ASSISTANT)
    if indoor:
        if (config.THERMOSTAT.get("use_felt_temperature")
                and indoor.get("temperature") is not None
                and indoor.get("humidity") is not None):
            indoor["felt_temperature"] = thermostat_module.felt_temperature(
                indoor["temperature"], indoor["humidity"], config.THERMOSTAT
            )
        result["indoor"] = indoor

    _cache["data"] = result
    _cache["expires_at"] = now + ttl
    return result


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
        vacation = thermostat_module.get_vacation()
        on_vacation = thermostat_module.is_on_vacation()
        return render_template("index.html", data=data, config=config, thermostat_state=thermostat_state,
                               next_schedule_start=next_start, vacation=vacation, on_vacation=on_vacation)
    except Exception as e:
        logger.exception("Erreur index : %s", e)
        return render_template("index.html", data=None, error=str(e), config=config, thermostat_state={},
                               next_schedule_start=None, vacation={}, on_vacation=False)


@app.route("/api/data")
def api_data():
    try:
        data = get_analysis()
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/refresh")
def api_refresh():
    try:
        data = get_analysis(force_refresh=True)
        return jsonify({"status": "ok", "timestamp": data["timestamp"]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/notify-test", methods=["POST"])
def api_notify_test():
    try:
        success = _run_notify()
        if success:
            return jsonify({"status": "ok"})
        return jsonify({"error": "Envoi échoué — vérifiez les logs et la configuration SMTP"}), 500
    except Exception as e:
        logger.exception("Erreur test notification : %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/ha/turn_on", methods=["POST"])
def api_ha_turn_on():
    if not ha_client.is_configured(config.HOME_ASSISTANT):
        return jsonify({"error": "Home Assistant non configuré"}), 400
    return jsonify({"status": "ok" if ha_client.turn_on(config.HOME_ASSISTANT) else "error"})


@app.route("/api/ha/turn_off", methods=["POST"])
def api_ha_turn_off():
    if not ha_client.is_configured(config.HOME_ASSISTANT):
        return jsonify({"error": "Home Assistant non configuré"}), 400
    return jsonify({"status": "ok" if ha_client.turn_off(config.HOME_ASSISTANT) else "error"})


@app.route("/api/ha/auto_control", methods=["POST"])
def api_ha_auto_control():
    """Active ou désactive le pilotage automatique sans toucher au reste de la config."""
    data = request.get_json(force=True)
    enabled = bool(data.get("enabled", False))
    config.HOME_ASSISTANT["auto_control"] = enabled
    # Persiste dans le fichier d'overrides
    try:
        override = {}
        if os.path.exists(OVERRIDE_FILE):
            with open(OVERRIDE_FILE) as f:
                override = json.load(f)
        override.setdefault("HOME_ASSISTANT", {})["auto_control"] = enabled
        with open(OVERRIDE_FILE, "w") as f:
            json.dump(override, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error("Sauvegarde auto_control échouée : %s", e)
    logger.info("HA auto_control → %s", enabled)
    return jsonify({"status": "ok", "auto_control": enabled})


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
    return jsonify({
        "thermostat_enabled": config.THERMOSTAT.get("enabled", False),
        "in_schedule": is_in_schedule(config.THERMOSTAT),
        "indoor": indoor,
        "poele_real_state": ha_state.get("state") if ha_state else None,
        "thermostat_state": state,
        "temp_on": config.THERMOSTAT.get("temp_on"),
        "temp_off": config.THERMOSTAT.get("temp_off"),
        "min_on_minutes": config.THERMOSTAT.get("min_on_minutes"),
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


@app.route("/api/thermostat/state")
def api_thermostat_state():
    state = thermostat_module.get_state()
    return jsonify({
        **state,
        "enabled": config.THERMOSTAT.get("enabled", False),
        "in_schedule": thermostat_module.is_in_schedule(config.THERMOSTAT),
        "temp_on": config.THERMOSTAT.get("temp_on"),
        "temp_off": config.THERMOSTAT.get("temp_off"),
    })


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
        from datetime import date as _date
        s = datetime.fromisoformat(start).date()
        e = datetime.fromisoformat(end).date()
        if s > e:
            return jsonify({"error": "La date de début doit être avant la date de fin"}), 400
    except Exception:
        return jsonify({"error": "Dates invalides (format YYYY-MM-DD attendu)"}), 400
    thermostat_module.set_vacation(start, end)
    logger.info("Mode vacances activé : %s → %s", start, end)
    return jsonify({"status": "ok"})


@app.route("/api/thermostat/vacation", methods=["DELETE"])
def api_thermostat_vacation_clear():
    thermostat_module.clear_vacation()
    logger.info("Mode vacances annulé")
    return jsonify({"status": "ok"})


@app.route("/api/thermostat/resume", methods=["POST"])
def api_thermostat_resume():
    """Annule la suspension du thermostat."""
    state = thermostat_module.get_state()
    state["suspended_until"] = None
    from modules.thermostat import _save_state
    _save_state(state)
    logger.info("Thermostat : suspension annulée manuellement")
    return jsonify({"status": "ok"})


@app.route("/api/thermostat/toggle", methods=["POST"])
def api_thermostat_toggle():
    data = request.get_json(force=True)
    enabled = bool(data.get("enabled", False))
    config.THERMOSTAT["enabled"] = enabled
    try:
        override = {}
        if os.path.exists(OVERRIDE_FILE):
            with open(OVERRIDE_FILE) as f:
                override = json.load(f)
        override.setdefault("THERMOSTAT", {})["enabled"] = enabled
        with open(OVERRIDE_FILE, "w") as f:
            json.dump(override, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error("Sauvegarde thermostat enabled échouée : %s", e)
    _reschedule_thermostat()
    logger.info("Thermostat → %s", "activé" if enabled else "désactivé")
    return jsonify({"status": "ok", "enabled": enabled})


@app.route("/statistics")
def statistics_page():
    return render_template("statistics.html", config=config)


@app.route("/api/statistics")
def api_statistics():
    try:
        hours = int(request.args.get("hours", 24))
        hours = min(max(hours, 1), 168)  # entre 1h et 7 jours
        data = history_module.get_history(hours)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/statistics/daily")
def api_statistics_daily():
    try:
        days = int(request.args.get("days", 30))
        days = min(max(days, 1), 30)
        data = history_module.get_daily_summary(days)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/thermostat/diagnose/history")
def api_diagnose_history():
    try:
        hours = int(request.args.get("hours", 24))
        hours = min(max(hours, 1), 168)
        data = history_module.get_diagnose_history(hours)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/config")
def config_page():
    purchase = {}
    if os.path.exists(OVERRIDE_FILE):
        try:
            with open(OVERRIDE_FILE) as f:
                purchase = json.load(f).get("_poele_purchase", {})
        except Exception:
            pass
    return render_template("config.html", config=config, purchase=purchase)


@app.route("/api/config", methods=["POST"])
def api_config_save():
    data = request.get_json(force=True)
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
                "meteociel_url": str(data["meteociel_url"]),
                "nas_ip": str(data["nas_ip"]),
                "nas_port": int(data["nas_port"]),
                "public_url": str(data.get("public_url", "")).strip(),
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
            "HOME_ASSISTANT": {
                "enabled": bool(data.get("ha_enabled", False)),
                "url": str(data.get("ha_url", config.HOME_ASSISTANT.get("url", "http://192.168.1.2:8123"))).strip().rstrip("/"),
                "token": final_ha_token,
                "poele_entity_id": str(data.get("ha_entity_id", config.HOME_ASSISTANT.get("poele_entity_id", ""))),
                "auto_control": config.HOME_ASSISTANT.get("auto_control", False),
            },
            "THERMOSTAT": {
                "enabled": config.THERMOSTAT.get("enabled", False),
                "temp_on": float(data.get("thermostat_temp_on", config.THERMOSTAT.get("temp_on", 20.0))),
                "temp_off": float(data.get("thermostat_temp_off", config.THERMOSTAT.get("temp_off", 22.9))),
                "min_on_minutes": int(data.get("thermostat_min_on", config.THERMOSTAT.get("min_on_minutes", 90))),
                "end_of_schedule_grace_minutes": int(data.get("thermostat_grace", config.THERMOSTAT.get("end_of_schedule_grace_minutes", 45))),
                "manual_off_suspend_hours": float(data.get("thermostat_suspend_hours", config.THERMOSTAT.get("manual_off_suspend_hours", 4))),
                "presence_enabled": bool(data.get("thermostat_presence_enabled", config.THERMOSTAT.get("presence_enabled", False))),
                "person_entities": [e.strip() for e in data.get("person_entities", config.THERMOSTAT.get("person_entities", [])) if e.strip()],
                "nearby_zone_name": str(data.get("nearby_zone_name", config.THERMOSTAT.get("nearby_zone_name", "nearby"))).strip(),
                "nearby_no_ignition_after": int(data.get("nearby_no_ignition_after", config.THERMOSTAT.get("nearby_no_ignition_after", 20))),
                "nearby_grace_minutes": int(data.get("nearby_grace_minutes", config.THERMOSTAT.get("nearby_grace_minutes", 20))),
                "away_grace_minutes": int(data.get("away_grace_minutes", config.THERMOSTAT.get("away_grace_minutes", 5))),
                "use_felt_temperature": bool(data.get("thermostat_use_felt", config.THERMOSTAT.get("use_felt_temperature", True))),
                "humidity_reference": float(data.get("thermostat_humidity_ref", config.THERMOSTAT.get("humidity_reference", 50.0))),
                "humidity_correction_factor": float(data.get("thermostat_humidity_factor", config.THERMOSTAT.get("humidity_correction_factor", 0.05))),
                "schedule": data.get("thermostat_schedule", config.THERMOSTAT.get("schedule", {})),
            },
        }
        os.makedirs(os.path.dirname(OVERRIDE_FILE), exist_ok=True)
        with open(OVERRIDE_FILE, "w") as f:
            json.dump(override, f, indent=2, ensure_ascii=False)
        apply_overrides(config, override)
        _cache["data"] = None
        _cache["expires_at"] = None
        _reschedule_notify()
        return jsonify({"status": "ok"})
    except (KeyError, ValueError) as e:
        return jsonify({"error": str(e)}), 400


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
