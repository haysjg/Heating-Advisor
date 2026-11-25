"""
Règles de rallumage des radiateurs éteints par la règle jour rouge.

Certains radiateurs (combles) ne doivent jamais être rallumés automatiquement le week-end,
sauf si l'utilisateur l'autorise explicitement depuis le dashboard. Le rallumage est alors
reporté au premier jour ouvré non rouge. Les commandes manuelles ne sont pas concernées.
"""

import os
import re
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


# Durée d'une journée Tempo (6h → 6h le lendemain), en minutes
TEMPO_DAY_MINUTES = 24 * 60


def parse_resume_at(raw) -> str:
    """Horaire de reprise saisi pour un radiateur : « HH:MM », ou vide = au retour d'un jour bleu ou blanc."""
    value = str(raw or "").strip()
    if not value:
        return ""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", value)
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError("Reprise d'un radiateur : format HH:MM attendu")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def minutes_since_day_start(now: datetime, hp_start: int) -> int:
    """Minutes écoulées depuis le début de la journée Tempo en cours (hp_start, 6h)."""
    return (now.hour * 60 + now.minute - hp_start * 60) % TEMPO_DAY_MINUTES


def cut_minutes(resume_at: str, hp_start: int) -> int:
    """Durée de la coupure d'un jour rouge, en minutes depuis hp_start : jusqu'à l'horaire de reprise
    (le soir même, ou la nuit suivante s'il est avant hp_start), toute la journée Tempo sans horaire."""
    if not resume_at:
        return TEMPO_DAY_MINUTES
    h, m = (int(x) for x in resume_at.split(":"))
    return (h * 60 + m - hp_start * 60) % TEMPO_DAY_MINUTES or TEMPO_DAY_MINUTES


def plan_red_day(now: datetime, color: str, radiators: dict, managed: list,
                 hp_start: int, hp_end: int) -> tuple[list, list]:
    """Radiateurs à éteindre et à rallumer (avant la restriction week-end).

    radiators : {entity_id: horaire de reprise} des radiateurs pilotés ; managed : ceux que l'appli a éteints.
    Extinction : jour rouge, heures pleines, avant l'horaire de reprise (un rallumage manuel la nuit est respecté).
    Rallumage, seulement de ceux que l'appli a éteints : horaire de reprise atteint, ou journée Tempo bleue
    ou blanche. Couleur inconnue (UNKNOWN) : seul l'horaire de reprise compte.
    """
    elapsed = minutes_since_day_start(now, hp_start)
    to_off, to_on = [], []
    for entity_id, resume_at in radiators.items():
        in_cut = elapsed < cut_minutes(resume_at, hp_start)
        if entity_id in managed:
            # Avant hp_start, color est celle de la journée Tempo qui commence à hp_start : ignorée
            day_ok = now.hour >= hp_start and color in ("BLUE", "WHITE")
            if not in_cut or day_ok:
                to_on.append(entity_id)
        elif color == "RED" and hp_start <= now.hour < hp_end and in_cut:
            to_off.append(entity_id)
    return to_off, to_on


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
