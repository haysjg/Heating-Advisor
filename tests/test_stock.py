"""Tests pour modules/stock.py — calculs purs du stock de granulés.

Couvre :
- Cas de base : aucune livraison
- kg livrés / consommés / restants
- Pourcentage restant
- Jours restants + date d'épuisement
- date d'épuisement None si pas assez de données
- Alerte stock faible (seuil)
- Prix : total_cost, avg_price_per_kg, prix_par_kg par livraison
- Livraisons sans prix (rétrocompatibilité)
- Mélange livraisons avec/sans prix
"""

from datetime import datetime

import pytest

from modules.stock import compute_stock_stats


# ── Fixtures ─────────────────────────────────────────────────────


def _consumption(total_minutes, daily=None):
    return {
        "total_on_minutes": total_minutes,
        "daily_breakdown": daily or [],
    }


NOW = datetime(2026, 4, 15, 12, 0, 0)

DELIVERY_SIMPLE = [{"date": "2026-03-01", "nb_sacs": 72, "poids_sac": 15.0}]  # 1080 kg


# ── Cas limites ───────────────────────────────────────────────────


class TestNoDeliveries:
    def test_returns_not_configured(self):
        result = compute_stock_stats([], _consumption(0), 1.0, NOW)
        assert result == {"configured": False}


# ── Calculs de base ───────────────────────────────────────────────


class TestKgCalculations:
    def test_total_kg_delivered(self):
        result = compute_stock_stats(DELIVERY_SIMPLE, _consumption(0), 1.0, NOW)
        assert result["total_kg_delivered"] == 1080.0

    def test_total_kg_consumed(self):
        # 600 minutes = 10h × 1 kg/h = 10 kg
        result = compute_stock_stats(DELIVERY_SIMPLE, _consumption(600), 1.0, NOW)
        assert result["total_kg_consumed"] == 10.0

    def test_remaining_kg(self):
        result = compute_stock_stats(DELIVERY_SIMPLE, _consumption(600), 1.0, NOW)
        assert result["remaining_kg"] == 1070.0

    def test_remaining_kg_never_negative(self):
        # Consommation fictive supérieure à la livraison
        result = compute_stock_stats(DELIVERY_SIMPLE, _consumption(999999), 1.0, NOW)
        assert result["remaining_kg"] == 0.0

    def test_remaining_pct(self):
        # 10 kg consommés sur 1080 → ~99.1 %
        result = compute_stock_stats(DELIVERY_SIMPLE, _consumption(600), 1.0, NOW)
        assert result["remaining_pct"] == round(1070 / 1080 * 100, 1)

    def test_consumption_rate_applied(self):
        # 60 minutes × 0.5 kg/h = 0.5 kg
        result = compute_stock_stats(DELIVERY_SIMPLE, _consumption(60), 0.5, NOW)
        assert result["total_kg_consumed"] == 0.5

    def test_multiple_deliveries_summed(self):
        deliveries = [
            {"date": "2026-01-01", "nb_sacs": 10, "poids_sac": 15.0},
            {"date": "2026-02-01", "nb_sacs": 20, "poids_sac": 15.0},
        ]
        result = compute_stock_stats(deliveries, _consumption(0), 1.0, NOW)
        assert result["total_kg_delivered"] == 450.0


# ── Jours restants et date d'épuisement ──────────────────────────


class TestDaysRemainingAndDepletionDate:
    def _make(self, remaining_kg, avg_daily_kg):
        """Construit un jeu de données qui donne le avg_daily_kg voulu."""
        # oldest_date = NOW - days_since jours, consommé = avg_daily_kg * days_since
        days_since = 30
        oldest = (NOW.replace(hour=0, minute=0, second=0) -
                  __import__("datetime").timedelta(days=days_since)).strftime("%Y-%m-%d")
        total_consumed_kg = avg_daily_kg * days_since
        total_delivered_kg = remaining_kg + total_consumed_kg
        # nb_sacs × poids_sac = total_delivered_kg
        deliveries = [{"date": oldest, "nb_sacs": int(total_delivered_kg), "poids_sac": 1.0}]
        on_minutes = total_consumed_kg * 60  # 1 kg/h
        return deliveries, _consumption(on_minutes)

    def test_days_remaining_computed(self):
        deliveries, cons = self._make(remaining_kg=300, avg_daily_kg=10)
        result = compute_stock_stats(deliveries, cons, 1.0, NOW)
        assert result["days_remaining"] == 30

    def test_depletion_date_present_when_days_known(self):
        deliveries, cons = self._make(remaining_kg=300, avg_daily_kg=10)
        result = compute_stock_stats(deliveries, cons, 1.0, NOW)
        assert result["depletion_date"] is not None
        # 30 jours après NOW = 15 mai 2026
        assert result["depletion_date"] == "15 May 2026"

    def test_depletion_date_none_when_no_consumption(self):
        # Aucune consommation → avg_daily_kg = None → pas de date
        result = compute_stock_stats(DELIVERY_SIMPLE, _consumption(0), 1.0, NOW)
        assert result["depletion_date"] is None

    def test_days_remaining_none_when_no_consumption(self):
        result = compute_stock_stats(DELIVERY_SIMPLE, _consumption(0), 1.0, NOW)
        assert result["days_remaining"] is None


# ── Alerte stock faible ───────────────────────────────────────────


