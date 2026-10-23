"""
Client Home Assistant — contrôle optionnel du poêle via l'API REST HA.
N'est utilisé que si HOME_ASSISTANT.enabled = True dans la config.
"""

import logging
import threading
import time

import requests

from modules.crypto import decrypt_password

logger = logging.getLogger(__name__)


# Session HTTP partagée : connexions persistantes vers HA au lieu d'une connexion TCP par appel
_session = requests.Session()

READ_TIMEOUT = 5         # lecture d'état (HA local)
SERVICE_TIMEOUT = 15     # appel de service (peut transiter par le cloud du fabricant)
STATES_TTL_SECONDS = 5   # durée de validité de l'instantané de tous les états

_states_lock = threading.Lock()
_states_cache = {"url": None, "at": 0.0, "states": {}}


def _request(url: str, token: str, method: str = "GET", body: dict = None) -> dict:
    resp = _session.request(
        method,
        url,
        json=body,
        headers={"Authorization": f"Bearer {decrypt_password(token)}"},
        timeout=SERVICE_TIMEOUT if method != "GET" else READ_TIMEOUT,
    )
    resp.raise_for_status()
    if method != "GET":
        invalidate_states()
    return resp.json()


def invalidate_states() -> None:
    """Oublie l'instantané des états (après une commande, la lecture suivante interroge HA)."""
    with _states_lock:
        _states_cache["at"] = 0.0


def _all_states(cfg: dict) -> dict:
    """Instantané de tous les états HA (un seul GET /api/states), partagé quelques secondes
    entre les routes et les jobs au lieu d'un appel par entité."""
    base = cfg["url"].rstrip("/")
    with _states_lock:
        if _states_cache["url"] == base and time.monotonic() - _states_cache["at"] < STATES_TTL_SECONDS:
            return _states_cache["states"]
        states = {s["entity_id"]: s for s in _request(f"{base}/api/states", cfg["token"])}
        _states_cache.update(url=base, at=time.monotonic(), states=states)
        return states


def _get_entity(cfg: dict, entity_id: str) -> dict:
    """État d'une entité. Lève LookupError si l'entité n'existe pas dans HA."""
    state = _all_states(cfg).get(entity_id)
    if state is None:
        raise LookupError(f"entité {entity_id} introuvable dans Home Assistant")
    return state


def is_configured(cfg: dict) -> bool:
    """Retourne True si HA est activé et configuré."""
    return bool(
        cfg.get("enabled")
        and cfg.get("url")
        and cfg.get("token")
        and cfg.get("poele_entity_id")
    )


def turn_on(cfg: dict) -> bool:
    """Allume le poêle via HA."""
    if not is_configured(cfg):
        return False
    try:
        entity_id = cfg["poele_entity_id"]
        _request(
            f"{cfg['url'].rstrip('/')}/api/services/climate/turn_on",
            cfg["token"],
            method="POST",
            body={"entity_id": entity_id},
        )
        logger.info("HA : poêle allumé (%s)", entity_id)
        return True
    except Exception as e:
        logger.error("HA turn_on échoué : %s", e)
        return False


def turn_off(cfg: dict) -> bool:
    """Éteint le poêle via HA."""
    if not is_configured(cfg):
        return False
    try:
        entity_id = cfg["poele_entity_id"]
        _request(
            f"{cfg['url'].rstrip('/')}/api/services/climate/turn_off",
            cfg["token"],
            method="POST",
            body={"entity_id": entity_id},
        )
        logger.info("HA : poêle éteint (%s)", entity_id)
        return True
    except Exception as e:
        logger.error("HA turn_off échoué : %s", e)
        return False


def get_state(cfg: dict) -> dict | None:
    """Retourne l'état actuel de l'entité poêle."""
    if not is_configured(cfg):
        return None
    try:
        entity_id = cfg["poele_entity_id"]
        return _get_entity(cfg, entity_id)
    except Exception as e:
        logger.error("HA get_state échoué : %s", e)
        return None


