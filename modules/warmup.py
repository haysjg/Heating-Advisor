"""
Démarrage optimisé : apprendre la vitesse de montée en température de la maison et retarder
l'allumage du matin au plus tard possible pour avoir chaud à l'heure de confort.

Principe « retarder seulement, jamais avancer » : l'allumage n'a jamais lieu avant le début de la
plage du thermostat, donc la consommation ne peut pas augmenter.

1. Chaque nuit (et au démarrage), les montées en température des lectures encore présentes
   (readings, purgées à ~30 jours) sont résumées en une ligne par montée dans warmup_events,
   jamais purgée : la courbe s'affine de saison en saison.
2. Modèle par système (poêle / clim) : durée = latence + écart ÷ vitesse(temp. extérieure),
   vitesse en °C/h pondérée par la proximité de température extérieure (noyau gaussien).
3. Chaque cycle du thermostat entre le début de plage et l'heure de confort : calcul de l'heure
   de démarrage au plus tard. Mode « simulation » : journal seulement ; mode « pilotage » :
   le thermostat attend cette heure pour allumer.
"""
import logging
import math
from datetime import date, datetime, timedelta
from statistics import median

from modules.history import _connect

logger = logging.getLogger(__name__)

DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MODES = ("off", "simulation", "pilot")

# Montées retenues : départ le matin (les apports solaires de la journée fausseraient la vitesse)
MORNING_START_HOUR, MORNING_END_HOUR = 4, 11
MIN_RISE = 0.5            # °C de montée minimale pour qu'une montée soit mesurable
LAG_RISE = 0.2            # °C au-dessus du départ : fin de la latence (temps de mise en chauffe)
MIN_RISE_MINUTES = 20     # durée minimale de montée après la latence
MAX_RUN_HOURS = 6
RATE_BOUNDS = (0.1, 6.0)  # °C/h plausibles ; au-delà : sonde perturbée (soleil, porte, sonde près du poêle)

# Modèle : noyau gaussien sur la température extérieure
KERNEL_SIGMA = 2.0        # °C
KERNEL_MAX_DISTANCE = 4.0 # °C
MIN_EVENTS = 3            # montées proches nécessaires pour prédire
MIN_WEIGHT = 2.0          # poids cumulé minimal (≈ 2 montées à la même température)

DEFAULT_COMFORT_OFFSET_MINUTES = 45
DEFAULT_SAFETY_MARGIN_MINUTES = 15
# Prévision jugée juste si la durée réelle est à moins de N min de la durée prévue
# (lectures toutes les 10 min : la durée réelle est connue à un cycle près)
ACCURACY_TOLERANCE_MINUTES = 10


# ── Extraction des montées ──────────────────────────────────

def _system_on(r: dict, system: str) -> bool | None:
    if system == "poele":
        return r.get("poele_state") in ("on", "poele")
    state = r.get("clim_state")
    return None if state is None else state == "on"


def extract_events(readings: list[dict]) -> list[dict]:
    """Montées en température du matin à partir des lectures triées par ts.

    Une montée = passage arrêt → marche d'un seul système (l'autre à l'arrêt tout du long),
    suivie jusqu'à l'arrêt ou MAX_RUN_HOURS. Mesures :
    - latence : du démarrage (milieu entre la dernière lecture « arrêt » et la première « marche »)
      au premier relevé à +LAG_RISE °C ;
    - vitesse : de la fin de latence au maximum atteint pendant la marche (°C/h). Prudente : la montée
      ralentit à l'approche de la consigne, la vitesse moyenne jusqu'au maximum sous-estime le début.
    """
    events = []
    rows = [dict(r, _t=datetime.fromisoformat(r["ts"])) for r in readings]
    for system, other in (("poele", "clim"), ("clim", "poele")):
        for i in range(1, len(rows)):
            prev, cur = rows[i - 1], rows[i]
            if not (_system_on(cur, system) and _system_on(prev, system) is False):
                continue
            if cur["_t"] - prev["_t"] > timedelta(minutes=20) or prev.get("indoor_temp") is None:
                continue
            start = prev["_t"] + (cur["_t"] - prev["_t"]) / 2
            if not MORNING_START_HOUR <= start.hour < MORNING_END_HOUR:
                continue
            run = [prev]
            for r in rows[i:]:
                if not _system_on(r, system) or _system_on(r, other) or r["_t"] - start > timedelta(hours=MAX_RUN_HOURS):
                    break
                if r["_t"] - run[-1]["_t"] > timedelta(minutes=20):
                    break  # trou dans les lectures
                run.append(r)
            if any(_system_on(r, other) for r in run):
                continue
            event = _measure(run, start, system)
            if event:
                events.append(event)
    return sorted(events, key=lambda e: e["start_ts"])


