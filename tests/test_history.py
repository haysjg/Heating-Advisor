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