def get_indoor_climate(cfg: dict) -> dict | None:
    """Retourne la température et/ou l'humidité intérieure depuis le Shelly via HA."""
    if not cfg.get("enabled") or not cfg.get("url") or not cfg.get("token"):
        return None
    temp_entity = cfg.get("shelly_temp_entity_id")
    humidity_entity = cfg.get("shelly_humidity_entity_id")
    if not temp_entity and not humidity_entity:
        return None
    result = {}
    try:
        if temp_entity:
            state = _get_entity(cfg, temp_entity)
            raw = state.get("state")
            result["temperature"] = float(raw) if raw not in (None, "unavailable", "unknown") else None
        if humidity_entity:
            state = _get_entity(cfg, humidity_entity)
            raw = state.get("state")
            result["humidity"] = float(raw) if raw not in (None, "unavailable", "unknown") else None
    except Exception as e:
        logger.error("HA get_indoor_climate échoué : %s", e)
        return None
    return result or None


def get_presence_extended(cfg: dict, person_entities: list, nearby_zone_name: str) -> str | None:
    """
    Retourne le statut de présence étendu :
    - "home"   : au moins une personne est à la maison (zone home)
    - "nearby" : personne à la maison, mais au moins une dans la zone de proximité
    - "away"   : tout le monde hors des deux zones
    - None     : erreur ou config manquante
    """
    if not cfg.get("enabled") or not cfg.get("url") or not cfg.get("token"):
        return None
    if not person_entities:
        return None
    try:
        states = []
        for entity_id in person_entities:
            state = _get_entity(cfg, entity_id)
            person_state = state.get("state", "not_home")
            states.append(person_state)
            logger.info("HA présence — %s: %s", entity_id, person_state)
        if any(s == "home" for s in states):
            return "home"
        nearby_lower = nearby_zone_name.lower()
        if any(s.lower() == nearby_lower for s in states):
            if not any(s == nearby_zone_name for s in states):
                logger.warning(
                    "HA présence — zone '%s' reconnue en ignorant la casse (config: '%s'). "
                    "Corriger nearby_zone_name dans la config.",
                    next(s for s in states if s.lower() == nearby_lower),
                    nearby_zone_name,
                )
            return "nearby"
        logger.debug("HA présence — aucun dans zone proximité '%s', états: %s", nearby_zone_name, states)
        return "away"
    except Exception as e:
        logger.error("HA get_presence_extended échoué : %s", e)
        return None


def get_presence(cfg: dict, person_entities: list) -> bool | None:
    """
    Retourne True si au moins une personne est à la maison,
    False si tout le monde est absent, None en cas d'erreur ou config manquante.
    """
    if not cfg.get("enabled") or not cfg.get("url") or not cfg.get("token"):
        return None
    if not person_entities:
        return None
    try:
        for entity_id in person_entities:
            state = _get_entity(cfg, entity_id)
            if state.get("state") == "home":
                return True
        return False
    except Exception as e:
        logger.error("HA get_presence échoué : %s", e)
        return None


def get_entity_state(cfg: dict, entity_id: str) -> dict | None:
    """Retourne l'état d'une entité HA quelconque."""
    if not cfg.get("enabled") or not cfg.get("url") or not cfg.get("token"):
        return None
    try:
        return _get_entity(cfg, entity_id)
    except Exception as e:
        logger.error("HA get_entity_state(%s) échoué : %s", entity_id, e)
        return None


def turn_off_entity(cfg: dict, entity_id: str) -> bool:
    """Éteint une entité HA (climate, switch, etc.)."""
    if not cfg.get("enabled") or not cfg.get("url") or not cfg.get("token"):
        return False
    domain = entity_id.split(".")[0]
    try:
        _request(
            f"{cfg['url'].rstrip('/')}/api/services/{domain}/turn_off",
            cfg["token"],
            method="POST",
            body={"entity_id": entity_id},
        )
        logger.info("HA : %s éteint", entity_id)
        return True
    except Exception as e:
        logger.error("HA turn_off_entity(%s) échoué : %s", entity_id, e)
        return False


