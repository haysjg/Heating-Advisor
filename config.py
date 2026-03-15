# ============================================================
# Configuration du conseiller de chauffage
# Modifiez ces valeurs selon votre situation
# ============================================================

LOCATION = {
    "city": "Verrière-le-Buisson",
    "postal_code": "91370",
    "latitude": 48.7484,
    "longitude": 2.2655,
    # URL directe météociel (laissez vide pour détection auto)
    "meteociel_url": "",
}

# ── Tarifs EDF Tempo (€/kWh) – saison 2024-2025 ─────────────
TEMPO_PRICES = {
    "BLUE":    {"HP": 0.1369, "HC": 0.1056},
    "WHITE":   {"HP": 0.1894, "HC": 0.1259},
    "RED":     {"HP": 0.7561, "HC": 0.1369},
    "UNKNOWN": {"HP": 0.1894, "HC": 0.1259},  # fallback = Blanc
}

# Heures Pleines : 6h → 22h (les autres sont Heures Creuses)
HP_START = 6
HP_END = 22

# Pas de chauffage la nuit (Heures Creuses)
NO_HEATING_AT_NIGHT = True

# Température cible intérieure (°C)
TARGET_TEMP = 21

# ── Climatisation réversible ─────────────────────────────────
CLIM = {
    "model": "Mitsubishi MSZ-FA35VA",
    "nominal_capacity_kw": 4.0,   # kW thermiques nominaux en chauffage
    "nominal_cop": 2.8,           # COP estimé réel : modèle années 2000 (COP d'origine ~3,0-3,2)
                                  # + dégradation liée à l'âge (~20%) → estimation prudente à 2,8
    "min_outdoor_temp": -10,      # température extérieure minimale d'utilisation (°C)
    "comfort_min_temp": 7,        # en dessous de cette température, la clim chauffe insuffisamment (confort)
    # Courbe COP réaliste pour un appareil ancien des années 2000 (valeurs prudentes)
    # Interpolation linéaire entre les points
    "cop_curve": [
        (-10, 1.5),
        (-7,  1.8),
        ( 0,  2.2),
        ( 7,  2.8),
        (12,  3.2),
        (20,  3.8),
    ],
}

# ── Poêle à granulés ─────────────────────────────────────────
POELE = {
    "pellet_price_per_kg": 0.4233,    # €/kg — 457,20 € pour 72 sacs × 15 kg = 1 080 kg
    "consumption_kg_per_hour": 1.0,   # kg/h — 1 sac de 15 kg dure ~15h (fourchette 14-16h)
    "efficiency": 0.90,               # rendement du poêle
    "thermal_output_kw": 6.0,         # puissance thermique en fonctionnement normal
}

# ── Superficie à chauffer (pour info) ────────────────────────
SURFACE_M2 = 80  # m²

# ── Rafraîchissement automatique ─────────────────────────────
REFRESH_INTERVAL_MINUTES = 30
