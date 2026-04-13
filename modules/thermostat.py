"""
Thermostat automatique — pilotage du poêle basé sur la température intérieure.
"""
import json
import logging
import os
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

STATE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "thermostat_state.json"
)

DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

ALERT_FAILURES_THRESHOLD = 3    # nombre de checks consécutifs en échec avant alerte
ALERT_RESEND_HOURS = 4          # délai min entre deux alertes (heures)


def _load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                state = json.load(f)

                # Migration automatique : anciens fichiers sans system_history
                if "system_history" not in state:
                    logger.info("Migration : création de system_history depuis les champs legacy")
                    system_history = {
                        "poele": {"last_turned_on": None, "last_turned_off": None},
                        "clim": {"last_turned_on": None, "last_turned_off": None}
                    }

                    # Migrer les timestamps legacy selon active_system
                    active = state.get("active_system")
                    last_on = state.get("last_turned_on")
                    last_off = state.get("last_turned_off")

                    if active in ("poele", "clim") and last_on:
                        system_history[active]["last_turned_on"] = last_on

                    # Si éteint, on met last_off dans le système qui était actif, ou les deux si inconnu
                    if last_off:
                        if active in ("poele", "clim"):
                            system_history[active]["last_turned_off"] = last_off
                        else:
                            # Active system inconnu : mettre dans les deux pour ne pas perdre l'info
                            system_history["poele"]["last_turned_off"] = last_off
                            system_history["clim"]["last_turned_off"] = last_off

                    state["system_history"] = system_history

                return state
        except Exception:
            pass
    return {
        "state": "off",
        "active_system": None,
        "last_turned_on": None,
        "last_turned_off": None,
        "sensor_failures": 0,
        "last_alert_sent": None,
        "suspended_until": None,
        "system_history": {
            "poele": {"last_turned_on": None, "last_turned_off": None},
            "clim": {"last_turned_on": None, "last_turned_off": None}
        }
    }


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def _update_system_timestamp(state: dict, system: str, event_type: str) -> None:
    """Met à jour le timestamp d'un système (poêle/clim) pour un événement (on/off).

    Args:
        state: État du thermostat
        system: "poele" ou "clim"
        event_type: "on" ou "off"
    """
    if "system_history" not in state:
        state["system_history"] = {
            "poele": {"last_turned_on": None, "last_turned_off": None},
            "clim": {"last_turned_on": None, "last_turned_off": None}
        }

    if system not in ("poele", "clim"):
        return

    timestamp = datetime.now().isoformat()
    field = f"last_turned_{event_type}"

    # Mettre à jour system_history
    state["system_history"][system][field] = timestamp

    # Maintenir les champs legacy pour rétrocompatibilité
    state[field] = timestamp


def _get_system_timestamp(state: dict, system: str, event_type: str) -> str | None:
    """Récupère le timestamp d'un système pour un événement.

    Args:
        state: État du thermostat
        system: "poele" ou "clim"
        event_type: "on" ou "off"

    Returns:
        Le timestamp ISO ou None
    """
    if "system_history" not in state:
        return None

    if system not in ("poele", "clim"):
        return None

    field = f"last_turned_{event_type}"
    return state["system_history"][system].get(field)


def get_state() -> dict:
    """Retourne l'état courant du thermostat."""
    state = _load_state()
    if state.get("suspended_until"):
        try:
            if datetime.now() >= datetime.fromisoformat(state["suspended_until"]):
                state = {**state, "suspended_until": None}
                _save_state(state)
        except Exception:
            pass
    return state


def get_vacation() -> dict:
    """Retourne les dates de vacances stockées (start/end en YYYY-MM-DD, ou None)."""
    state = _load_state()
    return {"start": state.get("vacation_start"), "end": state.get("vacation_end")}


def set_vacation(start: str, end: str) -> None:
    """Active le mode vacances du date start au date end (YYYY-MM-DD inclus)."""
    state = _load_state()
    _save_state({**state, "vacation_start": start, "vacation_end": end})


