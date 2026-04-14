"""
Moteur de décision : compare le coût horaire de la climatisation et du poêle à granulés,
et recommande le système le plus économique.
"""

import math
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def get_effective_cop_curve(config: dict) -> list:
    """
    Retourne la courbe COP effective selon la stratégie :
    - Si COP_LEARNING désactivé ou confiance < 0.3 : courbe théorique pure
    - Si confiance >= seuil et auto_switch : courbe apprise pure
    - Sinon : blend pondéré (blend_factor = (confidence - 0.3) / 0.7)
    """
    cop_cfg = config.get("COP_LEARNING", {})
    if not cop_cfg.get("enabled", False):
        return config["CLIM"]["cop_curve"]

    try:
        from modules import cop_learning
        confidence = cop_learning.get_confidence_score()
        learned_curve = cop_learning.get_cop_curve_learned()

        if confidence < 0.3 or not learned_curve:
            # Confiance trop faible, utiliser courbe théorique
            return config["CLIM"]["cop_curve"]

        threshold = cop_cfg.get("confidence_threshold", 0.6)
        auto_switch = cop_cfg.get("auto_switch_to_learned", False)

        if auto_switch and confidence >= threshold:
            # Confiance suffisante et auto-switch activé : courbe apprise pure
            logger.info(f"Utilisation courbe COP apprise (confiance {confidence:.2f})")
            return learned_curve

        # Blend pondéré
        blend_factor = (confidence - 0.3) / 0.7
        theoretical = config["CLIM"]["cop_curve"]

        # Créer une courbe blendée
        blended = []
        for temp_theo, cop_theo in theoretical:
            # Trouver le COP appris le plus proche
            closest = min(learned_curve, key=lambda x: abs(x[0] - temp_theo), default=None)
            if closest and abs(closest[0] - temp_theo) <= 5:
                cop_learned = closest[1]
                cop_blended = cop_theo * (1 - blend_factor) + cop_learned * blend_factor
                blended.append((temp_theo, cop_blended))
            else:
                blended.append((temp_theo, cop_theo))

        logger.info(f"Utilisation courbe COP blendée (confiance {confidence:.2f}, facteur {blend_factor:.2f})")
        return blended

    except Exception as e:
        logger.error(f"Erreur calcul courbe effective : {e}")
        return config["CLIM"]["cop_curve"]


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


