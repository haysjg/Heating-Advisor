"""
Système de migration du schéma SQLite.

Chaque fonction migrate_NNN applique une étape de migration.
run(conn) est appelé à chaque connexion et n'applique que les migrations manquantes.
"""
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def _migrate_001(conn):
    """Baseline : crée toutes les tables et index du schéma initial."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS readings (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           TEXT    NOT NULL,
            outdoor_temp REAL,
            indoor_temp  REAL,
            poele_state  TEXT,
            tempo_color  TEXT
        );

        CREATE TABLE IF NOT EXISTS diagnose_log (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            ts               TEXT    NOT NULL,
            presence_status  TEXT,
            poele_real_state TEXT,
            thermostat_state TEXT,
            felt_temperature REAL,
            indoor_temp      REAL,
            in_schedule      INTEGER,
            everyone_away    INTEGER,
            suspended_until  TEXT,
            recommendation   TEXT
        );

        CREATE TABLE IF NOT EXISTS cop_tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            tag TEXT NOT NULL,
            outdoor_temp REAL,
            total_power REAL,
            heater_power REAL,
            base_consumption REAL,
            deduced_ac_power REAL,
            notes TEXT
        );

        CREATE TABLE IF NOT EXISTS cop_base_profile (
            hour_of_day INTEGER PRIMARY KEY,
            avg_base_watts REAL NOT NULL,
            sample_count INTEGER NOT NULL DEFAULT 0,
            last_updated TEXT
        );

        CREATE TABLE IF NOT EXISTS cop_measurements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            outdoor_temp REAL NOT NULL,
            ac_power_watts REAL NOT NULL,
            thermal_kw REAL NOT NULL DEFAULT 4.0,
            calculated_cop REAL NOT NULL,
            confidence_score REAL DEFAULT 1.0,
            tag_id INTEGER,
            FOREIGN KEY (tag_id) REFERENCES cop_tags(id)
        );

        CREATE TABLE IF NOT EXISTS cop_curve_learned (
            temp_bin_center REAL PRIMARY KEY,
            avg_cop REAL NOT NULL,
            sample_count INTEGER NOT NULL,
            std_deviation REAL,
            last_updated TEXT
        );

        CREATE TABLE IF NOT EXISTS cop_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_cop_tags_ts ON cop_tags(ts);
        CREATE INDEX IF NOT EXISTS idx_cop_measurements_temp ON cop_measurements(outdoor_temp);
        CREATE INDEX IF NOT EXISTS idx_cop_measurements_ts ON cop_measurements(ts);
    """)


def _migrate_002(conn):
    """Ajoute la table monthly_reports pour l'agrégation des coûts mensuels."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS monthly_reports (
            month TEXT PRIMARY KEY,
            total_readings INTEGER DEFAULT 0,
            poele_on_minutes INTEGER DEFAULT 0,
            heating_days INTEGER DEFAULT 0,
            avg_outdoor_temp REAL,
            avg_indoor_temp REAL,
            tempo_blue_days INTEGER DEFAULT 0,
            tempo_white_days INTEGER DEFAULT 0,
            tempo_red_days INTEGER DEFAULT 0,
            estimated_poele_cost REAL DEFAULT 0,
            estimated_clim_equiv_cost REAL DEFAULT 0,
            rec_poele_minutes INTEGER DEFAULT 0,
            rec_clim_minutes INTEGER DEFAULT 0,
            rec_none_minutes INTEGER DEFAULT 0,
            updated_at TEXT
        );
    """)


def _migrate_003(conn):
    """Ajoute les colonnes clim_real_state et active_system à diagnose_log."""
    # ALTER TABLE ADD COLUMN est safe en SQLite (no-op if column exists with IF NOT EXISTS not supported,
    # but we catch errors for safety)
    for col, col_type in [("clim_real_state", "TEXT"), ("active_system", "TEXT")]:
        try:
            conn.execute(f"ALTER TABLE diagnose_log ADD COLUMN {col} {col_type}")
        except Exception:
            pass  # column already exists


# Liste ordonnée des migrations. L'index+1 correspond au numéro de version.
MIGRATIONS = [
    _migrate_001,
    _migrate_002,
    _migrate_003,
]


def run(conn):
    """Applique les migrations manquantes sur la connexion donnée."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version    INTEGER PRIMARY KEY,
            applied_at TEXT    NOT NULL
        )
    """)
    conn.commit()

    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current = row[0] or 0

    for i, migration in enumerate(MIGRATIONS, start=1):
        if i <= current:
            continue
        migration(conn)
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (i, datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
        logger.info("Migration %d appliquée", i)
