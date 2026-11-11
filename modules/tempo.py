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


def get_current_period(hp_start: int, hp_end: int, now: datetime | None = None) -> str:
    """Retourne 'HP' ou 'HC' selon l'heure (actuelle par défaut).

    Heure entière : 05:59 → HC, 06:00 → HP (tarif réellement en vigueur).
    """
    now = now or datetime.now()
    return "HP" if is_hp(now.hour, hp_start, hp_end) else "HC"


def at_time(tempo: dict, hp_start: int, hp_end: int, now: datetime | None = None) -> dict:
    """Copie des infos Tempo avec la période HP/HC et l'heure recalculées pour `now`.

    Les couleurs (today/tomorrow) viennent du dernier appel API ; seule la partie
    dépendant de l'heure est recalculée, sans aucun appel réseau.
    """
    now = now or datetime.now()
    return {
        **tempo,
        "current_period": get_current_period(hp_start, hp_end, now),
        "current_hour": now.hour,
        "hp_start": hp_start,
        "hp_end": hp_end,
    }


def get_tempo_info(hp_start: int, hp_end: int) -> dict:
    """Retourne un dictionnaire complet avec les infos Tempo du jour et de demain."""
    return at_time({"today": get_today(), "tomorrow": get_tomorrow()}, hp_start, hp_end)


# ── Quota de la saison (1er septembre → 31 août) ──────────────
SEASON_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "tempo_season.json")
SEASON_QUOTAS = {"RED": 22, "WHITE": 43}
SEASON_REFRESH_HOURS = 3   # les couleurs publiées ne changent que vers 11h (J+1)
SEASON_FETCH_TIMEOUT = 5


def season_of(day: date) -> str:
    """Saison Tempo contenant `day` : « 2026-2027 » du 1er sept. 2026 au 31 août 2027."""
    start = day.year if day.month >= 9 else day.year - 1
    return f"{start}-{start + 1}"


def _fetch_season(season: str) -> list | None:
    """Jours de la saison publiés par l'API ({dateJour, codeJour}), None en cas d'échec."""
    try:
        req = urllib.request.Request(f"{BASE_URL}/joursTempo?periode={season}", headers=HEADERS)
        with urllib.request.urlopen(req, timeout=SEASON_FETCH_TIMEOUT) as resp:
            days = json.loads(resp.read())
        return days if isinstance(days, list) else None
    except Exception as e:
        logger.warning("API Tempo (saison %s) échouée : %s", season, e)
        return None


def _count_colors(days: list) -> dict:
    counts = {"BLUE": 0, "WHITE": 0, "RED": 0}
    for d in days:
        color = COLOR_MAP.get(d.get("codeJour"))
        if color:
            counts[color] += 1
    return counts


def _season_summary(season: str, counts: dict) -> dict:
    start = int(season[:4])
    season_days = (date(start + 1, 9, 1) - date(start, 9, 1)).days
    quotas = {**SEASON_QUOTAS, "BLUE": season_days - sum(SEASON_QUOTAS.values())}
    return {
        "season": season,
        "label": f"{start}-{str(start + 1)[2:]}",
        "colors": {
            c: {"used": counts[c], "total": quotas[c], "left": max(quotas[c] - counts[c], 0)}
            for c in ("RED", "WHITE", "BLUE")
        },
    }


def get_season_stats(now: datetime | None = None) -> dict | None:
    """Jours Tempo tirés et restants sur la saison en cours (quotas 22 rouges / 43 blancs).

    Les couleurs publiées (demain compris, une fois annoncé) sont comptées comme tirées.
    Le résultat est mis en cache quelques heures ; en cas de panne de l'API, le dernier
    décompte connu de la saison est servi. None si aucun décompte n'est disponible.
    """
    now = now or datetime.now()
    season = season_of(now.date())
    cache = jsonstore.load(SEASON_FILE, {})
    cache = cache if isinstance(cache, dict) and cache.get("season") == season else {}

    try:
        last_try = datetime.fromisoformat(cache.get("checked_at", ""))
    except ValueError:
        last_try = None
    if last_try is None or now - last_try >= timedelta(hours=SEASON_REFRESH_HOURS):
        days = _fetch_season(season)
        counts = _count_colors(days) if days is not None else None
        cache = {**cache, "season": season, "checked_at": now.isoformat(timespec="seconds")}
        if counts is not None:
            cache["counts"] = counts
        try:
            jsonstore.save(SEASON_FILE, cache)
        except OSError as e:
            logger.warning("Cache saison Tempo : écriture impossible : %s", e)

    counts = cache.get("counts")
    return _season_summary(season, counts) if isinstance(counts, dict) else None


# ── Couleurs passées, jour par jour (comparatif des options tarifaires) ──
HISTORY_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "tempo_history.json")


def get_day_colors(start: date, end: date, now: datetime | None = None) -> dict:
    """Couleur Tempo de chaque jour de [start, end] : {"AAAA-MM-JJ": "BLUE" | "WHITE" | "RED"}.

    Les couleurs publiées ne changent plus : elles sont gardées dans data/tempo_history.json.
    Une saison n'est redemandée à l'API que s'il lui manque des jours passés, au plus toutes les
    SEASON_REFRESH_HOURS heures. Les jours sans couleur connue sont absents du résultat.
    """
    now = now or datetime.now()
    end = min(end, now.date())
    cache = jsonstore.load(HISTORY_FILE, {})
    cache = cache if isinstance(cache, dict) else {}
    colors = cache.get("colors") if isinstance(cache.get("colors"), dict) else {}
    checked = cache.get("checked") if isinstance(cache.get("checked"), dict) else {}

    missing_seasons = []
    day = start
    while day <= end:
        if day.isoformat() not in colors:
            season = season_of(day)
            if season not in missing_seasons:
                missing_seasons.append(season)
        day += timedelta(days=1)

    changed = False
    for season in missing_seasons:
        try:
            last_try = datetime.fromisoformat(checked.get(season, ""))
        except ValueError:
            last_try = None
        if last_try is not None and now - last_try < timedelta(hours=SEASON_REFRESH_HOURS):
            continue
        days = _fetch_season(season)
        checked[season] = now.isoformat(timespec="seconds")
        changed = True
        for d in days or []:
            color = COLOR_MAP.get(d.get("codeJour"))
            if color and isinstance(d.get("dateJour"), str):
                colors[d["dateJour"]] = color

    if changed:
        try:
            jsonstore.save(HISTORY_FILE, {"colors": colors, "checked": checked}, indent=None)
        except OSError as e:
            logger.warning("Historique Tempo : écriture impossible : %s", e)

    first, last = start.isoformat(), end.isoformat()
    return {d: c for d, c in colors.items() if first <= d <= last}