def compute_clim_cost(temp: float, clim_cfg: dict, elec_price_kwh: float, cop_curve_override: list = None) -> dict:
    """
    Calcule le coût horaire de la climatisation.
    Retourne le coût (€/h) et les paramètres utilisés.
    cop_curve_override : courbe COP à utiliser (si fournie, remplace celle de clim_cfg)
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

    cop_curve = cop_curve_override if cop_curve_override is not None else clim_cfg.get("cop_curve", [])
    cop = interpolate_cop(temp, cop_curve)
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


def build_inactive_reasons(
    temp: float,
    clim_result: dict,
    poele_result: dict,
    recommendation: dict,
    tempo_color: str,
    tempo_period: str,
    target_temp: float,
    hp_start: int,
    hp_end: int,
    no_heating_at_night: bool,
) -> dict:
    """
    Retourne, pour chaque système inactif, la raison principale.
    None = le système est celui recommandé.
    Si les deux sont inactifs pour la même raison, `shared` est rempli.
    """
    system = recommendation.get("system")

    # Les deux inactifs pour la même raison
    if system == "none":
        if temp is not None and temp >= target_temp:
            msg = f"Température extérieure ({temp:.1f}°C) ≥ température cible ({target_temp}°C) — chauffage inutile."
        elif no_heating_at_night and tempo_period == "HC":
            msg = f"Hors plage de chauffage ({hp_end}h–{hp_start}h) — pas de chauffage la nuit."
        else:
            msg = "Aucun chauffage nécessaire actuellement."
        return {"clim": msg, "poele": msg, "shared": True}

    clim_reason = None
    poele_reason = None

    # Clim inactive
    if system != "clim":
        if not clim_result.get("available"):
            clim_reason = clim_result.get("note", "Non opérationnelle.")
        elif tempo_color == "RED":
            clim_reason = "Jour Tempo ROUGE — clim exclue pour éviter le tarif à 0,756 €/kWh."
        elif clim_result.get("comfort_insufficient"):
            comfort_min = clim_result.get("note", "température trop basse")
            clim_reason = f"{comfort_min} — le poêle chauffe mieux à cette température."
        elif clim_result.get("cost_per_hour") and poele_result.get("cost_per_hour"):
            diff = round(clim_result["cost_per_hour"] - poele_result["cost_per_hour"], 3)
            clim_reason = (
                f"Plus coûteuse que le poêle : {clim_result['cost_per_hour']:.3f} €/h "
                f"vs {poele_result['cost_per_hour']:.3f} €/h (+{diff:.3f} €/h)."
            )

    # Poêle inactif
    if system != "poele":
        if clim_result.get("cost_per_hour") and poele_result.get("cost_per_hour"):
            diff = round(poele_result["cost_per_hour"] - clim_result["cost_per_hour"], 3)
            poele_reason = (
                f"Plus coûteux que la clim : {poele_result['cost_per_hour']:.3f} €/h "
                f"vs {clim_result['cost_per_hour']:.3f} €/h (+{diff:.3f} €/h)."
            )
        else:
            poele_reason = "La climatisation est préférée actuellement."

    return {"clim": clim_reason, "poele": poele_reason, "shared": False}


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

    _inactive_common = {
        "clim": None, "poele": None, "shared": False,
        "_temp": temp, "_target": target_temp,
        "_period": period, "_hp_start": config["HP_START"], "_hp_end": config["HP_END"],
        "_no_night": no_heating_at_night,
    }

    # Heures Creuses + pas de chauffage la nuit → rien à recommander
    if no_heating_at_night and period == "HC":
        rec_none = {
            "system": "none",
            "level": "info",
            "title": "Pas de chauffage la nuit",
            "explanation": f"Il est {tempo['current_hour']}h — hors plage de chauffage ({config['HP_START']}h–{config['HP_END']}h).",
            "savings_per_hour": 0,
        }
        clim_none = {"available": False, "note": "Pas de chauffage la nuit"}
        poele_none = {"available": False, "note": "Pas de chauffage la nuit"}
        return {
            "timestamp": datetime.now().isoformat(),
            "weather": weather,
            "tempo": tempo,
            "clim": clim_none,
            "poele": poele_none,
            "recommendation": rec_none,
            "inactive_reasons": build_inactive_reasons(
                temp, clim_none, poele_none, rec_none, color, period,
                target_temp, config["HP_START"], config["HP_END"], no_heating_at_night,
            ),
            "daily_estimate": None,
        }

    # Température extérieure ≥ température cible → pas de chauffage nécessaire
    if temp is not None and temp >= target_temp:
        rec_none = {
            "system": "none",
            "level": "info",
            "title": "Pas de chauffage nécessaire",
            "explanation": f"La température extérieure ({temp:.1f}°C) est supérieure ou égale à la température cible ({target_temp}°C).",
            "savings_per_hour": 0,
        }
        clim_none = {"available": False, "note": "Chauffage inutile"}
        poele_none = {"available": False, "note": "Chauffage inutile"}
        return {
            "timestamp": datetime.now().isoformat(),
            "weather": weather,
            "tempo": tempo,
            "clim": clim_none,
            "poele": poele_none,
            "recommendation": rec_none,
            "inactive_reasons": build_inactive_reasons(
                temp, clim_none, poele_none, rec_none, color, period,
                target_temp, config["HP_START"], config["HP_END"], no_heating_at_night,
            ),
            "daily_estimate": None,
        }

    # Utiliser la courbe COP effective (blend théorique + apprise)
    effective_cop_curve = get_effective_cop_curve(config)
    clim_result = compute_clim_cost(temp, config["CLIM"], elec_price, effective_cop_curve) if temp is not None else {
        "available": False, "note": "Température indisponible", "cost_per_hour": None
    }
    poele_result = compute_poele_cost(config["POELE"])

    recommendation = make_recommendation(temp, clim_result, poele_result, color, period)

    # Estimation journalière sur les HP uniquement (pas de chauffage la nuit)
    hp_hours = config["HP_END"] - config["HP_START"]  # 16h
    daily_estimate = None
    if clim_result.get("available") and temp is not None:
        hp_price = config["TEMPO_PRICES"][color]["HP"]
        clim_hp = compute_clim_cost(temp, config["CLIM"], hp_price, effective_cop_curve)
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
        "inactive_reasons": build_inactive_reasons(
            temp, clim_result, poele_result, recommendation, color, period,
            target_temp, config["HP_START"], config["HP_END"], no_heating_at_night,
        ),
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
        effective_cop_curve = get_effective_cop_curve(config)
        clim_result = compute_clim_cost(temp, config["CLIM"], hp_price, effective_cop_curve) if temp is not None else {
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

    # Couleur Tempo de demain non encore publiée
    if color == "UNKNOWN":
        return {
            "weather": tomorrow_weather,
            "recommendation": None,
            "daily_estimate": None,
            "tempo_unknown": True,
        }

    # Jours bleu/blanc → comparaison des coûts
    hp_price = config["TEMPO_PRICES"][color]["HP"]
    effective_cop_curve = get_effective_cop_curve(config)
    clim_result = compute_clim_cost(temp, config["CLIM"], hp_price, effective_cop_curve) if temp is not None else {
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
        "tempo_unknown": False,
    }
