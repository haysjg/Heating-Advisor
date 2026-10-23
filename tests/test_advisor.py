"""Tests unitaires pour modules/advisor.py — moteur de décision.

Ce module teste toutes les fonctions de calcul et de décision :
- Interpolation COP selon température extérieure
- Calcul des coûts horaires (clim, poêle)
- Recommandation finale (règles métier : jour rouge, confort, coûts)
- Blend des courbes COP théorique/apprise
- Analyses aujourd'hui et demain
"""

from datetime import datetime

import pytest

from modules.advisor import (
    interpolate_cop,
    compute_clim_cost,
    compute_poele_cost,
    make_recommendation,
    analyze,
    analyze_tomorrow,
)


# ── interpolate_cop ──────────────────────────────────────────────


class TestInterpolateCop:
    """Tests de l'interpolation linéaire du COP sur la courbe de température."""

    def test_empty_curve_returns_default(self):
        assert interpolate_cop(10.0, []) == 3.0

    def test_below_curve_returns_min_cop(self):
        curve = [(-10, 1.5), (0, 2.2), (7, 2.8)]
        assert interpolate_cop(-15.0, curve) == 1.5

    def test_above_curve_returns_max_cop(self):
        curve = [(-10, 1.5), (0, 2.2), (7, 2.8)]
        assert interpolate_cop(25.0, curve) == 2.8

    def test_exact_point_returns_that_cop(self):
        curve = [(-10, 1.5), (0, 2.2), (7, 2.8)]
        assert interpolate_cop(0.0, curve) == 2.2

    def test_interpolation_between_points(self):
        curve = [(0, 2.2), (7, 2.8)]
        # 3.5°C = milieu → COP = (2.2 + 2.8) / 2 = 2.5
        assert interpolate_cop(3.5, curve) == 2.5

    def test_interpolation_unsorted_curve(self):
        # La courbe est triée automatiquement
        curve = [(7, 2.8), (-10, 1.5), (0, 2.2)]
        assert interpolate_cop(-10.0, curve) == 1.5
        assert interpolate_cop(7.0, curve) == 2.8


# ── compute_clim_cost ────────────────────────────────────────────


class TestComputeClimCost:
    """Tests du calcul du coût horaire de la climatisation.

    Vérifie :
    - Calcul standard (COP, puissance électrique, coût)
    - Indisponibilité si température trop basse (< min_outdoor_temp)
    - Confort insuffisant si temp < comfort_min_temp
    - COP plancher à 1.0 (sécurité)
    - Override de la courbe COP
    """

    def test_normal_calculation(self, base_config):
        clim_cfg = base_config["CLIM"]
        result = compute_clim_cost(10.0, clim_cfg, 0.1369)
        assert result["available"] is True
        assert result["comfort_insufficient"] is False
        assert result["cop"] > 0
        assert result["cost_per_hour"] > 0
        # COP à 10°C (entre 7/2.8 et 12/3.2) → interpolé ≈ 3.0
        assert 2.8 <= result["cop"] <= 3.2

    def test_too_cold_unavailable(self, base_config):
        clim_cfg = base_config["CLIM"]
        result = compute_clim_cost(-15.0, clim_cfg, 0.1369)
        assert result["available"] is False
        assert result["cost_per_hour"] is None

    def test_comfort_insufficient(self, base_config):
        clim_cfg = base_config["CLIM"]
        # 5°C < comfort_min_temp (7°C) mais > min_outdoor_temp (-10°C)
        result = compute_clim_cost(5.0, clim_cfg, 0.1369)
        assert result["available"] is True
        assert result["comfort_insufficient"] is True

    def test_cop_floor_at_1(self):
        # Courbe avec COP < 1.0 → doit être clampé à 1.0
        clim_cfg = {
            "nominal_capacity_kw": 4.0,
            "min_outdoor_temp": -20,
            "comfort_min_temp": 7,
            "cop_curve": [(-20, 0.5), (0, 0.8)],
        }
        result = compute_clim_cost(-15.0, clim_cfg, 0.20)
        assert result["cop"] == 1.0
        # cost = 4.0 kW / 1.0 * 0.20 = 0.80 €/h
        assert result["cost_per_hour"] == 0.8


# ── compute_poele_cost ───────────────────────────────────────────


class TestComputePoeleCost:
    """Tests du calcul du coût horaire du poêle à granulés.

    Vérifie le calcul simple : consommation (kg/h) × prix (€/kg).
    Le poêle est toujours disponible.
    """

    def test_standard_calculation(self, base_config):
        poele_cfg = base_config["POELE"]
        result = compute_poele_cost(poele_cfg)
        # 1.0 kg/h × 0.4233 €/kg = 0.4233 €/h
        assert result["cost_per_hour"] == 0.4233
        assert result["available"] is True

    def test_returns_all_fields(self, base_config):
        poele_cfg = base_config["POELE"]
        result = compute_poele_cost(poele_cfg)
        assert "consumption_kg_per_hour" in result
        assert "pellet_price_per_kg" in result
        assert "efficiency" in result
        assert "thermal_output_kw" in result


# ── make_recommendation ──────────────────────────────────────────


