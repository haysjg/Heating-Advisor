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


from datetime import date, datetime

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


class TestIgnitions:
    """Allumages du poêle : passage off → on entre deux lectures, cumul journalier non purgé."""

    @pytest.fixture(autouse=True)
    def db(self, tmp_path, monkeypatch):
        monkeypatch.setattr(history, "DB_FILE", str(tmp_path / "history.db"))

    def _feed(self, day, states):
        from freezegun import freeze_time
        for i, st in enumerate(states):
            with freeze_time(f"{day} {8 + i // 6:02d}:{i % 6 * 10:02d}:00"):
                history.record(5.0, 20.0, st, "BLUE")

    def test_off_to_on_counts_once(self):
        from freezegun import freeze_time
        with freeze_time("2026-11-09 08:00:00"):
            assert history.record(5.0, 20.0, "off") is False
        with freeze_time("2026-11-09 08:10:00"):
            assert history.record(5.0, 20.0, "on") is True
        with freeze_time("2026-11-09 08:20:00"):
            assert history.record(5.0, 20.0, "on") is False
        assert history.get_ignition_stats(date(2026, 11, 9))["today"] == 1

    def test_first_reading_on_is_not_an_ignition(self):
        # Pas de lecture précédente : poêle déjà allumé au démarrage de l'app
        assert history.record(5.0, 20.0, "on") is False

    def test_stats(self):
        self._feed("2026-11-07", ["off", "on", "on", "off", "on", "off"])   # 2 allumages, 30 min
        self._feed("2026-11-08", ["off", "on", "on", "on", "on", "off"])    # 1 allumage, 40 min
        self._feed("2026-11-09", ["off", "on", "off", "on", "off", "on"])   # 3 aujourd'hui
        st = history.get_ignition_stats(date(2026, 11, 9))
        assert st["today"] == 3
        assert st["avg_per_day_7d"] == 1.5          # 3 allumages sur 2 jours connus, aujourd'hui exclu
        assert st["avg_cycle_minutes_7d"] == 23     # 70 min / 3 allumages
        assert st["season"] == 6
        assert st["since"] == "2026-11-07"
        assert st["daily_7d"][-3:] == [
            {"date": "2026-11-07", "ignitions": 2}, {"date": "2026-11-08", "ignitions": 1},
            {"date": "2026-11-09", "ignitions": 3},
        ]
        assert st["daily_7d"][0] == {"date": "2026-11-03", "ignitions": None}   # aucune lecture ce jour-là

    def test_season_starts_september_first(self):
        assert history.season_start(date(2026, 11, 9)) == date(2026, 9, 1)
        assert history.season_start(date(2027, 3, 1)) == date(2026, 9, 1)

    def test_ignitions_survive_purge(self):
        from freezegun import freeze_time
        self._feed("2026-08-01", ["off", "on"])
        with freeze_time("2026-10-20 10:00:00"):
            history.purge_old(30)
        assert history.get_ignition_stats(date(2026, 9, 2))["season"] == 0
        assert history.get_ignition_stats(date(2026, 8, 2))["season"] == 1

    def test_migration_backfills_ignitions(self):
        import sqlite3
        from modules import migrate
        conn = sqlite3.connect(history.DB_FILE)
        for fn in migrate.MIGRATIONS[:7]:
            fn(conn)
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        conn.executemany("INSERT INTO schema_version VALUES (?, '2026-01-01')", [(i,) for i in range(1, 8)])
        conn.executemany("INSERT INTO readings (ts, poele_state) VALUES (?, ?)", [
            ("2026-11-08T23:50:00", "off"), ("2026-11-09T00:00:00", "on"), ("2026-11-09T00:10:00", "poele"),
            ("2026-11-09T00:20:00", "off"), ("2026-11-09T00:30:00", "on"),
        ])
        conn.execute("INSERT INTO pellet_daily (day, on_minutes) VALUES ('2026-11-09', 30)")
        conn.commit(); conn.close()
        st = history.get_ignition_stats(date(2026, 11, 9))
        assert st["today"] == 2
        assert history.get_pellet_consumption_since("2026-11-09")["total_on_minutes"] == 30


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


class TestAggregateMonth:
    """Agrégats de la page /reports : coût poêle, équivalent clim, jours Tempo, moyennes."""

    CFG = {
        "POELE": {"consumption_kg_per_hour": 1.0, "pellet_price_per_kg": 0.6,
                  "efficiency": 0.9, "thermal_output_kw": 7.2},
        "CLIM": {"nominal_capacity_kw": 4.0, "cop_curve": [(0, 2.0), (10, 4.0)]},
        "TEMPO_PRICES": {"BLUE": {"HP": 0.2, "HC": 0.1}, "RED": {"HP": 0.8, "HC": 0.1}},
        "HP_START": 6,
        "HP_END": 22,
    }

    @pytest.fixture(autouse=True)
    def db(self, tmp_path, monkeypatch):
        monkeypatch.setattr(history, "DB_FILE", str(tmp_path / "history.db"))

    def _record(self, ts, outdoor, state, color):
        from freezegun import freeze_time
        with freeze_time(ts):
            history.record(outdoor, 20.0, state, color)

    def test_costs_and_tempo_days(self):
        # 03/11 : 6 lectures poêle allumé en HP Bleu à 0 °C (COP 2) → 1 h
        for i in range(6):
            self._record(f"2026-11-03 10:{i}0:00", 0.0, "on", "BLUE")
        # 04/11 : 6 lectures en HC Rouge à 10 °C (COP 4), puis 1 lecture éteint
        for i in range(6):
            self._record(f"2026-11-04 23:{i}0:00", 10.0, "on", "RED")
        self._record("2026-11-05 12:00:00", 10.0, "off", "BLUE")
        assert history.aggregate_month("2026-11", self.CFG)
        r = history.get_monthly_reports(1)[0]
        assert r["poele_on_minutes"] == 120
        assert r["heating_days"] == 2
        assert r["estimated_poele_cost"] == 1.2           # 2 h × 1 kg/h × 0,60 €
        # 1 h × 4 kW / 2 × 0,20 € + 1 h × 4 kW / 4 × 0,10 € = 0,40 + 0,10
        assert r["estimated_clim_equiv_cost"] == 0.5
        assert (r["tempo_blue_days"], r["tempo_white_days"], r["tempo_red_days"]) == (2, 0, 1)
        assert r["avg_outdoor_temp"] == round((0 * 6 + 10 * 7) / 13, 1)

    def test_no_readings_returns_false(self):
        assert history.aggregate_month("2026-11", self.CFG) is False
        assert history.get_monthly_reports() == []
