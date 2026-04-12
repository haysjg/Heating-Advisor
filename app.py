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
import modules.cop_learning as cop_learning_module
import modules.cop_sampling as cop_sampling_module

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

RADIATEURS_MANAGED_OFF_FILE = os.path.join(os.path.dirname(__file__), "data", "radiateurs_managed_off.json")


def _load_managed_off() -> list:
    try:
        if os.path.exists(RADIATEURS_MANAGED_OFF_FILE):
            with open(RADIATEURS_MANAGED_OFF_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return []


def _save_managed_off(entities: list) -> None:
    try:
        os.makedirs(os.path.dirname(RADIATEURS_MANAGED_OFF_FILE), exist_ok=True)
        with open(RADIATEURS_MANAGED_OFF_FILE, "w") as f:
            json.dump(entities, f)
    except Exception as e:
        logger.error("Sauvegarde managed_off échouée : %s", e)


def _suspend_thermostat_after_manual_off(entity_type: str) -> None:
    """Suspend thermostat for configured hours after manual off."""
    try:
        state = thermostat_module.get_state()
        suspend_hours = config.THERMOSTAT.get("manual_off_suspend_hours", 4)
        suspended_until = (datetime.now() + timedelta(hours=suspend_hours)).isoformat()

        new_state = {
            **state,
            "state": "off",
            "active_system": None,
            "last_turned_off": datetime.now().isoformat(),
            "suspended_until": suspended_until,
        }
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
    """Enregistre un point d'historique (températures + état chauffage + couleur Tempo)."""
    try:
        data = get_analysis()
        outdoor_temp = data.get("weather", {}).get("temperature")
        indoor_temp = data.get("indoor", {}).get("temperature") if data.get("indoor") else None
        th_state = thermostat_module.get_state()
        # Enregistre le système actif au lieu de simplement on/off
        if th_state.get("state") == "on":
            heating_state = th_state.get("active_system", "poele")
        else:
            heating_state = "off"
        tempo_color = data.get("tempo", {}).get("today", {}).get("color")
        history_module.record(outdoor_temp, indoor_temp, heating_state, tempo_color)
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
        today_color = tempo.get("today", {}).get("color", "BLUE")
    except Exception as e:
        logger.error("Radiateurs tempo rouge : erreur récupération tempo : %s", e)
        return

    from modules.ntfy_push import send as ntfy_send

    if today_color == "RED":
        turned_off = []
        managed = _load_managed_off()
        for entity_id in entity_ids:
            state = ha_client.get_entity_state(config.HOME_ASSISTANT, entity_id)
            if state and state.get("state") not in ("off", "unavailable", "unknown"):
                if ha_client.turn_off_entity(config.HOME_ASSISTANT, entity_id):
                    turned_off.append(entity_id)
                    if entity_id not in managed:
                        managed.append(entity_id)
        if turned_off:
            _save_managed_off(managed)
            names = ", ".join(label_map.get(e, e) for e in turned_off)
            ntfy_send("🔴 Jour Rouge — Radiateurs éteints", f"Extinction automatique : {names}", config.NTFY)
            logger.info("Radiateurs éteints (jour rouge) : %s", turned_off)
    else:
        managed = _load_managed_off()
        turned_on = []
        for entity_id in list(managed):
            if entity_id in entity_ids:
                if ha_client.turn_on_entity(config.HOME_ASSISTANT, entity_id):
                    turned_on.append(entity_id)
                    managed.remove(entity_id)
        if turned_on:
            _save_managed_off(managed)
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


_reschedule_notify()
_reschedule_thermostat()
_reschedule_radiateurs()

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
# Purge hebdomadaire des données COP > 90 jours
_scheduler.add_job(
    lambda: cop_learning_module.purge_old(90),
    CronTrigger(day_of_week="mon", hour=3, minute=20, timezone="Europe/Paris"),
    id="cop_purge_job",
    misfire_grace_time=300,
)
# Nettoyage des tasks d'échantillonnage COP toutes les 5 minutes
_scheduler.add_job(
    lambda: cop_sampling_module.cleanup_old_tasks(max_age_minutes=10),
    "interval",
    minutes=5,
    id="cop_sampling_cleanup"
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
                })
        return render_template("index.html", data=data, config=config, thermostat_state=thermostat_state,
                               next_schedule_start=next_start, radiateurs_info=radiateurs_info,
                               poele_state=poele_state_value, ajax_interval=config.AJAX_REFRESH_INTERVAL)
    except Exception as e:
        logger.exception("Erreur index : %s", e)
        return render_template("index.html", data=None, error=str(e), config=config, thermostat_state={},
                               next_schedule_start=None, radiateurs_info=[], poele_state=None,
                               ajax_interval=config.AJAX_REFRESH_INTERVAL)


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