class TestMakeRecommendation:
    """Tests de l'arbre de décision (recommandation finale).

    Priorités testées (ordre décroissant) :
    1. Les deux systèmes indisponibles → erreur
    2. Clim indisponible → poêle (warning)
    3. **JOUR ROUGE** → poêle (danger, override absolu)
    4. Poêle indisponible → clim (info)
    5. Confort insuffisant (temp < 7°C) → poêle (warning)
    6. Comparaison des coûts → le moins cher gagne
       - Écart > 0.01 €/h → success
       - Écart ≤ 0.01 €/h → info (marginal)
    """

    def _clim(self, cost, available=True, comfort=True, elec_price=0.1369):
        return {
            "cost_per_hour": cost,
            "cop": 3.0,
            "available": available,
            "comfort_insufficient": not comfort,
            "elec_price_kwh": elec_price,
            "note": "" if comfort else "Confort insuffisant en dessous de 7°C",
        }

    def _poele(self, cost=0.4233, available=True):
        return {
            "cost_per_hour": cost,
            "available": available,
        }

    def test_both_unavailable(self):
        r = make_recommendation(5.0, self._clim(None, available=False), self._poele(available=False), "BLUE", "HP")
        assert r["level"] == "error"
        assert r["system"] is None

    def test_clim_unavailable_poele_ok(self):
        r = make_recommendation(5.0, self._clim(None, available=False), self._poele(), "BLUE", "HP")
        assert r["system"] == "poele"
        assert r["level"] == "warning"

    def test_red_day_forces_poele(self):
        # Même si clim est moins chère, jour rouge → poêle
        r = make_recommendation(10.0, self._clim(0.10, elec_price=0.7561), self._poele(0.42), "RED", "HP")
        assert r["system"] == "poele"
        assert r["level"] == "danger"
        assert r.get("red_day_override") is True

    def test_poele_unavailable_clim_ok(self):
        r = make_recommendation(10.0, self._clim(0.30), self._poele(available=False), "BLUE", "HP")
        assert r["system"] == "clim"
        assert r["level"] == "info"

    def test_comfort_insufficient_forces_poele(self):
        r = make_recommendation(5.0, self._clim(0.30, comfort=False), self._poele(), "BLUE", "HP")
        assert r["system"] == "poele"
        assert r["level"] == "warning"
        assert r.get("comfort_override") is True

    def test_clim_cheaper_success(self):
        r = make_recommendation(10.0, self._clim(0.20), self._poele(0.42), "BLUE", "HP")
        assert r["system"] == "clim"
        assert r["level"] == "success"
        assert r["savings_per_hour"] == pytest.approx(0.22, abs=0.01)

    def test_poele_cheaper_success(self):
        r = make_recommendation(10.0, self._clim(0.50), self._poele(0.42), "BLUE", "HP")
        assert r["system"] == "poele"
        assert r["level"] == "success"

    def test_marginal_difference_info(self):
        # Écart ≤ 0.01 → level="info"
        r = make_recommendation(10.0, self._clim(0.425), self._poele(0.4233), "BLUE", "HP")
        assert r["level"] == "info"


# ── analyze ──────────────────────────────────────────────────────


class TestAnalyze:
    """Tests de l'analyse complète aujourd'hui (point d'entrée principal).

    Vérifie :
    - NO_HEATING_AT_NIGHT + période HC → system="none"
    - Température ≥ cible → system="none"
    - Jour ROUGE → system="poele" (level="danger")
    - Jour BLEU → comparaison des coûts
    - Retourne clim, poele, recommendation, daily_estimate
    """

    def _weather(self, temp):
        return {"temperature": temp, "source": "test"}

    def _tempo(self, color="BLUE", period="HP", hour=14):
        return {
            "today": {"color": color},
            "tomorrow": {"color": "BLUE"},
            "current_period": period,
            "current_hour": hour,
        }

    def test_no_heating_at_night_hc(self, base_config):
        result = analyze(self._weather(5.0), self._tempo(period="HC", hour=3), base_config)
        assert result["recommendation"]["system"] == "none"

    def test_temp_above_target_no_heating(self, base_config):
        result = analyze(self._weather(25.0), self._tempo(), base_config)
        assert result["recommendation"]["system"] == "none"

    def test_red_day_forces_poele(self, base_config):
        result = analyze(self._weather(10.0), self._tempo(color="RED"), base_config)
        assert result["recommendation"]["system"] == "poele"
        assert result["recommendation"]["level"] == "danger"

    def test_blue_day_compares_costs(self, base_config):
        result = analyze(self._weather(5.0), self._tempo(color="BLUE"), base_config)
        assert result["recommendation"]["system"] in ("clim", "poele")
        assert result["clim"]["available"] is True
        assert result["poele"]["available"] is True


# ── analyze_tomorrow ─────────────────────────────────────────────


class TestAnalyzeTomorrow:
    """Tests de l'analyse simplifiée pour demain.

    Vérifie :
    - Couleur UNKNOWN → recommendation=None, tempo_unknown=True
    - Jour ROUGE demain → system="poele" (level="danger")
    - Température ≥ cible → system="none"
    - Jour BLEU/BLANC → comparaison des coûts sur période HP
    """

    def _tempo(self, tomorrow_color):
        return {
            "today": {"color": "BLUE"},
            "tomorrow": {"color": tomorrow_color},
            "current_period": "HP",
            "current_hour": 14,
        }

    def test_unknown_color(self, base_config):
        result = analyze_tomorrow({"temperature": 5.0}, self._tempo("UNKNOWN"), base_config)
        assert result["recommendation"] is None
        assert result["tempo_unknown"] is True

    def test_red_tomorrow(self, base_config):
        result = analyze_tomorrow({"temperature": 5.0}, self._tempo("RED"), base_config)
        assert result["recommendation"]["system"] == "poele"
        assert result["recommendation"]["level"] == "danger"
        assert result["recommendation"].get("red_day_override") is True

    def test_temp_above_target(self, base_config):
        result = analyze_tomorrow({"temperature": 25.0}, self._tempo("BLUE"), base_config)
        assert result["recommendation"]["system"] == "none"

    def test_blue_tomorrow_compares_costs(self, base_config):
        result = analyze_tomorrow({"temperature": 5.0}, self._tempo("BLUE"), base_config)
        assert result["recommendation"]["system"] in ("clim", "poele")
        assert result["tempo_unknown"] is False