def _measure(run: list[dict], start: datetime, system: str) -> dict | None:
    indoor_start = run[0]["indoor_temp"]
    temps = [(r["_t"], r["indoor_temp"]) for r in run[1:] if r.get("indoor_temp") is not None]
    if not temps:
        return None
    lag_point = next(((t, v) for t, v in temps if v >= indoor_start + LAG_RISE), None)
    peak_t, peak = max(temps, key=lambda tv: (tv[1], -tv[0].timestamp()))
    if lag_point is None or peak - indoor_start < MIN_RISE:
        return None
    lag_t, lag_v = lag_point
    rise_minutes = (peak_t - lag_t).total_seconds() / 60
    if rise_minutes < MIN_RISE_MINUTES:
        return None
    rate = (peak - lag_v) / (rise_minutes / 60)
    if not RATE_BOUNDS[0] <= rate <= RATE_BOUNDS[1]:
        return None
    outdoor = [r["outdoor_temp"] for r in run if r.get("outdoor_temp") is not None and r["_t"] <= peak_t]
    if not outdoor:
        return None
    return {
        "start_ts": start.isoformat(timespec="seconds"),
        "system": system,
        "outdoor_avg": round(sum(outdoor) / len(outdoor), 1),
        "indoor_start": indoor_start,
        "indoor_peak": peak,
        "lag_minutes": round((lag_t - start).total_seconds() / 60),
        "rate": round(rate, 2),
    }


# ── Modèle ──────────────────────────────────────────────────

def predict(events: list[dict], system: str, outdoor: float | None, delta: float) -> dict | None:
    """Durée estimée (min) pour monter de `delta` °C à cette température extérieure.
    None si trop peu de montées proches de cette température (l'allumage reste alors au début de plage)."""
    if outdoor is None:
        return None
    near = [(e, math.exp(-((e["outdoor_avg"] - outdoor) / KERNEL_SIGMA) ** 2 / 2))
            for e in events if e["system"] == system and abs(e["outdoor_avg"] - outdoor) <= KERNEL_MAX_DISTANCE]
    weight = sum(w for _, w in near)
    if len(near) < MIN_EVENTS or weight < MIN_WEIGHT:
        return None
    rate = sum(e["rate"] * w for e, w in near) / weight
    lag = median(e["lag_minutes"] for e, _ in near)
    return {
        "minutes": round(lag + max(delta, 0) / rate * 60),
        "rate": round(rate, 2),
        "lag_minutes": round(lag),
        "events": len(near),
    }


def curve(events: list[dict], system: str, temps=range(-10, 21, 2)) -> list[dict]:
    """Vitesse de montée apprise par température extérieure (points connus seulement), pour l'affichage."""
    points = []
    for t in temps:
        p = predict(events, system, float(t), 1.0)
        if p:
            points.append({"outdoor": t, "rate": p["rate"], "lag_minutes": p["lag_minutes"], "events": p["events"]})
    return points


# Tranches de température extérieure affichées pour l'avancement de l'apprentissage (°C, centre ± 1)
PROGRESS_TEMPS = range(-8, 19, 2)


def progress(events: list[dict], system: str) -> dict:
    """Avancement de l'apprentissage d'un système : matins appris, premier matin, et par tranche de 2 °C
    le nombre de matins et si une prévision est possible (sinon : allumage au début de plage)."""
    mine = [e for e in events if e["system"] == system]
    bands = [{
        "outdoor": t,
        "events": sum(1 for e in mine if t - 1 <= e["outdoor_avg"] < t + 1),
        "ready": predict(mine, system, float(t), 1.0) is not None,
    } for t in PROGRESS_TEMPS]
    ready = [b["outdoor"] for b in bands if b["ready"]]
    return {
        "mornings": len(mine),
        "first_day": mine[0]["start_ts"][:10] if mine else None,
        "bands": bands,
        "ready_min": min(ready) if ready else None,
        "ready_max": max(ready) if ready else None,
    }


