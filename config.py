# ============================================================
# Configuration du conseiller de chauffage
# Modifiez ces valeurs selon votre situation
# ============================================================

LOCATION = {
    "city": "Verrière-le-Buisson",
    "postal_code": "91370",
    "latitude": 48.7484,
    "longitude": 2.2655,
    # Station météociel la plus proche : Orsay (~5 km)
    "meteociel_url": "https://www.meteociel.fr/temps-reel/obs_villes.php?code2=7149",
    # IP du NAS pour le lien dans les emails
    "nas_ip": "192.168.1.2",
    "nas_port": 8888,
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
    "thermal_output_kw": 7.2,         # puissance thermique en fonctionnement normal
}

# ── Superficie à chauffer (pour info) ────────────────────────
SURFACE_M2 = 80  # m²

# ── Rafraîchissement automatique ─────────────────────────────
REFRESH_INTERVAL_MINUTES = 30

# ── Home Assistant (optionnel) ───────────────────────────────
HOME_ASSISTANT = {
    "enabled": False,
    "url": "http://192.168.1.2:8123",
    "token": "",                          # token d'accès longue durée
    "poele_entity_id": "climate.edilkamin_climate",
    "auto_control": False,                # pilotage automatique ON/OFF selon recommandation
    "shelly_temp_entity_id": "sensor.sonde_temperature_temperature",
    "shelly_humidity_entity_id": "sensor.sonde_temperature_humidity",
}

# ── Thermostat automatique ───────────────────────────────────
THERMOSTAT = {
    "enabled": False,
    "temp_on": 20.0,                      # allumage si temp intérieure < cette valeur (°C)
    "temp_off": 22.9,                     # extinction si temp intérieure >= cette valeur (°C)
    "min_on_minutes": 90,                 # durée minimale ON avant extinction sur temp
    "end_of_schedule_grace_minutes": 45,  # durée minimale ON avant extinction fin de plage
    "check_interval_minutes": 10,         # fréquence de vérification
    "manual_off_suspend_hours": 4,        # suspension du thermostat après extinction manuelle (heures)
    "presence_enabled": False,            # mode absent : thermostat en pause si tout le monde est parti
    "person_entities": [],                # entités HA person à surveiller (ex. ["person.jg", "person.carine"])
    "nearby_zone_name": "nearby",         # nom de la zone HA de proximité (slug, ex. "nearby")
    "nearby_no_ignition_after": 20,       # heure à partir de laquelle la zone proximité = restriction (0-23)
    "nearby_grace_minutes": 20,           # grâce avant extinction si tout le monde en zone proximité après l'heure
    "use_felt_temperature": True,         # utiliser la température ressentie (temp + correction humidité)
    "humidity_reference": 50.0,           # humidité de référence (%)
    "humidity_correction_factor": 0.05,   # correction °C par % d'écart à la référence
    "schedule": {
        "mon": {"start": "05:45", "end": "22:00"},
        "tue": {"start": "05:45", "end": "22:00"},
        "wed": {"start": "06:00", "end": "22:00"},
        "thu": {"start": "05:45", "end": "22:00"},
        "fri": {"start": "05:45", "end": "22:00"},
        "sat": {"start": "07:00", "end": "22:00"},
        "sun": {"start": "07:00", "end": "22:00"},
    },
}

# ── Notifications email ───────────────────────────────────────
EMAIL = {
    "enabled": True,
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 587,
    "smtp_login": "",     # login SMTP (si vide, utilise sender) — utile pour Brevo etc.
    "sender": "hays.jg@gmail.com",
    "app_password": "REMPLACER_PAR_MOT_DE_PASSE_APP",  # mot de passe d'application Gmail
    "recipients": ["hays.jg@gmail.com"],
    "notify_hour": 20,    # heure d'envoi automatique (0-23)
    "notify_minute": 0,   # minute d'envoi automatique (0-59)
}