@app.route("/api/dashboard/refresh")
def api_dashboard_refresh():
    """Endpoint AJAX pour rafraîchir uniquement les données du dashboard sans recharger la page."""
    try:
        # Récupérer les données cachées (météo, Tempo)
        data = get_analysis()

        # Vérifier que les données sont valides
        if data is None:
            return jsonify({"error": "Impossible de récupérer les données"}), 500

        # Force refresh de la température intérieure (HA local, rapide)
        _fetch_indoor()

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
                "source": weather.get("source", "météociel.fr")
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
                "last_turned_on": thermostat_state.get("last_turned_on")
            },
            "clim_state": clim_state_value,
            "poele_state": poele_state_value,
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
        logger.exception("Erreur api_dashboard_refresh : %s", e)
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
    managed = _load_managed_off()
    if entity_id in managed:
        managed.remove(entity_id)
        _save_managed_off(managed)

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

    # Ajouter à managed_off si pas présent
    managed = _load_managed_off()
    if entity_id not in managed:
        managed.append(entity_id)
        _save_managed_off(managed)

    success = ha_client.turn_off_entity(config.HOME_ASSISTANT, entity_id)
    return jsonify({"status": "ok" if success else "error"})


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
        logger.exception("Erreur test ntfy : %s", e)
        return jsonify({"error": str(e)}), 500


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


@app.route("/api/ha/clim/turn_on", methods=["POST"])
def api_ha_clim_turn_on():
    if not ha_client.is_clim_configured(config.HOME_ASSISTANT):
        return jsonify({"error": "Clim non configurée"}), 400
    target_temp = config.THERMOSTAT.get("temp_off", 22.9)
    success = ha_client.turn_on_clim(config.HOME_ASSISTANT, target_temp)

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


@app.route("/absence")
def absence_page():
    vacation = thermostat_module.get_vacation()
    on_vacation = thermostat_module.is_on_vacation()
    return render_template("absence.html", config=config, vacation=vacation, on_vacation=on_vacation)


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
        return jsonify({"error": str(e)}), 500


@app.route("/api/reports/aggregate-now", methods=["POST"])
def api_reports_aggregate_now():
    """Déclenche manuellement l'agrégation du mois en cours et du mois précédent."""
    try:
        _run_monthly_aggregation()
        return jsonify({"status": "ok"})
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


@app.route("/cop-learning")
def cop_learning_page():
    return render_template("cop_learning.html", config=config)


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


@app.route("/api/cop/tag", methods=["POST"])
def api_cop_tag():
    """Enregistre un tag ON/OFF pour l'apprentissage du COP."""
    try:
        data = request.get_json(force=True)
        tag = data.get("tag", "").lower()
        notes = data.get("notes", "").strip()

        if tag not in ("on", "off"):
            return jsonify({"error": "Tag invalide (attendu: on ou off)"}), 400

        # Récupérer les capteurs Shelly
        cop_cfg = config.COP_LEARNING
        sensors = cop_learning_module.get_current_sensors(config.HOME_ASSISTANT, cop_cfg)

        if not sensors:
            return jsonify({"error": "Capteurs Shelly indisponibles — vérifiez la configuration Home Assistant"}), 400

        # Règle de sécurité : interdire tag ON si ballon en chauffe (fausserait le calcul COP)
        heater_power_threshold = 50  # Watts
        if tag == "on" and sensors["heater_power"] > heater_power_threshold:
            return jsonify({
                "error": f"Tag ON refusé : ballon eau chaude en chauffe ({sensors['heater_power']:.0f}W > {heater_power_threshold}W). "
                        f"Cela fausserait le calcul du COP. Attendez que le ballon ait terminé sa chauffe."
            }), 400

        # Récupérer la température extérieure
        outdoor_temp = None
        try:
            data_analysis = get_analysis()
            outdoor_temp = data_analysis.get("weather", {}).get("temperature")
        except Exception as e:
            logger.warning(f"Impossible de récupérer la température extérieure : {e}")

        # Enregistrer le tag
        result = cop_learning_module.record_tag(
            tag=tag,
            outdoor_temp=outdoor_temp,
            total_power=sensors["total_power"],
            heater_power=sensors["heater_power"],
            notes=notes,
            config=config
        )

        return jsonify(result)

    except Exception as e:
        logger.exception("Erreur enregistrement tag COP : %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/cop/start-sampling", methods=["POST"])
