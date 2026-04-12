"""
Module d'apprentissage automatique du COP.

Surveille l'état de la clim via Home Assistant et lance automatiquement
des cycles d'échantillonnage avec validation statistique pour rejeter
les mesures faussées par d'autres appareils (plaque, four, voiture, etc.).
"""

import logging
import time
import statistics
from datetime import datetime, timedelta
from threading import Thread, Event
from typing import Optional, Dict, List

from modules import homeassistant as ha
from modules import cop_learning
from modules import cop_sampling
from modules import weather
from modules.migrate import run as migrate_run

logger = logging.getLogger(__name__)

# État global du worker
_worker_thread: Optional[Thread] = None
_worker_stop_event: Event = Event()
_worker_status = {
    "enabled": False,
    "running": False,
    "last_check": None,
    "last_clim_state": None,
    "last_sample_time": None,
    "current_sampling_task_id": None,
    "stats": {
        "total_detections": 0,
        "total_samples_started": 0,
        "total_samples_successful": 0,
        "total_samples_rejected": 0,
    }
}

# Historique des rejets (en mémoire, limité aux 50 derniers)
_rejection_history: List[Dict] = []


def start_worker(config) -> bool:
    """Démarre le worker d'auto-learning en arrière-plan."""
    global _worker_thread, _worker_stop_event, _worker_status

    if not config.COP_LEARNING.get("enabled"):
        logger.warning("COP_LEARNING désactivé, worker non démarré")
        return False

    auto_cfg = config.COP_LEARNING.get("auto_learning", {})
    if not auto_cfg.get("enabled"):
        logger.info("Auto-learning désactivé dans la config")
        return False

    if not ha.is_clim_configured(config.HOME_ASSISTANT):
        logger.warning("Clim non configurée dans Home Assistant, worker non démarré")
        return False

    if _worker_thread and _worker_thread.is_alive():
        logger.warning("Worker auto-learning déjà actif")
        return False

    _worker_stop_event.clear()
    _worker_status["enabled"] = True
    _worker_status["running"] = True

    _worker_thread = Thread(target=_worker_loop, args=(config,), daemon=True)
    _worker_thread.start()

    logger.info("🤖 Worker auto-learning COP démarré")
    return True


def stop_worker() -> bool:
    """Arrête le worker d'auto-learning."""
    global _worker_stop_event, _worker_status

    if not _worker_status.get("running"):
        return False

    logger.info("Arrêt du worker auto-learning...")
    _worker_stop_event.set()
    _worker_status["enabled"] = False
    _worker_status["running"] = False

    return True


def _worker_loop(config):
    """Boucle principale du worker de surveillance."""
    global _worker_status, _worker_stop_event

    auto_cfg = config.COP_LEARNING.get("auto_learning", {})
    polling_interval = auto_cfg.get("polling_interval_seconds", 45)

    logger.info(f"Worker auto-learning : polling toutes les {polling_interval}s")

    while not _worker_stop_event.is_set():
        try:
            _check_and_sample(config)
        except Exception as e:
            logger.exception("Erreur dans worker auto-learning")

        # Attendre l'intervalle de polling (avec possibilité d'interruption)
        _worker_stop_event.wait(polling_interval)

    logger.info("Worker auto-learning arrêté")
    _worker_status["running"] = False