def clear_vacation() -> None:
    """Annule le mode vacances."""
    state = _load_state()
    _save_state({**state, "vacation_start": None, "vacation_end": None})


def is_on_vacation() -> bool:
    """Retourne True si la date d'aujourd'hui est dans la période de vacances."""
    state = _load_state()
    start = state.get("vacation_start")
    end = state.get("vacation_end")
    if not start or not end:
        return False
    try:
        today = datetime.now().date()
        return datetime.fromisoformat(start).date() <= today <= datetime.fromisoformat(end).date()
    except Exception:
        return False


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


def _send_sensor_alert(email_cfg: dict, subject: str, message: str) -> None:
    """Envoie un email d'alerte thermostat."""
    if not email_cfg.get("enabled"):
        return
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from modules.crypto import decrypt_password

    html = f"""<!DOCTYPE html>
<html lang="fr">
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#0f1117;font-family:-apple-system,sans-serif;color:#e2e8f0">
  <div style="max-width:480px;margin:32px auto;background:#1a1d27;border-radius:16px;overflow:hidden;border:1px solid #2a2d3e">
    <div style="background:#0f1117;padding:20px 24px;border-bottom:1px solid #2a2d3e">
      <p style="margin:0;font-size:0.85em;color:#8892a4">🌡️ Thermostat automatique — Heating Advisor</p>
      <h1 style="margin:4px 0 0;font-size:1.1em">{subject}</h1>
    </div>
    <div style="padding:20px 24px">
      <p style="margin:0;color:#8892a4;line-height:1.6">{message}</p>
      <p style="margin:16px 0 0;font-size:0.8em;color:#8892a4">{datetime.now().strftime('%d/%m/%Y à %Hh%M')}</p>
    </div>
  </div>
</body>
</html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = email_cfg["sender"]
    msg["To"] = ", ".join(email_cfg["recipients"])
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        with smtplib.SMTP(email_cfg["smtp_host"], email_cfg["smtp_port"]) as server:
            server.ehlo()
            server.starttls()
            login = email_cfg.get("smtp_login") or email_cfg["sender"]
            server.login(login, decrypt_password(email_cfg["app_password"]))
            server.sendmail(email_cfg["sender"], email_cfg["recipients"], msg.as_string())
        logger.info("Alerte thermostat envoyée : %s", subject)
    except Exception as e:
        logger.error("Échec envoi alerte thermostat : %s", e)


def _handle_sensor_failure(state: dict, email_cfg: dict) -> dict:
    """Incrémente le compteur d'échecs et envoie une alerte si nécessaire."""
    failures = state.get("sensor_failures", 0) + 1
    last_alert = state.get("last_alert_sent")
    state = {**state, "sensor_failures": failures}

    should_alert = failures >= ALERT_FAILURES_THRESHOLD
    if should_alert and last_alert:
        elapsed = (datetime.now() - datetime.fromisoformat(last_alert)).total_seconds() / 3600
        should_alert = elapsed >= ALERT_RESEND_HOURS

    if should_alert:
        logger.warning("Thermostat : alerte sonde injoignable (%d échecs)", failures)
        _send_sensor_alert(
            email_cfg,
            "⚠️ Sonde température injoignable",
            f"La sonde de température intérieure n'est pas accessible depuis {failures} vérifications "
            f"(~{failures * 10} min).<br><br>"
            "Le poêle sera éteint aux horaires prévus mais ne pourra pas être piloté par la température "
            "tant que la sonde est hors ligne.",
        )
        state["last_alert_sent"] = datetime.now().isoformat()

    _save_state(state)
    return state


