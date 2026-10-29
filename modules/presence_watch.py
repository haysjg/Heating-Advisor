"""
Surveillance des traqueurs de présence.

Une entité person dont le téléphone n'envoie plus de position reste figée sur son dernier état
(souvent « home ») : la détection d'absence est alors bloquée sans que rien ne le signale.
Ce module repère les entités person qui ne se sont pas mises à jour depuis
THERMOSTAT.tracker_stale_hours heures et envoie une alerte ntfy (une seule par épisode),
puis une notification quand le traqueur redonne signe de vie.
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


def stale_ages(states: dict, now: datetime) -> dict:
    """Âge en heures de la dernière mise à jour de chaque entité (entités sans date ignorées)."""
    ages = {}
    for entity_id, state in states.items():
        raw = state.get("last_updated")
        if not raw:
            continue
        try:
            updated = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        ages[entity_id] = (now - updated).total_seconds() / 3600
    return ages


def transitions(ages: dict, alerted: dict, max_age_hours: float) -> tuple[list, list]:
    """Retourne (nouvellement figés, rétablis) par rapport aux alertes déjà envoyées."""
    newly_stale = [e for e, age in ages.items() if age >= max_age_hours and e not in alerted]
    recovered = [e for e, age in ages.items() if age < max_age_hours and e in alerted]
    return newly_stale, recovered


def check(ha_cfg: dict, thermostat_cfg: dict, ntfy_cfg: dict, now: datetime | None = None) -> None:
    """Job périodique : alerte ntfy si un traqueur de présence est figé."""
    max_age = float(thermostat_cfg.get("tracker_stale_hours", 3) or 0)
    entities = thermostat_cfg.get("person_entities", [])
    if not thermostat_cfg.get("presence_enabled") or max_age <= 0 or not entities:
        return
    states = ha_client.get_entities(ha_cfg, entities)
    if states is None:
        return  # HA injoignable : on ne conclut rien
    now = now or datetime.now(timezone.utc)
    ages = stale_ages(states, now)

    with jsonstore.lock_for(ALERTS_FILE):
        alerted = jsonstore.load(ALERTS_FILE, {})
        if not isinstance(alerted, dict):
            alerted = {}
        newly_stale, recovered = transitions(ages, alerted, max_age)

        for entity_id in newly_stale:
            state = states[entity_id]
            name = state.get("attributes", {}).get("friendly_name") or entity_id
            last = datetime.fromisoformat(state["last_updated"]).astimezone().strftime("%d/%m %Hh%M")
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
        removed = [e for e in alerted if e not in entities]
        for entity_id in removed:
            del alerted[entity_id]

        if newly_stale or recovered or removed:
            jsonstore.save(ALERTS_FILE, alerted)
