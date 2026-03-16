"""
Application Flask – Conseiller Chauffage
Déployable via Docker sur NAS Synology
"""

import logging
import json
import os
from datetime import datetime
from flask import Flask, render_template, jsonify, request

import config
from modules.weather import get_current_temperature, get_tomorrow_weather, get_hourly_forecast
from modules.tempo import get_tempo_info
from modules.advisor import analyze, analyze_tomorrow

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

OVERRIDE_FILE = os.path.join(os.path.dirname(__file__), "data", "config_override.json")


def _apply_overrides(data: dict) -> None:
    for key in ("TARGET_TEMP", "SURFACE_M2", "REFRESH_INTERVAL_MINUTES", "HP_START", "HP_END"):
        if key in data:
            setattr(config, key, data[key])
    for key in ("POELE", "CLIM", "LOCATION"):
        if key in data and isinstance(data[key], dict):
            getattr(config, key).update(data[key])
    if "TEMPO_PRICES" in data:
        for color, prices in data["TEMPO_PRICES"].items():
            if color in config.TEMPO_PRICES and isinstance(prices, dict):
                config.TEMPO_PRICES[color].update(prices)


def _load_overrides() -> None:
    if os.path.exists(OVERRIDE_FILE):
        try:
            with open(OVERRIDE_FILE) as f:
                _apply_overrides(json.load(f))
            logger.info("Overrides chargés depuis %s", OVERRIDE_FILE)
        except Exception as e:
            logger.warning("Impossible de charger les overrides : %s", e)


_load_overrides()

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

    _cache["data"] = result
    _cache["expires_at"] = now + ttl
    return result


@app.route("/")
def index():
    try:
        data = get_analysis()
        return render_template("index.html", data=data, config=config)
    except Exception as e:
        logger.exception("Erreur index : %s", e)
        return render_template("index.html", data=None, error=str(e), config=config)


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
        }
        os.makedirs(os.path.dirname(OVERRIDE_FILE), exist_ok=True)
        with open(OVERRIDE_FILE, "w") as f:
            json.dump(override, f, indent=2, ensure_ascii=False)
        _apply_overrides(override)
        _cache["data"] = None
        _cache["expires_at"] = None
        return jsonify({"status": "ok"})
    except (KeyError, ValueError) as e:
        return jsonify({"error": str(e)}), 400


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
