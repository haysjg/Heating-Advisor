"""
Récupération de la température extérieure.
Sources mesurées : stations Infoclimat (open data), puis Météo-France (API DPObs)
Fallback         : Open-Meteo API (modèle, gratuit, sans clé)
"""

import csv
import io
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from modules.crypto import decrypt_password

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


# Mesure plus ancienne que ça : ignorée (station en panne, valeur figée)
OBSERVATION_MAX_AGE_MINUTES = 90
# Dernière mesure resservie avant de basculer sur le modèle Open-Meteo
LAST_MEASURED_MAX_AGE_MINUTES = 60
# Pause d'une source après un HTTP 429
RATE_LIMIT_BACKOFF_MINUTES = 60

# Dernière température obtenue : resservie si toutes les sources échouent ponctuellement
LAST_KNOWN_MAX_AGE_HOURS = 3
_last_known: dict | None = None
_last_measured: dict | None = None
_paused_until: dict[str, datetime] = {}

# Les API officielles reçoivent un User-Agent honnête (pas d'imitation de navigateur)
API_HEADERS = {"User-Agent": "heating-advisor/1.0 (usage personnel)"}


def _is_paused(source: str) -> bool:
    until = _paused_until.get(source)
    if until and datetime.now() < until:
        logger.debug("%s : en pause jusqu'à %s (HTTP 429)", source, until.strftime("%Hh%M"))
        return True
    return False


class SourceError(Exception):
    """Échec d'une source de mesure. auth=True : identifiants refusés (clé, IP, abonnement)."""

    def __init__(self, message: str, auth: bool = False):
        super().__init__(message)
        self.auth = auth


def _source_error(source: str, e: Exception) -> SourceError:
    """Traduit une exception réseau en SourceError, sans jamais l'URL (Infoclimat y met la clé)."""
    if isinstance(e, SourceError):
        return e
    if isinstance(e, urllib.error.HTTPError):
        if e.code == 429:
            _paused_until[source] = datetime.now() + timedelta(minutes=RATE_LIMIT_BACKOFF_MINUTES)
            return SourceError(f"HTTP 429 — pause jusqu'à {_paused_until[source].strftime('%Hh%M')}")
        return SourceError(f"HTTP {e.code}", auth=e.code in (401, 403))
    if isinstance(e, urllib.error.URLError):
        return SourceError(str(e.reason))
    return SourceError(type(e).__name__)


def _log_http_failure(source: str, e: Exception) -> None:
    logger.warning("%s échoué : %s", source, _source_error(source, e))


def _parse_utc(value: str) -> datetime:
    """'2026-11-01T07:48:00Z' ou '2026-11-01 07:48:00' (UTC) → datetime UTC aware."""
    dt = datetime.fromisoformat(value.strip().replace(" ", "T").replace("Z", ""))
    return dt.replace(tzinfo=timezone.utc)


def _age_minutes(observed_at: datetime) -> float:
    return (datetime.now(timezone.utc) - observed_at).total_seconds() / 60


# ── Météo-France : API « Données d'observation » (DPObs) ──────
# Mesures du réseau officiel, pas de 6 min. Authentification OAuth2 : l'« Application ID »
# du portail (client_id:secret en base64) s'échange contre un jeton valable 1 h.
#   POST https://portail-api.meteofrance.fr/token   grant_type=client_credentials
#   GET  .../DPObs/v2/station/infrahoraire-6m?id_station=<8 chiffres>&format=json
#        → [{"validity_time": "2026-11-01T07:48:00Z", "t": 283.55, ...}]   (t en Kelvin)

METEOFRANCE_TOKEN_URL = "https://portail-api.meteofrance.fr/token"
METEOFRANCE_OBS_URL = "https://public-api.meteofrance.fr/public/DPObs/v2/station/infrahoraire-6m"
METEOFRANCE_STATIONS_URL = "https://public-api.meteofrance.fr/public/DPObs/v2/liste-stations"
_mf_token: dict | None = None
_mf_station_names: dict[str, str] | None = None