def api_cop_start_sampling():
    """Démarre un échantillonnage asynchrone pour tag ON."""
    try:
        data = request.get_json(force=True)
        notes = data.get("notes", "").strip()

        # Validation préalable (capteurs, ballon)
        cop_cfg = config.COP_LEARNING
        sensors = cop_learning_module.get_current_sensors(config.HOME_ASSISTANT, cop_cfg)

        if not sensors:
            return jsonify({"error": "Capteurs Shelly indisponibles"}), 400

        if sensors["heater_power"] > 50:
            return jsonify({"error": "Tag ON refusé : ballon en chauffe"}), 400

        # Capturer température extérieure
        outdoor_temp = None
        try:
            data_analysis = get_analysis()
            outdoor_temp = data_analysis.get("weather", {}).get("temperature")
        except Exception as e:
            logger.warning(f"Température extérieure indisponible : {e}")

        # Démarrer task
        task_id = cop_sampling_module.start_sampling_task(notes, outdoor_temp, config)

        return jsonify({
            "status": "ok",
            "task_id": task_id,
            "message": "Échantillonnage démarré"
        })

    except Exception as e:
        logger.exception("Erreur start sampling")
        return jsonify({"error": str(e)}), 500


@app.route("/api/cop/sampling-status/<task_id>")
def api_cop_sampling_status(task_id):
    """Retourne l'état d'un échantillonnage en cours."""
    status = cop_sampling_module.get_task_status(task_id)

    if not status:
        return jsonify({"error": "Task non trouvée"}), 404

    return jsonify(status)


@app.route("/api/cop/cancel-sampling/<task_id>", methods=["POST"])
def api_cop_cancel_sampling(task_id):
    """Annule un échantillonnage en cours."""
    success = cop_sampling_module.cancel_task(task_id)

    if not success:
        return jsonify({"error": "Task non trouvée"}), 404

    return jsonify({"status": "ok", "message": "Échantillonnage annulé"})


@app.route("/api/cop/data")
def api_cop_data():
    """Retourne les données pour l'interface COP Learning."""
    try:
        cop_cfg = config.COP_LEARNING

        # Statistiques
        stats = cop_learning_module.get_statistics()

        # Tags récents
        recent_tags = cop_learning_module.get_recent_tags(20)

        # Courbes (théorique vs apprise)
        comparison = cop_learning_module.get_cop_curve_comparison(config.CLIM["cop_curve"])

        # Profil de base
        base_profile = cop_learning_module.get_base_profile()

        # Capteurs temps réel
        sensors = cop_learning_module.get_current_sensors(config.HOME_ASSISTANT, cop_cfg)

        # Température extérieure
        outdoor_temp = None
        try:
            data_analysis = get_analysis()
            outdoor_temp = data_analysis.get("weather", {}).get("temperature")
        except Exception:
            pass

        return jsonify({
            "enabled": cop_cfg.get("enabled", False),
            "stats": stats,
            "recent_tags": recent_tags,
            "curves": comparison,
            "base_profile": base_profile,
            "sensors": sensors,
            "outdoor_temp": outdoor_temp,
            "config": {
                "nominal_thermal_kw": cop_cfg.get("nominal_thermal_kw", 4.0),
                "confidence_threshold": cop_cfg.get("confidence_threshold", 0.6),
                "auto_switch_to_learned": cop_cfg.get("auto_switch_to_learned", False),
            }
        })

    except Exception as e:
        logger.exception("Erreur récupération données COP : %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/cop/calibrate", methods=["POST"])
