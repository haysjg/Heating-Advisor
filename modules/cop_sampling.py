"""
Module de gestion de l'échantillonnage asynchrone pour COP Learning.

Permet de collecter des échantillons de puissance sur une durée définie
pour calculer une moyenne représentative du cycle complet de la climatisation.
"""

import uuid
import time
import logging
from datetime import datetime, timedelta
from threading import Thread
from typing import Dict, Optional, List

# Import des modules nécessaires
import modules.cop_learning as cop_learning_module

logger = logging.getLogger(__name__)

# Stockage en mémoire des tasks d'échantillonnage
_sampling_tasks: Dict[str, Dict] = {}


def start_sampling_task(notes: str, outdoor_temp: Optional[float], config) -> str:
    """
    Démarre une tâche d'échantillonnage asynchrone.

    Args:
        notes: Notes optionnelles de l'utilisateur
        outdoor_temp: Température extérieure au moment du démarrage
        config: Configuration de l'application

    Returns:
        task_id: Identifiant unique de la tâche
    """
    task_id = str(uuid.uuid4())

    # Initialiser la structure de la task
    _sampling_tasks[task_id] = {
        "status": "running",
        "created_at": datetime.now(),
        "progress": 0,
        "samples_collected": 0,
        "samples_target": 0,
        "samples": [],
        "outdoor_temp": outdoor_temp,
        "notes": notes,
        "error_message": None,
        "tag_id": None,
        "calculated_cop": None,
        "deduced_ac_power": None,
        "validation_message": None
    }

    # Lancer le worker dans un thread séparé
    thread = Thread(target=_sampling_worker, args=(task_id, config), daemon=True)
    thread.start()

    logger.info(f"Tâche d'échantillonnage {task_id} démarrée")

    return task_id


def _sampling_worker(task_id: str, config):
    """
    Worker thread qui effectue l'échantillonnage.

    Args:
        task_id: Identifiant de la tâche
        config: Configuration de l'application
    """
    task = _sampling_tasks.get(task_id)
    if not task:
        logger.error(f"Task {task_id} introuvable")
        return

    try:
        # Récupérer la configuration d'échantillonnage
        sampling_cfg = config.COP_LEARNING.get("sampling", {})

        duration = sampling_cfg.get("duration_seconds", 120)
        interval = sampling_cfg.get("interval_seconds", 10)
        num_samples = duration // interval
        max_errors = sampling_cfg.get("max_errors", 3)
        min_required = sampling_cfg.get("min_samples_required", 8)

        task["samples_target"] = num_samples

        samples = []
        consecutive_errors = 0

        logger.info(f"Task {task_id}: Démarrage échantillonnage ({num_samples} mesures, intervalle {interval}s)")

        for i in range(num_samples):
            # Vérifier si la tâche a été annulée
            if task["status"] == "cancelled":
                logger.info(f"Task {task_id}: Annulée par l'utilisateur")
                return

            # Échantillonner les capteurs
            try:
                sensors = cop_learning_module.get_current_sensors(
                    config.HOME_ASSISTANT,
                    config.COP_LEARNING
                )

                if sensors:
                    sample = {
                        "ts": datetime.now().isoformat(),
                        "total_power": sensors["total_power"],
                        "heater_power": sensors["heater_power"]
                    }
                    samples.append(sample)
                    consecutive_errors = 0

                    logger.debug(f"Task {task_id}: Échantillon {len(samples)}/{num_samples} - "
                               f"Total: {sensors['total_power']}W, Ballon: {sensors['heater_power']}W")
                else:
                    consecutive_errors += 1
                    logger.warning(f"Task {task_id}: Capteurs indisponibles (erreur {consecutive_errors}/{max_errors})")

                    if consecutive_errors >= max_errors:
                        task["status"] = "error"
                        task["error_message"] = f"Capteurs indisponibles ({consecutive_errors} échecs consécutifs)"
                        logger.error(f"Task {task_id}: Échec - trop d'erreurs consécutives")
                        return

            except Exception as e:
                consecutive_errors += 1
                logger.exception(f"Task {task_id}: Erreur échantillonnage")

                if consecutive_errors >= max_errors:
                    task["status"] = "error"
                    task["error_message"] = f"Erreur technique: {str(e)}"
                    return

            # Mettre à jour la progression
            task["samples_collected"] = len(samples)
            task["samples"] = samples
            task["progress"] = int((i + 1) / num_samples * 100)

            # Attendre avant le prochain échantillon (sauf pour le dernier)
            if i < num_samples - 1:
                time.sleep(interval)

        # Validation finale du nombre d'échantillons
        if len(samples) < min_required:
            task["status"] = "error"
            task["error_message"] = f"Échantillons insuffisants ({len(samples)}/{min_required} requis)"
            logger.error(f"Task {task_id}: Échec - échantillons insuffisants")
            return

        # Calcul des moyennes
        avg_total = sum(s["total_power"] for s in samples) / len(samples)
        avg_heater = sum(s["heater_power"] for s in samples) / len(samples)

        logger.info(f"Task {task_id}: Moyennes calculées - Total: {avg_total:.1f}W, Ballon: {avg_heater:.1f}W")

        # Enregistrer le tag avec les moyennes
        result = cop_learning_module.record_tag(
            tag="on",
            outdoor_temp=task["outdoor_temp"],
            total_power=avg_total,
            heater_power=avg_heater,
            notes=task["notes"],
            config=config
        )

        # Finaliser la tâche
        task["status"] = "completed"
        task["completed_at"] = datetime.now()
        task["tag_id"] = result.get("tag_id")
        task["calculated_cop"] = result.get("calculated_cop")
        task["deduced_ac_power"] = result.get("deduced_ac_power")
        task["validation_message"] = result.get("validation_message")

        logger.info(f"Task {task_id}: Terminée avec succès - COP={task['calculated_cop']}, "
                   f"Puissance={task['deduced_ac_power']}W")

    except Exception as e:
        task["status"] = "error"
        task["error_message"] = f"Erreur inattendue: {str(e)}"
        logger.exception(f"Task {task_id}: Erreur critique")