def _handle_sensor_recovery(state: dict, email_cfg: dict) -> dict:
    """Réinitialise le compteur et envoie un email de retour à la normale si nécessaire."""
    was_failing = state.get("sensor_failures", 0) >= ALERT_FAILURES_THRESHOLD
    state = {**state, "sensor_failures": 0}
    _save_state(state)
    if was_failing:
        logger.info("Thermostat : sonde température de nouveau joignable")
        _send_sensor_alert(
            email_cfg,
            "✅ Sonde température de nouveau joignable",
            "La sonde de température intérieure est à nouveau accessible. "
            "Le thermostat reprend son fonctionnement normal.",
        )
    return state


def felt_temperature(temp: float, humidity: float, cfg: dict) -> float:
    """Calcule la température ressentie en tenant compte de l'humidité."""
    if not cfg.get("use_felt_temperature") or humidity is None:
        return temp
    ref = cfg.get("humidity_reference", 50.0)
    factor = cfg.get("humidity_correction_factor", 0.05)
    return round(temp + (humidity - ref) * factor, 1)


def next_schedule_start(cfg: dict) -> str | None:
    """Retourne l'ISO datetime du prochain début de plage horaire (dans les 7 prochains jours)."""
    now = datetime.now()
    for delta in range(8):
        check = now + timedelta(days=delta)
        day_key = DAY_KEYS[check.weekday()]
        schedule = cfg.get("schedule", {}).get(day_key)
        if not schedule:
            continue
        try:
            start_h, start_m = map(int, schedule["start"].split(":"))
            start = check.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
            if start > now:
                return start.isoformat()
        except Exception:
            continue
    return None


def _turn_off_active_system(ha_client, ha_cfg: dict, active_system: str | None) -> bool:
    """Éteint le système actuellement actif (poêle ou clim)."""
    if active_system == "clim":
        return ha_client.turn_off_clim(ha_cfg)
    else:
        return ha_client.turn_off(ha_cfg)


def _system_label(system: str | None) -> str:
    """Retourne le label français du système."""
    return {"poele": "poêle", "clim": "clim"}.get(system, system or "inconnu")


def _system_icon(system: str | None) -> str:
    """Retourne l'icône emoji du système."""
    return {"poele": "🔥", "clim": "❄️"}.get(system, "⏹")