class TestAlert:
    def _result_with_days(self, days_remaining):
        """Construit un résultat avec un days_remaining précis."""
        days_since = 30
        import datetime as dt
        oldest = (NOW - dt.timedelta(days=days_since)).strftime("%Y-%m-%d")
        avg_daily = 10.0
        remaining = avg_daily * days_remaining
        total_delivered = remaining + avg_daily * days_since
        deliveries = [{"date": oldest, "nb_sacs": int(total_delivered), "poids_sac": 1.0}]
        on_minutes = avg_daily * days_since * 60
        return compute_stock_stats(deliveries, _consumption(on_minutes), 1.0, NOW)

    def test_alert_triggered_below_threshold(self):
        result = self._result_with_days(10)
        assert result["alert"] is True

    def test_alert_triggered_at_threshold(self):
        result = self._result_with_days(15)
        assert result["alert"] is True

    def test_no_alert_above_threshold(self):
        result = self._result_with_days(30)
        assert result["alert"] is False

    def test_alert_threshold_days_in_result(self):
        result = compute_stock_stats(DELIVERY_SIMPLE, _consumption(0), 1.0, NOW)
        assert result["alert_threshold_days"] == 15


# ── Prix ─────────────────────────────────────────────────────────


class TestPrix:
    def test_no_price_gives_none_stats(self):
        result = compute_stock_stats(DELIVERY_SIMPLE, _consumption(0), 1.0, NOW)
        assert result["total_cost"] is None
        assert result["avg_price_per_kg"] is None

    def test_total_cost_single_delivery(self):
        deliveries = [{"date": "2026-03-01", "nb_sacs": 72, "poids_sac": 15.0, "prix_total": 350.0}]
        result = compute_stock_stats(deliveries, _consumption(0), 1.0, NOW)
        assert result["total_cost"] == 350.0

    def test_total_cost_multiple_deliveries(self):
        deliveries = [
            {"date": "2026-01-01", "nb_sacs": 10, "poids_sac": 15.0, "prix_total": 100.0},
            {"date": "2026-02-01", "nb_sacs": 10, "poids_sac": 15.0, "prix_total": 120.0},
        ]
        result = compute_stock_stats(deliveries, _consumption(0), 1.0, NOW)
        assert result["total_cost"] == 220.0

    def test_avg_price_per_kg(self):
        # 1 livraison : 72 sacs × 15 kg = 1080 kg, prix = 432 €  → 0.4 €/kg
        deliveries = [{"date": "2026-03-01", "nb_sacs": 72, "poids_sac": 15.0, "prix_total": 432.0}]
        result = compute_stock_stats(deliveries, _consumption(0), 1.0, NOW)
        assert result["avg_price_per_kg"] == round(432.0 / 1080, 4)

    def test_avg_price_per_kg_weighted(self):
        # Livraison A : 100 kg à 0.40 €/kg = 40 €
        # Livraison B : 100 kg à 0.50 €/kg = 50 €
        # Moyenne pondérée : 90 / 200 = 0.45 €/kg
        deliveries = [
            {"date": "2026-01-01", "nb_sacs": 10, "poids_sac": 10.0, "prix_total": 40.0},
            {"date": "2026-02-01", "nb_sacs": 10, "poids_sac": 10.0, "prix_total": 50.0},
        ]
        result = compute_stock_stats(deliveries, _consumption(0), 1.0, NOW)
        assert result["avg_price_per_kg"] == round(90 / 200, 4)

    def test_prix_par_kg_enriched_on_delivery(self):
        deliveries = [{"date": "2026-03-01", "nb_sacs": 4, "poids_sac": 25.0, "prix_total": 50.0}]
        result = compute_stock_stats(deliveries, _consumption(0), 1.0, NOW)
        # 4 × 25 = 100 kg, prix = 50 € → 0.5 €/kg
        assert result["deliveries"][0]["prix_par_kg"] == 0.5

    def test_delivery_without_price_not_enriched(self):
        result = compute_stock_stats(DELIVERY_SIMPLE, _consumption(0), 1.0, NOW)
        assert "prix_par_kg" not in result["deliveries"][0]

    def test_mixed_deliveries_only_priced_counted(self):
        # Une livraison avec prix, une sans → total_cost = seulement celle avec prix
        deliveries = [
            {"date": "2026-01-01", "nb_sacs": 10, "poids_sac": 15.0, "prix_total": 100.0},
            {"date": "2026-02-01", "nb_sacs": 10, "poids_sac": 15.0},
        ]
        result = compute_stock_stats(deliveries, _consumption(0), 1.0, NOW)
        assert result["total_cost"] == 100.0
        assert result["avg_price_per_kg"] == round(100.0 / 150, 4)


# ── Daily breakdown ───────────────────────────────────────────────


class TestDailyBreakdown:
    def test_daily_breakdown_kg_computed(self):
        daily = [
            {"date": "2026-03-01", "on_minutes": 120},
            {"date": "2026-03-02", "on_minutes": 60},
        ]
        result = compute_stock_stats(DELIVERY_SIMPLE, _consumption(180, daily), 1.0, NOW)
        assert result["daily_breakdown"][0]["kg"] == round(120 / 60, 3)
        assert result["daily_breakdown"][1]["kg"] == round(60 / 60, 3)

    def test_empty_breakdown(self):
        result = compute_stock_stats(DELIVERY_SIMPLE, _consumption(0, []), 1.0, NOW)
        assert result["daily_breakdown"] == []
