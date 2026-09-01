"""Tests unitaires pour modules/imap_client.py.

Pas de réseau réel : la connexion IMAP est mockée (unittest.mock). Couvre les parties
à risque — formatage de date IMAP, décodage MIME du sujet, filtrage client-side — qui
ne dépendent pas d'un serveur.
"""

from datetime import datetime
from unittest.mock import MagicMock

from modules.imap_client import (
    _imap_date,
    _decode_subject,
    is_connected,
    list_matching_messages,
    fetch_message,
)


# ── _imap_date ────────────────────────────────────────────────────


class TestImapDate:
    def test_format_is_always_english(self):
        # Le format IMAP SEARCH exige des mois en anglais, indépendamment de la locale système.
        result = _imap_date(0)
        today = datetime.now()
        assert result == f"{today.day:02d}-{['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][today.month - 1]}-{today.year}"

    def test_days_ago_moves_into_the_past(self):
        result = _imap_date(365)
        assert str(datetime.now().year - 1) in result or str(datetime.now().year) in result


# ── _decode_subject ───────────────────────────────────────────────


class TestDecodeSubject:
    def test_plain_ascii_subject(self):
        assert _decode_subject("Fwd: flash elec") == "Fwd: flash elec"

    def test_mime_encoded_subject(self):
        # "Fwd: flash élec" encodé en UTF-8 base64 (RFC 2047)
        encoded = "=?UTF-8?B?RndkOiBmbGFzaCDDqWxlYw==?="
        assert _decode_subject(encoded) == "Fwd: flash élec"

    def test_empty_subject_returns_empty(self):
        assert _decode_subject("") == ""


# ── is_connected ──────────────────────────────────────────────────


class TestIsConnected:
    def test_true_when_user_and_password_set(self):
        assert is_connected({"imap_user": "a@orange.fr", "imap_password": "enc:v1:x"}) is True

    def test_false_when_password_missing(self):
        assert is_connected({"imap_user": "a@orange.fr", "imap_password": ""}) is False

    def test_false_when_empty_config(self):
        assert is_connected({}) is False


# ── list_matching_messages / fetch_message (connexion mockée) ─────


class TestListMatchingMessages:
    def _mock_conn(self, uids, headers_by_uid):
        conn = MagicMock()
        conn.search.return_value = ("OK", [b" ".join(uids)])

        def fetch_side_effect(uid, spec):
            return "OK", [(b"1 (BODY[HEADER])", headers_by_uid[uid])]

        conn.fetch.side_effect = fetch_side_effect
        return conn

    def test_filters_by_decoded_subject(self):
        headers = {
            b"1": b"Subject: Fwd: flash elec\r\nMessage-ID: <a@x>\r\n\r\n",
            b"2": b"Subject: Autre chose\r\nMessage-ID: <b@x>\r\n\r\n",
        }
        conn = self._mock_conn([b"1", b"2"], headers)
        result = list_matching_messages(conn, "bonjour@lite.eco", "flash elec", 90)
        assert len(result) == 1
        assert result[0]["message_id"] == "a@x"

    def test_no_search_results_returns_empty(self):
        conn = MagicMock()
        conn.search.return_value = ("OK", [b""])
        assert list_matching_messages(conn, "x@y.com", "sujet", 90) == []

    def test_search_failure_returns_empty(self):
        conn = MagicMock()
        conn.search.return_value = ("NO", [])
        assert list_matching_messages(conn, "x@y.com", "sujet", 90) == []

    def test_missing_message_id_falls_back_to_uid(self):
        headers = {b"5": b"Subject: flash elec\r\n\r\n"}
        conn = self._mock_conn([b"5"], headers)
        result = list_matching_messages(conn, "x@y.com", "flash elec", 90)
        assert result[0]["message_id"] == "5"


class TestFetchMessage:
    def test_returns_none_on_fetch_failure(self):
        conn = MagicMock()
        conn.fetch.return_value = ("NO", [])
        assert fetch_message(conn, b"1") is None

    def test_returns_parsed_message_on_success(self):
        conn = MagicMock()
        conn.fetch.return_value = ("OK", [(b"1 (BODY[])", b"Subject: test\r\n\r\nbody")])
        msg = fetch_message(conn, b"1")
        assert msg is not None
        assert msg.get("Subject") == "test"