def _meteofrance_token(application_id: str, renew: bool = False) -> str:
    global _mf_token
    if not renew and _mf_token and datetime.now() < _mf_token["expires"]:
        return _mf_token["value"]
    req = urllib.request.Request(
        METEOFRANCE_TOKEN_URL,
        data=b"grant_type=client_credentials",
        headers={**API_HEADERS, "Authorization": f"Basic {application_id}",
                 "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403):
            raise SourceError(f"Application ID refusé (HTTP {e.code})", auth=True) from None
        raise
    lifetime = int(data.get("expires_in", 3600))
    _mf_token = {
        "value": data["access_token"],
        "expires": datetime.now() + timedelta(seconds=max(lifetime - 300, 60)),
    }
    return _mf_token["value"]


def meteofrance_station_name(station_id: str, application_id: str) -> str:
    """Nom usuel d'une station (liste CSV « Id_station;...;Nom_usuel;... » lue une fois)."""
    global _mf_station_names
    if _mf_station_names is None:
        try:
            req = urllib.request.Request(METEOFRANCE_STATIONS_URL, headers={
                **API_HEADERS, "Authorization": f"Bearer {_meteofrance_token(application_id)}"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                text = resp.read().decode("iso-8859-1")
            _mf_station_names = {
                row["Id_station"]: row["Nom_usuel"].replace("_", " ").title()
                for row in csv.DictReader(io.StringIO(text), delimiter=";")
                if row.get("Id_station") and row.get("Nom_usuel")
            }
        except Exception as e:
            _log_http_failure("Météo-France (liste des stations)", e)
            return station_id  # nouvel essai au prochain appel
    return _mf_station_names.get(station_id, station_id)


def fetch_meteofrance(station_id: str, application_id: str) -> dict:
    """Dernière mesure d'une station Météo-France : {"temperature", "observed_at" (UTC)}.

    Lève SourceError (auth=True si l'Application ID ou l'abonnement est refusé).
    """
    url = f"{METEOFRANCE_OBS_URL}?{urllib.parse.urlencode({'id_station': station_id, 'format': 'json'})}"
    try:
        for attempt in range(2):
            token = _meteofrance_token(application_id, renew=attempt > 0)
            req = urllib.request.Request(url, headers={**API_HEADERS, "Authorization": f"Bearer {token}"})
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read())
                break
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 0:
                    continue  # jeton expiré ou révoqué : on en redemande un
                if e.code in (401, 403):
                    raise SourceError(f"accès refusé (HTTP {e.code}) — abonnement expiré ?", auth=True) from None
                raise
    except Exception as e:
        raise _source_error("Météo-France", e) from None

    rows = data if isinstance(data, list) else [data]
    rows = [r for r in rows if isinstance(r, dict) and r.get("t") is not None and r.get("validity_time")]
    if not rows:
        raise SourceError(f"station {station_id} : aucune température dans la réponse")
    latest = max(rows, key=lambda r: r["validity_time"])
    temp = round(float(latest["t"]) - 273.15, 1)
    observed_at = _parse_utc(latest["validity_time"])
    logger.info("Météo-France %s : %.1f°C (mesure %s UTC)", station_id, temp, observed_at.strftime("%Hh%M"))
    return {"temperature": temp, "observed_at": observed_at}


def get_temperature_meteofrance(station_id: str, application_id: str) -> dict | None:
    """Comme fetch_meteofrance, mais None en cas d'échec (journalisé)."""
    if _is_paused("Météo-France"):
        return None
    try:
        return fetch_meteofrance(station_id, application_id)
    except SourceError as e:
        logger.warning("Météo-France échoué : %s", e)
    return None


# ── Infoclimat open data ──────────────────────────────────────
# Stations officielles et réseau StatIC, relevés toutes les 10-15 min. Association bénévole :
# toutes les stations en UNE requête par rafraîchissement (30 min), jamais de rafale.
#   GET https://www.infoclimat.fr/opendata/?version=2&method=get&format=json
#       &stations[]=<id>...&start=YYYY-MM-DD&end=YYYY-MM-DD&token=<clé>   (dates UTC, incluses)
#   → {"status": "OK", "errors": [], "stations": [...],
#      "hourly": {"<id>": [{"id_station": ..., "dh_utc": "2026-11-01 07:50:00", "temperature": "9.8"}],
#                 "_params": [...]}}
# La clé voyage dans l'URL : l'URL n'est jamais journalisée.

INFOCLIMAT_URL = "https://www.infoclimat.fr/opendata/"


# Refus de clé : « Could not authenticate request » (sans clé), « No account found for this
# token: '…' » (clé inconnue, HTTP 400), « Wrong ip address » (clé liée à une autre IP)
_INFOCLIMAT_AUTH_RE = re.compile(r"authenticate|token|ip address|unauthori|forbidden", re.IGNORECASE)


def _infoclimat_error(body: str, token: str, status: int | None = None) -> SourceError:
    text = " ".join(body.replace(token, "<clé>").split())[:120] if token else body[:120]
    if status in (401, 403) or _INFOCLIMAT_AUTH_RE.search(body):
        return SourceError(f"clé refusée ({text or f'HTTP {status}'})", auth=True)
    return SourceError(text or f"HTTP {status}")


def fetch_infoclimat(station_ids: list[str], token: str) -> dict:
    """Dernière mesure de chaque station : {id: {"temperature", "observed_at" (UTC), "name"}}.

    Lève SourceError (auth=True si la clé est refusée, par exemple après un changement d'IP).
    """
    today = datetime.now(timezone.utc).date()
    params = [("version", "2"), ("method", "get"), ("format", "json")]
    params += [("stations[]", sid) for sid in station_ids]
    params += [("start", (today - timedelta(days=1)).isoformat()), ("end", today.isoformat()), ("token", token)]
    req = urllib.request.Request(f"{INFOCLIMAT_URL}?{urllib.parse.urlencode(params)}", headers=API_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise _source_error("Infoclimat", e) from None
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        raise _infoclimat_error(body, token, e.code) from None
    except Exception as e:
        raise _source_error("Infoclimat", e) from None
    try:
        data = json.loads(body)
    except ValueError:
        raise _infoclimat_error(body, token) from None
    if not isinstance(data, dict) or data.get("errors") or data.get("status", "OK") != "OK":
        errors = "; ".join(map(str, (data or {}).get("errors") or [])) if isinstance(data, dict) else ""
        raise _infoclimat_error(errors or "statut inattendu", token)

    names = {
        str(s.get("id", "")).lower(): s.get("name") or s.get("libelle")
        for s in data.get("stations") or [] if isinstance(s, dict)
    }
    hourly = data.get("hourly") or {}
    result = {}
    for sid in station_ids:
        rows = [
            r for key, rs in hourly.items() if key != "_params" and isinstance(rs, list)
            for r in rs
            if isinstance(r, dict) and str(r.get("id_station", key)).lower() == sid.lower()
            and r.get("temperature") not in (None, "") and r.get("dh_utc")
        ]
        if not rows:
            continue
        latest = max(rows, key=lambda r: r["dh_utc"])
        try:
            temp = round(float(latest["temperature"]), 1)
            observed_at = _parse_utc(latest["dh_utc"])
        except ValueError:
            continue
        result[sid] = {"temperature": temp, "observed_at": observed_at, "name": names.get(sid.lower())}
        logger.info("Infoclimat %s : %.1f°C (mesure %s UTC)", sid, temp, observed_at.strftime("%Hh%M"))
    return result


def get_temperatures_infoclimat(station_ids: list[str], token: str) -> dict:
    """Comme fetch_infoclimat, mais {} en cas d'échec (journalisé)."""
    if not station_ids or _is_paused("Infoclimat"):
        return {}
    try:
        return fetch_infoclimat(station_ids, token)
    except SourceError as e:
        logger.warning("Infoclimat échoué : %s", e)
    return {}


def probe_sources(observations: dict) -> dict[str, SourceError | None]:
    """Teste chaque source configurée, même celle qui ne sert qu'en secours.

    Retourne {source: None si elle répond, SourceError sinon}. Une source en pause (429)
    ou non configurée est absente du résultat.
    """
    results: dict[str, SourceError | None] = {}
    ic_token = decrypt_password(observations.get("infoclimat_token", ""))
    ic_stations = observations.get("infoclimat_stations") or []
    if ic_token and ic_stations and not _is_paused("Infoclimat"):
        try:
            found = fetch_infoclimat(ic_stations, ic_token)
            results["Infoclimat"] = None if found else SourceError("aucune mesure pour les stations configurées")
        except SourceError as e:
            results["Infoclimat"] = e

    mf_id = decrypt_password(observations.get("meteofrance_application_id", ""))
    mf_stations = observations.get("meteofrance_stations") or []
    if mf_id and mf_stations and not _is_paused("Météo-France"):
        try:
            fetch_meteofrance(mf_stations[0], mf_id)
            results["Météo-France"] = None
        except SourceError as e:
            results["Météo-France"] = e
    return results


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
    Retry 3 fois avec backoff (5s, 10s) pour absorber les timeouts au réveil réseau.
    """
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&hourly=temperature_2m"
        f"&forecast_days=2"
        f"&timezone=Europe%2FParis"
    )
    delays = [5, 10]
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())

            times = data["hourly"]["time"]
            temps = data["hourly"]["temperature_2m"]
            tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

            # Filtrer les heures de demain dans la plage HP
            hp_temps = [
                t for time_str, t in zip(times, temps)
                if time_str.startswith(tomorrow) and hp_start <= int(time_str[11:13]) < hp_end
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
            logger.warning("Open-Meteo prévision demain échouée (tentative %d/3) : %s", attempt + 1, e)
            if attempt < 2:
                time.sleep(delays[attempt])
    return None


# ── Point d'entrée ────────────────────────────────────────────

def _measurements(observations: dict):
    """Mesures de station, dans l'ordre de préférence : (libellé, {"temperature", "observed_at"})."""
    # Infoclimat d'abord : la station de Massy (2 km) est la plus représentative de la maison
    ic_token = decrypt_password(observations.get("infoclimat_token", ""))
    ic_stations = observations.get("infoclimat_stations") or []
    if ic_token and ic_stations:
        found = get_temperatures_infoclimat(ic_stations, ic_token)
        for sid in ic_stations:
            if sid in found:
                yield f"Infoclimat {found[sid]['name'] or sid}", found[sid]

    mf_id = decrypt_password(observations.get("meteofrance_application_id", ""))
    if mf_id:
        for sid in observations.get("meteofrance_stations") or []:
            obs = get_temperature_meteofrance(sid, mf_id)
            if obs:
                yield f"Météo-France {meteofrance_station_name(sid, mf_id)}", obs


def get_current_temperature(config: dict, observations: dict | None = None) -> dict:
    """
    Retourne la température extérieure avec la source utilisée.
    config = LOCATION dict depuis config.py, observations = OBSERVATIONS dict.

    Ordre : stations Infoclimat → stations Météo-France → dernière mesure (≤ 1 h)
    → Open-Meteo (modèle) → dernière valeur connue (≤ 3 h).
    "measured" distingue une mesure de station d'une valeur de modèle.
    """
    global _last_known, _last_measured
    observations = observations or {}
    now = datetime.now()
    result = {"temperature": None, "source": None, "measured": False, "observed_at": None,
              "error": None, "timestamp": now.isoformat()}
    max_age = float(observations.get("max_age_minutes", OBSERVATION_MAX_AGE_MINUTES))

    # 1. Mesures de station récentes
    for label, obs in _measurements(observations):
        age = _age_minutes(obs["observed_at"])
        if age > max_age:
            logger.info("%s : mesure trop ancienne (%d min), ignorée", label, age)
            continue
        observed = obs["observed_at"].astimezone().strftime("%Hh%M")
        result.update(temperature=obs["temperature"], source=label, measured=True, observed_at=observed)
        _last_measured = {"temperature": obs["temperature"], "source": label, "observed_at": observed, "at": now}
        _last_known = {**_last_measured, "measured": True}
        return result

    # 2. Dernière mesure, si assez récente : plus fiable que le modèle
    if _last_measured and now - _last_measured["at"] <= timedelta(minutes=LAST_MEASURED_MAX_AGE_MINUTES):
        logger.warning("Température extérieure : stations HS, dernière mesure %.1f°C (%s)",
                       _last_measured["temperature"], _last_measured["observed_at"])
        result.update(temperature=_last_measured["temperature"], source=f"{_last_measured['source']} (dernière mesure)",
                      measured=True, observed_at=_last_measured["observed_at"])
        return result

    # 3. Fallback Open-Meteo (modèle)
    temp = get_temperature_openmeteo(config["latitude"], config["longitude"])
    if temp is not None:
        logger.warning("Température extérieure : aucune mesure de station, modèle Open-Meteo utilisé (%.1f°C)", temp)
        result["temperature"] = temp
        result["source"] = "Open-Meteo"
        _last_known = {"temperature": temp, "source": "Open-Meteo", "observed_at": None, "measured": False, "at": now}
        return result

    # 4. Dernière valeur connue, si assez récente
    if _last_known and now - _last_known["at"] <= timedelta(hours=LAST_KNOWN_MAX_AGE_HOURS):
        at = _last_known["at"].strftime("%Hh%M")
        logger.warning("Température extérieure : sources HS, dernière valeur connue %.1f°C (%s)", _last_known["temperature"], at)
        result.update(temperature=_last_known["temperature"], source=f"{_last_known['source']} (dernière valeur, {at})",
                      measured=_last_known["measured"], observed_at=_last_known["observed_at"])
        return result

    result["error"] = "Impossible de récupérer la température (stations de mesure et Open-Meteo inaccessibles)"
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
