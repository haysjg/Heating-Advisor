"""Tests unitaires pour modules/overrides.py — allow-list de fusion des surcharges.

Vérifie que les blocs de config sont bien fusionnés par apply(). En particulier,
un bloc absent de l'allow-list de apply() serait silencieusement ignoré (footgun) —
ce test capture la régression si "ELECTRICITY" venait à être oublié.
"""

from modules import overrides


class _FakeConfig:
    """Objet minimal imitant le module config.py pour les besoins du test."""

    def __init__(self):
        self.NTFY = {"enabled": False, "url": "", "topic": "heating-advisor", "token": ""}
        self.ELECTRICITY = {
            "enabled": False,
            "gmail_client_id": "",
            "gmail_client_secret": "",
            "gmail_refresh_token": "",
            "sender_filter": "bonjour@lite.eco",
            "subject_filter": "flash élec",
            "check_interval_hours": 24,
            "lookback_days": 90,
            "oauth_redirect_base": "",
        }


class TestApplyElectricity:
    def test_electricity_block_is_merged(self):
        cfg = _FakeConfig()
        overrides.apply(cfg, {"ELECTRICITY": {"enabled": True, "gmail_client_id": "abc123"}})
        assert cfg.ELECTRICITY["enabled"] is True
        assert cfg.ELECTRICITY["gmail_client_id"] == "abc123"
        # Les autres clés du bloc restent inchangées (merge shallow, pas un remplacement complet)
        assert cfg.ELECTRICITY["sender_filter"] == "bonjour@lite.eco"

    def test_electricity_refresh_token_preserved_when_not_in_override(self):
        cfg = _FakeConfig()
        cfg.ELECTRICITY["gmail_refresh_token"] = "enc:v1:secret"
        overrides.apply(cfg, {"ELECTRICITY": {"enabled": True}})
        assert cfg.ELECTRICITY["gmail_refresh_token"] == "enc:v1:secret"

    def test_unrelated_block_untouched(self):
        cfg = _FakeConfig()
        overrides.apply(cfg, {"ELECTRICITY": {"enabled": True}})
        assert cfg.NTFY["enabled"] is False
