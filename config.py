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
    "nominal_capacity_kw": 4.0,   # kW thermiques nominaux en chauffage (3,5 kW = puissance froid)
    "nominal_cop": 4.5,           # COP nominal estimé (A7°C / W20°C) — FD35VA similaire = 4,62
    "min_outdoor_temp": -10,      # température extérieure minimale d'utilisation (°C)
    "comfort_min_temp": 7,        # en dessous de cette température, la clim chauffe insuffisamment (confort)
    # Courbe COP simplifiée : liste de tuples (temp_extérieure, COP)
    # Interpolation linéaire entre les points
    "cop_curve": [
        (-10, 2.3),
        (-7,  2.8),
        ( 0,  3.5),
        ( 7,  4.5),
        (12,  5.0),
        (20,  5.8),
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
