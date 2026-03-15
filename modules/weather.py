"""
Récupération de la température extérieure.
Source principale : météociel.fr (scraping)
Fallback         : Open-Meteo API (gratuit, sans clé)
"""

import json
import logging
import re
import urllib.request
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


# ── Météociel.fr ─────────────────────────────────────────────

def _meteociel_search_url(city: str, postal_code: str) -> str | None:
    """Cherche la ville sur météociel et retourne l'URL de sa page."""
    import urllib.parse

    query = urllib.parse.quote_plus(city)
    search_url = f"https://www.meteociel.fr/villes/communes.php?q={query}&pays=fr"
    try:
        req = urllib.request.Request(search_url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        # Chercher un lien de prévisions contenant le code postal ou le nom de ville
        pattern = r'href="(/previsions/\d+/[^"]+\.htm)"'
        matches = re.findall(pattern, html)
        city_slug = city.lower().replace(" ", "-").replace("'", "-")

        for m in matches:
            if city_slug in m or postal_code in html:
                return "https://www.meteociel.fr" + m

        # Prendre le premier résultat si pas de correspondance exacte
        if matches:
            return "https://www.meteociel.fr" + matches[0]

    except Exception as e:
        logger.warning("Recherche météociel échouée : %s", e)

    return None


def _scrape_meteociel(url: str) -> float | None:
    """Scrape la température actuelle depuis une page météociel.fr."""
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        # Patterns possibles pour la température sur météociel
        patterns = [
            r'Temp[ée]rature\s*:\s*([-\d]+(?:\.\d+)?)\s*°C',
            r'"temperature"\s*:\s*([-\d]+(?:\.\d+)?)',
            r'<b>([-\d]+(?:\.\d+)?)\s*°C</b>',
            r'([-\d]+(?:\.\d+)?)\s*°C',
        ]
        for p in patterns:
            m = re.search(p, html, re.IGNORECASE)
            if m:
                temp = float(m.group(1))
                if -30 <= temp <= 50:
                    logger.info("Météociel : %.1f°C (pattern: %s)", temp, p)
                    return temp

    except Exception as e:
        logger.warning("Scraping météociel échoué : %s", e)

    return None


def get_temperature_meteociel(city: str, postal_code: str, forced_url: str = "") -> float | None:
    """Récupère la température via météociel.fr."""
    url = forced_url or _meteociel_search_url(city, postal_code)
    if not url:
        return None
    return _scrape_meteociel(url)


# ── Open-Meteo (fallback) ─────────────────────────────────────

def get_temperature_openmeteo(lat: float, lon: float) -> float | None:
    """Récupère la température actuelle via l'API Open-Meteo (gratuit, sans clé)."""
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&current=temperature_2m"
        f"&timezone=Europe%2FParis"
    )
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        temp = data["current"]["temperature_2m"]
        logger.info("Open-Meteo : %.1f°C", temp)
        return float(temp)
    except Exception as e:
        logger.warning("Open-Meteo échoué : %s", e)
    return None


def get_tomorrow_forecast_openmeteo(lat: float, lon: float, hp_start: int = 6, hp_end: int = 22) -> dict | None:
    """
    Récupère les prévisions de demain via Open-Meteo.
    Retourne la température min, max et moyenne sur la plage de chauffage (HP).
    """
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&hourly=temperature_2m"
        f"&forecast_days=2"
        f"&timezone=Europe%2FParis"
    )
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

        times = data["hourly"]["time"]
        temps = data["hourly"]["temperature_2m"]
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

        # Filtrer les heures de demain dans la plage HP
        hp_temps = [
            t for time, t in zip(times, temps)
            if time.startswith(tomorrow) and hp_start <= int(time[11:13]) < hp_end
        ]

        if not hp_temps:
            return None

        avg = round(sum(hp_temps) / len(hp_temps), 1)
        logger.info("Open-Meteo demain (plage %dh-%dh) : min=%.1f max=%.1f moy=%.1f",
                    hp_start, hp_end, min(hp_temps), max(hp_temps), avg)
        return {
            "temperature": avg,
            "temp_min": round(min(hp_temps), 1),
            "temp_max": round(max(hp_temps), 1),
            "source": "Open-Meteo (prévision)",
        }
    except Exception as e:
        logger.warning("Open-Meteo prévision demain échouée : %s", e)
    return None


# ── Point d'entrée ────────────────────────────────────────────

def get_current_temperature(config: dict) -> dict:
    """
    Retourne la température extérieure avec la source utilisée.
    config = LOCATION dict depuis config.py
    """
    result = {"temperature": None, "source": None, "error": None, "timestamp": datetime.now().isoformat()}

    # 1. Essai météociel.fr
    temp = get_temperature_meteociel(
        config["city"],
        config["postal_code"],
        config.get("meteociel_url", ""),
    )
    if temp is not None:
        result["temperature"] = temp
        result["source"] = "météociel.fr"
        return result

    # 2. Fallback Open-Meteo
    temp = get_temperature_openmeteo(config["latitude"], config["longitude"])
    if temp is not None:
        result["temperature"] = temp
        result["source"] = "Open-Meteo"
        return result

    result["error"] = "Impossible de récupérer la température (météociel.fr et Open-Meteo inaccessibles)"
    return result


def get_tomorrow_weather(config: dict) -> dict:
    """Retourne la prévision météo de demain (plage de chauffage HP)."""
    result = {"temperature": None, "temp_min": None, "temp_max": None,
              "source": None, "error": None}

    forecast = get_tomorrow_forecast_openmeteo(
        config["latitude"], config["longitude"],
        config.get("hp_start", 6), config.get("hp_end", 22),
    )
    if forecast:
        result.update(forecast)
        return result

    result["error"] = "Prévision météo de demain indisponible"
    return result
