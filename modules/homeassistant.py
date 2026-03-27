"""
Client Home Assistant — contrôle optionnel du poêle via l'API REST HA.
N'est utilisé que si HOME_ASSISTANT.enabled = True dans la config.
"""

import logging
import urllib.request
import urllib.error
import json

from modules.crypto import decrypt_password

logger = logging.getLogger(__name__)


def _request(url: str, token: str, method: str = "GET", body: dict = None) -> dict:
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {decrypt_password(token)}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


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
        return _request(
            f"{cfg['url'].rstrip('/')}/api/states/{entity_id}",
            cfg["token"],
        )
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
            state = _request(
                f"{cfg['url'].rstrip('/')}/api/states/{temp_entity}",
                cfg["token"],
            )
            raw = state.get("state")
            result["temperature"] = float(raw) if raw not in (None, "unavailable", "unknown") else None
        if humidity_entity:
            state = _request(
                f"{cfg['url'].rstrip('/')}/api/states/{humidity_entity}",
                cfg["token"],
            )
            raw = state.get("state")
            result["humidity"] = float(raw) if raw not in (None, "unavailable", "unknown") else None
    except Exception as e:
        logger.error("HA get_indoor_climate échoué : %s", e)
        return None
    return result or None


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
