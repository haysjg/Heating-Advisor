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
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL,
            outdoor_temp REAL,
            indoor_temp  REAL,
            poele_state  TEXT
        )
    """)
    conn.commit()
    return conn


def record(outdoor_temp, indoor_temp, poele_state: str) -> None:
    """Insère une lecture. Appelé par le scheduler toutes les ~10 min."""
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO readings (ts, outdoor_temp, indoor_temp, poele_state) VALUES (?, ?, ?, ?)",
                (datetime.now().isoformat(timespec="seconds"), outdoor_temp, indoor_temp, poele_state),
            )
        logger.debug("History : enregistrement outdoor=%.1f indoor=%s poele=%s",
                     outdoor_temp or 0, indoor_temp, poele_state)
    except Exception as e:
        logger.error("History : erreur enregistrement : %s", e)


def get_history(hours: int = 24) -> list[dict]:
    """Retourne les enregistrements des N dernières heures, triés par timestamp."""
    since = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT ts, outdoor_temp, indoor_temp, poele_state FROM readings WHERE ts >= ? ORDER BY ts",
                (since,),
            ).fetchall()
        return [
            {"ts": r[0], "outdoor_temp": r[1], "indoor_temp": r[2], "poele_state": r[3]}
            for r in rows
        ]
    except Exception as e:
        logger.error("History : erreur lecture : %s", e)
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
