# Heating Advisor

Application Flask de recommandation de chauffage. Compare en temps réel le coût horaire de la **climatisation réversible** et du **poêle à granulés** en fonction de la météo et des tarifs EDF Tempo, et pilote automatiquement le poêle via Home Assistant.

---

## Table des matières

1. [Architecture](#architecture)
2. [Règles métier](#règles-métier)
3. [Modules](#modules)
4. [Thermostat automatique](#thermostat-automatique)
5. [Détection de présence](#détection-de-présence)
6. [API REST](#api-rest)
7. [Configuration](#configuration)
8. [Déploiement](#déploiement)

---

## Architecture

```
app.py                   # Point d'entrée Flask + scheduler APScheduler
notify.py                # Envoi email de recommandation J+1
config.py                # Configuration par défaut
data/
  config_override.json   # Surcharges persistées via l'interface /config
  history.db             # Historique SQLite des températures et état poêle
  thermostat_state.json  # État persisté du thermostat automatique
modules/
  advisor.py             # Moteur de décision (comparaison des coûts)
  weather.py             # Récupération météo (météociel.fr + Open-Meteo)
  tempo.py               # Couleur EDF Tempo (api-couleur-tempo.fr)
  thermostat.py          # Pilotage automatique du poêle
  homeassistant.py       # Client API REST Home Assistant
  history.py             # Historisation SQLite
  overrides.py           # Chargement/application de config_override.json
  crypto.py              # Chiffrement AES des mots de passe en config
```

**Flux de données principal :**

```
Météo (météociel.fr / Open-Meteo)
        ↓
    weather.py → température extérieure
        ↓
    advisor.py ← tempo.py (couleur EDF Tempo)
        ↓
  Recommandation (clim / poêle / aucun)
        ↓
    thermostat.py → homeassistant.py → poêle Edilkamin (climate entity)
```

---

## Règles métier

### 1. Pas de chauffage la nuit

Si `NO_HEATING_AT_NIGHT = True`, aucune recommandation n'est émise pendant les **Heures Creuses** (par défaut 22h–6h). Le thermostat respecte également ses propres plages horaires configurables.

### 2. Température extérieure ≥ température cible

Si la température extérieure est supérieure ou égale à `TARGET_TEMP` (21°C par défaut), aucun chauffage n'est nécessaire — aucune recommandation n'est émise.

### 3. Jour ROUGE EDF Tempo → toujours le poêle

Règle absolue : un jour Tempo ROUGE, la climatisation est **systématiquement exclue**, quelle que soit la comparaison de coûts. Le poêle est recommandé.

### 4. Seuil de confort de la clim (7°C)

En dessous de `CLIM.comfort_min_temp` (7°C), la climatisation chauffe insuffisamment même si elle est techniquement disponible. Le poêle est recommandé indépendamment des coûts.

### 5. Température minimale d'utilisation de la clim

En dessous de `CLIM.min_outdoor_temp` (-10°C), la climatisation est considérée **hors service**. Seul le poêle est disponible.

### 6. Comparaison des coûts (jours Bleu et Blanc)

Le coût horaire de chaque système est calculé puis comparé :

- **Clim** : `coût = (puissance_thermique / COP) × prix_kWh`
  - Le COP est interpolé linéairement sur `CLIM.cop_curve` selon la température extérieure.
- **Poêle** : `coût = consommation_kg/h × prix_granulés_€/kg`

Le système le moins cher est recommandé.

---

## Modules

### `advisor.py` — Moteur de décision

| Fonction | Description |
|---|---|
| `interpolate_cop(temp, cop_curve)` | Interpolation linéaire du COP sur la courbe constructeur selon la température extérieure. Applique les valeurs extrêmes si hors plage. |
| `compute_clim_cost(temp, clim_cfg, elec_price)` | Calcule le coût horaire (€/h) de la clim. Retourne `available: False` si température trop basse. Signale `comfort_insufficient` si sous le seuil de confort. |
| `compute_poele_cost(poele_cfg)` | Calcule le coût horaire fixe du poêle (indépendant de la météo). |
| `make_recommendation(temp, clim, poele, color, period)` | Applique les règles métier dans l'ordre : disponibilité → jour ROUGE → confort → comparaison de coût. Retourne le système recommandé avec niveau d'alerte et explication textuelle. |
| `analyze(weather, tempo, config)` | Point d'entrée pour l'analyse du jour courant. Orchestre les appels ci-dessus et calcule l'estimation journalière (sur les HP uniquement). |
| `analyze_tomorrow(weather, tempo, config)` | Analyse simplifiée pour J+1 : utilise la température moyenne prévue et la couleur Tempo de demain. Retourne `tempo_unknown: True` si la couleur n'est pas encore publiée. |

### `weather.py` — Météo

| Fonction | Description |
|---|---|
| `get_current_temperature(config)` | Récupère la température extérieure actuelle. Essaie d'abord **météociel.fr** (scraping HTML), puis bascule sur **Open-Meteo** en cas d'échec. |
| `get_tomorrow_weather(config)` | Récupère la température moyenne prévue pour demain via Open-Meteo. |
| `get_hourly_forecast(lat, lon, hours)` | Récupère les prévisions horaires sur N heures via Open-Meteo (utilisé pour le graphique du dashboard). |

### `tempo.py` — EDF Tempo

| Fonction | Description |
|---|---|
| `get_tempo_info(hp_start, hp_end)` | Retourne la couleur Tempo d'aujourd'hui et de demain, la période courante (HP/HC) et l'heure. Utilise l'API communautaire `api-couleur-tempo.fr`. |
| `is_hp(hour, hp_start, hp_end)` | Retourne `True` si l'heure est en Heure Pleine. |
| `get_current_period(hp_start, hp_end)` | Retourne `"HP"` ou `"HC"` selon l'heure actuelle. |

### `homeassistant.py` — Client Home Assistant

| Fonction | Description |
|---|---|
| `turn_on(cfg)` | Allume le poêle via l'API HA (`climate/turn_on`). |
| `turn_off(cfg)` | Éteint le poêle via l'API HA (`climate/turn_off`). |
| `get_state(cfg)` | Retourne l'état brut de l'entité poêle dans HA. |
| `get_indoor_climate(cfg)` | Récupère la température et l'humidité intérieure depuis le Shelly via HA. |
| `get_presence(cfg, person_entities)` | Retourne `True` si au moins une personne est `"home"`, `False` si tout le monde est absent, `None` en cas d'erreur. |
| `get_presence_extended(cfg, person_entities, nearby_zone_name)` | Retourne `"home"` / `"nearby"` / `"away"` selon la position des personnes par rapport aux zones HA. |
| `apply_recommendation(cfg, system)` | Allume le poêle si `system == "poele"`, l'éteint sinon. Utilisé en mode pilotage automatique. |

### `history.py` — Historisation

Enregistrement toutes les ~10 min dans une base SQLite (`data/history.db`).

| Fonction | Description |
|---|---|
| `record(outdoor_temp, indoor_temp, poele_state, tempo_color)` | Insère une lecture. Appelé par le scheduler de `app.py`. |
| `get_history(hours)` | Retourne les enregistrements des N dernières heures (utilisé par `/api/statistics`). |
| `get_daily_summary(days)` | Agrège par journée : minutes allumé/éteint, température moyenne ext/int, couleur Tempo majoritaire. |
| `purge_old(days)` | Supprime les données de plus de N jours. Lancé chaque lundi à 3h. |

### `overrides.py` — Surcharges de configuration

| Fonction | Description |
|---|---|
| `load(cfg)` | Charge `data/config_override.json` et l'applique sur le module `config` au démarrage. |
| `apply(cfg, data)` | Applique un dict de surcharges sur le module `config`. Supporte tous les blocs (`THERMOSTAT`, `HOME_ASSISTANT`, `EMAIL`, `LOCATION`, etc.). |

---

## Thermostat automatique

Le thermostat automatique pilote le poêle selon la température intérieure mesurée par la sonde Shelly via HA. Il s'exécute toutes les 10 minutes via le scheduler.

### Cycle de décision

```
1. HA configuré ?                          → sinon, skip
2. Sonde accessible ?                      → sinon, alerte email + extinction sécurité hors plage
3. Synchronisation avec l'état réel HA     → détecte allumage/extinction manuels
4. Mode absent / proximité                 → voir section suivante
5. Suspension active ?                     → si oui, skip jusqu'à expiration
6. Dans la plage horaire ?
   - OUI + poêle OFF + ressenti < temp_on + recommandation "poele" → allumage
   - OUI + poêle ON  + ressenti ≥ temp_off + allumé ≥ min_on_minutes → extinction
   - NON + poêle ON  + allumé ≥ grace_minutes → extinction fin de plage
```

### Paramètres clés

| Paramètre | Rôle |
|---|---|
| `temp_on` | Seuil d'allumage (température ressentie, °C) |
| `temp_off` | Seuil d'extinction (température ressentie, °C) |
| `min_on_minutes` | Durée minimale de fonctionnement avant extinction par température |
| `end_of_schedule_grace_minutes` | Délai avant extinction après la fin de la plage horaire |
| `manual_off_suspend_hours` | Suspension du thermostat après extinction manuelle |
| `use_felt_temperature` | Utilise la température ressentie (corrigée par l'humidité) |

### Température ressentie

`ressenti = temp_réelle + (humidité - humidité_référence) × facteur_correction`

Exemple : 20°C, 65% d'humidité, référence 50%, facteur 0.05 → ressenti = 20 + (65-50) × 0.05 = **20.75°C**

### Gestion des échecs de sonde

Si la sonde est inaccessible ≥ 3 fois consécutives (~30 min), une alerte email est envoyée. Le poêle continue de s'éteindre à la fin des plages horaires (extinction de sécurité) mais ne peut plus être allumé automatiquement par la température.

---

## Détection de présence

### Mode absent simple (sans zone proximité)

Si `presence_enabled = True` et `nearby_zone_name` est vide, le thermostat s'arrête dès que **tout le monde** est hors de la zone `home` HA, et reprend dès qu'une personne rentre.

### Mode deux zones (recommandé)

Nécessite deux zones configurées dans Home Assistant :
- **Zone `home`** (petite, ~200m) : la maison stricto sensu
- **Zone `nearby`** (grande, ex. 3 500m) : le périmètre de proximité

| Situation | Comportement |
|---|---|
| Au moins une personne dans `home` | Fonctionnement normal |
| Tout le monde dans `nearby`, avant `nearby_no_ignition_after` | Fonctionnement normal |
| Tout le monde dans `nearby`, après `nearby_no_ignition_after` | Grâce `nearby_grace_minutes` → plus d'allumage, puis extinction |
| Tout le monde hors `nearby` | Extinction immédiate, pas d'allumage |

**`nearby_restricted_since`** est persisté dans `thermostat_state.json` pour calculer la durée de la grâce de façon fiable entre deux cycles de 10 min.

---

## API REST

| Endpoint | Description |
|---|---|
| `GET /` | Dashboard principal |
| `GET /config` | Page de configuration |
| `GET /statistics` | Page historique températures et poêle |
| `GET /api/data` | Données JSON complètes (météo, Tempo, recommandation) |
| `GET /api/refresh` | Force le rechargement des données |
| `GET /api/statistics?hours=N` | Historique des N dernières heures |
| `GET /api/statistics/daily?days=N` | Résumé journalier sur N jours |
| `GET /api/thermostat/state` | État courant du thermostat |
| `GET /api/thermostat/diagnose` | Diagnostic complet (temp, sonde, présence, scheduler) |
| `POST /api/thermostat/toggle` | Active/désactive le thermostat |
| `POST /config/save` | Sauvegarde la configuration depuis l'interface |
| `POST /api/notify/test` | Déclenche manuellement l'email de recommandation |

---

## Configuration

Tous les paramètres sont modifiables via l'interface `/config` sans rebuild Docker. Les valeurs sont sauvegardées dans `data/config_override.json` et surchargent `config.py` au démarrage.

### Blocs principaux

**`LOCATION`** — Localisation et météo
| Clé | Description |
|---|---|
| `city`, `postal_code` | Ville et code postal |
| `latitude`, `longitude` | Coordonnées GPS |
| `meteociel_url` | URL de la station météociel.fr la plus proche |
| `nas_ip`, `nas_port` | Adresse du NAS pour les liens dans les emails |

**`CLIM`** — Climatisation réversible
| Clé | Description |
|---|---|
| `model` | Nom du modèle (informatif) |
| `nominal_capacity_kw` | Puissance thermique nominale en chauffage (kW) |
| `nominal_cop` | COP moyen estimé |
| `min_outdoor_temp` | Température minimale d'utilisation (°C) |
| `comfort_min_temp` | Seuil en dessous duquel le confort est insuffisant (°C) |
| `cop_curve` | Courbe COP en fonction de la temp. extérieure — liste de `(temp, COP)` |

**`POELE`** — Poêle à granulés
| Clé | Description |
|---|---|
| `pellet_price_per_kg` | Prix des granulés (€/kg) |
| `consumption_kg_per_hour` | Consommation horaire (kg/h) |
| `efficiency` | Rendement du poêle (0–1) |
| `thermal_output_kw` | Puissance thermique nominale (kW) |

**`TEMPO_PRICES`** — Tarifs EDF Tempo (€/kWh)
Trois couleurs (`BLUE`, `WHITE`, `RED`), deux périodes chacune (`HP`, `HC`).

**`THERMOSTAT`** — Thermostat automatique
| Clé | Description |
|---|---|
| `enabled` | Active/désactive le thermostat |
| `temp_on` / `temp_off` | Seuils d'allumage et d'extinction (°C ressenti) |
| `min_on_minutes` | Durée minimale ON avant extinction par température |
| `end_of_schedule_grace_minutes` | Délai avant extinction fin de plage |
| `manual_off_suspend_hours` | Suspension après extinction manuelle |
| `presence_enabled` | Active la détection de présence |
| `person_entities` | Entités HA `person` à surveiller |
| `nearby_zone_name` | Slug de la zone de proximité HA |
| `nearby_no_ignition_after` | Heure de restriction si tout le monde en zone proximité |
| `nearby_grace_minutes` | Grâce avant extinction en mode proximité restreint |
| `use_felt_temperature` | Utilise la température ressentie (humidité) |
| `schedule` | Plages horaires par jour (`start`/`end` au format `HH:MM`) |

**`HOME_ASSISTANT`**
| Clé | Description |
|---|---|
| `enabled` | Active l'intégration HA |
| `url` | URL de l'instance HA (ex. `http://192.168.1.2:8123`) |
| `token` | Token d'accès longue durée (chiffré en AES en config) |
| `poele_entity_id` | Entité climate du poêle dans HA |
| `auto_control` | Pilotage automatique ON/OFF selon la recommandation J+1 |
| `shelly_temp_entity_id` | Entité capteur de température intérieure (Shelly) |
| `shelly_humidity_entity_id` | Entité capteur d'humidité intérieure (Shelly) |

**`EMAIL`**
| Clé | Description |
|---|---|
| `enabled` | Active les notifications email |
| `smtp_host` / `smtp_port` | Serveur SMTP |
| `sender` / `recipients` | Expéditeur et destinataires |
| `app_password` | Mot de passe applicatif Gmail (chiffré) |
| `notify_hour` / `notify_minute` | Heure d'envoi automatique de la recommandation J+1 |

---

## Déploiement

### Première installation sur NAS Synology

```bash
cd /volume1/docker
wget https://github.com/haysjg/Heating-Advisor/archive/refs/heads/main.tar.gz -O ha.tar.gz
tar xzf ha.tar.gz && mv Heating-Advisor-main heating-advisor && rm ha.tar.gz
cd heating-advisor
mkdir -p data
sudo docker-compose up -d --build
```

### Mise à jour

```bash
cd /volume1/docker/heating-advisor
wget https://github.com/haysjg/Heating-Advisor/archive/refs/heads/main.tar.gz -O ha.tar.gz
tar xzf ha.tar.gz --strip-components=1 && rm ha.tar.gz
mkdir -p data
sudo docker-compose down && sudo docker-compose up -d --build
```

### En local (développement)

```bash
pip install -r requirements.txt
python app.py
# → http://localhost:5000
```

Le répertoire `data/` est monté en volume Docker — la configuration, l'historique et l'état du thermostat sont **persistés entre les redémarrages**.
