"""
Chargement et application des surcharges de configuration (config_override.json).
Partagé entre app.py et notify.py.
"""

import logging
import os

from modules import jsonstore

logger = logging.getLogger(__name__)


def patch_override(updater) -> None:
    """Read-modify-write thread-safe et atomique sur OVERRIDE_FILE. updater(dict) applique les modifications."""
    def _apply(data):
        if not isinstance(data, dict):
            data = {}
        updater(data)
        return data
    jsonstore.update(OVERRIDE_FILE, _apply, {})


def write_override(data: dict) -> None:
    """Écrase OVERRIDE_FILE avec data (thread-safe, atomique)."""
    jsonstore.save(OVERRIDE_FILE, data)

OVERRIDE_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "config_override.json")


def apply(cfg, data: dict) -> None:
    """Applique le dict d'overrides sur le module config."""
    for key in ("TARGET_TEMP", "SURFACE_M2", "REFRESH_INTERVAL_MINUTES", "HP_START", "HP_END"):
        if key in data:
            setattr(cfg, key, data[key])
    for key in ("POELE", "CLIM", "LOCATION", "EMAIL", "HOME_ASSISTANT", "THERMOSTAT", "AUTH", "NTFY", "RADIATEURS_TEMPO_ROUGE", "ELECTRICITY"):
        if key in data and isinstance(data[key], dict):
            getattr(cfg, key).update(data[key])
    if "TEMPO_PRICES" in data:
        for color, prices in data["TEMPO_PRICES"].items():
            if color in cfg.TEMPO_PRICES and isinstance(prices, dict):
                cfg.TEMPO_PRICES[color].update(prices)


def load(cfg) -> None:
    """Charge config_override.json et l'applique si le fichier existe."""
    data = jsonstore.load(OVERRIDE_FILE, None)
    if isinstance(data, dict):
        try:
            apply(cfg, data)
            logger.info("Overrides chargés depuis %s", OVERRIDE_FILE)
        except Exception as e:
            logger.warning("Impossible de charger les overrides : %s", e)
