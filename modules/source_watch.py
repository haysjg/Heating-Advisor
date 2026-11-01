"""
Surveillance des sources de température mesurée (Infoclimat, Météo-France).

Chaque source est testée périodiquement, y compris celle qui ne sert qu'en secours : sans ça,
une clé refusée sur la source de secours passerait inaperçue jusqu'au jour où on en a besoin.
- identifiants refusés (clé Infoclimat liée à une IP qui a changé, abonnement Météo-France
  expiré…) : alerte ntfy immédiate ;
- autre panne : alerte si elle dure FAILURES_BEFORE_ALERT tests de suite (pas pour un incident
  passager) ;
- une seule alerte par épisode, puis une notification quand la source répond de nouveau.
S'y ajoute un rappel avant la fin de l'abonnement à l'API Météo-France (date connue d'avance).
"""

import logging
import os
from datetime import date

from modules import jsonstore
from modules import weather
from modules.crypto import decrypt_password
from modules.ntfy_push import send as ntfy_send

logger = logging.getLogger(__name__)

ALERTS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "source_alerts.json")

CHECK_INTERVAL_HOURS = 6
FAILURES_BEFORE_ALERT = 2          # 2 tests à 6 h d'écart : panne d'au moins 6 h
SUBSCRIPTION_REMINDER_DAYS = (30, 7)

HINTS = {
    "Infoclimat": "La clé est liée à l'IP publique de la box, qui a peut-être changé : en générer une "
                  "nouvelle sur infoclimat.fr/opendata, puis la coller dans Config → Stations de mesure.",
    "Météo-France": "Vérifier l'abonnement « Données d'observation » et l'Application ID sur "
                    "portail-api.meteofrance.fr (connexion en navigation privée), puis Config → Stations de mesure.",
}


def _update_failures(failures: dict, results: dict, ntfy_cfg: dict) -> None:
    for source, error in results.items():
        entry = failures.get(source)
        if error is None:
            if entry and entry.get("alerted"):
                logger.info("%s répond de nouveau", source)
                ntfy_send(f"✅ {source} répond de nouveau", "Les mesures de cette source sont de nouveau disponibles.", ntfy_cfg)
            failures.pop(source, None)
            continue

        entry = failures.setdefault(source, {"count": 0, "alerted": False})
        entry["count"] += 1
        entry["last_error"] = str(error)
        logger.warning("Surveillance %s : échec %d (%s)", source, entry["count"], error)
        if entry["alerted"] or not (error.auth or entry["count"] >= FAILURES_BEFORE_ALERT):
            continue
        if error.auth:
            title = f"🔑 {source} refuse les identifiants"
        else:
            title = f"⚠️ {source} ne répond plus"
        ntfy_send(
            title,
            f"{error}. {HINTS.get(source, '')} En attendant, l'autre source prend le relais "
            "(puis le modèle Open-Meteo si aucune ne répond).",
            ntfy_cfg,
        )
        entry["alerted"] = True


def _subscription_reminder(reminders: dict, observations: dict, ntfy_cfg: dict, today: date) -> None:
    end_raw = observations.get("meteofrance_subscription_end")
    if not end_raw or not decrypt_password(observations.get("meteofrance_application_id", "")):
        return
    try:
        end = date.fromisoformat(str(end_raw))
    except ValueError:
        logger.warning("Date de fin d'abonnement Météo-France invalide : %s", end_raw)
        return
    days_left = (end - today).days
    due = [d for d in SUBSCRIPTION_REMINDER_DAYS if days_left <= d]
    sent = reminders.setdefault(end.isoformat(), [])
    if not due or min(due) in sent:
        return
    when = f"se termine le {end:%d/%m/%Y} (dans {days_left} j)" if days_left >= 0 else f"s'est terminé le {end:%d/%m/%Y}"
    ntfy_send(
        "📅 Abonnement Météo-France à renouveler",
        f"L'abonnement à l'API « Données d'observation » {when}. Le renouveler sur portail-api.meteofrance.fr "
        "(connexion en navigation privée), puis mettre à jour la date dans Config → Stations de mesure.",
        ntfy_cfg,
    )
    sent.extend(d for d in due if d not in sent)


def check(observations: dict, ntfy_cfg: dict, today: date | None = None) -> None:
    """Job périodique : teste les sources et envoie les alertes / rappels ntfy."""
    results = weather.probe_sources(observations)
    today = today or date.today()

    with jsonstore.lock_for(ALERTS_FILE):
        state = jsonstore.load(ALERTS_FILE, {})
        if not isinstance(state, dict):
            state = {}
        failures = state.setdefault("failures", {})
        _update_failures(failures, results, ntfy_cfg)
        _subscription_reminder(state.setdefault("reminders", {}), observations, ntfy_cfg, today)
        jsonstore.save(ALERTS_FILE, state)
