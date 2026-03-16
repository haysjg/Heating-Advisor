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
# Structure HTML de la page d'observations :
#   - Tableau horaire : bgcolor="#EBFAF7" — colonnes : Heure|Néb.|Temps|Visi|Température|...
#   - Ligne la plus récente = rows[1] (ordre décroissant)
#   - Encodage : ISO-8859-1

def get_temperature_meteociel(city: str, postal_code: str, forced_url: str = "") -> float | None:
    """Récupère la dernière température observée depuis la page d'observations météociel.fr."""
    if not forced_url:
        logger.warning("Météociel : URL non configurée, scraping ignoré")
        return None
    try:
        from bs4 import BeautifulSoup
        req = urllib.request.Request(forced_url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()

        soup = BeautifulSoup(raw.decode("iso-8859-1", errors="replace"), "lxml")

        # Tableau des observations horaires (identifié par son bgcolor unique)
        hourly_table = soup.find("table", attrs={"bgcolor": "#EBFAF7"})
        if not hourly_table:
            logger.warning("Météociel : tableau horaire introuvable")
            return None

        rows = hourly_table.find_all("tr")
        if len(rows) < 2:
            logger.warning("Météociel : pas de données dans le tableau")
            return None

        # Première ligne de données = observation la plus récente
        cells = rows[1].find_all("td")
        if len(cells) < 5:
            logger.warning("Météociel : structure de ligne inattendue (%d cellules)", len(cells))
            return None

        temp_text = cells[4].get_text(strip=True)  # colonne 4 = Température
        m = re.search(r"(-?\d+(?:\.\d+)?)", temp_text)
        if not m:
            logger.warning("Météociel : température non parsée depuis '%s'", temp_text)
            return None

        temp = float(m.group(1))
        hour_text = cells[0].get_text(strip=True)
        logger.info("Météociel : %.1f°C (obs. %s)", temp, hour_text)
        return temp

    except Exception as e:
        logger.warning("Scraping météociel échoué : %s", e)
    return None


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


def get_hourly_forecast(lat: float, lon: float, hours: int = 48) -> list:
    """
    Retourne les températures horaires pour les prochaines `hours` heures.
    Format : [{"time": "2026-03-16T14:00", "temp": 11.2}, ...]
    """
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&hourly=temperature_2m"
        f"&forecast_days=3"
        f"&timezone=Europe%2FParis"
    )
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

        now = datetime.now()
        result = []
        for time_str, temp in zip(data["hourly"]["time"], data["hourly"]["temperature_2m"]):
            dt = datetime.fromisoformat(time_str)
            if dt >= now.replace(minute=0, second=0, microsecond=0):
                result.append({"time": time_str, "temp": temp})
            if len(result) >= hours:
                break
        return result
    except Exception as e:
        logger.warning("Open-Meteo prévision horaire échouée : %s", e)
    return []


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