# ── Réglages et plan du matin ───────────────────────────────

def settings(thermostat_cfg: dict) -> dict:
    s = thermostat_cfg.get("optimal_start") or {}
    mode = s.get("mode", "simulation")
    return {
        "mode": mode if mode in MODES else "simulation",
        "safety_margin_minutes": int(s.get("safety_margin_minutes", DEFAULT_SAFETY_MARGIN_MINUTES)),
    }


def _hm(value: str, day: datetime) -> datetime | None:
    try:
        h, m = map(int, str(value).split(":"))
        return day.replace(hour=h, minute=m, second=0, microsecond=0)
    except (TypeError, ValueError):
        return None


def _day_times(thermostat_cfg: dict, now: datetime) -> tuple[datetime, datetime] | None:
    """(début de plage, heure de confort) du jour, sans contrôle ; heure de confort = « comfort »
    de la plage, sinon début + 45 min. None sans plage."""
    sched = (thermostat_cfg.get("schedule") or {}).get(DAY_KEYS[now.weekday()])
    if not sched:
        return None
    start = _hm(sched.get("start"), now)
    if start is None:
        return None
    comfort = _hm(sched.get("comfort"), now) if sched.get("comfort") else None
    return start, comfort or start + timedelta(minutes=DEFAULT_COMFORT_OFFSET_MINUTES)


def morning_window(thermostat_cfg: dict, now: datetime) -> tuple[datetime, datetime] | None:
    """(début de plage, heure de confort) du jour, None sans plage ou si l'heure de confort n'est pas
    après le début : heure de confort = début de plage désactive le démarrage optimisé ce jour-là."""
    times = _day_times(thermostat_cfg, now)
    return times if times and times[1] > times[0] else None


def disabled_today(thermostat_cfg: dict, now: datetime) -> bool:
    """Heure de confort réglée égale au début de plage : pas de démarrage optimisé ce jour-là."""
    times = _day_times(thermostat_cfg, now)
    return bool(times) and times[1] <= times[0]


def plan(thermostat_cfg: dict, now: datetime, effective_temp: float, outdoor: float | None,
         system: str, events: list[dict]) -> dict | None:
    """Plan de démarrage pendant la fenêtre du matin, None hors fenêtre.

    allow : l'allumage peut avoir lieu à ce cycle. Démarrage au cycle où attendre le suivant
    ferait dépasser l'heure de démarrage au plus tard (comfort − durée − marge)."""
    window = morning_window(thermostat_cfg, now)
    if not window or not window[0] <= now < window[1]:
        return None
    start, comfort = window
    temp_on = thermostat_cfg.get("temp_on", 20.0)
    delta = round(temp_on - effective_temp, 2)
    result = {"schedule_start": start, "comfort_time": comfort, "delta": delta, "system": system,
              "prediction": None, "latest_start": None, "allow": True}
    if delta <= 0 or system not in ("poele", "clim"):
        return result
    p = predict(events, system, outdoor, delta)
    if not p:
        return result
    latest = comfort - timedelta(minutes=p["minutes"] + settings(thermostat_cfg)["safety_margin_minutes"])
    interval = timedelta(minutes=int(thermostat_cfg.get("check_interval_minutes", 10)))
    result.update(prediction=p, latest_start=latest, allow=now + interval > latest)
    return result


# ── Base : montées et journal des matins ────────────────────

