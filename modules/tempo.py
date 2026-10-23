"""
Récupération de la couleur EDF Tempo (Bleu / Blanc / Rouge).
API utilisée : api-couleur-tempo.fr (communautaire, gratuite, sans clé)
"""

import json
import logging
import os
import urllib.request
from datetime import date, datetime, timedelta

from modules import jsonstore

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


CACHE_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "tempo_cache.json")
CACHE_RETENTION_DAYS = 30
FETCH_ATTEMPTS = 2


def _day_result(color: str, raw: dict) -> dict:
    return {"color": color, "label": COLOR_LABELS[color], "emoji": COLOR_EMOJI[color], "raw": raw}


def _cached_color(day: str) -> str | None:
    cache = jsonstore.load(CACHE_FILE, {})
    return cache.get(day) if isinstance(cache, dict) else None


def _store_color(day: str, color: str) -> None:
    """Mémorise une couleur publiée (elle ne change plus) et purge les entrées anciennes."""
    cutoff = (date.today() - timedelta(days=CACHE_RETENTION_DAYS)).isoformat()

    def _apply(cache):
        cache = cache if isinstance(cache, dict) else {}
        cache[day] = color
        return {d: c for d, c in cache.items() if d >= cutoff}

    try:
        jsonstore.update(CACHE_FILE, _apply, {})
    except OSError as e:
        logger.warning("Cache Tempo : écriture impossible : %s", e)


def _fetch_day(endpoint: str) -> dict:
    """Retourne la couleur du jour demandé ("today" ou "tomorrow").

    Une couleur publiée est définitive : elle est servie depuis le cache local, ce qui évite
    les appels répétés et protège d'une panne de l'API dans la journée.
    """
    day = (date.today() + timedelta(days=1 if endpoint == "tomorrow" else 0)).isoformat()
    cached = _cached_color(day)
    if cached in COLOR_MAP.values():
        return _day_result(cached, {"dateJour": day, "source": "cache"})

    url = f"{BASE_URL}/jourTempo/{endpoint}"
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            color = COLOR_MAP.get(data.get("codeJour"), "UNKNOWN")
            # Ne mémoriser qu'une couleur publiée et datée du jour attendu
            if color != "UNKNOWN" and data.get("dateJour", day) == day:
                _store_color(day, color)
            return _day_result(color, data)
        except Exception as e:
            logger.warning("API Tempo (%s) échouée (tentative %d/%d) : %s", endpoint, attempt, FETCH_ATTEMPTS, e)
    return _day_result("UNKNOWN", {})


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
