"""
Historisation des températures et état du poêle en base SQLite.
Enregistrement toutes les ~10 min via le scheduler de app.py.
"""
import logging
import os
import sqlite3
import threading
from datetime import date, datetime, timedelta

from modules import migrate
from modules.advisor import interpolate_cop, compute_poele_cost

logger = logging.getLogger(__name__)

DB_FILE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "history.db"
)


# Délai d'attente (ms) quand la base est verrouillée par un autre thread (Flask / APScheduler)
BUSY_TIMEOUT_MS = 5000

# Chemins de base déjà migrés dans ce processus (les migrations ne tournent qu'une fois)
_migrated_paths: set[str] = set()
_migrate_lock = threading.Lock()


def _open(path: str) -> sqlite3.Connection:
    """Ouvre une connexion configurée pour l'accès concurrent : WAL + busy_timeout."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    # WAL : les lectures ne bloquent plus les écritures (mode persistant, stocké dans le fichier)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """Applique les migrations de la base. Appelé une fois au démarrage de l'app ;
    idempotent (ne refait rien si ce chemin de base a déjà été migré dans ce processus)."""
    path = os.path.abspath(DB_FILE)
    with _migrate_lock:
        if path in _migrated_paths:
            return
        conn = _open(path)
        try:
            migrate.run(conn)
        finally:
            conn.close()
        _migrated_paths.add(path)


def _connect() -> sqlite3.Connection:
    # Garde-fou pour les tests et scripts qui n'appellent pas init_db()
    if os.path.abspath(DB_FILE) not in _migrated_paths:
        init_db()
    return _open(DB_FILE)


def poele_state_for_history(real_state: str | None, thermostat_state: dict) -> str:
    """État du poêle à historiser ("on"/"off").

    Priorité à l'état réel Home Assistant (compte aussi les allumages manuels et les périodes où
    le thermostat est désactivé). Repli sur l'état du thermostat si HA ne répond pas.
    """
    if real_state not in (None, "unavailable", "unknown"):
        return "off" if real_state == "off" else "on"
    active = thermostat_state.get("active_system")
    return "on" if thermostat_state.get("state") == "on" and active in (None, "poele") else "off"


def clim_state_for_history(real_state: str | None, thermostat_state: dict, configured: bool) -> str | None:
    """État de la clim à historiser : "on" si elle chauffe (mode HA « heat »), "off" sinon,
    None si la clim n'est pas configurée. Repli sur l'état du thermostat si HA ne répond pas."""
    if not configured:
        return None
    if real_state not in (None, "unavailable", "unknown"):
        return "on" if real_state == "heat" else "off"
    on = thermostat_state.get("state") == "on" and thermostat_state.get("active_system") == "clim"
    return "on" if on else "off"


def _is_on(poele_state: str | None) -> bool:
    return poele_state in ("on", "poele")


# Température extérieure retenue pour le COP quand une lecture n'en a pas (rare : plusieurs sources de secours)
CLIM_FALLBACK_OUTDOOR_TEMP = 7.0


def clim_electric_kw(outdoor_temp, clim_cfg: dict) -> float:
    """Puissance électrique estimée de la clim (kW) : puissance thermique nominale ÷ COP de la courbe.
    Estimation haute : une clim inverter module sa puissance."""
    temp = outdoor_temp if outdoor_temp is not None else CLIM_FALLBACK_OUTDOOR_TEMP
    cop = max(interpolate_cop(temp, clim_cfg.get("cop_curve", [])), 1.0)
    return clim_cfg.get("nominal_capacity_kw", 4.0) / cop


def _elec_price(tempo_prices: dict, color: str | None, period: str) -> float:
    return tempo_prices.get(color or "BLUE", tempo_prices.get("BLUE", {})).get(period, 0.15)


def record(outdoor_temp, indoor_temp, poele_state: str, tempo_color: str = None,
           clim_state: str | None = None) -> bool:
    """Insère une lecture. Appelé par le scheduler toutes les ~10 min.
    Retourne True si la lecture correspond à un allumage (lecture précédente « off »)."""
    now = datetime.now()
    try:
        with _connect() as conn:
            prev = conn.execute("SELECT poele_state FROM readings ORDER BY ts DESC LIMIT 1").fetchone()
            ignited = _is_on(poele_state) and prev is not None and prev[0] == "off"
            conn.execute(
                "INSERT INTO readings (ts, outdoor_temp, indoor_temp, poele_state, tempo_color, clim_state) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (now.isoformat(timespec="seconds"), outdoor_temp, indoor_temp, poele_state, tempo_color, clim_state),
            )
            # Cumul journalier non purgé (stock de granulés) : une lecture = 10 min
            conn.execute(
                "INSERT INTO pellet_daily (day, on_minutes, ignitions) VALUES (?, ?, ?) "
                "ON CONFLICT(day) DO UPDATE SET on_minutes = on_minutes + excluded.on_minutes, "
                "ignitions = ignitions + excluded.ignitions",
                (now.strftime("%Y-%m-%d"), 10 if _is_on(poele_state) else 0, 1 if ignited else 0),
            )
        logger.debug("History : enregistrement outdoor=%.1f indoor=%s poele=%s clim=%s tempo=%s",
                     outdoor_temp or 0, indoor_temp, poele_state, clim_state, tempo_color)
        if ignited:
            logger.info("History : allumage du poêle détecté")
        return ignited
    except Exception as e:
        logger.error("History : erreur enregistrement : %s", e)
        return False


def season_start(today: date) -> date:
    """Début de la saison de chauffe : 1er septembre (de l'année précédente avant septembre)."""
    return date(today.year if today.month >= 9 else today.year - 1, 9, 1)


def get_ignition_stats(today: date | None = None) -> dict:
    """Allumages du poêle (usure) : aujourd'hui, moyenne et cycle moyen sur les 7 jours précédents,
    cumul de la saison, détail des 7 derniers jours (aujourd'hui compris ; None = aucune lecture ce jour-là).
    Source : pellet_daily (non purgée)."""
    today = today or date.today()
    week_start = (today - timedelta(days=7)).isoformat()
    start = season_start(today).isoformat()
    try:
        with _connect() as conn:
            today_n = conn.execute("SELECT ignitions FROM pellet_daily WHERE day = ?", (today.isoformat(),)).fetchone()
            week = conn.execute(
                "SELECT COUNT(*), SUM(ignitions), SUM(on_minutes) FROM pellet_daily WHERE day >= ? AND day < ?",
                (week_start, today.isoformat()),
            ).fetchone()
            days_7 = [(today - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
            by_day = dict(conn.execute(
                "SELECT day, ignitions FROM pellet_daily WHERE day >= ? AND day <= ?", (days_7[0], days_7[-1]),
            ).fetchall())
            season = conn.execute(
                "SELECT SUM(ignitions), MIN(CASE WHEN ignitions > 0 THEN day END) FROM pellet_daily WHERE day >= ?",
                (start,),
            ).fetchone()
    except Exception as e:
        logger.error("History : erreur statistiques allumages : %s", e)
        return {"today": 0, "avg_per_day_7d": None, "avg_cycle_minutes_7d": None, "season": 0, "since": None,
                "daily_7d": []}
    days, week_ign, week_min = week[0], week[1] or 0, week[2] or 0
    return {
        "today": today_n[0] if today_n else 0,
        "avg_per_day_7d": round(week_ign / days, 1) if days else None,
        "avg_cycle_minutes_7d": round(week_min / week_ign) if week_ign else None,
        "season": season[0] or 0,
        # Suivi récent (reprise ~30 jours) : le cumul part du premier allumage connu de la saison
        "since": season[1],
        "daily_7d": [{"date": d, "ignitions": by_day.get(d)} for d in days_7],
    }


def get_history(hours: int = 24) -> list[dict]:
    """Retourne les enregistrements des N dernières heures, triés par timestamp."""
    since = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT ts, outdoor_temp, indoor_temp, poele_state, tempo_color, clim_state "
                "FROM readings WHERE ts >= ? ORDER BY ts",
                (since,),
            ).fetchall()
        return [
            {"ts": r[0], "outdoor_temp": r[1], "indoor_temp": r[2], "poele_state": r[3], "tempo_color": r[4],
             "clim_state": r[5]}
            for r in rows
        ]
    except Exception as e:
        logger.error("History : erreur lecture : %s", e)
        return []


def get_daily_summary(days: int = 30) -> list[dict]:
    """
    Retourne un résumé par jour sur les N derniers jours :
    date, on_minutes, off_minutes, tempo_color (couleur majoritaire de la journée).
    """
    since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    try:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    substr(ts, 1, 10) AS day,
                    SUM(CASE WHEN poele_state IN ('on', 'poele') THEN 1 ELSE 0 END) * 10 AS on_minutes,
                    SUM(CASE WHEN poele_state = 'off' THEN 1 ELSE 0 END) * 10 AS off_minutes,
                    SUM(CASE WHEN clim_state = 'on' THEN 1 ELSE 0 END) * 10 AS clim_on_minutes,
                    COUNT(clim_state) AS clim_known,
                    AVG(outdoor_temp) AS avg_outdoor,
                    AVG(indoor_temp)  AS avg_indoor
                FROM readings
                WHERE ts >= ?
                GROUP BY day
                ORDER BY day DESC
                """,
                (since,),
            ).fetchall()

            color_rows = conn.execute(
                """
                SELECT substr(ts, 1, 10) AS day, tempo_color, COUNT(*) AS cnt
                FROM readings
                WHERE ts >= ? AND tempo_color IS NOT NULL
                GROUP BY day, tempo_color
                ORDER BY day, cnt DESC
                """,
                (since,),
            ).fetchall()

        dominant_color: dict = {}
        for day, color, _ in color_rows:
            if day not in dominant_color:
                dominant_color[day] = color

        return [
            {
                "date": day,
                "on_minutes": on_min,
                "off_minutes": off_min,
                # None : jour sans suivi de la clim (avant son historisation ou clim non configurée)
                "clim_on_minutes": clim_min if clim_known else None,
                "tempo_color": dominant_color.get(day),
                "avg_outdoor_temp": round(avg_out, 1) if avg_out is not None else None,
                "avg_indoor_temp": round(avg_in, 1) if avg_in is not None else None,
            }
            for day, on_min, off_min, clim_min, clim_known, avg_out, avg_in in rows
        ]
    except Exception as e:
        logger.error("History : erreur résumé journalier : %s", e)
        return []


def get_pellet_consumption_since(since_date: str) -> dict:
    """
    Calcule la consommation réelle de granulés depuis une date donnée.
    since_date : format YYYY-MM-DD
    Retourne : {total_on_minutes, daily_breakdown: [{date, on_minutes}]}
    Source : pellet_daily (non purgée), alimentée à chaque lecture par record().
    """
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT day, on_minutes FROM pellet_daily WHERE day >= ? ORDER BY day",
                (since_date,),
            ).fetchall()
        total_on_minutes = sum(r[1] for r in rows)
        return {
            "total_on_minutes": total_on_minutes,
            "daily_breakdown": [{"date": r[0], "on_minutes": r[1]} for r in rows],
        }
    except Exception as e:
        logger.error("History : erreur consommation granulés : %s", e)
        return {"total_on_minutes": 0, "daily_breakdown": []}


def record_diagnose(
    presence_status: str,
    poele_real_state: str,
    thermostat_state: str,
    felt_temperature: float,
    indoor_temp: float,
    in_schedule: bool,
    everyone_away: bool,
    suspended_until: str,
    recommendation: str,
    clim_real_state: str = None,
    active_system: str = None,
) -> None:
    """Insère un snapshot de diagnostic. Appelé toutes les ~10 min."""
    try:
        with _connect() as conn:
            conn.execute(
                """INSERT INTO diagnose_log
                   (ts, presence_status, poele_real_state, thermostat_state,
                    felt_temperature, indoor_temp, in_schedule, everyone_away,
                    suspended_until, recommendation, clim_real_state, active_system)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    datetime.now().isoformat(timespec="seconds"),
                    presence_status,
                    poele_real_state,
                    thermostat_state,
                    felt_temperature,
                    indoor_temp,
                    1 if in_schedule else 0,
                    1 if everyone_away else 0,
                    suspended_until,
                    recommendation,
                    clim_real_state,
                    active_system,
                ),
            )
    except Exception as e:
        logger.error("History : erreur enregistrement diagnose : %s", e)


