"""
Récupération de la couleur EDF Tempo (Bleu / Blanc / Rouge).
API utilisée : api-couleur-tempo.fr (communautaire, gratuite, sans clé)
"""

import json
import logging
import urllib.request
from datetime import datetime

logger = logging.getLogger(__name__)

BASE_URL = "https://www.api-couleur-tempo.fr/api"

COLOR_MAP = {
    1: "BLUE",
    2: "WHITE",
    3: "RED",
}

COLOR_LABELS = {
    "BLUE":    "Bleu",
    "WHITE":   "Blanc",
    "RED":     "Rouge",
    "UNKNOWN": "Inconnu",
}

COLOR_EMOJI = {
    "BLUE":    "🔵",
    "WHITE":   "⚪",
    "RED":     "🔴",
    "UNKNOWN": "❓",
}

HEADERS = {"User-Agent": "heating-advisor/1.0"}


def _fetch_day(endpoint: str) -> dict:
    """Appelle l'API et retourne la couleur du jour demandé."""
    url = f"{BASE_URL}/jourTempo/{endpoint}"
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

        code = data.get("codeJour")
        color = COLOR_MAP.get(code, "UNKNOWN")
        return {"color": color, "label": COLOR_LABELS[color], "emoji": COLOR_EMOJI[color], "raw": data}
    except Exception as e:
        logger.warning("API Tempo (%s) échouée : %s", endpoint, e)
        return {"color": "UNKNOWN", "label": COLOR_LABELS["UNKNOWN"], "emoji": COLOR_EMOJI["UNKNOWN"], "raw": {}}


def get_today() -> dict:
    return _fetch_day("today")


def get_tomorrow() -> dict:
    return _fetch_day("tomorrow")


def is_hp(hour: int, hp_start: int, hp_end: int) -> bool:
    """Retourne True si l'heure donnée est en Heure Pleine."""
    return hp_start <= hour < hp_end


def get_current_period(hp_start: int, hp_end: int) -> str:
    """Retourne 'HP' ou 'HC' selon l'heure actuelle."""
    return "HP" if is_hp(datetime.now().hour, hp_start, hp_end) else "HC"


def get_tempo_info(hp_start: int, hp_end: int) -> dict:
    """Retourne un dictionnaire complet avec les infos Tempo du jour et de demain."""
    today = get_today()
    tomorrow = get_tomorrow()
    period = get_current_period(hp_start, hp_end)
    hour = datetime.now().hour

    return {
        "today": today,
        "tomorrow": tomorrow,
        "current_period": period,
        "current_hour": hour,
        "hp_start": hp_start,
        "hp_end": hp_end,
    }