def get_task_status(task_id: str) -> Optional[Dict]:
    """
    Récupère le statut actuel d'une tâche d'échantillonnage.

    Args:
        task_id: Identifiant de la tâche

    Returns:
        Dictionnaire avec le statut, ou None si la tâche n'existe pas
    """
    task = _sampling_tasks.get(task_id)
    if not task:
        return None

    # Calculer les temps
    elapsed = (datetime.now() - task["created_at"]).total_seconds()

    # Estimer le temps restant pour les tasks en cours
    estimated_remaining = None
    if task["status"] == "running" and task["progress"] > 0:
        total_estimated = elapsed / (task["progress"] / 100)
        estimated_remaining = int(total_estimated - elapsed)

    return {
        "task_id": task_id,
        "status": task["status"],
        "progress": task["progress"],
        "samples_collected": task["samples_collected"],
        "samples_target": task["samples_target"],
        "elapsed_seconds": int(elapsed),
        "estimated_remaining_seconds": estimated_remaining,
        "error_message": task.get("error_message"),
        "tag_id": task.get("tag_id"),
        "calculated_cop": task.get("calculated_cop"),
        "deduced_ac_power": task.get("deduced_ac_power"),
        "validation_message": task.get("validation_message"),
        "samples": task.get("samples", []),
        "outdoor_temp": task.get("outdoor_temp")
    }


def cancel_task(task_id: str) -> bool:
    """
    Annule une tâche d'échantillonnage en cours.

    Args:
        task_id: Identifiant de la tâche

    Returns:
        True si la tâche a été annulée, False si introuvable
    """
    task = _sampling_tasks.get(task_id)
    if not task:
        return False

    if task["status"] == "running":
        task["status"] = "cancelled"
        logger.info(f"Task {task_id}: Annulation demandée")

    return True


def cleanup_old_tasks(max_age_minutes: int = 10):
    """
    Nettoie les tâches terminées anciennes pour libérer la mémoire.

    Args:
        max_age_minutes: Âge maximum des tâches à conserver (en minutes)
    """
    now = datetime.now()
    cutoff = now - timedelta(minutes=max_age_minutes)

    tasks_to_delete = []

    for task_id, task in _sampling_tasks.items():
        # Ne supprimer que les tasks terminées (completed, error, cancelled)
        if task["status"] in ["completed", "error", "cancelled"]:
            created_at = task["created_at"]
            if created_at < cutoff:
                tasks_to_delete.append(task_id)

    for task_id in tasks_to_delete:
        del _sampling_tasks[task_id]
        logger.debug(f"Task {task_id}: Nettoyée (ancienne)")

    if tasks_to_delete:
        logger.info(f"Nettoyage: {len(tasks_to_delete)} tâche(s) supprimée(s)")
