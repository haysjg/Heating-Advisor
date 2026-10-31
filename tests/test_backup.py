"""Tests unitaires pour modules/backup.py."""

import os
import sqlite3
import stat
from datetime import datetime

import pytest

from modules import backup


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    conn = sqlite3.connect(d / "history.db")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE readings (ts TEXT, outdoor_temp REAL)")
    conn.execute("INSERT INTO readings VALUES ('2026-10-31T10:00:00', 5.0)")
    conn.commit()
    conn.close()
    (d / "config_override.json").write_text('{"TARGET_TEMP": 21}')
    (d / ".secret_key").write_text("cle")
    (d / "secret.key").write_text("abc")
    return d


NOW = datetime(2026, 10, 31, 3, 30)


class TestRun:
    def test_creates_dated_folder_with_restricted_rights(self, data_dir):
        dest = backup.run(str(data_dir), now=NOW)
        assert dest == str(data_dir / "backups" / "2026-10-31")
        assert stat.S_IMODE(os.stat(dest).st_mode) == 0o700
        for name in ("config_override.json", ".secret_key", "secret.key", "history.db"):
            path = os.path.join(dest, name)
            assert os.path.isfile(path)
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert open(os.path.join(dest, "config_override.json")).read() == '{"TARGET_TEMP": 21}'

    def test_db_copy_is_readable(self, data_dir):
        dest = backup.run(str(data_dir), now=NOW)
        conn = sqlite3.connect(os.path.join(dest, "history.db"))
        try:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert conn.execute("SELECT outdoor_temp FROM readings").fetchall() == [(5.0,)]
        finally:
            conn.close()

    def test_backups_folder_and_sqlite_side_files_excluded(self, data_dir):
        backup.run(str(data_dir), now=NOW)
        dest = backup.run(str(data_dir), now=datetime(2026, 11, 1, 3, 30))
        names = os.listdir(dest)
        assert "backups" not in names
        assert not [n for n in names if n.endswith(("-wal", "-shm"))]

    def test_same_day_rerun_replaces(self, data_dir):
        backup.run(str(data_dir), now=NOW)
        (data_dir / "config_override.json").write_text('{"TARGET_TEMP": 22}')
        dest = backup.run(str(data_dir), now=NOW)
        assert open(os.path.join(dest, "config_override.json")).read() == '{"TARGET_TEMP": 22}'

    def test_rotation_removes_older_than_seven_days(self, data_dir):
        root = data_dir / "backups"
        for name in ("2026-10-23", "2026-10-24", "2026-10-25", "2026-10-30", "notes"):
            (root / name).mkdir(parents=True)
        backup.run(str(data_dir), now=NOW)
        # 31/10 − 7 jours = 24/10 : supprimé ; 25/10 (6 jours) conservé ; dossier non daté ignoré
        assert sorted(os.listdir(root)) == ["2026-10-25", "2026-10-30", "2026-10-31", "notes"]

    def test_failed_file_does_not_stop_backup(self, data_dir, monkeypatch):
        def _boom(src, dst):
            raise sqlite3.OperationalError("database is locked")
        monkeypatch.setattr(backup, "_backup_sqlite", _boom)
        dest = backup.run(str(data_dir), now=NOW)
        assert os.path.isfile(os.path.join(dest, "config_override.json"))
        assert not os.path.exists(os.path.join(dest, "history.db"))
