"""
Compteur d'heures de chauffe depuis le dernier remplissage du poêle.

Le job d'historique (toutes les 10 min) appelle tick() avec l'état réel du poêle : chaque lecture
« allumé » ajoute 10 min. Une heure avant la durée d'un sac, une alerte ntfy + email est envoyée
(une seule par cycle). À la durée d'un sac, le compteur repart de 0 tout seul ; il peut aussi être
remis à 0 à la main depuis l'accueil après un remplissage.
"""

import logging
import os
from datetime import datetime

from modules import jsonstore
from modules.ntfy_push import send as ntfy_send
from modules.thermostat import _send_sensor_alert

logger = logging.getLogger(__name__)

STATE_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "pellet_refill.json")

TICK_MINUTES = 10
DEFAULT_CYCLE_HOURS = 15.0
ALERT_BEFORE_HOURS = 1.0


def _default_state() -> dict:
    return {"minutes": 0, "reset_at": datetime.now().isoformat(timespec="seconds"), "reset_by": None, "alerted": False}


def _load() -> dict:
    state = jsonstore.load(STATE_FILE, None)
    if not isinstance(state, dict):
        state = _default_state()
    return state


def alert_hours(cycle_hours: float) -> float:
    """Seuil d'alerte : une heure avant la fin du sac (jamais négatif)."""
    return max(cycle_hours - ALERT_BEFORE_HOURS, 0.0)


def status(cycle_hours: float = DEFAULT_CYCLE_HOURS) -> dict:
    """État du compteur pour l'affichage."""
    state = _load()
    minutes = int(state.get("minutes", 0))
    return {
        "minutes": minutes,
        "hours": round(minutes / 60, 2),
        "cycle_hours": cycle_hours,
        "alert_hours": alert_hours(cycle_hours),
        "percent": min(round(minutes / (cycle_hours * 60) * 100, 1), 100.0) if cycle_hours > 0 else 0.0,
        "reset_at": state.get("reset_at"),
        "reset_by": state.get("reset_by"),
        "alerted": bool(state.get("alerted")),
    }


def reset(by: str = "manual") -> dict:
    """Remet le compteur à 0 (remplissage effectué)."""
    state = _default_state()
    state["reset_by"] = by
    jsonstore.save(STATE_FILE, state)
    logger.info("Poêle : compteur de remplissage remis à 0 (%s)", by)
    return state


def _send_alert(hours: float, cycle_hours: float, email_cfg: dict, ntfy_cfg: dict) -> None:
    remaining = max(cycle_hours - hours, 0)
    title = "🪵 Poêle : remplissage à prévoir"
    message = (f"Le poêle a chauffé {hours:.0f} h depuis le dernier remplissage "
               f"(sac prévu pour {cycle_hours:.0f} h). Il reste environ {remaining * 60:.0f} min de granulés.")
    ntfy_send(title, message, ntfy_cfg)
    _send_sensor_alert(email_cfg or {}, title, message, kicker="🪵 Poêle à granulés — Heating Advisor")


def tick(poele_on: bool, cycle_hours: float, email_cfg: dict, ntfy_cfg: dict) -> dict:
    """Ajoute une lecture au compteur ; alerte au seuil, remise à 0 automatique en fin de sac."""
    with jsonstore.lock_for(STATE_FILE):
        state = _load()
        if not poele_on:
            return state
        minutes = int(state.get("minutes", 0)) + TICK_MINUTES

        if cycle_hours > 0 and minutes >= cycle_hours * 60:
            logger.info("Poêle : %.0f h de chauffe atteintes, compteur de remplissage remis à 0", cycle_hours)
            state = _default_state()
            state["reset_by"] = "auto"
            jsonstore.save(STATE_FILE, state)
            return state

        state["minutes"] = minutes
        if not state.get("alerted") and minutes >= alert_hours(cycle_hours) * 60:
            state["alerted"] = True
            jsonstore.save(STATE_FILE, state)
            _send_alert(minutes / 60, cycle_hours, email_cfg, ntfy_cfg)
            return state

        jsonstore.save(STATE_FILE, state)
        return state
