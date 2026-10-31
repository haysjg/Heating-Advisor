"""
Analyse courante : configuration du moteur de décision partagée (dashboard, thermostat,
email du soir) et cache des données externes séparé du calcul de la décision.

Les données externes (météo, Tempo, prévisions) sont coûteuses à récupérer : elles sont
mises en cache. La décision, elle, est un calcul pur et peu coûteux : elle est refaite à
chaque lecture avec l'heure courante, pour que la période HP/HC soit toujours juste.
"""

import logging
import threading
import time
from datetime import datetime
from typing import Callable, Hashable

from modules import tempo as tempo_module
from modules.advisor import analyze, analyze_tomorrow

logger = logging.getLogger(__name__)


def build_advisor_config(settings) -> dict:
    """Configuration unique du moteur de décision, lue depuis le module `config`.

    Utilisée par app.py (dashboard, thermostat, agrégation) et notify.py (email du soir)
    pour qu'ils décident avec les mêmes paramètres (notamment TARGET_TEMP, modifiable
    depuis la page de configuration).

    NO_HEATING_AT_NIGHT n'est volontairement PAS transmis : avec cette règle, l'advisor
    renvoie system="none" pendant toute la période HC, et le thermostat — qui lit cette
    décision — n'allumerait plus rien avant 06:00 alors que sa plage démarre à 05:45.
    Le chauffage de nuit est déjà borné par les plages horaires du thermostat.
    """
    return {
        "TEMPO_PRICES": settings.TEMPO_PRICES,
        "CLIM": settings.CLIM,
        "POELE": settings.POELE,
        "HP_START": settings.HP_START,
        "HP_END": settings.HP_END,
        "TARGET_TEMP": settings.TARGET_TEMP,
    }


def tariff_slot(hp_start: int, hp_end: int, now: datetime | None = None) -> tuple:
    """Identifie la tranche tarifaire courante : (jour, HP/HC).

    Change aux bascules HP/HC et au changement de jour Tempo : le cache des données
    externes est alors considéré comme périmé.
    """
    now = now or datetime.now()
    return now.date().isoformat(), tempo_module.get_current_period(hp_start, hp_end, now)


def compute_analysis(raw: dict, cfg: dict, now: datetime | None = None) -> dict:
    """Calcule l'analyse complète à partir des données externes en cache.

    Pur (aucun appel réseau) : la période HP/HC et l'heure sont celles de `now`.
    Le champ `timestamp` reste l'heure de récupération des données (« dernière mise à jour »).
    """
    tempo = tempo_module.at_time(raw["tempo"], cfg["HP_START"], cfg["HP_END"], now)
    result = analyze(raw["weather"], tempo, cfg)
    result["timestamp"] = raw["fetched_at"]
    result["tomorrow"] = analyze_tomorrow(raw["tomorrow_weather"], tempo, cfg)
    result["hourly_forecast"] = raw.get("hourly_forecast")
    if raw.get("indoor"):
        result["indoor"] = raw["indoor"]
    return result


class DataCache:
    """Cache « stale-while-revalidate » des données externes.

    fetch : récupère les données (météo, Tempo…), potentiellement lent
    ttl   : durée de validité en secondes (relue à chaque fois, modifiable à chaud)
    slot  : tranche tarifaire courante ; un changement de tranche périme le cache
    clock : horloge (injectable pour les tests)
    """

    def __init__(self, fetch: Callable[[], dict], ttl: Callable[[], float],
                 slot: Callable[[], Hashable], clock: Callable[[], float] = time.time):
        self._fetch = fetch
        self._ttl = ttl
        self._slot = slot
        self._clock = clock
        self._lock = threading.Lock()
        self.data: dict | None = None
        self.expires_at: float | None = None
        self.slot: Hashable | None = None

    def invalidate(self) -> None:
        self.data = None
        self.expires_at = None
        self.slot = None

    def is_fresh(self) -> bool:
        return (
            self.data is not None
            and self.expires_at is not None
            and self._clock() < self.expires_at
            and self.slot == self._slot()
        )

    def refresh(self, force: bool = False) -> dict:
        """Recharge les données (un seul rafraîchissement à la fois)."""
        with self._lock:
            # Un autre thread vient peut-être de rafraîchir pendant l'attente du verrou
            if not force and self.is_fresh():
                return self.data
            logger.info("Rafraîchissement des données…")
            data = self._fetch()
            self.data = data
            self.expires_at = self._clock() + self._ttl()
            self.slot = self._slot()
            return data

    def refresh_in_background(self) -> None:
        """Lance un rafraîchissement en tâche de fond, sauf s'il y en a déjà un en cours."""
        if self._lock.locked():
            return

        def _run():
            try:
                self.refresh()
            except Exception as e:
                logger.error("Rafraîchissement en arrière-plan échoué : %s", e)

        threading.Thread(target=_run, name="analysis-refresh", daemon=True).start()

    def get(self, force: bool = False) -> dict:
        """Retourne les données en cache.

        Périmées (TTL ou changement de tranche tarifaire) : les dernières données sont
        servies immédiatement et le rafraîchissement part en arrière-plan. Bloquant
        seulement au premier appel ou sur demande explicite.
        """
        if force or self.data is None:
            return self.refresh(force=force)
        if not self.is_fresh():
            self.refresh_in_background()
        return self.data
