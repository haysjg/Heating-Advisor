"""Tests unitaires pour modules/crypto.py — chiffrement des secrets."""

from unittest.mock import patch

import pytest

import modules.crypto as crypto


@pytest.fixture(autouse=True)
def isolated_key(tmp_path, monkeypatch):
    monkeypatch.setattr(crypto, "_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setattr(crypto, "_decrypted_cache", {})


def test_roundtrip():
    stored = crypto.encrypt_password("s3cret")
    assert stored.startswith("enc:v1:")
    assert crypto.decrypt_password(stored) == "s3cret"


def test_plaintext_passthrough():
    assert crypto.decrypt_password("clair") == "clair"
    assert crypto.decrypt_password("") == ""


def test_derivation_done_once_per_secret():
    stored = crypto.encrypt_password("token-ha")
    with patch.object(crypto, "_derive_fernet_key", wraps=crypto._derive_fernet_key) as derive:
        for _ in range(5):
            assert crypto.decrypt_password(stored) == "token-ha"
    assert derive.call_count == 1


def test_new_secret_not_served_from_cache():
    first = crypto.encrypt_password("ancien")
    second = crypto.encrypt_password("nouveau")
    assert crypto.decrypt_password(first) == "ancien"
    assert crypto.decrypt_password(second) == "nouveau"


def test_invalid_value_not_cached():
    assert crypto.decrypt_password("enc:v1:invalide") == ""
    assert "enc:v1:invalide" not in crypto._decrypted_cache
