"""Tests unitaires pour modules/electricity.py — parsing des emails "flash élec" Gmail.

Teste uniquement les fonctions pures (pas de réseau, pas de SQLite) :
- extraction de la période depuis le header X-CampaignID
- décodage du corps HTML (base64url + fallback quoted-printable)
- extraction de la valeur kWh et du delta hebdomadaire
- orchestration parse_message sur un message Gmail API factice
"""

import base64

from modules.electricity import (
    extract_campaign_period,
    decode_html_part,
    parse_kwh,
    parse_delta,
    parse_message,
)

SAMPLE_HTML = (
    'Vous avez consommé <span style="color: #000000;font-weight:700;">172,5 kWh</span> '
    "la semaine dernière (c'est 55,8% de plus par rapport à la semaine précédente)."
)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode()


def _fake_message(html: str = SAMPLE_HTML, with_campaign: bool = True, message_id: str = "18abc") -> dict:
    headers = []
    if with_campaign:
        headers.append({"name": "X-CampaignID", "value": "WEEKLY_2026-08-10_2026-08-16"})
    return {
        "id": message_id,
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": headers,
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64("version texte")}},
                {"mimeType": "text/html", "body": {"data": _b64(html)}},
            ],
        },
    }


# ── extract_campaign_period ──────────────────────────────────────


class TestExtractCampaignPeriod:
    def test_extracts_period_from_header(self):
        headers = [{"name": "X-CampaignID", "value": "WEEKLY_2026-08-10_2026-08-16"}]
        assert extract_campaign_period(headers) == ("2026-08-10", "2026-08-16")

    def test_header_missing_returns_none(self):
        assert extract_campaign_period([{"name": "Subject", "value": "hello"}]) is None

    def test_header_malformed_returns_none(self):
        headers = [{"name": "X-CampaignID", "value": "NOTWEEKLY"}]
        assert extract_campaign_period(headers) is None

    def test_empty_headers_returns_none(self):
        assert extract_campaign_period([]) is None
        assert extract_campaign_period(None) is None


# ── decode_html_part ──────────────────────────────────────────────


class TestDecodeHtmlPart:
    def test_decodes_nested_multipart(self):
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {"mimeType": "text/plain", "body": {"data": _b64("texte")}},
                        {"mimeType": "text/html", "body": {"data": _b64(SAMPLE_HTML)}},
                    ],
                }
            ],
        }
        assert decode_html_part(payload) == SAMPLE_HTML

    def test_no_html_part_returns_none(self):
        payload = {"mimeType": "text/plain", "body": {"data": _b64("texte seul")}}
        assert decode_html_part(payload) is None

    def test_quoted_printable_fallback(self):
        # Corps contenant des séquences quoted-printable non résolues par l'API.
        qp_body = "172,5 kWh=3C/span=3E"  # =3C = "<", =3E = ">"
        payload = {"mimeType": "text/html", "body": {"data": _b64(qp_body)}}
        result = decode_html_part(payload)
        assert "kWh</span>" not in result or "kWh=3C/span=3E" not in result  # décodé d'une façon ou d'une autre
        # Le fallback quopri doit avoir résolu =3C/=3E en </>
        assert "<" in result or "=3C" not in result


# ── parse_kwh ─────────────────────────────────────────────────────


class TestParseKwh:
    def test_parses_french_decimal_comma(self):
        assert parse_kwh(SAMPLE_HTML) == 172.5

    def test_parses_integer_value(self):
        html = 'consommé <span>150 kWh</span> cette semaine'
        assert parse_kwh(html) == 150.0

    def test_returns_none_when_absent(self):
        assert parse_kwh("<p>rien à voir ici</p>") is None

    def test_returns_none_on_empty_input(self):
        assert parse_kwh("") is None
        assert parse_kwh(None) is None


# ── parse_delta ───────────────────────────────────────────────────


class TestParseDelta:
    def test_parses_increase(self):
        assert parse_delta(SAMPLE_HTML) == (55.8, "plus")

    def test_parses_decrease(self):
        html = "c'est 12,3% de moins par rapport à la semaine précédente"
        assert parse_delta(html) == (12.3, "moins")

    def test_returns_none_when_absent(self):
        assert parse_delta("<p>pas de comparaison</p>") is None

    def test_returns_none_on_empty_input(self):
        assert parse_delta("") is None


# ── parse_message ─────────────────────────────────────────────────


class TestParseMessage:
    def test_full_message_parses_correctly(self):
        result = parse_message(_fake_message())
        assert result == {
            "message_id": "18abc",
            "period_start": "2026-08-10",
            "period_end": "2026-08-16",
            "kwh": 172.5,
            "delta_percent": 55.8,
            "delta_direction": "plus",
        }

    def test_missing_campaign_header_still_records_kwh(self):
        result = parse_message(_fake_message(with_campaign=False))
        assert result["kwh"] == 172.5
        assert result["period_start"] is None
        assert result["period_end"] is None

    def test_message_without_kwh_returns_none(self):
        message = _fake_message(html="<p>Pas de consommation ici</p>")
        assert parse_message(message) is None

    def test_none_message_returns_none(self):
        assert parse_message(None) is None
        assert parse_message({}) is None
