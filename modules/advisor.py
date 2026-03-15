"""
Moteur de décision : compare le coût horaire de la climatisation et du poêle à granulés,
et recommande le système le plus économique.
"""

import math
from datetime import datetime


def interpolate_cop(temp: float, cop_curve: list) -> float:
    """
    Calcule le COP de la climatisation par interpolation linéaire
    sur la courbe COP en fonction de la température extérieure.
    """
    if not cop_curve:
        return 3.0

    sorted_curve = sorted(cop_curve, key=lambda x: x[0])

    if temp <= sorted_curve[0][0]:
        return sorted_curve[0][1]
    if temp >= sorted_curve[-1][0]:
        return sorted_curve[-1][1]

    for i in range(len(sorted_curve) - 1):
        t0, c0 = sorted_curve[i]
        t1, c1 = sorted_curve[i + 1]
        if t0 <= temp <= t1:
            ratio = (temp - t0) / (t1 - t0)
            return c0 + ratio * (c1 - c0)

    return 3.0


def compute_clim_cost(temp: float, clim_cfg: dict, elec_price_kwh: float) -> dict:
    """
    Calcule le coût horaire de la climatisation.
    Retourne le coût (€/h) et les paramètres utilisés.
    """
    if temp < clim_cfg.get("min_outdoor_temp", -15):
        return {
            "cost_per_hour": None,
            "cop": None,
            "power_input_kw": None,
            "available": False,
            "comfort_insufficient": False,
            "note": f"Température trop basse (< {clim_cfg['min_outdoor_temp']}°C), clim non opérationnelle",
        }

    comfort_min = clim_cfg.get("comfort_min_temp")
    comfort_insufficient = comfort_min is not None and temp < comfort_min

    cop = interpolate_cop(temp, clim_cfg.get("cop_curve", []))
    cop = max(cop, 1.0)  # plancher de sécurité

    thermal_kw = clim_cfg["nominal_capacity_kw"]
    electric_kw = thermal_kw / cop
    cost = electric_kw * elec_price_kwh

    return {
        "cost_per_hour": round(cost, 4),
        "cop": round(cop, 2),
        "power_input_kw": round(electric_kw, 2),
        "thermal_output_kw": thermal_kw,
        "elec_price_kwh": elec_price_kwh,
        "available": True,
        "comfort_insufficient": comfort_insufficient,
        "note": f"Confort insuffisant en dessous de {comfort_min}°C" if comfort_insufficient else "",
    }


def compute_poele_cost(poele_cfg: dict) -> dict:
    """
    Calcule le coût horaire du poêle à granulés.
    """
    consumption = poele_cfg["consumption_kg_per_hour"]
    price = poele_cfg["pellet_price_per_kg"]
    cost = consumption * price

    return {
        "cost_per_hour": round(cost, 4),
        "consumption_kg_per_hour": consumption,
        "pellet_price_per_kg": price,
        "efficiency": poele_cfg["efficiency"],
        "thermal_output_kw": poele_cfg["thermal_output_kw"],
        "available": True,
        "note": "",
    }