def refresh_events() -> int:
    """Extrait les montées des lectures encore présentes ; les montées déjà connues sont conservées
    telles quelles (warmup_events n'est jamais purgée). Retourne le nombre de nouvelles montées."""
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT ts, outdoor_temp, indoor_temp, poele_state, clim_state FROM readings ORDER BY ts"
            ).fetchall()
            readings = [{"ts": r[0], "outdoor_temp": r[1], "indoor_temp": r[2], "poele_state": r[3],
                         "clim_state": r[4]} for r in rows]
            events = extract_events(readings)
            before = conn.total_changes
            conn.executemany(
                "INSERT OR IGNORE INTO warmup_events (start_ts, system, outdoor_avg, indoor_start, indoor_peak, "
                "lag_minutes, rate) VALUES (:start_ts, :system, :outdoor_avg, :indoor_start, :indoor_peak, "
                ":lag_minutes, :rate)",
                events,
            )
            added = conn.total_changes - before
        if added:
            logger.info("Démarrage optimisé : %d nouvelle(s) montée(s) en température", added)
        return added
    except Exception as e:
        logger.error("Démarrage optimisé : erreur extraction des montées : %s", e)
        return 0


def get_events() -> list[dict]:
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT start_ts, system, outdoor_avg, indoor_start, indoor_peak, lag_minutes, rate "
                "FROM warmup_events ORDER BY start_ts"
            ).fetchall()
        keys = ("start_ts", "system", "outdoor_avg", "indoor_start", "indoor_peak", "lag_minutes", "rate")
        return [dict(zip(keys, r)) for r in rows]
    except Exception as e:
        logger.error("Démarrage optimisé : erreur lecture des montées : %s", e)
        return []


_LOG_KEYS = ("day", "mode", "schedule_start", "comfort_time", "system", "need_ts", "outdoor", "indoor_need",
             "delta", "predicted_minutes", "planned_start", "actual_start", "reached_ts", "indoor_at_comfort",
             "cost_per_hour")


def get_log(days: int = 30, since: date | None = None) -> list[dict]:
    """Journal des matins depuis `since` (sinon les `days` derniers jours), du plus récent au plus ancien."""
    since = (since or date.today() - timedelta(days=days)).isoformat()
    try:
        with _connect() as conn:
            rows = conn.execute(f"SELECT {', '.join(_LOG_KEYS)} FROM optimal_start_log WHERE day >= ? "
                                "ORDER BY day DESC", (since,)).fetchall()
        return [dict(zip(_LOG_KEYS, r)) for r in rows]
    except Exception as e:
        logger.error("Démarrage optimisé : erreur lecture du journal : %s", e)
        return []


def _get_day(conn, day: str) -> dict | None:
    row = conn.execute(f"SELECT {', '.join(_LOG_KEYS)} FROM optimal_start_log WHERE day = ?", (day,)).fetchone()
    return dict(zip(_LOG_KEYS, row)) if row else None


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


def observe(thermostat_cfg: dict, now: datetime, effective_temp: float, outdoor: float | None, system: str,
            actual_start: datetime | None, cost_per_hour: float | None, the_plan: dict | None) -> None:
    """Journal du matin (un enregistrement par jour), appelé à chaque cycle du thermostat.

    - Premier cycle de la fenêtre où il faut chauffer : besoin, écart, durée prévue et heure de démarrage
      prévue (au plus tôt le début de plage), figées à ce moment ;
    - allumage pendant la fenêtre : heure de démarrage réelle ;
    - premier cycle à la consigne d'allumage après le démarrage : durée réelle de montée ;
    - premier cycle à l'heure de confort ou après : température ressentie atteinte."""
    mode = settings(thermostat_cfg)["mode"]
    if mode == "off":
        return
    window = morning_window(thermostat_cfg, now)
    if not window and disabled_today(thermostat_cfg, now):
        _log_day_off(thermostat_cfg, now)
        return
    if not window or now < window[0]:
        return
    start, comfort = window
    day = now.date().isoformat()
    try:
        with _connect() as conn:
            entry = _get_day(conn, day)
            if entry is None:
                if now >= comfort:
                    return  # fenêtre déjà passée (démarrage de l'appli en cours de matinée)
                entry = {k: None for k in _LOG_KEYS}
                entry.update(day=day, mode=mode, schedule_start=_iso(start), comfort_time=_iso(comfort))
            if the_plan and the_plan["delta"] > 0 and entry["need_ts"] is None and not entry["actual_start"]:
                p = the_plan["prediction"]
                entry.update(
                    need_ts=_iso(now), system=system, outdoor=outdoor, indoor_need=effective_temp,
                    delta=the_plan["delta"], predicted_minutes=p["minutes"] if p else None,
                    planned_start=_iso(max(the_plan["latest_start"], now)) if the_plan["latest_start"] else None,
                    cost_per_hour=cost_per_hour,
                )
            if actual_start and start - timedelta(minutes=15) <= actual_start < comfort and not entry["actual_start"]:
                entry["actual_start"] = _iso(actual_start)
            if entry["actual_start"] and not entry["reached_ts"] \
                    and effective_temp >= thermostat_cfg.get("temp_on", 20.0):
                entry["reached_ts"] = _iso(now)
            if now >= comfort and entry["indoor_at_comfort"] is None:
                entry["indoor_at_comfort"] = effective_temp
            conn.execute(
                f"INSERT OR REPLACE INTO optimal_start_log ({', '.join(_LOG_KEYS)}) "
                f"VALUES ({', '.join('?' for _ in _LOG_KEYS)})",
                [entry[k] for k in _LOG_KEYS],
            )
    except Exception as e:
        logger.error("Démarrage optimisé : erreur journal du matin : %s", e)


