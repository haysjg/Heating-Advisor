"""
Compteur d'heures de chauffe depuis le dernier remplissage du poêle, et débit réel mesuré.

Le job d'historique (toutes les 10 min) appelle tick() avec l'état réel du poêle : chaque lecture
« allumé » ajoute 10 min. Une heure avant la durée d'un sac, une alerte ntfy + email est envoyée
(une seule par cycle). Passé la durée d'un sac, la jauge reste à 100 % (le compteur continue) jusqu'au
clic « Trémie remplie » sur l'accueil, seul geste qui le remet à 0.

Mesure du débit : un second compteur (refill_minutes) ne repart de 0 qu'au clic « Trémie remplie ».
Chaque clic enregistre un cycle (heures de chauffe depuis le clic précédent, kg versés). Le niveau
restant au moment du remplissage varie d'une fois à l'autre ; le cumul des derniers cycles le dilue.
L'estimation n'est jamais appliquée seule : l'utilisateur la valide (stock et alerte en dépendent).
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
DEFAULT_BAG_KG = 15.0
ALERT_BEFORE_HOURS = 1.0

ESTIMATE_CYCLES = 8      # cycles récents pris en compte (suit la saison)
MIN_CYCLES = 3           # en dessous, pas d'estimation proposée
MIN_CYCLE_HOURS = 1.0    # double clic ou correction : cycle non enregistré
MAX_REFILLS_KEPT = 50


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _load() -> dict:
    state = jsonstore.load(STATE_FILE, None)
    if not isinstance(state, dict):
        state = {}
    state.setdefault("minutes", 0)
    state.setdefault("reset_at", _now())
    state.setdefault("reset_by", None)
    state.setdefault("alerted", False)
    state.setdefault("refill_minutes", 0)
    state.setdefault("last_refill_at", None)
    state.setdefault("refills", [])
    return state


def _start_cycle(state: dict, by: str) -> None:
    """Remet le compteur affiché à 0 (sans toucher au compteur de mesure)."""
    state.update(minutes=0, reset_at=_now(), reset_by=by, alerted=False)


def alert_hours(cycle_hours: float) -> float:
    """Seuil d'alerte : une heure avant la fin du sac (jamais négatif)."""
    return max(cycle_hours - ALERT_BEFORE_HOURS, 0.0)


def estimate(refills: list, bag_kg: float = DEFAULT_BAG_KG) -> dict | None:
    """Débit réel à partir des derniers remplissages : cumul kg / cumul heures."""
    recent = [r for r in refills if r.get("hours", 0) > 0 and r.get("kg", 0) > 0][-ESTIMATE_CYCLES:]
    if len(recent) < MIN_CYCLES:
        return None
    total_h = sum(r["hours"] for r in recent)
    total_kg = sum(r["kg"] for r in recent)
    per_bag = [r["hours"] / r["kg"] * bag_kg for r in recent]
    return {
        "cycles": len(recent),
        "kg_per_hour": round(total_kg / total_h, 3),
        "hours_per_bag": round(total_h / total_kg * bag_kg, 1),
        "min_hours_per_bag": round(min(per_bag), 1),
        "max_hours_per_bag": round(max(per_bag), 1),
    }


def status(cycle_hours: float = DEFAULT_CYCLE_HOURS, bag_kg: float = DEFAULT_BAG_KG) -> dict:
    """État du compteur pour l'affichage."""
    state = _load()
    minutes = int(state["minutes"])
    refills = state["refills"]
    return {
        "minutes": minutes,
        "hours": round(minutes / 60, 2),
        "cycle_hours": cycle_hours,
        "alert_hours": alert_hours(cycle_hours),
        "percent": min(round(minutes / (cycle_hours * 60) * 100, 1), 100.0) if cycle_hours > 0 else 0.0,
        "reset_at": state["reset_at"],
        "reset_by": state["reset_by"],
        "alerted": bool(state["alerted"]),
        "bag_kg": bag_kg,
        "measuring": state["last_refill_at"] is not None,
        "refill_hours": round(int(state["refill_minutes"]) / 60, 2),
        "refills_count": len([r for r in refills if r.get("hours", 0) > 0 and r.get("kg", 0) > 0]),
        "min_cycles": MIN_CYCLES,
        "estimate": estimate(refills, bag_kg),
    }


def reset(by: str = "manual", kg: float | None = None) -> dict:
    """Remise à 0. Au clic « Trémie remplie » (kg renseigné), enregistre le cycle de mesure écoulé."""
    with jsonstore.lock_for(STATE_FILE):
        state = _load()
        _start_cycle(state, by)
        if kg is not None and kg > 0:
            hours = int(state["refill_minutes"]) / 60
            # Premier clic : pas de remplissage connu avant, la mesure démarre seulement
            if state["last_refill_at"] and hours >= MIN_CYCLE_HOURS:
                state["refills"].append({"at": _now(), "hours": round(hours, 2), "kg": round(kg, 2)})
                state["refills"] = state["refills"][-MAX_REFILLS_KEPT:]
                logger.info("Poêle : remplissage %.1f kg après %.1f h de chauffe", kg, hours)
            state["refill_minutes"] = 0
            state["last_refill_at"] = _now()
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
    """Ajoute une lecture aux compteurs ; alerte au seuil. Pas de remise à 0 automatique en fin de sac."""
    with jsonstore.lock_for(STATE_FILE):
        state = _load()
        if not poele_on:
            return state
        state["refill_minutes"] = int(state["refill_minutes"]) + TICK_MINUTES
        minutes = int(state["minutes"]) + TICK_MINUTES
        state["minutes"] = minutes
        send = not state["alerted"] and minutes >= alert_hours(cycle_hours) * 60
        if send:
            state["alerted"] = True
        jsonstore.save(STATE_FILE, state)
        if send:
            _send_alert(minutes / 60, cycle_hours, email_cfg, ntfy_cfg)
        return state