def api_cop_calibrate():
    """Calibration manuelle de la consommation de base."""
    try:
        data = request.get_json(force=True)
        base_watts = float(data.get("base_watts", 0))
        hour = data.get("hour")

        if hour is not None:
            hour = int(hour)
            if not (0 <= hour <= 23):
                return jsonify({"error": "Heure invalide (0-23)"}), 400

        cop_learning_module.calibrate_base_consumption(base_watts, hour)

        return jsonify({"status": "ok"})

    except (ValueError, TypeError) as e:
        return jsonify({"error": f"Valeur invalide : {e}"}), 400
    except Exception as e:
        logger.exception("Erreur calibration COP : %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/cop/clear", methods=["DELETE"])
def api_cop_clear():
    """Efface les données d'apprentissage."""
    try:
        keep_config = request.args.get("keep_config", "true").lower() == "true"
        cop_learning_module.clear_all(keep_config)
        return jsonify({"status": "ok"})
    except Exception as e:
        logger.exception("Erreur effacement données COP : %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/cop/tag/<int:tag_id>", methods=["DELETE"])
def api_cop_delete_tag(tag_id):
    """Supprime un tag spécifique."""
    try:
        success = cop_learning_module.delete_tag(tag_id)
        if success:
            return jsonify({"status": "ok", "message": f"Tag {tag_id} supprimé"})
        else:
            return jsonify({"error": "Tag non trouvé"}), 404
    except Exception as e:
        logger.exception("Erreur suppression tag %s : %s", tag_id, e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/cop/tag/last-on", methods=["GET"])
def api_cop_last_on_tag():
    """Retourne le dernier tag ON enregistré."""
    try:
        tag = cop_learning_module.get_last_on_tag()
        if tag:
            return jsonify({"status": "ok", "tag": tag})
        else:
            return jsonify({"error": "Aucun tag ON trouvé"}), 404
    except Exception as e:
        logger.exception("Erreur récupération dernier tag ON : %s", e)
        return jsonify({"error": str(e)}), 500


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

        # Token Ntfy : ne mettre à jour que si un nouveau est fourni, puis chiffrer
        new_ntfy_token = str(data.get("ntfy_token", "")).strip()
        if new_ntfy_token:
            final_ntfy_token = encrypt_password(new_ntfy_token)
        else:
            final_ntfy_token = config.NTFY.get("token", "")

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
            "RADIATEURS_TEMPO_ROUGE": {
                "enabled": bool(data.get("radiateurs_enabled", False)),
                "entities": [
                    {"entity_id": e["entity_id"].strip(), "name": e.get("name", "").strip(), "enabled": bool(e.get("enabled", True))}
                    for e in (data.get("radiateurs_entities") or [])
                    if isinstance(e, dict) and e.get("entity_id", "").strip()
                ],
            },
            "COP_LEARNING": {
                "enabled": bool(data.get("cop_enabled", False)),
                "shelly_total_power_entity_id": str(data.get("cop_shelly_total", config.COP_LEARNING.get("shelly_total_power_entity_id", ""))).strip(),
                "shelly_heater_power_entity_id": str(data.get("cop_shelly_heater", config.COP_LEARNING.get("shelly_heater_power_entity_id", ""))).strip(),
                "nominal_thermal_kw": float(data.get("cop_thermal_kw", config.COP_LEARNING.get("nominal_thermal_kw", 4.0))),
                "confidence_threshold": float(data.get("cop_confidence_threshold", config.COP_LEARNING.get("confidence_threshold", 0.6))),
                "min_samples_per_bin": int(data.get("cop_min_samples", config.COP_LEARNING.get("min_samples_per_bin", 3))),
                "temp_bin_size": int(data.get("cop_temp_bin", config.COP_LEARNING.get("temp_bin_size", 5))),
                "min_ac_power": int(data.get("cop_min_power", config.COP_LEARNING.get("min_ac_power", 500))),
                "max_ac_power": int(data.get("cop_max_power", config.COP_LEARNING.get("max_ac_power", 3000))),
                "auto_switch_to_learned": bool(data.get("cop_auto_switch", config.COP_LEARNING.get("auto_switch_to_learned", False))),
            },
        }
        os.makedirs(os.path.dirname(OVERRIDE_FILE), exist_ok=True)
        with open(OVERRIDE_FILE, "w") as f:
            json.dump(override, f, indent=2, ensure_ascii=False)
        apply_overrides(config, override)
        _cache["data"] = None
        _cache["expires_at"] = None
        _reschedule_notify()
        _reschedule_radiateurs()
        return jsonify({"status": "ok"})
    except (KeyError, ValueError) as e:
        return jsonify({"error": str(e)}), 400


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