def get_diagnose_history(hours: int = 168) -> list[dict]:
    """Retourne les snapshots de diagnostic des N dernières heures."""
    since = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    try:
        with _connect() as conn:
            rows = conn.execute(
                """SELECT ts, presence_status, poele_real_state, thermostat_state,
                          felt_temperature, indoor_temp, in_schedule, everyone_away,
                          suspended_until, recommendation, clim_real_state, active_system
                   FROM diagnose_log WHERE ts >= ? ORDER BY ts DESC""",
                (since,),
            ).fetchall()
        return [
            {
                "ts": r[0], "presence_status": r[1], "poele_real_state": r[2],
                "thermostat_state": r[3], "felt_temperature": r[4], "indoor_temp": r[5],
                "in_schedule": bool(r[6]), "everyone_away": bool(r[7]),
                "suspended_until": r[8], "recommendation": r[9],
                "clim_real_state": r[10] if len(r) > 10 else None,
                "active_system": r[11] if len(r) > 11 else None,
            }
            for r in rows
        ]
    except Exception as e:
        logger.error("History : erreur lecture diagnose : %s", e)
        return []


def purge_diagnose_old(days: int = 7) -> None:
    """Supprime les snapshots de diagnostic de plus de N jours."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    try:
        with _connect() as conn:
            conn.execute("DELETE FROM diagnose_log WHERE ts < ?", (cutoff,))
        logger.info("History : purge diagnose > %d jours", days)
    except Exception as e:
        logger.error("History : erreur purge diagnose : %s", e)


def purge_cutoff(now: datetime, days: int = 30) -> str:
    """Date limite de purge des lectures : N jours, sans jamais entamer le mois précédent,
    que l'agrégation mensuelle recalcule chaque jour (sinon le rapport du mois serait tronqué)."""
    first_of_prev_month = (now.replace(day=1) - timedelta(days=1)).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0)
    return min(now - timedelta(days=days), first_of_prev_month).isoformat(timespec="seconds")


