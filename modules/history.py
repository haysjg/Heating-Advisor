"""
Historisation des températures et état du poêle en base SQLite.
Enregistrement toutes les ~10 min via le scheduler de app.py.
"""
import logging
import os
import sqlite3
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

DB_FILE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "history.db"
)


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS readings (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           TEXT    NOT NULL,
            outdoor_temp REAL,
            indoor_temp  REAL,
            poele_state  TEXT,
            tempo_color  TEXT
        )
    """)
    # Migration : ajoute la colonne si elle n'existe pas encore
    try:
        conn.execute("ALTER TABLE readings ADD COLUMN tempo_color TEXT")
    except sqlite3.OperationalError:
        pass  # colonne déjà présente
    conn.commit()
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


def purge_old(days: int = 30) -> None:
    """Supprime les données plus vieilles que N jours."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    try:
        with _connect() as conn:
            conn.execute("DELETE FROM readings WHERE ts < ?", (cutoff,))
        logger.info("History : purge données > %d jours", days)
    except Exception as e:
        logger.error("History : erreur purge : %s", e)
