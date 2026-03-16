"""
Application Flask – Conseiller Chauffage
Déployable via Docker sur NAS Synology
"""

import logging
import json
from datetime import datetime
from flask import Flask, render_template, jsonify

import config
from modules.weather import get_current_temperature, get_tomorrow_weather, get_hourly_forecast
from modules.tempo import get_tempo_info
from modules.advisor import analyze, analyze_tomorrow

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