def _check_and_sample(config):
    """Vérifie l'état de la clim et lance un échantillonnage si nécessaire."""
    global _worker_status

    auto_cfg = config.COP_LEARNING.get("auto_learning", {})
    now = datetime.now()

    _worker_status["last_check"] = now.isoformat()

    # 1. Vérifier si un échantillonnage est déjà en cours
    current_task = _worker_status.get("current_sampling_task_id")
    if current_task:
        task_status = cop_sampling.get_task_status(current_task)
        if task_status and task_status["status"] == "running":
            logger.debug("Échantillonnage déjà en cours, skip")
            return
        elif task_status and task_status["status"] == "completed":
            # Valider le résultat
            _validate_and_finalize(current_task, config)
            _worker_status["current_sampling_task_id"] = None
        elif task_status and task_status["status"] in ("error", "cancelled"):
            logger.warning(f"Échantillonnage {current_task} échoué : {task_status.get('error_message')}")
            _log_rejection(now, None, "sampling_failed", task_status.get("error_message", "Unknown error"))
            _worker_status["current_sampling_task_id"] = None
            _worker_status["stats"]["total_samples_rejected"] += 1

    # 2. Récupérer l'état actuel de la clim
    clim_state = ha.get_clim_state(config.HOME_ASSISTANT)
    if not clim_state:
        logger.debug("État clim indisponible")
        return

    current_hvac_mode = clim_state.get("state", "off")
    last_hvac_mode = _worker_status.get("last_clim_state")

    _worker_status["last_clim_state"] = current_hvac_mode

    # 3. Détecter transition OFF → heat
    if last_hvac_mode in (None, "off") and current_hvac_mode == "heat":
        logger.info(f"🔥 Transition détectée : {last_hvac_mode} → {current_hvac_mode}")
        _worker_status["stats"]["total_detections"] += 1

        # Vérifier les conditions pour lancer l'échantillonnage
        can_sample, reason = _can_start_sampling(config, now)

        if can_sample:
            _schedule_sampling(config, now)
        else:
            logger.info(f"❌ Échantillonnage refusé : {reason}")
            _log_rejection(now, None, "precondition_failed", reason)


def _can_start_sampling(config, now: datetime) -> tuple[bool, str]:
    """Vérifie si les conditions sont réunies pour démarrer un échantillonnage."""
    auto_cfg = config.COP_LEARNING.get("auto_learning", {})

    # 1. Vérifier l'intervalle minimum entre échantillonnages
    last_sample = _worker_status.get("last_sample_time")
    if last_sample:
        min_interval_hours = auto_cfg.get("min_interval_between_samples_hours", 2)
        if isinstance(last_sample, str):
            last_sample = datetime.fromisoformat(last_sample)
        elapsed = (now - last_sample).total_seconds() / 3600
        if elapsed < min_interval_hours:
            return False, f"Dernier échantillonnage il y a {elapsed:.1f}h (min: {min_interval_hours}h)"

    # 2. Vérifier les horaires blackout (heures de cuisine)
    blackout_hours = auto_cfg.get("blackout_hours", [])
    current_time = now.strftime("%H:%M")
    for period in blackout_hours:
        if period["start"] <= current_time <= period["end"]:
            return False, f"Période blackout ({period['start']}-{period['end']})"

    # 3. Vérifier le planning du thermostat (si configuré)
    if auto_cfg.get("only_during_thermostat_schedule", True):
        thermostat_cfg = config.THERMOSTAT
        if thermostat_cfg.get("enabled"):
            schedule = thermostat_cfg.get("schedule", {})
            weekday = now.strftime("%a").lower()[:3]  # mon, tue, etc.
            day_schedule = schedule.get(weekday)
            if day_schedule:
                if not (day_schedule["start"] <= current_time <= day_schedule["end"]):
                    return False, f"Hors planning chauffage ({day_schedule['start']}-{day_schedule['end']})"

    # 4. Vérifier les capteurs Shelly
    cop_cfg = config.COP_LEARNING
    sensors = cop_learning.get_current_sensors(config.HOME_ASSISTANT, cop_cfg)
    if not sensors:
        return False, "Capteurs Shelly indisponibles"

    # 5. Vérifier que le ballon n'est pas en chauffe
    heater_threshold = auto_cfg.get("heater_power_threshold", 50)
    if sensors["heater_power"] > heater_threshold:
        return False, f"Ballon en chauffe ({sensors['heater_power']:.0f}W > {heater_threshold}W)"

    return True, "OK"


def _schedule_sampling(config, detection_time: datetime):
    """Programme un échantillonnage avec délai de stabilisation."""
    global _worker_status

    auto_cfg = config.COP_LEARNING.get("auto_learning", {})
    cooldown_minutes = auto_cfg.get("cooldown_after_startup_minutes", 5)

    logger.info(f"⏳ Échantillonnage programmé dans {cooldown_minutes} min (stabilisation clim)")

    # Lancer un thread qui attend puis lance l'échantillonnage
    Thread(
        target=_delayed_sampling,
        args=(config, detection_time, cooldown_minutes),
        daemon=True
    ).start()


