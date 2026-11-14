"""
Système de migration du schéma SQLite.

Chaque fonction migrate_NNN applique une étape de migration.
run(conn) est appelé à chaque connexion et n'applique que les migrations manquantes.
"""
import bisect
import logging
from datetime import datetime, timedelta

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


def _migrate_004(conn):
    """Ajoute la table electricity_readings pour le suivi conso Gmail."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS electricity_readings (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id      TEXT    NOT NULL UNIQUE,
            period_start    TEXT,
            period_end      TEXT,
            kwh             REAL    NOT NULL,
            delta_percent   REAL,
            delta_direction TEXT,
            fetched_at      TEXT    NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_electricity_period ON electricity_readings(period_start);
    """)


def _migrate_005(conn):
    """Ajoute electricity_import_log : trace chaque tentative de sync email (succès/échec),
    indépendamment de electricity_readings qui ne garde que les lectures réussies."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS electricity_import_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id  TEXT,
            email_date  TEXT,
            imported_at TEXT    NOT NULL,
            kwh         REAL,
            status      TEXT    NOT NULL,
            reason      TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_electricity_import_log_imported_at ON electricity_import_log(imported_at);
    """)


def _migrate_006(conn):
    """Ajoute pellet_daily : minutes de fonctionnement du poêle par jour, jamais purgées
    (readings est purgé à 30 jours, ce qui faussait le stock de granulés). Reprise des jours existants."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS pellet_daily (
            day        TEXT    PRIMARY KEY,
            on_minutes INTEGER NOT NULL DEFAULT 0
        );

        INSERT OR IGNORE INTO pellet_daily (day, on_minutes)
            SELECT substr(ts, 1, 10),
                   SUM(CASE WHEN poele_state IN ('on', 'poele') THEN 1 ELSE 0 END) * 10
            FROM readings
            GROUP BY substr(ts, 1, 10);
    """)


def _migrate_007(conn):
    """Index sur readings(ts) et diagnose_log(ts) : toutes les lectures filtrent ou purgent par date."""
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(ts);
        CREATE INDEX IF NOT EXISTS idx_diagnose_log_ts ON diagnose_log(ts);
    """)


def _migrate_008(conn):
    """Ajoute pellet_daily.ignitions : allumages du poêle par jour (passage off → on entre deux lectures),
    suivi de l'usure (résistance d'allumage, creuset). Reprise sur les lectures encore présentes (~30 jours)."""
    conn.executescript("""
        ALTER TABLE pellet_daily ADD COLUMN ignitions INTEGER NOT NULL DEFAULT 0;

        INSERT INTO pellet_daily (day, on_minutes, ignitions)
            SELECT substr(ts, 1, 10), 0, COUNT(*)
            FROM (SELECT ts, poele_state, LAG(poele_state) OVER (ORDER BY ts) AS prev FROM readings)
            WHERE poele_state IN ('on', 'poele') AND prev = 'off'
            GROUP BY substr(ts, 1, 10)
        ON CONFLICT(day) DO UPDATE SET ignitions = excluded.ignitions;
    """)


def _migrate_009(conn):
    """Historise la clim comme le poêle : readings.clim_state ("on" = clim en chauffe, NULL = état inconnu
    ou clim non configurée), clim_daily (minutes et kWh estimés par jour, jamais purgée) et colonnes clim
    de monthly_reports. Reprise de clim_state depuis diagnose_log (7 derniers jours), au point le plus
    proche à ±5 min ; clim_daily est recalculée au démarrage depuis readings (history.refresh_clim_daily)."""
    conn.executescript("""
        ALTER TABLE readings ADD COLUMN clim_state TEXT;

        CREATE TABLE IF NOT EXISTS clim_daily (
            day        TEXT    PRIMARY KEY,
            on_minutes INTEGER NOT NULL DEFAULT 0,
            kwh        REAL    NOT NULL DEFAULT 0
        );

        ALTER TABLE monthly_reports ADD COLUMN clim_on_minutes INTEGER;
        ALTER TABLE monthly_reports ADD COLUMN estimated_clim_cost REAL;
        ALTER TABLE monthly_reports ADD COLUMN estimated_poele_equiv_cost REAL;
    """)
    diag = [(datetime.fromisoformat(ts), state) for ts, state in conn.execute(
        "SELECT ts, clim_real_state FROM diagnose_log ORDER BY ts").fetchall()]
    if not diag:
        return
    times = [d for d, _ in diag]
    updates = []
    for (ts,) in conn.execute("SELECT ts FROM readings WHERE ts >= ?",
                              ((diag[0][0] - timedelta(minutes=5)).isoformat(timespec="seconds"),)).fetchall():
        when = datetime.fromisoformat(ts)
        i = bisect.bisect_left(times, when)
        gap, state = min((abs((diag[j][0] - when).total_seconds()), diag[j][1])
                         for j in (i - 1, i) if 0 <= j < len(diag))
        if gap <= 300 and state not in (None, "unavailable", "unknown"):
            updates.append(("on" if state == "heat" else "off", ts))
    conn.executemany("UPDATE readings SET clim_state = ? WHERE ts = ?", updates)


def _migrate_010(conn):
    """Démarrage optimisé (modules/warmup.py) : warmup_events, une ligne par montée en température du matin
    (jamais purgée, base de la courbe vitesse = f(temp. extérieure)), et optimal_start_log, journal des matins."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS warmup_events (
            start_ts     TEXT PRIMARY KEY,
            system       TEXT NOT NULL,
            outdoor_avg  REAL NOT NULL,
            indoor_start REAL NOT NULL,
            indoor_peak  REAL NOT NULL,
            lag_minutes  INTEGER NOT NULL,
            rate         REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS optimal_start_log (
            day               TEXT PRIMARY KEY,
            mode              TEXT NOT NULL,
            schedule_start    TEXT NOT NULL,
            comfort_time      TEXT NOT NULL,
            system            TEXT,
            need_ts           TEXT,
            outdoor           REAL,
            indoor_need       REAL,
            delta             REAL,
            predicted_minutes INTEGER,
            planned_start     TEXT,
            actual_start      TEXT,
            reached_ts        TEXT,
            indoor_at_comfort REAL,
            cost_per_hour     REAL
        );
    """)


def _migrate_011(conn):
    """Courbe de charge Linky (modules/linky.py, via MyElectricalData) : linky_load_curve, une ligne par
    intervalle de mesure (début en heure locale, kWh consommés), jamais purgée — base du comparatif d'options."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS linky_load_curve (
            ts  TEXT PRIMARY KEY,
            kwh REAL NOT NULL
        );
    """)


def _migrate_012(conn):
    """radiator_daily : minutes de marche, kWh et coût estimés de chaque radiateur géré, par jour (jamais purgée).
    Alimentée toutes les 10 min (puissance pleine × tarif Tempo du moment), base du coût du jour de l'accueil."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS radiator_daily (
            day        TEXT    NOT NULL,
            entity_id  TEXT    NOT NULL,
            on_minutes INTEGER NOT NULL DEFAULT 0,
            kwh        REAL    NOT NULL DEFAULT 0,
            cost       REAL    NOT NULL DEFAULT 0,
            PRIMARY KEY (day, entity_id)
        );
    """)


# Liste ordonnée des migrations. L'index+1 correspond au numéro de version.
MIGRATIONS = [
    _migrate_001,
    _migrate_002,
    _migrate_003,
    _migrate_004,
    _migrate_005,
    _migrate_006,
    _migrate_007,
    _migrate_008,
    _migrate_009,
    _migrate_010,
    _migrate_011,
    _migrate_012,
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
