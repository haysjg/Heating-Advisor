"""Tests unitaires pour modules/electricity.py — parsing des emails "flash élec" (Lite),
transférés depuis Gmail vers une boîte tierce et lus en IMAP brut (email.message.Message).

Teste uniquement les fonctions pures (pas de réseau, pas de SQLite) :
- extraction de la période depuis le header X-CampaignID
- décodage du corps HTML (multipart MIME, transfer-encoding quelconque)
- extraction de la valeur kWh et du delta hebdomadaire
- orchestration parse_message sur un email.message.Message factice
"""

import email
from email.message import EmailMessage

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


def _fake_message(html: str = SAMPLE_HTML, with_campaign: bool = True, message_id: str = "<18abc@mail.example>") -> EmailMessage:
    msg = EmailMessage()
    msg["Message-ID"] = message_id
    if with_campaign:
        msg["X-CampaignID"] = "WEEKLY_2026-08-10_2026-08-16"
    msg.set_content("version texte")
    msg.add_alternative(html, subtype="html")
    # Round-trip via bytes pour simuler fidèlement un fetch IMAP (BODY.PEEK[]).
    return email.message_from_bytes(msg.as_bytes())


# ── extract_campaign_period ──────────────────────────────────────


class TestExtractCampaignPeriod:
    def test_extracts_period_from_header(self):
        msg = _fake_message()
        assert extract_campaign_period(msg) == ("2026-08-10", "2026-08-16")

    def test_header_missing_returns_none(self):
        msg = _fake_message(with_campaign=False)
        assert extract_campaign_period(msg) is None

    def test_none_message_returns_none(self):
        assert extract_campaign_period(None) is None


# ── decode_html_part ──────────────────────────────────────────────


class TestDecodeHtmlPart:
    def test_decodes_multipart_alternative(self):
        msg = _fake_message()
        assert decode_html_part(msg).rstrip("\n") == SAMPLE_HTML

    def test_no_html_part_returns_none(self):
        msg = EmailMessage()
        msg.set_content("texte seul")
        msg = email.message_from_bytes(msg.as_bytes())
        assert decode_html_part(msg) is None

    def test_handles_accented_content(self):
        html = "<p>Consommé : 10 kWh — période précédente</p>"
        msg = EmailMessage()
        msg.set_content("texte")
        msg.add_alternative(html, subtype="html")
        msg = email.message_from_bytes(msg.as_bytes())
        assert decode_html_part(msg).rstrip("\n") == html

    def test_none_message_returns_none(self):
        assert decode_html_part(None) is None


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
            "message_id": "18abc@mail.example",
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