def _delayed_sampling(config, detection_time: datetime, delay_minutes: int):
    """Attend le délai de stabilisation puis lance l'échantillonnage."""
    global _worker_status

    time.sleep(delay_minutes * 60)

    # Re-vérifier les conditions (au cas où elles auraient changé)
    can_sample, reason = _can_start_sampling(config, datetime.now())
    if not can_sample:
        logger.warning(f"❌ Conditions changées après stabilisation : {reason}")
        _log_rejection(detection_time, None, "condition_changed", reason)
        return

    # Récupérer la température extérieure
    outdoor_temp = None
    try:
        outdoor_temp = weather.get_current_temperature()
    except Exception as e:
        logger.warning(f"Température extérieure indisponible : {e}")

    if outdoor_temp is None:
        logger.warning("❌ Température extérieure indisponible, échantillonnage annulé")
        _log_rejection(detection_time, None, "no_outdoor_temp", "Température extérieure indisponible")
        return

    # Lancer l'échantillonnage
    try:
        task_id = cop_sampling.start_sampling_task(
            notes=f"Auto-learning (T_ext={outdoor_temp:.1f}°C)",
            outdoor_temp=outdoor_temp,
            config=config
        )

        _worker_status["current_sampling_task_id"] = task_id
        _worker_status["last_sample_time"] = detection_time.isoformat()
        _worker_status["stats"]["total_samples_started"] += 1

        logger.info(f"✅ Échantillonnage démarré : task_id={task_id}")
    except Exception as e:
        logger.exception("Erreur démarrage échantillonnage")
        _log_rejection(detection_time, outdoor_temp, "sampling_error", str(e))


def _validate_and_finalize(task_id: str, config):
    """Valide statistiquement un échantillonnage terminé."""
    global _worker_status

    task_status = cop_sampling.get_task_status(task_id)
    if not task_status:
        return

    samples = task_status.get("samples", [])
    outdoor_temp = task_status.get("outdoor_temp")
    calculated_cop = task_status.get("calculated_cop")
    deduced_ac_power = task_status.get("deduced_ac_power")

    if not samples or calculated_cop is None:
        logger.warning(f"Task {task_id} : données incomplètes")
        _log_rejection(datetime.now(), outdoor_temp, "incomplete_data", "Données d'échantillonnage incomplètes")
        _worker_status["stats"]["total_samples_rejected"] += 1
        return

    # Validation statistique
    validation_cfg = config.COP_LEARNING.get("auto_learning", {}).get("validation", {})

    is_valid, rejection_reason = _validate_samples(
        samples, outdoor_temp, calculated_cop, deduced_ac_power, config, validation_cfg
    )

    if is_valid:
        logger.info(f"✅ Échantillonnage {task_id} validé : COP={calculated_cop:.2f} à {outdoor_temp:.1f}°C")
        _worker_status["stats"]["total_samples_successful"] += 1
        # Le tag a déjà été enregistré par cop_sampling
    else:
        logger.warning(f"❌ Échantillonnage {task_id} rejeté : {rejection_reason}")
        _log_rejection(datetime.now(), outdoor_temp, "validation_failed", rejection_reason)
        _worker_status["stats"]["total_samples_rejected"] += 1

        # Supprimer le tag erroné
        tag_id = task_status.get("tag_id")
        if tag_id:
            cop_learning.delete_tag(tag_id, config)