def turn_on_entity(cfg: dict, entity_id: str) -> bool:
    """Allume une entité HA (climate, switch, etc.)."""
    if not cfg.get("enabled") or not cfg.get("url") or not cfg.get("token"):
        return False
    domain = entity_id.split(".")[0]
    try:
        _request(
            f"{cfg['url'].rstrip('/')}/api/services/{domain}/turn_on",
            cfg["token"],
            method="POST",
            body={"entity_id": entity_id},
        )
        logger.info("HA : %s allumé", entity_id)
        return True
    except Exception as e:
        logger.error("HA turn_on_entity(%s) échoué : %s", entity_id, e)
        return False


def is_clim_configured(cfg: dict) -> bool:
    """Retourne True si HA est activé et la clim configurée."""
    return bool(
        cfg.get("enabled")
        and cfg.get("url")
        and cfg.get("token")
        and cfg.get("clim_entity_id")
    )


def turn_on_clim(cfg: dict, target_temp: float) -> bool:
    """Allume la clim en mode chauffage et règle la température cible."""
    if not is_clim_configured(cfg):
        return False
    try:
        entity_id = cfg["clim_entity_id"]
        _request(
            f"{cfg['url'].rstrip('/')}/api/services/climate/set_hvac_mode",
            cfg["token"],
            method="POST",
            body={"entity_id": entity_id, "hvac_mode": "heat"},
        )
        _request(
            f"{cfg['url'].rstrip('/')}/api/services/climate/set_temperature",
            cfg["token"],
            method="POST",
            body={"entity_id": entity_id, "temperature": target_temp},
        )
        logger.info("HA : clim allumée en mode heat à %.1f°C (%s)", target_temp, entity_id)
        return True
    except Exception as e:
        logger.error("HA turn_on_clim échoué : %s", e)
        return False


def set_clim_temperature(cfg: dict, target_temp: float) -> bool:
    """Change la consigne de la clim sans toucher à son mode (marche/arrêt)."""
    if not is_clim_configured(cfg):
        return False
    try:
        entity_id = cfg["clim_entity_id"]
        _request(
            f"{cfg['url'].rstrip('/')}/api/services/climate/set_temperature",
            cfg["token"],
            method="POST",
            body={"entity_id": entity_id, "temperature": target_temp},
        )
        logger.info("HA : consigne clim → %.1f°C (%s)", target_temp, entity_id)
        return True
    except Exception as e:
        logger.error("HA set_clim_temperature échoué : %s", e)
        return False


def turn_off_clim(cfg: dict) -> bool:
    """Éteint la clim via HA."""
    if not is_clim_configured(cfg):
        return False
    try:
        entity_id = cfg["clim_entity_id"]
        _request(
            f"{cfg['url'].rstrip('/')}/api/services/climate/turn_off",
            cfg["token"],
            method="POST",
            body={"entity_id": entity_id},
        )
        logger.info("HA : clim éteinte (%s)", entity_id)
        return True
    except Exception as e:
        logger.error("HA turn_off_clim échoué : %s", e)
        return False


def get_clim_state(cfg: dict) -> dict | None:
    """Retourne l'état actuel de l'entité clim."""
    if not is_clim_configured(cfg):
        return None
    try:
        entity_id = cfg["clim_entity_id"]
        return _get_entity(cfg, entity_id)
    except Exception as e:
        logger.error("HA get_clim_state échoué : %s", e)
        return None


def apply_recommendation(cfg: dict, system: str) -> bool:
    """
    Applique la recommandation du Heating Advisor au poêle.
    system : 'poele' → allume | autre → éteint
    """
    if not is_configured(cfg) or not cfg.get("auto_control"):
        return False
    if system == "poele":
        return turn_on(cfg)
    else:
        return turn_off(cfg)