def purge_old(days: int = 30) -> None:
    """Supprime les lectures plus vieilles que N jours (en conservant le mois précédent entier)."""
    cutoff = purge_cutoff(datetime.now(), days)
    try:
        with _connect() as conn:
            conn.execute("DELETE FROM readings WHERE ts < ?", (cutoff,))
        logger.info("History : purge données > %d jours", days)
    except Exception as e:
        logger.error("History : erreur purge : %s", e)


def aggregate_month(year_month: str, config: dict) -> bool:
    """
    Agrège les données du mois year_month (YYYY-MM) dans monthly_reports.
    Retourne True si des données ont été trouvées et agrégées.
    """
    month_start = f"{year_month}-01"
    # Calculer le premier jour du mois suivant
    y, m = int(year_month[:4]), int(year_month[5:7])
    if m == 12:
        month_end = f"{y + 1}-01-01"
    else:
        month_end = f"{y}-{m + 1:02d}-01"

    poele_cfg = config.get("POELE", {})
    clim_cfg = config.get("CLIM", {})
    tempo_prices = config.get("TEMPO_PRICES", {})
    hp_start = config.get("HP_START", 6)
    hp_end = config.get("HP_END", 22)

    poele_cost_per_hour = 0
    if poele_cfg:
        poele_result = compute_poele_cost(poele_cfg)
        poele_cost_per_hour = poele_result.get("cost_per_hour", 0)

    try:
        with _connect() as conn:
            # 1. Readings pour le mois
            readings = conn.execute(
                "SELECT ts, outdoor_temp, indoor_temp, poele_state, tempo_color, clim_state "
                "FROM readings WHERE ts >= ? AND ts < ? ORDER BY ts",
                (month_start, month_end),
            ).fetchall()

            if not readings:
                logger.info("Agrégation %s : aucune donnée readings", year_month)
                return False

            total_readings = len(readings)
            poele_on_count = 0
            total_poele_cost = 0.0
            total_clim_equiv_cost = 0.0
            clim_on_count = 0
            clim_known = 0
            total_clim_cost = 0.0
            total_poele_equiv_cost = 0.0
            outdoor_temps = []
            indoor_temps = []
            heating_days_set = set()
            tempo_day_colors = {}  # day -> {color: count}

            for ts, outdoor_temp, indoor_temp, poele_state, tempo_color, clim_state in readings:
                day = ts[:10]
                hour = int(ts[11:13])
                period = "HP" if hp_start <= hour < hp_end else "HC"

                if outdoor_temp is not None:
                    outdoor_temps.append(outdoor_temp)
                if indoor_temp is not None:
                    indoor_temps.append(indoor_temp)

                # Tempo couleur par jour
                if tempo_color:
                    tempo_day_colors.setdefault(day, {})
                    tempo_day_colors[day][tempo_color] = tempo_day_colors[day].get(tempo_color, 0) + 1

                # Coût poêle si allumé
                if poele_state in ("on", "poele"):
                    poele_on_count += 1
                    heating_days_set.add(day)
                    total_poele_cost += (10 / 60) * poele_cost_per_hour

                # Coût clim équivalent (pour toutes les lectures où le poêle est on
                # OU où du chauffage est nécessaire)
                if poele_state in ("on", "poele") and outdoor_temp is not None:
                    elec_price = _elec_price(tempo_prices, tempo_color, period)
                    total_clim_equiv_cost += (10 / 60) * clim_electric_kw(outdoor_temp, clim_cfg) * elec_price

                # Clim réelle (en chauffe) : coût estimé, et coût du poêle s'il avait chauffé à sa place
                if clim_state is not None:
                    clim_known += 1
                if clim_state == "on":
                    clim_on_count += 1
                    heating_days_set.add(day)
                    elec_price = _elec_price(tempo_prices, tempo_color, period)
                    total_clim_cost += (10 / 60) * clim_electric_kw(outdoor_temp, clim_cfg) * elec_price
                    total_poele_equiv_cost += (10 / 60) * poele_cost_per_hour

            # 2. Recommandations depuis diagnose_log
            diag_rows = conn.execute(
                "SELECT recommendation FROM diagnose_log WHERE ts >= ? AND ts < ?",
                (month_start, month_end),
            ).fetchall()

            rec_poele = sum(1 for (r,) in diag_rows if r == "poele") * 10
            rec_clim = sum(1 for (r,) in diag_rows if r == "clim") * 10
            rec_none = sum(1 for (r,) in diag_rows if r in ("none", None)) * 10

            # 3. Comptage jours Tempo
            tempo_blue = 0
            tempo_white = 0
            tempo_red = 0
            for day, colors in tempo_day_colors.items():
                dominant = max(colors, key=colors.get)
                if dominant == "BLUE":
                    tempo_blue += 1
                elif dominant == "WHITE":
                    tempo_white += 1
                elif dominant == "RED":
                    tempo_red += 1

            avg_outdoor = round(sum(outdoor_temps) / len(outdoor_temps), 1) if outdoor_temps else None
            avg_indoor = round(sum(indoor_temps) / len(indoor_temps), 1) if indoor_temps else None

            # 4. INSERT OR REPLACE
            conn.execute(
                """INSERT OR REPLACE INTO monthly_reports
                   (month, total_readings, poele_on_minutes, heating_days,
                    avg_outdoor_temp, avg_indoor_temp,
                    tempo_blue_days, tempo_white_days, tempo_red_days,
                    estimated_poele_cost, estimated_clim_equiv_cost,
                    rec_poele_minutes, rec_clim_minutes, rec_none_minutes,
                    updated_at, clim_on_minutes, estimated_clim_cost, estimated_poele_equiv_cost)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    year_month,
                    total_readings,
                    poele_on_count * 10,
                    len(heating_days_set),
                    avg_outdoor,
                    avg_indoor,
                    tempo_blue,
                    tempo_white,
                    tempo_red,
                    round(total_poele_cost, 2),
                    round(total_clim_equiv_cost, 2),
                    rec_poele,
                    rec_clim,
                    rec_none,
                    datetime.now().isoformat(timespec="seconds"),
                    # None : clim non suivie ce mois-là (avant son historisation ou non configurée)
                    clim_on_count * 10 if clim_known else None,
                    round(total_clim_cost, 2) if clim_known else None,
                    round(total_poele_equiv_cost, 2) if clim_known else None,
                ),
            )
            logger.info("Agrégation %s : %d lectures, poêle %d min, coût poêle %.2f€, clim equiv %.2f€, "
                        "clim %d min, coût clim %.2f€",
                         year_month, total_readings, poele_on_count * 10,
                         total_poele_cost, total_clim_equiv_cost, clim_on_count * 10, total_clim_cost)
            return True

    except Exception as e:
        logger.error("Agrégation %s échouée : %s", year_month, e)
        return False


def get_monthly_reports(months: int = 24) -> list[dict]:
    """Retourne les rapports mensuels agrégés, triés par mois DESC."""
    try:
        with _connect() as conn:
            rows = conn.execute(
                """SELECT month, total_readings, poele_on_minutes, heating_days,
                          avg_outdoor_temp, avg_indoor_temp,
                          tempo_blue_days, tempo_white_days, tempo_red_days,
                          estimated_poele_cost, estimated_clim_equiv_cost,
                          rec_poele_minutes, rec_clim_minutes, rec_none_minutes,
                          updated_at, clim_on_minutes, estimated_clim_cost, estimated_poele_equiv_cost
                   FROM monthly_reports
                   ORDER BY month DESC
                   LIMIT ?""",
                (months,),
            ).fetchall()
        return [
            {
                "month": r[0],
                "total_readings": r[1],
                "poele_on_minutes": r[2],
                "heating_days": r[3],
                "avg_outdoor_temp": r[4],
                "avg_indoor_temp": r[5],
                "tempo_blue_days": r[6],
                "tempo_white_days": r[7],
                "tempo_red_days": r[8],
                "estimated_poele_cost": r[9],
                "estimated_clim_equiv_cost": r[10],
                "rec_poele_minutes": r[11],
                "rec_clim_minutes": r[12],
                "rec_none_minutes": r[13],
                "updated_at": r[14],
                "clim_on_minutes": r[15],
                "estimated_clim_cost": r[16],
                "estimated_poele_equiv_cost": r[17],
            }
            for r in rows
        ]
    except Exception as e:
        logger.error("Lecture monthly_reports échouée : %s", e)
        return []


def refresh_clim_daily(clim_cfg: dict) -> None:
    """Recalcule clim_daily (minutes de chauffe et kWh estimés par jour) pour les jours encore présents
    dans readings. Les jours purgés de readings gardent leur dernière valeur : clim_daily n'est jamais purgée.
    Lancé au démarrage et chaque nuit avec l'agrégation mensuelle."""
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT substr(ts, 1, 10), outdoor_temp FROM readings WHERE clim_state = 'on'"
            ).fetchall()
            known_days = [r[0] for r in conn.execute(
                "SELECT DISTINCT substr(ts, 1, 10) FROM readings WHERE clim_state IS NOT NULL"
            ).fetchall()]
            totals = {day: [0, 0.0] for day in known_days}
            for day, outdoor_temp in rows:
                totals[day][0] += 10
                totals[day][1] += (10 / 60) * clim_electric_kw(outdoor_temp, clim_cfg)
            conn.executemany(
                "INSERT INTO clim_daily (day, on_minutes, kwh) VALUES (?, ?, ?) "
                "ON CONFLICT(day) DO UPDATE SET on_minutes = excluded.on_minutes, kwh = excluded.kwh",
                [(day, m, round(k, 3)) for day, (m, k) in totals.items()],
            )
    except Exception as e:
        logger.error("History : erreur recalcul clim_daily : %s", e)


