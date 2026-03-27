"""
Thermostat automatique — pilotage du poêle basé sur la température intérieure.
"""
import json
import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

STATE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "thermostat_state.json"
)

DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"state": "off", "last_turned_on": None, "last_turned_off": None}


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def get_state() -> dict:
    """Retourne l'état courant du thermostat."""
    return _load_state()


def is_in_schedule(cfg: dict) -> bool:
    """Retourne True si l'heure actuelle est dans la plage du jour courant."""
    now = datetime.now()
    day_key = DAY_KEYS[now.weekday()]
    schedule = cfg.get("schedule", {}).get(day_key)
    if not schedule:
        return False
    try:
        start_h, start_m = map(int, schedule["start"].split(":"))
        end_h, end_m = map(int, schedule["end"].split(":"))
        start = now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
        end = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
        return start <= now <= end
    except Exception:
        return False


def check_and_apply(ha_cfg: dict, thermostat_cfg: dict, recommendation: str) -> None:
    """
    Vérifie la température intérieure et pilote le poêle.
    recommendation : 'poele', 'clim', 'none', None
    """
    if not thermostat_cfg.get("enabled"):
        return
    if not ha_cfg.get("enabled") or not ha_cfg.get("url") or not ha_cfg.get("token"):
        logger.warning("Thermostat : Home Assistant non configuré, skip")
        return

    import modules.homeassistant as ha_client

    indoor = ha_client.get_indoor_climate(ha_cfg)
    if not indoor or indoor.get("temperature") is None:
        logger.warning("Thermostat : température intérieure indisponible, skip")
        return

    temp = indoor["temperature"]
    temp_on = thermostat_cfg.get("temp_on", 20.0)
    temp_off = thermostat_cfg.get("temp_off", 22.9)
    min_on = thermostat_cfg.get("min_on_minutes", 90)
    grace = thermostat_cfg.get("end_of_schedule_grace_minutes", 45)

    in_schedule = is_in_schedule(thermostat_cfg)
    state = _load_state()

    # ── Synchronisation avec l'état réel HA ──────────────────
    ha_state = ha_client.get_state(ha_cfg)
    real_on = ha_state is not None and ha_state.get("state") not in ("off", "unavailable", "unknown", None)
    current = state.get("state", "off")
    if real_on and current == "off":
        logger.info("Thermostat : poêle allumé manuellement, synchronisation état → on")
        # On considère la durée min déjà satisfaite pour permettre une extinction immédiate si besoin
        from datetime import timedelta
        pseudo_on = (datetime.now() - timedelta(minutes=min_on)).isoformat()
        state = {
            "state": "on",
            "last_turned_on": state.get("last_turned_on") or pseudo_on,
            "last_turned_off": state.get("last_turned_off"),
        }
        _save_state(state)
        current = "on"
    elif not real_on and current == "on":
        logger.info("Thermostat : poêle éteint manuellement, synchronisation état → off")
        state = {
            "state": "off",
            "last_turned_on": state.get("last_turned_on"),
            "last_turned_off": state.get("last_turned_off") or datetime.now().isoformat(),
        }
        _save_state(state)
        current = "off"
    last_on_str = state.get("last_turned_on")
    last_on = datetime.fromisoformat(last_on_str) if last_on_str else None
    on_minutes = (datetime.now() - last_on).total_seconds() / 60 if last_on else 0

    if current == "off":
        if in_schedule and temp < temp_on and recommendation == "poele":
            logger.info(
                "Thermostat : allumage poêle (%.1f°C < %.1f°C, recommandation=%s)",
                temp, temp_on, recommendation,
            )
            ha_client.turn_on(ha_cfg)
            _save_state({
                "state": "on",
                "last_turned_on": datetime.now().isoformat(),
                "last_turned_off": state.get("last_turned_off"),
            })
    else:  # current == "on"
        if in_schedule:
            if temp >= temp_off and on_minutes >= min_on:
                logger.info(
                    "Thermostat : extinction poêle (%.1f°C >= %.1f°C, allumé depuis %.0f min)",
                    temp, temp_off, on_minutes,
                )
                ha_client.turn_off(ha_cfg)
                _save_state({
                    "state": "off",
                    "last_turned_on": state.get("last_turned_on"),
                    "last_turned_off": datetime.now().isoformat(),
                })
        else:
            if on_minutes >= grace:
                logger.info(
                    "Thermostat : extinction poêle fin de plage (allumé depuis %.0f min >= %d min)",
                    on_minutes, grace,
                )
                ha_client.turn_off(ha_cfg)
                _save_state({
                    "state": "off",
                    "last_turned_on": state.get("last_turned_on"),
                    "last_turned_off": datetime.now().isoformat(),
                })