def _validate_samples(samples: List[Dict], outdoor_temp: float, calculated_cop: float,
                      deduced_ac_power: float, config, validation_cfg: Dict) -> tuple[bool, str]:
    """
    Valide statistiquement les échantillons.
    Retourne (is_valid, rejection_reason).
    """

    # 1. Variance des échantillons
    total_powers = [s["total_power"] for s in samples]
    if len(total_powers) > 1:
        variance = statistics.variance(total_powers)
        max_variance = validation_cfg.get("max_variance_watts", 200)

        if variance > max_variance ** 2:  # variance = écart-type²
            return False, f"Variance trop élevée ({variance:.0f}W² > {max_variance**2}W²) → autre appareil suspecté"

    # 2. Détection de sauts de puissance
    max_jump = validation_cfg.get("max_power_jump_watts", 500)
    for i in range(1, len(total_powers)):
        jump = abs(total_powers[i] - total_powers[i-1])
        if jump > max_jump:
            return False, f"Saut de puissance détecté ({jump:.0f}W > {max_jump}W) → appareil démarré pendant échantillonnage"

    # 3. Comparaison avec courbe théorique
    theoretical_cop = _get_theoretical_cop(outdoor_temp, config.CLIM["cop_curve"])
    if theoretical_cop:
        max_deviation = validation_cfg.get("max_deviation_from_theoretical", 0.5)
        deviation = abs(calculated_cop - theoretical_cop) / theoretical_cop

        if deviation > max_deviation:
            return False, f"COP={calculated_cop:.2f} trop éloigné du théorique ({theoretical_cop:.2f}, écart {deviation*100:.0f}% > {max_deviation*100:.0f}%)"

    # 4. Comparaison avec historique
    learned_curve = cop_learning.get_cop_curve_learned()
    if learned_curve:
        historical_cop = _get_cop_from_curve(outdoor_temp, learned_curve)
        if historical_cop:
            max_deviation_hist = validation_cfg.get("max_deviation_from_history", 0.3)
            deviation_hist = abs(calculated_cop - historical_cop) / historical_cop

            if deviation_hist > max_deviation_hist:
                return False, f"COP={calculated_cop:.2f} trop éloigné de l'historique ({historical_cop:.2f}, écart {deviation_hist*100:.0f}% > {max_deviation_hist*100:.0f}%)"

    # 5. Vérifier que la puissance clim est dans des limites raisonnables
    min_ac = config.COP_LEARNING.get("min_ac_power", 500)
    max_ac = config.COP_LEARNING.get("max_ac_power", 4500)

    if not (min_ac <= deduced_ac_power <= max_ac):
        return False, f"Puissance clim ({deduced_ac_power:.0f}W) hors limites ({min_ac}-{max_ac}W)"

    return True, "OK"


def _get_theoretical_cop(outdoor_temp: float, cop_curve: List[tuple]) -> Optional[float]:
    """Interpole le COP théorique depuis la courbe de config."""
    if not cop_curve:
        return None

    # Trouver les deux points encadrants
    sorted_curve = sorted(cop_curve, key=lambda x: x[0])

    if outdoor_temp <= sorted_curve[0][0]:
        return sorted_curve[0][1]
    if outdoor_temp >= sorted_curve[-1][0]:
        return sorted_curve[-1][1]

    for i in range(len(sorted_curve) - 1):
        t1, cop1 = sorted_curve[i]
        t2, cop2 = sorted_curve[i + 1]

        if t1 <= outdoor_temp <= t2:
            # Interpolation linéaire
            ratio = (outdoor_temp - t1) / (t2 - t1)
            return cop1 + ratio * (cop2 - cop1)

    return None


def _get_cop_from_curve(outdoor_temp: float, curve: List[tuple]) -> Optional[float]:
    """Récupère le COP depuis une courbe apprise (bin le plus proche)."""
    if not curve:
        return None

    closest = min(curve, key=lambda x: abs(x[0] - outdoor_temp))

    # Ne retourner que si la température est proche (±3°C)
    if abs(closest[0] - outdoor_temp) <= 3:
        return closest[1]

    return None


def _log_rejection(timestamp: datetime, outdoor_temp: Optional[float], reason_code: str, reason_message: str):
    """Enregistre un rejet dans l'historique."""
    global _rejection_history

    entry = {
        "timestamp": timestamp.isoformat(),
        "outdoor_temp": outdoor_temp,
        "reason_code": reason_code,
        "reason_message": reason_message
    }

    _rejection_history.insert(0, entry)

    # Limiter à 50 entrées
    if len(_rejection_history) > 50:
        _rejection_history = _rejection_history[:50]

    logger.info(f"📝 Rejet enregistré : {reason_code} - {reason_message}")


def get_status() -> Dict:
    """Retourne le statut actuel du worker."""
    return {
        **_worker_status,
        "rejection_count": len(_rejection_history),
    }


def get_rejection_history(limit: int = 20) -> List[Dict]:
    """Retourne l'historique des rejets."""
    return _rejection_history[:limit]


def toggle_worker(config, enable: bool) -> bool:
    """Active ou désactive le worker."""
    if enable:
        return start_worker(config)
    else:
        return stop_worker()