def get_clim_daily(since_date: str) -> dict:
    """{jour: kWh estimés} de la clim depuis une date (clim_daily, jours suivis uniquement)."""
    try:
        with _connect() as conn:
            rows = conn.execute("SELECT day, kwh FROM clim_daily WHERE day >= ? ORDER BY day",
                                (since_date,)).fetchall()
        return {day: kwh for day, kwh in rows}
    except Exception as e:
        logger.error("History : erreur lecture clim_daily : %s", e)
        return {}


def clim_tracking_since() -> str | None:
    """Premier jour où l'état de la clim est connu (début de son historisation), None si jamais suivie."""
    try:
        with _connect() as conn:
            row = conn.execute("SELECT MIN(day) FROM clim_daily").fetchone()
        return row[0] if row else None
    except Exception as e:
        logger.error("History : erreur lecture début du suivi clim : %s", e)
        return None


def season_balance(reports: list[dict], today: date | None = None) -> dict:
    """Bilan de la saison de chauffe en cours (1er septembre → 31 août) à partir des rapports mensuels.

    - cost_total : coût estimé réel (poêle + clim) ;
    - savings_vs_all_clim : coût si la clim avait chauffé à la place du poêle, moins le coût réel ;
    - savings_vs_all_poele : coût si le poêle avait chauffé à la place de la clim, moins le coût réel
      (None tant que la clim n'est pas suivie) ;
    - savings : économie par rapport au plus cher des deux scénarios « un seul système ».
    Durées égales dans chaque scénario : même approximation que la colonne « Clim équiv. »."""
    start = season_start(today or date.today())
    first, last = start.strftime("%Y-%m"), f"{start.year + 1}-08"
    months = [r for r in reports if first <= r["month"] <= last]
    clim_tracked = any(r.get("clim_on_minutes") is not None for r in months)

    def total(key):
        return sum(r.get(key) or 0 for r in months)

    poele_cost, clim_cost = total("estimated_poele_cost"), total("estimated_clim_cost")
    cost_total = poele_cost + clim_cost
    all_clim = total("estimated_clim_equiv_cost") + clim_cost
    all_poele = poele_cost + total("estimated_poele_equiv_cost") if clim_tracked else None
    worst = max(all_clim, all_poele) if all_poele is not None else all_clim
    heated = [r for r in months if (r.get("poele_on_minutes") or 0) + (r.get("clim_on_minutes") or 0) > 0]
    return {
        "season_start": start.isoformat(),
        "months": len(months),
        "poele_minutes": total("poele_on_minutes"),
        "clim_minutes": total("clim_on_minutes") if clim_tracked else None,
        "poele_cost": round(poele_cost, 2),
        "clim_cost": round(clim_cost, 2) if clim_tracked else None,
        "cost_total": round(cost_total, 2),
        "all_clim_cost": round(all_clim, 2),
        "all_poele_cost": round(all_poele, 2) if all_poele is not None else None,
        "savings_vs_all_clim": round(all_clim - cost_total, 2),
        "savings_vs_all_poele": round(all_poele - cost_total, 2) if all_poele is not None else None,
        "savings": round(worst - cost_total, 2),
        "avg_cost_per_heated_month": round(cost_total / len(heated), 2) if heated else None,
        "updated_at": max((r.get("updated_at") or "" for r in months), default="") or None,
    }
