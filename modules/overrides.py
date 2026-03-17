"""
Chargement et application des surcharges de configuration (config_override.json).
Partagé entre app.py et notify.py.
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

OVERRIDE_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "config_override.json")


def apply(cfg, data: dict) -> None:
    """Applique le dict d'overrides sur le module config."""
    for key in ("TARGET_TEMP", "SURFACE_M2", "REFRESH_INTERVAL_MINUTES", "HP_START", "HP_END"):
        if key in data:
            setattr(cfg, key, data[key])
    for key in ("POELE", "CLIM", "LOCATION", "EMAIL", "HOME_ASSISTANT"):
        if key in data and isinstance(data[key], dict):
            getattr(cfg, key).update(data[key])
    if "TEMPO_PRICES" in data:
        for color, prices in data["TEMPO_PRICES"].items():
            if color in cfg.TEMPO_PRICES and isinstance(prices, dict):
                cfg.TEMPO_PRICES[color].update(prices)


def load(cfg) -> None:
    """Charge config_override.json et l'applique si le fichier existe."""
    if os.path.exists(OVERRIDE_FILE):
        try:
            with open(OVERRIDE_FILE) as f:
                apply(cfg, json.load(f))
            logger.info("Overrides chargés depuis %s", OVERRIDE_FILE)
        except Exception as e:
            logger.warning("Impossible de charger les overrides : %s", e)