def make_recommendation(
    temp: float,
    clim_result: dict,
    poele_result: dict,
    tempo_color: str,
    tempo_period: str,
) -> dict:
    """
    Génère la recommandation finale avec niveau d'alerte et explication.
    """
    clim_ok = clim_result.get("available", False)
    clim_comfort = not clim_result.get("comfort_insufficient", False)
    poele_ok = poele_result.get("available", False)

    # ── Cas limites ──────────────────────────────────────────
    if not clim_ok and not poele_ok:
        return {
            "system": None,
            "level": "error",
            "title": "Aucun système disponible",
            "explanation": "Les deux systèmes semblent indisponibles.",
            "savings_per_hour": 0,
        }

    if not clim_ok:
        return {
            "system": "poele",
            "level": "warning",
            "title": "Poêle à granulés recommandé",
            "explanation": f"La climatisation n'est pas opérationnelle ({clim_result.get('note', '')}). Utilisez le poêle.",
            "savings_per_hour": 0,
        }

    # ── Règle absolue : JOUR ROUGE → jamais la clim ──────────
    # (quelle que soit la période HP/HC et le coût comparé)
    if tempo_color == "RED" and poele_ok:
        hp_price = clim_result["elec_price_kwh"]
        poele_cost = poele_result["cost_per_hour"]
        clim_cost = clim_result["cost_per_hour"]
        return {
            "system": "poele",
            "level": "danger",
            "title": "Poêle à granulés — JOUR ROUGE EDF",
            "explanation": (
                f"Jour Tempo ROUGE : la climatisation est exclue toute la journée. "
                f"Tarif électrique actuel : {hp_price:.4f} €/kWh "
                f"({'HP' if tempo_period == 'HP' else 'HC'}). "
                f"Poêle : {poele_cost:.3f} €/h — Clim (à titre indicatif) : {clim_cost:.3f} €/h."
            ),
            "savings_per_hour": round(abs(clim_cost - poele_cost), 4),
            "red_day_override": True,
        }

    if not poele_ok:
        return {
            "system": "clim",
            "level": "info",
            "title": "Climatisation recommandée",
            "explanation": "Le poêle n'est pas disponible. Utilisez la climatisation.",
            "savings_per_hour": 0,
        }

    # ── Confort insuffisant de la clim ───────────────────────
    if clim_ok and not clim_comfort:
        return {
            "system": "poele",
            "level": "warning",
            "title": "Poêle à granulés — confort insuffisant",
            "explanation": (
                f"La température extérieure ({temp:.1f}°C) est inférieure au seuil de confort "
                f"de la climatisation ({clim_result['note']}). "
                f"Le poêle est recommandé pour un chauffage efficace."
            ),
            "savings_per_hour": 0,
            "comfort_override": True,
        }

    # ── Comparaison des coûts (jours Bleu et Blanc uniquement) ──
    clim_cost = clim_result["cost_per_hour"]
    poele_cost = poele_result["cost_per_hour"]
    diff = abs(clim_cost - poele_cost)

    if clim_cost < poele_cost:
        system = "clim"
        level = "success" if diff > 0.01 else "info"
        title = "Climatisation recommandée"
        explanation = (
            f"La clim est plus économique : {clim_cost:.3f} €/h (COP {clim_result['cop']}) "
            f"contre {poele_cost:.3f} €/h pour le poêle. "
            f"Économie : {diff:.3f} €/h."
        )
    else:
        system = "poele"
        level = "success" if diff > 0.01 else "info"
        title = "Poêle à granulés recommandé"
        explanation = (
            f"Le poêle est plus économique : {poele_cost:.3f} €/h "
            f"contre {clim_cost:.3f} €/h pour la clim (COP {clim_result['cop']}). "
            f"Économie : {diff:.3f} €/h."
        )

    return {
        "system": system,
        "level": level,
        "title": title,
        "explanation": explanation,
        "savings_per_hour": round(diff, 4),
    }


def analyze(weather: dict, tempo: dict, config: dict) -> dict:
    """
    Point d'entrée principal. Retourne l'analyse complète.
    weather  = résultat de modules.weather.get_current_temperature()
    tempo    = résultat de modules.tempo.get_tempo_info()
    config   = dict depuis config.py (contient CLIM, POELE, TEMPO_PRICES)
    """
    temp = weather.get("temperature")
    color = tempo["today"]["color"]
    period = tempo["current_period"]
    elec_price = config["TEMPO_PRICES"][color][period]
    no_heating_at_night = config.get("NO_HEATING_AT_NIGHT", False)

    target_temp = config.get("TARGET_TEMP", 21)

    # Heures Creuses + pas de chauffage la nuit → rien à recommander
    if no_heating_at_night and period == "HC":
        return {
            "timestamp": datetime.now().isoformat(),
            "weather": weather,
            "tempo": tempo,
            "clim": {"available": False, "note": "Pas de chauffage la nuit"},
            "poele": {"available": False, "note": "Pas de chauffage la nuit"},
            "recommendation": {
                "system": "none",
                "level": "info",
                "title": "Pas de chauffage la nuit",
                "explanation": f"Il est {tempo['current_hour']}h — hors plage de chauffage ({config['HP_START']}h–{config['HP_END']}h).",
                "savings_per_hour": 0,
            },
            "daily_estimate": None,
        }

    # Température extérieure ≥ température cible → pas de chauffage nécessaire
    if temp is not None and temp >= target_temp:
        return {
            "timestamp": datetime.now().isoformat(),
            "weather": weather,
            "tempo": tempo,
            "clim": {"available": False, "note": "Chauffage inutile"},
            "poele": {"available": False, "note": "Chauffage inutile"},
            "recommendation": {
                "system": "none",
                "level": "info",
                "title": "Pas de chauffage nécessaire",
                "explanation": f"La température extérieure ({temp:.1f}°C) est supérieure ou égale à la température cible ({target_temp}°C).",
                "savings_per_hour": 0,
            },
            "daily_estimate": None,
        }

    clim_result = compute_clim_cost(temp, config["CLIM"], elec_price) if temp is not None else {
        "available": False, "note": "Température indisponible", "cost_per_hour": None
    }
    poele_result = compute_poele_cost(config["POELE"])

    recommendation = make_recommendation(temp, clim_result, poele_result, color, period)

    # Estimation journalière sur les HP uniquement (pas de chauffage la nuit)
    hp_hours = config["HP_END"] - config["HP_START"]  # 16h
    daily_estimate = None
    if clim_result.get("available") and temp is not None:
        hp_price = config["TEMPO_PRICES"][color]["HP"]
        clim_hp = compute_clim_cost(temp, config["CLIM"], hp_price)
        clim_daily = (clim_hp["cost_per_hour"] or 0) * hp_hours
        poele_daily = poele_result["cost_per_hour"] * hp_hours
        daily_estimate = {
            "clim": round(clim_daily, 2),
            "poele": round(poele_daily, 2),
            "hours": hp_hours,
        }

    return {
        "timestamp": datetime.now().isoformat(),
        "weather": weather,
        "tempo": tempo,
        "clim": clim_result,
        "poele": poele_result,
        "recommendation": recommendation,
        "daily_estimate": daily_estimate,
    }


