"""Tests pour les helpers d'écriture thread-safe de config_override.json.

Couvre :
- patch_override   : read-modify-write, création si absent, fichier corrompu, concurrence
- write_override   : écrasement complet
- migrate_deliveries_from_override : migration depuis l'ancien emplacement, idempotence
"""

import json
import os
import threading

import pytest

import modules.overrides as overrides_mod
from modules.overrides import patch_override, write_override


# ── Helpers ───────────────────────────────────────────────────────


def _read(path):
    with open(path) as f:
        return json.load(f)


# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture()
def override_file(tmp_path):
    return str(tmp_path / "config_override.json")


@pytest.fixture()
def deliveries_file(tmp_path):
    return str(tmp_path / "deliveries.json")


@pytest.fixture(autouse=True)
def patch_override_path(override_file, monkeypatch):
    """Redirige OVERRIDE_FILE vers un fichier temporaire isolé pour chaque test."""
    monkeypatch.setattr(overrides_mod, "OVERRIDE_FILE", override_file)


# ── patch_override ────────────────────────────────────────────────


class TestPatchOverride:
    """Tests du read-modify-write atomique sur config_override.json."""

    def test_creates_file_if_absent(self, override_file):
        patch_override(lambda d: d.update({"KEY": "val"}))
        assert os.path.exists(override_file)
        assert _read(override_file) == {"KEY": "val"}

    def test_preserves_existing_keys(self, override_file):
        with open(override_file, "w") as f:
            json.dump({"EXISTING": 1}, f)
        patch_override(lambda d: d.update({"NEW": 2}))
        assert _read(override_file) == {"EXISTING": 1, "NEW": 2}

    def test_nested_setdefault_preserves_sibling_keys(self, override_file):
        with open(override_file, "w") as f:
            json.dump({"AUTH": {"other": "x"}}, f)
        patch_override(
            lambda d: d.setdefault("AUTH", {}).__setitem__("password_hash", "hash123")
        )
        data = _read(override_file)
        assert data["AUTH"]["password_hash"] == "hash123"
        assert data["AUTH"]["other"] == "x"

    def test_corrupted_file_treated_as_empty(self, override_file):
        with open(override_file, "w") as f:
            f.write("not json {{{")
        patch_override(lambda d: d.update({"KEY": "val"}))
        assert _read(override_file) == {"KEY": "val"}

    def test_updater_can_delete_key(self, override_file):
        with open(override_file, "w") as f:
            json.dump({"A": 1, "B": 2}, f)
        patch_override(lambda d: d.pop("A"))
        assert _read(override_file) == {"B": 2}

    def test_concurrent_writes_no_data_loss(self, override_file):
        """20 threads écrivent chacun une clé distincte : aucune ne doit être perdue."""
        with open(override_file, "w") as f:
            json.dump({}, f)

        errors = []

        def write_key(i):
            try:
                patch_override(lambda d: d.update({f"key_{i}": i}))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=write_key, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        data = _read(override_file)
        assert len(data) == 20
        for i in range(20):
            assert data[f"key_{i}"] == i


# ── write_override ────────────────────────────────────────────────


class TestWriteOverride:
    """Tests de l'écrasement complet de config_override.json."""

    def test_creates_file_with_data(self, override_file):
        write_override({"A": 1, "B": 2})
        assert _read(override_file) == {"A": 1, "B": 2}

    def test_overwrites_existing_content(self, override_file):
        with open(override_file, "w") as f:
            json.dump({"OLD": True}, f)
        write_override({"NEW": True})
        data = _read(override_file)
        assert data == {"NEW": True}
        assert "OLD" not in data

    def test_writes_empty_dict(self, override_file):
        write_override({})
        assert _read(override_file) == {}

    def test_unicode_preserved(self, override_file):
        write_override({"unicode": "éàü", "nested": {"x": 1}})
        data = _read(override_file)
        assert data["unicode"] == "éàü"


# ── migrate_deliveries_from_override ─────────────────────────────


def _make_migrate_fn(override_path, deliveries_path):
    """Construit une fonction de migration pointant vers les fichiers temporaires."""
    import json as _json
    import os as _os
    import logging as _logging
    _logger = _logging.getLogger(__name__)

    def migrate():
        if _os.path.exists(deliveries_path):
            return
        try:
            if not _os.path.exists(override_path):
                return
            with open(override_path) as f:
                override = _json.load(f)
            if "_deliveries" not in override:
                return
            deliveries = override.pop("_deliveries")
            _os.makedirs(_os.path.dirname(deliveries_path), exist_ok=True)
            with open(deliveries_path, "w") as f:
                _json.dump(deliveries, f, indent=2, ensure_ascii=False)
            with open(override_path, "w") as f:
                _json.dump(override, f, indent=2, ensure_ascii=False)
            _logger.info("Migration : %d livraison(s) déplacée(s)", len(deliveries))
        except Exception as e:
            _logger.error("Migration échouée : %s", e)

    return migrate


class TestMigrateDeliveriesFromOverride:
    """Tests de la migration unique _deliveries → deliveries.json."""

    def _sample_deliveries(self):
        return [
            {"date": "2025-11-01", "nb_sacs": 72, "poids_sac": 15.0},
            {"date": "2026-01-15", "nb_sacs": 36, "poids_sac": 15.0},
        ]

    def test_migrates_deliveries_to_dedicated_file(self, override_file, deliveries_file):
        deliveries = self._sample_deliveries()
        with open(override_file, "w") as f:
            json.dump({"TARGET_TEMP": 21, "_deliveries": deliveries}, f)

        migrate = _make_migrate_fn(override_file, deliveries_file)
        migrate()

        assert os.path.exists(deliveries_file)
        assert _read(deliveries_file) == deliveries

    def test_removes_deliveries_key_from_override(self, override_file, deliveries_file):
        with open(override_file, "w") as f:
            json.dump({"TARGET_TEMP": 21, "_deliveries": self._sample_deliveries()}, f)

        migrate = _make_migrate_fn(override_file, deliveries_file)
        migrate()

        data = _read(override_file)
        assert "_deliveries" not in data
        assert data["TARGET_TEMP"] == 21

    def test_idempotent_if_deliveries_file_already_exists(self, override_file, deliveries_file):
        existing = [{"date": "2025-11-01", "nb_sacs": 10, "poids_sac": 15.0}]
        with open(deliveries_file, "w") as f:
            json.dump(existing, f)
        with open(override_file, "w") as f:
            json.dump({"_deliveries": self._sample_deliveries()}, f)

        migrate = _make_migrate_fn(override_file, deliveries_file)
        migrate()

        assert _read(deliveries_file) == existing
        assert "_deliveries" in _read(override_file)

    def test_noop_if_override_file_absent(self, override_file, deliveries_file):
        migrate = _make_migrate_fn(override_file, deliveries_file)
        migrate()
        assert not os.path.exists(deliveries_file)

    def test_noop_if_no_deliveries_key_in_override(self, override_file, deliveries_file):
        with open(override_file, "w") as f:
            json.dump({"TARGET_TEMP": 21}, f)

        migrate = _make_migrate_fn(override_file, deliveries_file)
        migrate()

        assert not os.path.exists(deliveries_file)
        assert _read(override_file) == {"TARGET_TEMP": 21}
