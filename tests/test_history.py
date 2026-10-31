"""Tests unitaires pour modules/history.py."""

import pytest

from modules.history import poele_state_for_history


ON_POELE = {"state": "on", "active_system": "poele"}
ON_CLIM = {"state": "on", "active_system": "clim"}
OFF = {"state": "off", "active_system": None}


class TestPoeleStateForHistory:
    @pytest.mark.parametrize("real", ["heat", "auto", "on"])
    def test_real_on_counts_even_if_thermostat_off(self, real):
        # Allumage manuel ou thermostat désactivé : la consommation doit être comptée
        assert poele_state_for_history(real, OFF) == "on"

    def test_real_off_wins_over_thermostat(self):
        assert poele_state_for_history("off", ON_POELE) == "off"

    @pytest.mark.parametrize("real", [None, "unavailable", "unknown"])
    def test_fallback_to_thermostat_when_ha_unavailable(self, real):
        assert poele_state_for_history(real, ON_POELE) == "on"
        assert poele_state_for_history(real, ON_CLIM) == "off"
        assert poele_state_for_history(real, OFF) == "off"


from datetime import datetime

import modules.history as history


class TestPurgeCutoff:
    def test_keeps_whole_previous_month(self):
        # 15 novembre : 30 jours = 16 octobre, mais octobre doit rester entier
        assert history.purge_cutoff(datetime(2026, 11, 15, 3, 0)) == "2026-10-01T00:00:00"

    def test_thirty_days_when_older_than_previous_month(self):
        # 1er mars : 30 jours = 30 janvier, antérieur au 1er février → la limite des 30 jours s'applique
        assert history.purge_cutoff(datetime(2026, 3, 1, 3, 0)) == "2026-01-30T03:00:00"

    def test_january_wraps_to_december(self):
        assert history.purge_cutoff(datetime(2026, 1, 20, 3, 0)) == "2025-12-01T00:00:00"


class TestPelletDaily:
    """Le stock de granulés doit compter les jours au-delà de la purge des lectures."""

    @pytest.fixture(autouse=True)
    def db(self, tmp_path, monkeypatch):
        monkeypatch.setattr(history, "DB_FILE", str(tmp_path / "history.db"))

    def test_consumption_survives_purge(self):
        from freezegun import freeze_time
        with freeze_time("2026-08-01 10:00:00"):
            for _ in range(6):
                history.record(5.0, 20.0, "on", "BLUE")   # 6 × 10 min = 60 min
            history.record(5.0, 20.0, "off", "BLUE")
        with freeze_time("2026-10-20 10:00:00"):
            history.record(5.0, 20.0, "on", "BLUE")
            history.purge_old(30)
            assert history.get_history(24 * 120) == [
                {"ts": "2026-10-20T10:00:00", "outdoor_temp": 5.0, "indoor_temp": 20.0, "poele_state": "on", "tempo_color": "BLUE"}
            ]
            data = history.get_pellet_consumption_since("2026-07-01")
        assert data["total_on_minutes"] == 70
        assert data["daily_breakdown"] == [{"date": "2026-08-01", "on_minutes": 60}, {"date": "2026-10-20", "on_minutes": 10}]

    def test_migration_backfills_existing_readings(self):
        import sqlite3
        from modules import migrate
        conn = sqlite3.connect(history.DB_FILE)
        for fn in migrate.MIGRATIONS[:5]:
            fn(conn)
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        conn.executemany("INSERT INTO schema_version VALUES (?, '2026-01-01')", [(i,) for i in range(1, 6)])
        conn.executemany("INSERT INTO readings (ts, poele_state) VALUES (?, ?)",
                         [("2026-10-01T08:00:00", "on"), ("2026-10-01T08:10:00", "poele"), ("2026-10-01T08:20:00", "off")])
        conn.commit(); conn.close()
        assert history.get_pellet_consumption_since("2026-10-01")["total_on_minutes"] == 20


class TestConnexion:
    """WAL, busy_timeout, index et migrations une seule fois par base."""

    @pytest.fixture(autouse=True)
    def db(self, tmp_path, monkeypatch):
        monkeypatch.setattr(history, "DB_FILE", str(tmp_path / "history.db"))

    def test_wal_and_busy_timeout(self):
        conn = history._connect()
        try:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == history.BUSY_TIMEOUT_MS
        finally:
            conn.close()

    def test_ts_indexes_present_after_migration(self):
        from modules import migrate
        history.init_db()
        conn = history._connect()
        try:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
            version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        finally:
            conn.close()
        assert {"idx_readings_ts", "idx_diagnose_log_ts"} <= names
        assert version == len(migrate.MIGRATIONS)

    def test_migrations_not_rerun_on_each_connection(self, monkeypatch):
        from modules import migrate
        calls = []
        real_run = migrate.run
        monkeypatch.setattr(migrate, "run", lambda conn: (calls.append(1), real_run(conn)))
        history.init_db()
        history.init_db()
        for _ in range(3):
            history._connect().close()
        history.record(5.0, 20.0, "on", "BLUE")
        assert len(calls) == 1

    def test_guard_migrates_without_explicit_init(self):
        # Scripts et tests qui n'appellent pas init_db() : la première connexion migre quand même
        history.record(5.0, 20.0, "on", "BLUE")
        assert len(history.get_history(1)) == 1
