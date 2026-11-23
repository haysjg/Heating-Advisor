"""
Surveillance des traqueurs de présence.

Une entité person dont le téléphone n'envoie plus de position reste figée sur son dernier état
(souvent « home ») : la détection d'absence est alors bloquée sans que rien ne le signale.
Ce module repère les entités person dont le téléphone n'a rien envoyé depuis
THERMOSTAT.tracker_stale_hours heures et envoie une alerte ntfy (une seule par épisode),
puis une notification quand le traqueur redonne signe de vie.

Il signale aussi un téléphone rattaché à plusieurs personnes : HA donne à chaque personne la position
du traqueur le plus récent, donc le départ de l'une rend l'autre absente alors qu'elle est à la maison.
"""

import logging
import os
from datetime import datetime, timezone

from modules import jsonstore
from modules import homeassistant as ha_client
from modules.ntfy_push import send as ntfy_send

logger = logging.getLogger(__name__)

ALERTS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "tracker_alerts.json")


def _format_age(hours: float) -> str:
    if hours < 48:
        return f"{int(hours)} h"
    return f"{int(hours // 24)} j"


def _parse(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def last_seen(state: dict, trackers: dict) -> datetime | None:
    """Dernier signe de vie d'une entité person.

    Le last_updated de la person ne bouge que si sa position change : un téléphone posé toute la nuit
    paraîtrait figé. Le last_reported du traqueur source (device_tracker de l'app) avance à chaque
    envoi, même identique ; on retient la date la plus récente des deux."""
    dates = [_parse(state.get("last_updated"))]
    tracker = trackers.get((state.get("attributes") or {}).get("source"))
    if tracker:
        dates += [_parse(tracker.get("last_reported")), _parse(tracker.get("last_updated"))]
    dates = [d for d in dates if d]
    return max(dates) if dates else None


def stale_ages(states: dict, now: datetime, trackers: dict | None = None) -> dict:
    """Âge en heures du dernier signe de vie de chaque entité (entités sans date ignorées)."""
    ages = {}
    for entity_id, state in states.items():
        seen = last_seen(state, trackers or {})
        if seen:
            ages[entity_id] = (now - seen).total_seconds() / 3600
    return ages


def transitions(ages: dict, alerted: dict, max_age_hours: float) -> tuple[list, list]:
    """Retourne (nouvellement figés, rétablis) par rapport aux alertes déjà envoyées."""
    newly_stale = [e for e, age in ages.items() if age >= max_age_hours and e not in alerted]
    recovered = [e for e, age in ages.items() if age < max_age_hours and e in alerted]
    return newly_stale, recovered


SHARED_PREFIX = "shared:"


def shared_trackers(states: dict) -> dict:
    """Traqueurs rattachés à plusieurs entités person : {device_tracker: [person, ...]}."""
    owners = {}
    for entity_id, state in states.items():
        for tracker in (state.get("attributes") or {}).get("device_trackers") or []:
            owners.setdefault(tracker, []).append(entity_id)
    return {t: sorted(p) for t, p in owners.items() if len(p) > 1}


def _check_shared(states: dict, alerted: dict, ntfy_cfg: dict, now: datetime) -> bool:
    """Alerte ntfy (une par épisode) pour chaque traqueur partagé. Retourne True si alerted a changé."""
    shared = shared_trackers(states)
    changed = False
    for tracker, persons in shared.items():
        key = SHARED_PREFIX + tracker
        if key in alerted:
            continue
        names = [(states[p].get("attributes") or {}).get("friendly_name") or p for p in persons]
        logger.warning("Traqueur %s rattaché à plusieurs personnes : %s", tracker, ", ".join(persons))
        ntfy_send(
            f"📍 Téléphone partagé — {tracker.split('.', 1)[-1]}",
            f"Ce traqueur est rattaché à {' et '.join(names)} dans Home Assistant : le départ de l'un "
            "fait passer l'autre pour absent et peut éteindre le chauffage. Le retirer des personnes "
            "à qui il n'appartient pas (Paramètres → Personnes).",
            ntfy_cfg,
        )
        alerted[key] = now.isoformat()
        changed = True
    for key in [k for k in alerted if k.startswith(SHARED_PREFIX) and k[len(SHARED_PREFIX):] not in shared]:
        logger.info("Traqueur %s de nouveau rattaché à une seule personne", key[len(SHARED_PREFIX):])
        del alerted[key]
        changed = True
    return changed


def check(ha_cfg: dict, thermostat_cfg: dict, ntfy_cfg: dict, now: datetime | None = None) -> None:
    """Job périodique : alerte ntfy si un traqueur de présence est figé ou partagé entre personnes."""
    max_age = float(thermostat_cfg.get("tracker_stale_hours", 3) or 0)
    entities = thermostat_cfg.get("person_entities", [])
    if not thermostat_cfg.get("presence_enabled") or not entities:
        return
    states = ha_client.get_entities(ha_cfg, entities)
    if states is None:
        return  # HA injoignable : on ne conclut rien
    sources = [(s.get("attributes") or {}).get("source") for s in states.values()]
    trackers = ha_client.get_entities(ha_cfg, [t for t in sources if t]) or {}
    now = now or datetime.now(timezone.utc)
    ages = stale_ages(states, now, trackers) if max_age > 0 else {}

    with jsonstore.lock_for(ALERTS_FILE):
        alerted = jsonstore.load(ALERTS_FILE, {})
        if not isinstance(alerted, dict):
            alerted = {}
        shared_changed = _check_shared(states, alerted, ntfy_cfg, now)
        newly_stale, recovered = transitions(ages, alerted, max_age)

        for entity_id in newly_stale:
            state = states[entity_id]
            name = state.get("attributes", {}).get("friendly_name") or entity_id
            last = last_seen(state, trackers).astimezone().strftime("%d/%m %Hh%M")
            logger.warning("Traqueur figé : %s (dernière mise à jour %s, %.1f h)", entity_id, last, ages[entity_id])
            ntfy_send(
                f"📍 Traqueur figé — {name}",
                f"Aucune position reçue depuis {_format_age(ages[entity_id])} (dernière : {last}, état « {state.get('state')} »). "
                "La détection d'absence peut être faussée. Vérifier l'app Home Assistant du téléphone "
                "(localisation « Toujours autoriser », batterie sans restriction).",
                ntfy_cfg,
            )
            alerted[entity_id] = now.isoformat()

        for entity_id in recovered:
            name = states[entity_id].get("attributes", {}).get("friendly_name") or entity_id
            logger.info("Traqueur de nouveau actif : %s", entity_id)
            ntfy_send(f"✅ Traqueur actif — {name}", "Les positions arrivent de nouveau dans Home Assistant.", ntfy_cfg)
            del alerted[entity_id]

        # Entités retirées de la config : on oublie leur alerte
        removed = [e for e in alerted if e not in entities and not e.startswith(SHARED_PREFIX)]
        for entity_id in removed:
            del alerted[entity_id]

        if shared_changed or newly_stale or recovered or removed:
            jsonstore.save(ALERTS_FILE, alerted)
