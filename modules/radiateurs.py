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