def _log_day_off(thermostat_cfg: dict, now: datetime) -> None:
    """Trace un matin sans démarrage optimisé (heure de confort = début de plage), une fois par jour."""
    start, comfort = _day_times(thermostat_cfg, now)
    if now < start:
        return
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO optimal_start_log (day, mode, schedule_start, comfort_time) VALUES (?, ?, ?, ?)",
                (now.date().isoformat(), "day_off", _iso(start), _iso(comfort)),
            )
    except Exception as e:
        logger.error("Démarrage optimisé : erreur journal du matin : %s", e)


def saved_minutes(entry: dict) -> int:
    """Minutes de chauffe évitées un matin.
    - Simulation : le thermostat a allumé dès le besoin ; gain = démarrage prévu − démarrage réel.
    - Pilotage : gain = démarrage réel − démarrage qu'aurait fait le thermostat (besoin constaté)."""
    if not entry.get("need_ts"):
        return 0
    need = datetime.fromisoformat(entry["need_ts"])
    actual = datetime.fromisoformat(entry["actual_start"]) if entry.get("actual_start") else None
    if entry["mode"] == "pilot":
        return max(0, round((actual - need).total_seconds() / 60)) if actual else 0
    if not entry.get("planned_start"):
        return 0
    planned = datetime.fromisoformat(entry["planned_start"])
    return max(0, round((planned - (actual or need)).total_seconds() / 60))


def summarize(log: list[dict], temp_on: float) -> dict:
    """Bilan du journal : matins chauffés, minutes de chauffe évitées (ou qui l'auraient été en simulation),
    gain estimé, matins où la consigne n'était pas atteinte à l'heure de confort."""
    mornings = [e for e in log if e["need_ts"]]
    saved = [(saved_minutes(e), e["cost_per_hour"] or 0) for e in mornings]
    checked = [e for e in mornings if e["indoor_at_comfort"] is not None]
    measured = [e for e in mornings if e["predicted_minutes"] is not None and real_minutes(e) is not None]
    return {
        "mornings": len(mornings),
        "predicted": sum(1 for e in mornings if e["predicted_minutes"] is not None),
        "saved_minutes": sum(m for m, _ in saved),
        "saved_eur": round(sum(m / 60 * c for m, c in saved), 2),
        "comfort_checked": len(checked),
        "comfort_missed": sum(1 for e in checked if e["indoor_at_comfort"] < temp_on - 0.2),
        # Fiabilité de la courbe : durée prévue comparée à la durée réelle de montée
        "accuracy_checked": len(measured),
        "accuracy_ok": sum(1 for e in measured
                           if abs(real_minutes(e) - e["predicted_minutes"]) <= ACCURACY_TOLERANCE_MINUTES),
        "accuracy_tolerance_minutes": ACCURACY_TOLERANCE_MINUTES,
    }


def real_minutes(entry: dict) -> int | None:
    """Durée réelle de montée d'un matin : de l'allumage au premier cycle à la consigne d'allumage."""
    if not entry.get("actual_start") or not entry.get("reached_ts"):
        return None
    delta = datetime.fromisoformat(entry["reached_ts"]) - datetime.fromisoformat(entry["actual_start"])
    return round(delta.total_seconds() / 60)