def check_and_apply(ha_cfg: dict, thermostat_cfg: dict, recommendation: str, email_cfg: dict = None,
                    ntfy_cfg: dict = None, outdoor_temp: float = None) -> None:
    """
    Vérifie la température intérieure et pilote le poêle et/ou la clim.
    recommendation : 'poele', 'clim', 'none', None
    """
    from modules.ntfy_push import send as ntfy_send
    if not thermostat_cfg.get("enabled"):
        return
    if is_on_vacation():
        state = _load_state()
        if state.get("state") == "on":
            import modules.homeassistant as ha_client
            active = state.get("active_system", "poele")
            logger.info("Thermostat : extinction %s — mode vacances actif", _system_label(active))
            _turn_off_active_system(ha_client, ha_cfg, active)
            new_state = {**state, "state": "off", "active_system": None}
            _update_system_timestamp(new_state, active, "off")
            _save_state(new_state)
            ntfy_send(f"✈️ {_system_label(active).capitalize()} éteint", "Mode vacances actif — thermostat suspendu.", ntfy_cfg)
        return
    if not ha_cfg.get("enabled") or not ha_cfg.get("url") or not ha_cfg.get("token"):
        logger.warning("Thermostat : Home Assistant non configuré, skip")
        return

    email_cfg = email_cfg or {}
    import modules.homeassistant as ha_client

    clim_available = ha_client.is_clim_configured(ha_cfg)

    indoor = ha_client.get_indoor_climate(ha_cfg)
    sensor_ok = indoor is not None and indoor.get("temperature") is not None

    state = _load_state()

    # ── Chevauchement clim → poêle : vérification expiration ────
    clim_overlap_str = state.get("clim_overlap_until")
    if clim_overlap_str:
        try:
            if datetime.now() >= datetime.fromisoformat(clim_overlap_str):
                logger.info("Thermostat : fin chevauchement clim → poêle, extinction clim")
                ha_client.turn_off_clim(ha_cfg)
                _update_system_timestamp(state, "clim", "off")
                state = {**state, "clim_overlap_until": None}
                _save_state(state)
            else:
                remaining_min = int((datetime.fromisoformat(clim_overlap_str) - datetime.now()).total_seconds() / 60)
                logger.debug("Thermostat : chevauchement clim → poêle actif, extinction clim dans %d min", remaining_min)
        except Exception:
            state = {**state, "clim_overlap_until": None}
            _save_state(state)

    if not sensor_ok:
        logger.warning("Thermostat : température intérieure indisponible")
        state = _handle_sensor_failure(state, email_cfg)

        # ── Extinction de sécurité si hors plage horaire ──────
        if state.get("state") == "on":
            in_schedule = is_in_schedule(thermostat_cfg)
            if not in_schedule:
                active = state.get("active_system", "poele")
                last_on_str = _get_system_timestamp(state, active, "on")
                last_on = datetime.fromisoformat(last_on_str) if last_on_str else None
                on_minutes = (datetime.now() - last_on).total_seconds() / 60 if last_on else 9999
                grace = thermostat_cfg.get("end_of_schedule_grace_minutes", 45)
                if on_minutes >= grace:
                    logger.info("Thermostat : extinction sécurité fin de plage (sonde HS) — %s", _system_label(active))
                    _turn_off_active_system(ha_client, ha_cfg, active)
                    new_state = {
                        **state,
                        "state": "off",
                        "active_system": None,
                    }
                    _update_system_timestamp(new_state, active, "off")
                    _save_state(new_state)
                    ntfy_send(
                        f"⚠️ {_system_label(active).capitalize()} éteint",
                        "Extinction sécurité — sonde hors service, fin de plage horaire.",
                        ntfy_cfg,
                    )
        return

    # Sonde OK — réinitialise le compteur d'échecs si nécessaire
    state = _handle_sensor_recovery(state, email_cfg)

    temp = indoor["temperature"]
    humidity = indoor.get("humidity")
    effective_temp = felt_temperature(temp, humidity, thermostat_cfg)
    temp_on = thermostat_cfg.get("temp_on", 20.0)
    temp_off = thermostat_cfg.get("temp_off", 22.9)
    min_on_poele = thermostat_cfg.get("min_on_minutes", 90)
    min_on_clim = thermostat_cfg.get("min_on_minutes_clim", 15)
    grace = thermostat_cfg.get("end_of_schedule_grace_minutes", 45)

    if thermostat_cfg.get("use_felt_temperature") and humidity is not None:
        logger.debug("Thermostat : temp réelle=%.1f°C, humidité=%.0f%%, ressentie=%.1f°C", temp, humidity, effective_temp)

    in_schedule = is_in_schedule(thermostat_cfg)

    # ── Synchronisation avec l'état réel HA (poêle + clim) ───
    ha_state = ha_client.get_state(ha_cfg)
    poele_real_on = ha_state is not None and ha_state.get("state") not in ("off", "unavailable", "unknown", None)

    clim_real_on = False
    if clim_available:
        clim_state = ha_client.get_clim_state(ha_cfg)
        clim_real_on = clim_state is not None and clim_state.get("state") not in ("off", "unavailable", "unknown", None)

    current = state.get("state", "off")
    active_system = state.get("active_system")

    # Sync : un système allumé manuellement
    if current == "off":
        if poele_real_on:
            was_suspended = bool(state.get("suspended_until"))
            if was_suspended:
                logger.info("Thermostat : poêle rallumé manuellement pendant la suspension → suspension annulée")
            else:
                logger.info("Thermostat : poêle allumé manuellement, synchronisation état → on")
            state = {**state, "state": "on", "active_system": "poele", "suspended_until": None}
            _update_system_timestamp(state, "poele", "on")
            _save_state(state)
            current = "on"
            active_system = "poele"
        elif clim_real_on:
            was_suspended = bool(state.get("suspended_until"))
            if was_suspended:
                logger.info("Thermostat : clim allumée manuellement pendant la suspension → suspension annulée")
            else:
                logger.info("Thermostat : clim allumée manuellement, synchronisation état → on")
            state = {**state, "state": "on", "active_system": "clim", "suspended_until": None}
            _update_system_timestamp(state, "clim", "on")
            _save_state(state)
            current = "on"
            active_system = "clim"

    elif current == "on":
        # Vérifier si le système actif a été éteint manuellement
        system_still_on = (active_system == "poele" and poele_real_on) or (active_system == "clim" and clim_real_on)
        in_clim_overlap = bool(state.get("clim_overlap_until"))
        if not system_still_on:
            # Vérifier si l'autre système a été allumé manuellement (transition manuelle)
            if active_system == "poele" and clim_real_on and in_clim_overlap:
                pass  # chevauchement clim → poêle : clim encore active normalement, pas une transition manuelle
            elif active_system == "poele" and clim_real_on:
                logger.info("Thermostat : transition manuelle poêle → clim détectée")
                state = {**state, "active_system": "clim"}
                _update_system_timestamp(state, "clim", "on")
                _save_state(state)
                active_system = "clim"
            elif active_system == "clim" and poele_real_on:
                logger.info("Thermostat : transition manuelle clim → poêle détectée")
                state = {**state, "active_system": "poele"}
                _update_system_timestamp(state, "poele", "on")
                _save_state(state)
                active_system = "poele"
            else:
                # Système éteint manuellement → suspension
                # Capturer le système actif AVANT de le mettre à None
                prev_system = active_system
                suspend_hours = thermostat_cfg.get("manual_off_suspend_hours", 4)
                suspended_until = (datetime.now() + timedelta(hours=suspend_hours)).isoformat()
                logger.info(
                    "Thermostat : %s éteint manuellement, suspension %dh jusqu'à %s",
                    _system_label(prev_system), suspend_hours, suspended_until[:16].replace("T", " "),
                )
                state = {
                    **state,
                    "state": "off",
                    "active_system": None,
                    "suspended_until": suspended_until,
                }
                _update_system_timestamp(state, prev_system, "off")
                _save_state(state)
                current = "off"
                active_system = None

    # Récupérer le timestamp du système actif
    last_on_str = _get_system_timestamp(state, active_system, "on") if active_system else None
    last_on = datetime.fromisoformat(last_on_str) if last_on_str else None
    on_minutes = (datetime.now() - last_on).total_seconds() / 60 if last_on else 0
    min_on = min_on_clim if active_system == "clim" else min_on_poele

    # ── Mode absent / proximité ───────────────────────────────
    if thermostat_cfg.get("presence_enabled"):
        person_entities = thermostat_cfg.get("person_entities", [])
        nearby_zone = thermostat_cfg.get("nearby_zone_name", "")
        no_ignition_after = thermostat_cfg.get("nearby_no_ignition_after", 20)
        nearby_grace = thermostat_cfg.get("nearby_grace_minutes", 20)

        if nearby_zone:
            presence = ha_client.get_presence_extended(ha_cfg, person_entities, nearby_zone)
        else:
            raw = ha_client.get_presence(ha_cfg, person_entities)
            presence = "home" if raw else "away" if raw is False else None

        away_grace = thermostat_cfg.get("away_grace_minutes", 5)

        if presence == "home":
            if state.get("nearby_restricted_since") or state.get("away_since"):
                state = {**state, "nearby_restricted_since": None, "away_since": None}
                _save_state(state)

        elif presence == "nearby":
            if state.get("away_since"):
                state = {**state, "away_since": None}
                _save_state(state)
            if datetime.now().hour >= no_ignition_after:
                if not state.get("nearby_restricted_since"):
                    state = {**state, "nearby_restricted_since": datetime.now().isoformat()}
                    _save_state(state)
                    logger.info("Thermostat : zone proximité après %dh — grâce %d min", no_ignition_after, nearby_grace)

                restricted_min = (datetime.now() - datetime.fromisoformat(state["nearby_restricted_since"])).total_seconds() / 60

                if restricted_min >= nearby_grace:
                    logger.debug("Thermostat : zone proximité après %dh, grâce écoulée — pause", no_ignition_after)
                    if current == "on":
                        # Capturer le système actif AVANT de le mettre à None
                        prev_system = active_system
                        logger.info("Thermostat : extinction %s — zone proximité après %dh (grâce écoulée)", _system_label(prev_system), no_ignition_after)
                        _turn_off_active_system(ha_client, ha_cfg, prev_system)
                        new_state = {**state, "state": "off", "active_system": None}
                        _update_system_timestamp(new_state, prev_system, "off")
                        _save_state(new_state)
                    return
                else:
                    logger.debug("Thermostat : zone proximité après %dh, grâce encore %d min", no_ignition_after, int(nearby_grace - restricted_min))
                    if current == "off":
                        return  # pas de nouvel allumage pendant la grâce
            else:
                if state.get("nearby_restricted_since"):
                    state = {**state, "nearby_restricted_since": None}
                    _save_state(state)

        elif presence == "away":
            if state.get("nearby_restricted_since"):
                state = {**state, "nearby_restricted_since": None}
                _save_state(state)
            if not state.get("away_since"):
                state = {**state, "away_since": datetime.now().isoformat()}
                _save_state(state)
                logger.info("Thermostat : tout le monde absent — grâce %d min", away_grace)

            away_min = (datetime.now() - datetime.fromisoformat(state["away_since"])).total_seconds() / 60
            if away_min >= away_grace:
                logger.debug("Thermostat : absence confirmée (%.0f min >= %d min), thermostat en pause", away_min, away_grace)
                if current == "on":
                    # Capturer le système actif AVANT de le mettre à None
                    prev_system = active_system
                    logger.info("Thermostat : extinction %s — absence confirmée", _system_label(prev_system))
                    _turn_off_active_system(ha_client, ha_cfg, prev_system)
                    new_state = {**state, "state": "off", "active_system": None}
                    _update_system_timestamp(new_state, prev_system, "off")
                    _save_state(new_state)
                    ntfy_send(f"🚗 {_system_label(prev_system).capitalize()} éteint", "Tout le monde absent — thermostat en pause.", ntfy_cfg)
                return
            else:
                logger.debug("Thermostat : absence détectée, grâce encore %d min", int(away_grace - away_min))
                if current == "off":
                    return

    # ── Vérification suspension ───────────────────────────────
    suspended_until_str = state.get("suspended_until")
    if suspended_until_str:
        suspended_until = datetime.fromisoformat(suspended_until_str)
        if datetime.now() < suspended_until:
            remaining = int((suspended_until - datetime.now()).total_seconds() / 60)
            logger.debug("Thermostat : suspendu encore %d min", remaining)
            return
        else:
            logger.info("Thermostat : fin de suspension, reprise normale")
            state = {**state, "suspended_until": None}
            _save_state(state)

    if current == "off":
        if in_schedule and effective_temp < temp_on:
            if recommendation == "poele":
                logger.info(
                    "Thermostat : allumage poêle (ressenti %.1f°C < %.1f°C, réel %.1f°C, recommandation=%s)",
                    effective_temp, temp_on, temp, recommendation,
                )
                ha_client.turn_on(ha_cfg)
                new_state = {
                    **state,
                    "state": "on",
                    "active_system": "poele",
                }
                _update_system_timestamp(new_state, "poele", "on")
                _save_state(new_state)
                _ext = f", {outdoor_temp:.1f}°C dehors" if outdoor_temp is not None else ""
                ntfy_send(
                    "🔥 Poêle allumé",
                    f"Intérieur : {temp:.1f}°C (ressenti {effective_temp:.1f}°C){_ext}.",
                    ntfy_cfg,
                )
            elif recommendation == "clim" and clim_available:
                logger.info(
                    "Thermostat : allumage clim (ressenti %.1f°C < %.1f°C, réel %.1f°C, recommandation=%s)",
                    effective_temp, temp_on, temp, recommendation,
                )
                ha_client.turn_on_clim(ha_cfg, temp_off)
                new_state = {
                    **state,
                    "state": "on",
                    "active_system": "clim",
                }
                _update_system_timestamp(new_state, "clim", "on")
                _save_state(new_state)
                _ext = f", {outdoor_temp:.1f}°C dehors" if outdoor_temp is not None else ""
                ntfy_send(
                    "❄️ Clim allumée",
                    f"Intérieur : {temp:.1f}°C (ressenti {effective_temp:.1f}°C){_ext}. Consigne : {temp_off}°C.",
                    ntfy_cfg,
                )
    else:  # current == "on"
        if in_schedule:
            # ── Transition : recommandation changée et min_on respecté ──
            if recommendation in ("poele", "clim") and recommendation != active_system and on_minutes >= min_on:
                can_switch_to_clim = recommendation == "clim" and clim_available
                can_switch_to_poele = recommendation == "poele"

                # ── Transition clim → poêle : chevauchement pour montée en température ──
                if active_system == "clim" and can_switch_to_poele:
                    overlap_min = thermostat_cfg.get("clim_to_poele_overlap_minutes", 20)
                    if effective_temp < temp_on:
                        overlap_until = (datetime.now() + timedelta(minutes=overlap_min)).isoformat()
                        logger.info(
                            "Thermostat : transition clim → poêle avec chevauchement %d min (allumé depuis %.0f min, temp %.1f°C)",
                            overlap_min, on_minutes, effective_temp,
                        )
                        ha_client.turn_on(ha_cfg)
                        new_state = {
                            **state,
                            "state": "on",
                            "active_system": "poele",
                            "clim_overlap_until": overlap_until,
                        }
                        _update_system_timestamp(new_state, "poele", "on")
                        _save_state(new_state)
                        ntfy_send(
                            "🔥 Transition → poêle",
                            f"Poêle allumé, clim maintenue {overlap_min} min le temps de la montée en température. Intérieur : {temp:.1f}°C.",
                            ntfy_cfg,
                        )
                    else:
                        # Temp déjà atteinte : éteindre clim normalement sans démarrer le poêle
                        logger.info(
                            "Thermostat : clim → poêle, temp déjà atteinte (%.1f°C), extinction clim sans démarrer le poêle",
                            effective_temp,
                        )
                        ha_client.turn_off_clim(ha_cfg)
                        prev_system = active_system
                        new_state = {
                            **state,
                            "state": "off",
                            "active_system": None,
                        }
                        _update_system_timestamp(new_state, prev_system, "off")
                        _save_state(new_state)
                        ntfy_send(
                            f"✅ {_system_label(prev_system).capitalize()} éteint",
                            f"Recommandation : {_system_label('poele')} mais temp déjà atteinte ({effective_temp:.1f}°C).",
                            ntfy_cfg,
                        )
                    return

                # ── Transition standard (poêle → clim ou autre) ──────────
                elif can_switch_to_clim or can_switch_to_poele:
                    logger.info(
                        "Thermostat : transition %s → %s (allumé depuis %.0f min, temp %.1f°C)",
                        _system_label(active_system), _system_label(recommendation), on_minutes, effective_temp,
                    )
                    # Éteindre l'ancien
                    _turn_off_active_system(ha_client, ha_cfg, active_system)
                    # Allumer le nouveau si temp encore basse
                    if effective_temp < temp_on:
                        if recommendation == "clim":
                            ha_client.turn_on_clim(ha_cfg, temp_off)
                        else:
                            ha_client.turn_on(ha_cfg)
                        new_state = {
                            **state,
                            "state": "on",
                            "active_system": recommendation,
                        }
                        _update_system_timestamp(new_state, recommendation, "on")
                        _save_state(new_state)
                        ntfy_send(
                            f"{_system_icon(recommendation)} Transition → {_system_label(recommendation)}",
                            f"{_system_label(active_system).capitalize()} éteint, {_system_label(recommendation)} allumé. Intérieur : {temp:.1f}°C.",
                            ntfy_cfg,
                        )
                    else:
                        # Capturer le système avant de le mettre à None
                        prev_system = active_system
                        new_state = {
                            **state,
                            "state": "off",
                            "active_system": None,
                        }
                        _update_system_timestamp(new_state, prev_system, "off")
                        _save_state(new_state)
                        ntfy_send(
                            f"✅ {_system_label(active_system).capitalize()} éteint",
                            f"Recommandation : {_system_label(recommendation)} mais temp déjà atteinte ({effective_temp:.1f}°C).",
                            ntfy_cfg,
                        )
                    return

            # ── Recommandation = "none" et min_on respecté ──
            if recommendation not in ("poele", "clim") and on_minutes >= min_on:
                # Capturer le système actif AVANT de le mettre à None
                prev_system = active_system
                logger.info(
                    "Thermostat : extinction %s — recommandation=%s (allumé depuis %.0f min)",
                    _system_label(prev_system), recommendation, on_minutes,
                )
                _turn_off_active_system(ha_client, ha_cfg, prev_system)
                new_state = {
                    **state,
                    "state": "off",
                    "active_system": None,
                }
                _update_system_timestamp(new_state, prev_system, "off")
                _save_state(new_state)
                ntfy_send(
                    f"⏹ {_system_label(prev_system).capitalize()} éteint",
                    f"Recommandation : aucun chauffage (allumé depuis {int(on_minutes)} min).",
                    ntfy_cfg,
                )
                return

            # ── Température cible atteinte ──
            if effective_temp >= temp_off and on_minutes >= min_on:
                # Capturer le système actif AVANT de le mettre à None
                prev_system = active_system
                logger.info(
                    "Thermostat : extinction %s (ressenti %.1f°C >= %.1f°C, réel %.1f°C, allumé depuis %.0f min)",
                    _system_label(prev_system), effective_temp, temp_off, temp, on_minutes,
                )
                _turn_off_active_system(ha_client, ha_cfg, prev_system)
                new_state = {
                    **state,
                    "state": "off",
                    "active_system": None,
                }
                _update_system_timestamp(new_state, prev_system, "off")
                _save_state(new_state)
                ntfy_send(
                    f"✅ {_system_label(prev_system).capitalize()} éteint",
                    f"Température atteinte : {effective_temp:.1f}°C (cible {temp_off}°C), allumé depuis {int(on_minutes)} min.",
                    ntfy_cfg,
                )
        else:
            if on_minutes >= grace:
                # Capturer le système actif AVANT de le mettre à None
                prev_system = active_system
                logger.info(
                    "Thermostat : extinction %s fin de plage (allumé depuis %.0f min >= %d min)",
                    _system_label(prev_system), on_minutes, grace,
                )
                _turn_off_active_system(ha_client, ha_cfg, prev_system)
                new_state = {
                    **state,
                    "state": "off",
                    "active_system": None,
                }
                _update_system_timestamp(new_state, prev_system, "off")
                _save_state(new_state)
                ntfy_send(f"🌙 {_system_label(prev_system).capitalize()} éteint", "Fin de plage horaire.", ntfy_cfg)