def analyze_tomorrow(tomorrow_weather: dict, tempo: dict, config: dict) -> dict:
    """
    Analyse simplifiée pour demain : utilise la température moyenne prévue
    et la couleur Tempo de demain.
    """
    temp = tomorrow_weather.get("temperature")
    color = tempo["tomorrow"]["color"]
    target_temp = config.get("TARGET_TEMP", 21)
    hp_hours = config["HP_END"] - config["HP_START"]

    # Pas de chauffage si temp >= cible
    if temp is not None and temp >= target_temp:
        return {
            "weather": tomorrow_weather,
            "recommendation": {
                "system": "none",
                "level": "info",
                "title": "Pas de chauffage nécessaire",
                "explanation": f"Température prévue ({temp:.1f}°C) ≥ température cible ({target_temp}°C).",
                "savings_per_hour": 0,
            },
            "daily_estimate": None,
        }

    # Jour rouge → toujours le poêle
    if color == "RED":
        poele_result = compute_poele_cost(config["POELE"])
        hp_price = config["TEMPO_PRICES"]["RED"]["HP"]
        clim_result = compute_clim_cost(temp, config["CLIM"], hp_price) if temp is not None else {
            "available": False, "cost_per_hour": None
        }
        clim_cost = clim_result.get("cost_per_hour")
        poele_cost = poele_result["cost_per_hour"]
        return {
            "weather": tomorrow_weather,
            "recommendation": {
                "system": "poele",
                "level": "danger",
                "title": "Poêle à granulés — JOUR ROUGE EDF",
                "explanation": (
                    f"Demain est un jour Tempo ROUGE : la climatisation est exclue. "
                    f"Prévision température : {temp:.1f}°C (moy. journée)."
                    + (f" Clim (indicatif) : {clim_cost:.3f} €/h — Poêle : {poele_cost:.3f} €/h." if clim_cost else "")
                ),
                "savings_per_hour": round(abs(clim_cost - poele_cost), 4) if clim_cost else 0,
                "red_day_override": True,
            },
            "daily_estimate": {
                "clim": round(clim_cost * hp_hours, 2) if clim_cost else None,
                "poele": round(poele_cost * hp_hours, 2),
                "hours": hp_hours,
            },
        }

    # Jours bleu/blanc → comparaison des coûts
    hp_price = config["TEMPO_PRICES"][color]["HP"]
    clim_result = compute_clim_cost(temp, config["CLIM"], hp_price) if temp is not None else {
        "available": False, "cost_per_hour": None
    }
    poele_result = compute_poele_cost(config["POELE"])

    recommendation = make_recommendation(temp, clim_result, poele_result, color, "HP")

    daily_estimate = None
    if clim_result.get("available") and temp is not None:
        clim_daily = (clim_result["cost_per_hour"] or 0) * hp_hours
        poele_daily = poele_result["cost_per_hour"] * hp_hours
        daily_estimate = {
            "clim": round(clim_daily, 2),
            "poele": round(poele_daily, 2),
            "hours": hp_hours,
        }

    return {
        "weather": tomorrow_weather,
        "recommendation": recommendation,
        "daily_estimate": daily_estimate,
    }
