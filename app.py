"""
Application Flask – Conseiller Chauffage
Déployable via Docker sur NAS Synology
"""

import logging
import json
import os
from datetime import datetime
from flask import Flask, render_template, jsonify, request
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

load_overrides(config)

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


def _run_thermostat():
    """Vérifie la température intérieure et pilote le poêle via le thermostat."""
    try:
        data = get_analysis()
        recommendation = data.get("recommendation", {}).get("system", "none")
        thermostat_module.check_and_apply(config.HOME_ASSISTANT, config.THERMOSTAT, recommendation)
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


def get_analysis(force_refresh: bool = False) -> dict:
    """Retourne l'analyse en cache ou en recharge une fraîche."""
    import time

    now = time.time()
    ttl = config.REFRESH_INTERVAL_MINUTES * 60

    if not force_refresh and _cache["data"] and _cache["expires_at"] and now < _cache["expires_at"]:
        logger.info("Retour depuis le cache")
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
        result["indoor"] = indoor

    _cache["data"] = result
    _cache["expires_at"] = now + ttl
    return result


@app.route("/")
def index():
    try:
        data = get_analysis()
        thermostat_state = thermostat_module.get_state()
        return render_template("index.html", data=data, config=config, thermostat_state=thermostat_state)
    except Exception as e:
        logger.exception("Erreur index : %s", e)
        return render_template("index.html", data=None, error=str(e), config=config, thermostat_state={})


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
    state = get_state()
    indoor = ha_client.get_indoor_climate(config.HOME_ASSISTANT)
    ha_state = ha_client.get_state(config.HOME_ASSISTANT)
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
