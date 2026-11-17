"""
Règles de rallumage des radiateurs éteints par la règle jour rouge.

Certains radiateurs (combles) ne doivent jamais être rallumés automatiquement le week-end,
sauf si l'utilisateur l'autorise explicitement depuis le dashboard. Le rallumage est alors
reporté au premier jour ouvré non rouge. Les commandes manuelles ne sont pas concernées.
"""

import os
from datetime import datetime

from modules import jsonstore

WEEKEND_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "radiateurs_weekend.json")


def is_weekend(now: datetime) -> bool:
    return now.weekday() >= 5


def weekend_auto_allowed() -> bool:
    """Autorisation de gestion automatique le week-end (slider du dashboard), refusée par défaut."""
    data = jsonstore.load(WEEKEND_FILE, {})
    return bool(data.get("allowed")) if isinstance(data, dict) else False


def set_weekend_auto_allowed(allowed: bool) -> None:
    jsonstore.save(WEEKEND_FILE, {"allowed": bool(allowed)})


def split_turn_back_on(managed: list, entity_ids: list, weekend_restricted: list,
                       now: datetime, weekend_allowed: bool) -> tuple[list, list]:
    """Parmi les radiateurs éteints par la règle jour rouge, sépare ceux à rallumer maintenant
    de ceux dont le rallumage est reporté (week-end, radiateur restreint, gestion non autorisée).

    Retourne (à_rallumer, reportés).
    """
    candidates = [e for e in managed if e in entity_ids]
    if weekend_allowed or not is_weekend(now):
        return candidates, []
    postponed = [e for e in candidates if e in weekend_restricted]
    return [e for e in candidates if e not in postponed], postponed


# Unités acceptées pour le compteur d'énergie HA d'un radiateur, en kWh
_ENERGY_UNITS = {"Wh": 0.001, "kWh": 1.0, "MWh": 1000.0}


def is_heating(ha_state: dict | None) -> bool:
    """Le radiateur chauffe-t-il ? `hvac_action` quand l'entité le fournit (thermostat intégré :
    « heating » seulement quand la résistance chauffe), sinon l'état de la prise (on).
    Un climate allumé sans `hvac_action` (sèche-serviettes Overkiz) n'est pas compté en chauffe :
    son mode (auto / heat) ne dit pas si la résistance chauffe — voir `heating_known`."""
    if not ha_state:
        return False
    action = (ha_state.get("attributes") or {}).get("hvac_action")
    if action is not None:
        return action == "heating"
    return ha_state.get("state") == "on"


def heating_known(ha_state: dict | None) -> bool:
    """Sait-on si le radiateur chauffe ? Non si HA ne le voit pas, ou pour un climate allumé
    qui ne publie pas `hvac_action`."""
    if not ha_state or ha_state.get("state") in (None, "unavailable", "unknown"):
        return False
    if (ha_state.get("attributes") or {}).get("hvac_action") is not None:
        return True
    return ha_state.get("state") in ("on", "off")


def meter_kwh(ha_state: dict | None) -> float | None:
    """Index du compteur d'énergie HA en kWh (Wh, kWh ou MWh), None si indisponible ou illisible."""
    if not ha_state:
        return None
    factor = _ENERGY_UNITS.get((ha_state.get("attributes") or {}).get("unit_of_measurement") or "Wh")
    try:
        value = float(ha_state.get("state"))
    except (TypeError, ValueError):
        return None
    return value * factor if factor is not None and value >= 0 else None
