"""
Calculs purs pour le suivi du stock de granulés.
Aucune dépendance I/O : prend toutes les données en paramètre.
"""

from datetime import datetime, timedelta


def compute_stock_stats(
    deliveries: list,
    consumption_data: dict,
    consumption_kg_per_hour: float,
    now: datetime,
    alert_threshold_days: int = 15,
) -> dict:
    """
    Calcule l'état complet du stock à partir des données brutes.

    deliveries           : liste de dicts {date, nb_sacs, poids_sac, prix_total?}
    consumption_data     : {total_on_minutes, daily_breakdown: [{date, on_minutes}]}
    consumption_kg_per_hour : taux de consommation du poêle
    now                  : datetime de référence (permet de mocker dans les tests)
    alert_threshold_days : seuil d'alerte stock faible
    """
    if not deliveries:
        return {"configured": False}

    deliveries_sorted = sorted(deliveries, key=lambda d: d["date"])
    oldest_date = deliveries_sorted[0]["date"]
    total_kg_delivered = sum(d["nb_sacs"] * d["poids_sac"] for d in deliveries_sorted)

    total_on_minutes = consumption_data["total_on_minutes"]
    total_kg_consumed = round(total_on_minutes / 60 * consumption_kg_per_hour, 2)
    remaining_kg = round(max(total_kg_delivered - total_kg_consumed, 0), 2)

    days_since = max((now - datetime.strptime(oldest_date, "%Y-%m-%d")).days, 1)
    avg_daily_kg = round(total_kg_consumed / days_since, 3) if total_kg_consumed > 0 else None
    days_remaining = round(remaining_kg / avg_daily_kg) if avg_daily_kg and avg_daily_kg > 0 else None
    depletion_date = (now + timedelta(days=days_remaining)).strftime("%d %b %Y") if days_remaining is not None else None

    daily_breakdown = [
        {
            "date": d["date"],
            "on_minutes": d["on_minutes"],
            "kg": round(d["on_minutes"] / 60 * consumption_kg_per_hour, 3),
        }
        for d in consumption_data["daily_breakdown"]
    ]

    # Statistiques de coût (uniquement sur les livraisons avec prix renseigné)
    priced = [d for d in deliveries_sorted if d.get("prix_total")]
    total_cost = round(sum(d["prix_total"] for d in priced), 2) if priced else None
    priced_kg = sum(d["nb_sacs"] * d["poids_sac"] for d in priced)
    avg_price_per_kg = round(sum(d["prix_total"] for d in priced) / priced_kg, 4) if priced and priced_kg > 0 else None

    # Enrichir chaque livraison avec le prix/kg calculé
    for d in deliveries_sorted:
        if d.get("prix_total"):
            kg = d["nb_sacs"] * d["poids_sac"]
            d["prix_par_kg"] = round(d["prix_total"] / kg, 4) if kg > 0 else None

    return {
        "configured": True,
        "deliveries": deliveries_sorted,
        "total_kg_delivered": round(total_kg_delivered, 1),
        "total_kg_consumed": total_kg_consumed,
        "remaining_kg": remaining_kg,
        "remaining_pct": round(remaining_kg / total_kg_delivered * 100, 1) if total_kg_delivered > 0 else 0,
        "avg_daily_kg": avg_daily_kg,
        "days_remaining": days_remaining,
        "depletion_date": depletion_date,
        "alert": days_remaining is not None and days_remaining <= alert_threshold_days,
        "alert_threshold_days": alert_threshold_days,
        "daily_breakdown": daily_breakdown,
        "total_cost": total_cost,
        "avg_price_per_kg": avg_price_per_kg,
        "oldest_date": oldest_date,
        "consumption_kg_per_hour": consumption_kg_per_hour,
    }
