"""
Historisation des températures et état du poêle en base SQLite.
Enregistrement toutes les ~10 min via le scheduler de app.py.
"""
import logging
import os
import sqlite3
from datetime import datetime, timedelta

from modules import migrate
from modules.advisor import interpolate_cop, compute_poele_cost

logger = logging.getLogger(__name__)

DB_FILE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "history.db"
)


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    migrate.run(conn)
    return conn


def record(outdoor_temp, indoor_temp, poele_state: str, tempo_color: str = None) -> None:
    """Insère une lecture. Appelé par le scheduler toutes les ~10 min."""
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO readings (ts, outdoor_temp, indoor_temp, poele_state, tempo_color) VALUES (?, ?, ?, ?, ?)",
                (datetime.now().isoformat(timespec="seconds"), outdoor_temp, indoor_temp, poele_state, tempo_color),
            )
        logger.debug("History : enregistrement outdoor=%.1f indoor=%s poele=%s tempo=%s",
                     outdoor_temp or 0, indoor_temp, poele_state, tempo_color)
    except Exception as e:
        logger.error("History : erreur enregistrement : %s", e)


def get_history(hours: int = 24) -> list[dict]:
    """Retourne les enregistrements des N dernières heures, triés par timestamp."""
    since = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT ts, outdoor_temp, indoor_temp, poele_state, tempo_color FROM readings WHERE ts >= ? ORDER BY ts",
                (since,),
            ).fetchall()
        return [
            {"ts": r[0], "outdoor_temp": r[1], "indoor_temp": r[2], "poele_state": r[3], "tempo_color": r[4]}
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
                    SUM(CASE WHEN poele_state = 'on'  THEN 1 ELSE 0 END) * 10 AS on_minutes,
                    SUM(CASE WHEN poele_state = 'off' THEN 1 ELSE 0 END) * 10 AS off_minutes,
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
                "tempo_color": dominant_color.get(day),
                "avg_outdoor_temp": round(avg_out, 1) if avg_out is not None else None,
                "avg_indoor_temp": round(avg_in, 1) if avg_in is not None else None,
            }
            for day, on_min, off_min, avg_out, avg_in in rows
        ]
    except Exception as e:
        logger.error("History : erreur résumé journalier : %s", e)
        return []


def get_pellet_consumption_since(since_date: str) -> dict:
    """
    Calcule la consommation réelle de granulés depuis une date donnée.
    since_date : format YYYY-MM-DD
    Retourne : {total_on_minutes, daily_breakdown: [{date, on_minutes}]}
    """
    try:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT substr(ts, 1, 10) AS day,
                       SUM(CASE WHEN poele_state = 'on' THEN 1 ELSE 0 END) * 10 AS on_minutes
                FROM readings
                WHERE ts >= ?
                GROUP BY day
                ORDER BY day
                """,
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


def purge_old(days: int = 30) -> None:
    """Supprime les données plus vieilles que N jours."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
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
    cop_curve = clim_cfg.get("cop_curve", [])

    poele_cost_per_hour = 0
    if poele_cfg:
        poele_result = compute_poele_cost(poele_cfg)
        poele_cost_per_hour = poele_result.get("cost_per_hour", 0)

    try:
        with _connect() as conn:
            # 1. Readings pour le mois
            readings = conn.execute(
                "SELECT ts, outdoor_temp, indoor_temp, poele_state, tempo_color "
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
            outdoor_temps = []
            indoor_temps = []
            heating_days_set = set()
            tempo_day_colors = {}  # day -> {color: count}

            for ts, outdoor_temp, indoor_temp, poele_state, tempo_color in readings:
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
                if poele_state == "on":
                    poele_on_count += 1
                    heating_days_set.add(day)
                    total_poele_cost += (10 / 60) * poele_cost_per_hour

                # Coût clim équivalent (pour toutes les lectures où le poêle est on
                # OU où du chauffage est nécessaire)
                if poele_state == "on" and outdoor_temp is not None:
                    color = tempo_color or "BLUE"
                    elec_price = tempo_prices.get(color, tempo_prices.get("BLUE", {})).get(period, 0.15)
                    cop = interpolate_cop(outdoor_temp, cop_curve)
                    cop = max(cop, 1.0)
                    thermal_kw = clim_cfg.get("nominal_capacity_kw", 4.0)
                    electric_kw = thermal_kw / cop
                    total_clim_equiv_cost += (10 / 60) * electric_kw * elec_price

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
                    updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                ),
            )
            logger.info("Agrégation %s : %d lectures, poêle %d min, coût poêle %.2f€, clim equiv %.2f€",
                         year_month, total_readings, poele_on_count * 10,
                         total_poele_cost, total_clim_equiv_cost)
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
                          updated_at
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
            }
            for r in rows
        ]
    except Exception as e:
        logger.error("Lecture monthly_reports échouée : %s", e)
        return []
