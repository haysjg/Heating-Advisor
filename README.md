# Heating Advisor

Application Flask de recommandation de chauffage. Compare en temps réel le coût horaire de la **climatisation réversible** et du **poêle à granulés** en fonction de la météo et des tarifs EDF Tempo, et pilote automatiquement le poêle via Home Assistant.

---

## Table des matières

1. [Architecture](#architecture)
2. [Règles métier](#règles-métier)
3. [Modules](#modules)
4. [Thermostat automatique](#thermostat-automatique)
5. [Détection de présence](#détection-de-présence)
6. [Mode Absence](#mode-absence)
7. [Notifications push](#notifications-push)
8. [Suivi électricité (IMAP)](#suivi-électricité-imap)
   - [Compteur Linky et comparatif des options](#compteur-linky-et-comparatif-des-options)
9. [API REST](#api-rest)
10. [Configuration](#configuration)
11. [Déploiement](#déploiement)

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
  tempo_cache.json       # Couleurs Tempo publiées, par date (30 jours)
  *.bak                  # Version précédente de chaque fichier JSON (restaurée si le fichier est illisible)
  backups/AAAA-MM-JJ/    # Sauvegarde nocturne locale de data/ (7 jours)
modules/
  advisor.py             # Moteur de décision (comparaison des coûts)
  weather.py             # Récupération météo (stations Infoclimat / Météo-France + Open-Meteo)
  tempo.py               # Couleur EDF Tempo (api-couleur-tempo.fr)
  thermostat.py          # Pilotage automatique du poêle
  homeassistant.py       # Client API REST Home Assistant
  history.py             # Historisation SQLite
  overrides.py           # Chargement/application de config_override.json
  jsonstore.py           # Écriture JSON atomique et verrouillée (fichiers de data/)
  crypto.py              # Chiffrement AES des mots de passe en config
  ntfy_push.py           # Notifications push via Ntfy
  imap_client.py         # Client IMAP (lecture seule) — boîte tierce recevant les mails Lite transférés
  electricity.py         # Parsing emails "flash élec" + historisation conso électrique
  warmup.py              # Démarrage optimisé : vitesse de montée apprise, allumage du matin retardé
```

**Flux de données principal :**

```
Météo (Infoclimat / Météo-France / Open-Meteo)
        ↓
    weather.py → température extérieure
        ↓
    advisor.py ← tempo.py (couleur EDF Tempo)
        ↓
  Recommandation (clim / poêle / aucun)
        ↓
    thermostat.py → homeassistant.py → poêle Edilkamin (climate entity)
        ↓
    ntfy_push.py → notification push (allumage / extinction)
```

---

## Règles métier

### 1. Pas de chauffage la nuit

Le chauffage de nuit est borné par les **plages horaires du thermostat** (ex. 05:45–22:00), pas par la recommandation. `NO_HEATING_AT_NIGHT` n'est volontairement pas transmis au moteur de décision (`build_advisor_config`) : la règle renverrait « aucun chauffage » pendant toutes les Heures Creuses et bloquerait l'allumage de 05:45. Entre 05:45 et 06:00, la comparaison de coûts utilise le tarif réellement en vigueur (HC jusqu'à 06:00, puis HP).

### 2. Température extérieure ≥ température cible

Si la température extérieure est supérieure ou égale à `TARGET_TEMP` (21°C par défaut), aucun chauffage n'est nécessaire — aucune recommandation n'est émise.

### 3. Jour ROUGE EDF Tempo → toujours le poêle

Règle absolue : un jour Tempo ROUGE, la climatisation est **systématiquement exclue**, quelle que soit la comparaison de coûts. Le poêle est recommandé.

### 4. Seuil de confort de la clim (7°C)

En dessous de `CLIM.comfort_min_temp` (7°C), la climatisation chauffe insuffisamment même si elle est techniquement disponible. Le poêle est recommandé indépendamment des coûts.

Pour éviter les va-et-vient quand la température extérieure oscille autour du seuil, le thermostat applique une hystérésis : un poêle allumé reste en place tant qu'il ne fait pas au moins `comfort_min_temp + THERMOSTAT.clim_comfort_hysteresis` (7 + 1 = 8°C) dehors.

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

### `analysis.py` — Analyse courante et cache

| Fonction | Description |
|---|---|
| `build_advisor_config(config)` | Configuration unique du moteur de décision (tarifs, clim, poêle, HP/HC, `TARGET_TEMP`), partagée par le dashboard, le thermostat et l'email du soir. |
| `DataCache` | Cache des **données externes** (météo, Tempo, prévisions) en *stale-while-revalidate* : périmé après `REFRESH_INTERVAL_MINUTES`, à chaque bascule HP/HC et au changement de jour ; les données périmées restent servies pendant le rafraîchissement en arrière-plan. |
| `compute_analysis(raw, cfg, now)` | Recalcule la décision à **chaque lecture** à partir des données en cache et de l'heure courante : période HP/HC et tarif toujours justes, sans appel réseau. `timestamp` = heure de récupération des données. |

### `weather.py` — Météo

| Fonction | Description |
|---|---|
| `get_current_temperature(config, observations)` | Récupère la température extérieure **mesurée** : stations Infoclimat (une seule requête), puis stations Météo-France (API DPObs v2, OAuth2), chacune dans l'ordre configuré. Une mesure plus vieille que `max_age_minutes` est ignorée. Sans mesure récente : dernière mesure obtenue il y a moins de 1 h, puis modèle **Open-Meteo** (badge « modèle »), puis dernière valeur connue de moins de 3 h ; sans aucune valeur, la recommandation vaut `unknown` et le thermostat garde le système en cours. Une source est mise en pause 1 h après un HTTP 429. |
| `get_tomorrow_weather(config)` | Récupère la température moyenne prévue pour demain via Open-Meteo. |
| `get_hourly_forecast(lat, lon, hours)` | Récupère les prévisions horaires sur N heures via Open-Meteo (utilisé pour le graphique du dashboard). |

### `tempo.py` — EDF Tempo

| Fonction | Description |
|---|---|
| `get_tempo_info(hp_start, hp_end)` | Retourne la couleur Tempo d'aujourd'hui et de demain, la période courante (HP/HC) et l'heure. Utilise l'API communautaire `api-couleur-tempo.fr` ; une couleur publiée est mémorisée dans `data/tempo_cache.json` (elle ne change plus), ce qui protège d'une panne de l'API dans la journée. Couleur inconnue → `UNKNOWN` (les radiateurs ne sont alors ni éteints ni rallumés). |
| `is_hp(hour, hp_start, hp_end)` | Retourne `True` si l'heure est en Heure Pleine. |
| `get_current_period(hp_start, hp_end, now=None)` | Retourne `"HP"` ou `"HC"` selon l'heure (actuelle par défaut). |
| `at_time(tempo, hp_start, hp_end, now=None)` | Copie des infos Tempo avec période HP/HC et heure recalculées (sans appel réseau). |
| `get_season_stats(now=None)` | Jours rouges et blancs tirés et restants sur la saison Tempo en cours (1er septembre → 31 août, quotas 22 rouges / 43 blancs, bleus = le reste). Couleurs publiées comptées, demain compris. Décompte mis en cache 3 h dans `data/tempo_season.json` ; dernier décompte servi si l'API est en panne. Affiché sur la carte Tempo de l'accueil. |

### `homeassistant.py` — Client Home Assistant

| Fonction | Description |
|---|---|
| `turn_on(cfg)` | Allume le poêle via l'API HA (`climate/turn_on`). |
| `turn_off(cfg)` | Éteint le poêle via l'API HA (`climate/turn_off`). |
| `get_state(cfg)` | Retourne l'état brut de l'entité poêle dans HA. |
| `get_indoor_climate(cfg)` | Récupère la température et l'humidité intérieure depuis le Shelly via HA. |
| `get_presence(cfg, person_entities)` | Retourne `True` si au moins une personne est `"home"`, `False` si tout le monde est absent, `None` en cas d'erreur. |
| `get_entities(cfg, entity_ids)` | États bruts de plusieurs entités (entités inconnues omises), `None` si HA injoignable. |
| `get_presence_extended(cfg, person_entities, nearby_zone_name)` | Retourne `"home"` / `"nearby"` / `"away"` selon la position des personnes par rapport aux zones HA. |
| `apply_recommendation(cfg, system)` | Allume le poêle si `system == "poele"`, l'éteint sinon. Utilisé en mode pilotage automatique. |

### `radiateurs.py` — Radiateurs Tempo Rouge

Le job radiateurs (toutes les 50 min, heures pleines) éteint les radiateurs configurés un jour rouge, puis rallume le premier jour non rouge **uniquement ceux qu'il a lui-même éteints** (`data/radiateurs_managed_off.json`). Un arrêt manuel depuis l'appli retire le radiateur de cette liste : il n'est jamais rallumé automatiquement.

Les radiateurs de `RADIATEURS_TEMPO_ROUGE.weekend_restricted_entities` (par défaut le radiateur des combles) ne sont **jamais rallumés automatiquement le week-end** : le rallumage est reporté au lundi. L'interrupteur « Auto le week-end » de leur commande sur le dashboard lève cette restriction (réglage permanent, `data/radiateurs_weekend.json`). Les interrupteurs du panneau Contrôles restent toujours utilisables.

| Fonction | Description |
|---|---|
| `split_turn_back_on(managed, entity_ids, weekend_restricted, now, weekend_allowed)` | Sépare les radiateurs à rallumer maintenant de ceux reportés (week-end). |
| `weekend_auto_allowed()` / `set_weekend_auto_allowed(allowed)` | Lecture / écriture de l'autorisation week-end (refusée par défaut). |

### `history.py` — Historisation

Enregistrement toutes les ~10 min dans une base SQLite (`data/history.db`).

| Fonction | Description |
|---|---|
| `record(outdoor_temp, indoor_temp, poele_state, tempo_color)` | Insère une lecture. Appelé par le scheduler de `app.py`. Retourne `True` si la lecture est un allumage (lecture précédente « off »), compté dans `pellet_daily.ignitions`. |
| `get_history(hours)` | Retourne les enregistrements des N dernières heures (utilisé par `/api/statistics`). |
| `get_daily_summary(days)` | Agrège par journée : minutes allumé/éteint, minutes de clim en chauffe (`None` si non suivie), température moyenne ext/int, couleur Tempo majoritaire. |
| `purge_old(days)` | Supprime les lectures de plus de N jours, en conservant tout le mois précédent (recalculé chaque jour par l'agrégation mensuelle). Lancé chaque lundi à 3h. |
| `get_pellet_consumption_since(date)` | Minutes de poêle par jour depuis une date, lues dans `pellet_daily` (jamais purgée) — base du suivi de stock. |
| `get_ignition_stats(today)` | Allumages du poêle (suivi de l'usure) : aujourd'hui, moyenne par jour et cycle moyen sur les 7 jours précédents, cumul depuis le 1er septembre, détail des 7 derniers jours. Affichés dans la card « Thermostat automatique » (chiffres clés + histogramme 7 jours, seuil d'alerte en pointillé). À partir du 4ᵉ allumage de la journée (`POELE.ignition_alert_daily`), chaque nouvel allumage envoie une alerte ntfy. Lecture toutes les 10 min : un cycle plus court peut passer inaperçu. |
| `poele_state_for_history(real_state, thermostat_state)` | État historisé : état réel HA du poêle, repli sur l'état du thermostat si HA ne répond pas. |
| `clim_state_for_history(real_state, thermostat_state, configured)` | État historisé de la clim (`readings.clim_state`) : `on` seulement en mode HA `heat` (le froid ne compte pas), repli sur le thermostat si HA ne répond pas, `None` si la clim n'est pas configurée. |
| `aggregate_month(year_month, config)` | Agrège un mois dans `monthly_reports` : coût poêle, « clim équiv. » (heures de poêle facturées au prix de la clim), et pour la clim réelle : minutes, coût estimé, « poêle équiv. » (heures de clim au prix du poêle). Colonnes clim à `NULL` pour un mois sans suivi de la clim. |
| `refresh_clim_daily(clim_cfg)` | Recalcule `clim_daily` (minutes et kWh estimés par jour, jamais purgée) pour les jours encore présents dans `readings`. Au démarrage et chaque nuit à 2h30. |
| `season_balance(reports, today)` | Bilan de la saison (1er sept → 31 août) : coût réel poêle + clim, scénarios « tout clim » et « tout poêle », économie par rapport au plus cher des deux. Affiché sur `/reports` et dans la card Thermostat. |

**Suivi de la clim.** Historisé depuis la migration 9 (reprise des 7 derniers jours depuis `diagnose_log`). Coût estimé = heures × `nominal_capacity_kw` ÷ COP de `cop_curve` × tarif Tempo : estimation haute, la clim inverter module sa puissance. Les scénarios « tout clim » / « tout poêle » gardent les mêmes heures de chauffe.

### `overrides.py` — Surcharges de configuration

| Fonction | Description |
|---|---|
| `load(cfg)` | Charge `data/config_override.json` et l'applique sur le module `config` au démarrage. |
| `apply(cfg, data)` | Applique un dict de surcharges sur le module `config`. Supporte tous les blocs (`THERMOSTAT`, `HOME_ASSISTANT`, `EMAIL`, `NTFY`, `LOCATION`, etc.). |

### `pellet_refill.py` — Remplissage du poêle

Compteur d'heures de chauffe depuis le dernier remplissage de la trémie (`data/pellet_refill.json`). Le job d'historique ajoute 10 min à chaque lecture où le poêle est allumé (état réel HA). Durée d'un sac = « heures par sac » de `/config` (15 h par défaut).

| Fonction | Description |
|---|---|
| `tick(poele_on, cycle_hours, email_cfg, ntfy_cfg)` | Ajoute une lecture. Une heure avant la fin du sac (14 h sur 15) : alerte ntfy + email, une seule par plein. Passé la fin du sac : pas de remise à 0, la jauge reste à 100 % (le compteur continue) jusqu'au clic « Trémie remplie ». |
| `reset(by, kg)` | Remise à 0 (bouton « Trémie remplie » de l'accueil, avec la quantité versée : ⅓, ½, ⅔ ou 1 sac). Enregistre le cycle de mesure écoulé. |
| `estimate(refills, bag_kg)` | Débit réel : cumul kg / cumul heures sur les 8 derniers remplissages (3 minimum), ramené en heures par sac. |
| `status(cycle_hours, bag_kg)` | Minutes, pourcentage, seuil d'alerte, mesure en cours et estimation — affichés dans la card « Remplissage du poêle ». |

**Débit réel mesuré.** Un second compteur ne repart de 0 qu'au clic « Trémie remplie ». Chaque clic enregistre un cycle : heures de chauffe depuis le clic précédent + kg versés (⅓, ½, ⅔ ou 1 sac, poids du sac de `/config`). Le premier clic démarre la mesure, un cycle de moins d'1 h (double clic) est ignoré. L'estimation n'est jamais appliquée seule : le bouton « Appliquer » de la card met à jour « heures par sac » (`/config`) et `POELE.consumption_kg_per_hour`, donc le seuil d'alerte et le stock de `/stock` (recalculé sur tout l'historique avec ce débit).

### `stock.py` — Stock de granulés

Calcul pur de la page `/stock` (`compute_stock_stats`), à partir des livraisons (`data/deliveries.json`) et des minutes de chauffe de `pellet_daily`.

| Statistique | Calcul |
|---|---|
| Kg restants | Cumul des livraisons − (minutes de chauffe depuis la 1re livraison × `consumption_kg_per_hour`). |
| % restant / jauge | Kg restants ÷ stock juste après la dernière livraison (reliquat + livraison), pour que la jauge revienne près de 100 % à chaque livraison. |
| Moyenne journalière | Conso des 14 derniers jours complets (aujourd'hui exclu). Si la 1re livraison a moins de 14 jours, sur les jours écoulés depuis. Repli sur la moyenne depuis la 1re livraison si aucune conso récente (hors saison). |
| Jours restants / date d'épuisement | Kg restants ÷ moyenne journalière. Alerte « stock faible » à 15 jours ou moins. |

### `ntfy_push.py` — Notifications push

| Fonction | Description |
|---|---|
| `send(title, message, ntfy_cfg)` | Envoie une notification push via Ntfy. Silencieux en cas d'erreur réseau. Le token est déchiffré automatiquement avant envoi. |

### `imap_client.py` — Client IMAP (lecture seule)

| Fonction | Description |
|---|---|
| `open_connection(cfg)` / `close_connection(conn)` | Ouvre/ferme une connexion IMAP authentifiée en lecture seule. |
| `test_connection(cfg)` | Teste la connexion avec les identifiants stockés. Retourne `(succès, message)`. |
| `is_connected(cfg)` | Retourne `True` si des identifiants IMAP sont configurés. |
| `list_matching_messages(conn, sender, subject, lookback_days)` | Liste `{uid, message_id}` des messages correspondant au filtre (sujet filtré côté client, après décodage MIME). |
| `fetch_message(conn, uid)` | Récupère le message complet (headers + corps MIME) pour un UID. |

### `electricity.py` — Parsing et historisation de la consommation

| Fonction | Description |
|---|---|
| `parse_message(msg)` | Extrait `kwh`, `period_start/end` et `delta_percent/direction` d'un `email.message.Message` "flash élec". |
| `fetch_new_readings(electricity_cfg)` | Job de synchronisation : liste les emails non traités, les parse, les historise (idempotent par `message_id`). |
| `get_readings(weeks)` | Retourne l'historique des lectures sur N semaines. |
| `get_latest_reading()` | Retourne la dernière lecture (carte dashboard). |
| `with_clim_estimate(readings, clim_kwh_by_day)` | Ajoute la part estimée de la clim à chaque semaine (`clim_kwh`, `clim_share` en %), à partir de `clim_daily` ; `None` si un jour de la semaine n'est pas suivi. |

### `linky.py` — Courbe de charge Linky et comparatif des options

| Fonction | Description |
|---|---|
| `sync(linky_cfg)` | Job quotidien : récupère via MyElectricalData les jours manquants (d'abord les récents, puis l'historique en remontant, par tranches de 7 jours), dans la limite de 40 appels par jour. Stocke dans `linky_load_curve`. |
| `compare_options(curve, colors, tempo_prices, options, hp_start, hp_end)` | Pur : coût de la même consommation en Tempo, Base et Heures Creuses (abonnement au prorata des jours), répartition kWh par couleur et HP/HC (avec % HP/HC par couleur, part de chaque couleur et ligne Total), détail mensuel. |
| `test_access(linky_cfg)` | Vérifie la clé et le consentement (`/valid_access`) : date de fin du consentement, appels du jour. |

---

## Thermostat automatique

Le thermostat automatique pilote le poêle selon la température intérieure mesurée par la sonde Shelly via HA. Il s'exécute toutes les 10 minutes via le scheduler.

### Cycle de décision

```
1. HA configuré ?                          → sinon, skip
2. Mode vacances actif ?                   → extinction + notification push + skip
3. Sonde accessible ?                      → sinon, alerte email + extinction sécurité hors plage
4. Synchronisation avec l'état réel HA     → détecte allumage/extinction manuels
5. Mode absent / proximité                 → voir section suivante
6. Suspension active ?                     → si oui, skip jusqu'à expiration
7. Dans la plage horaire ?
   - OUI + poêle OFF + ressenti < temp_on + recommandation "poele" → allumage + notification push
   - OUI + poêle ON  + ressenti ≥ temp_off + allumé ≥ min_on_minutes → extinction + notification push
   - NON + poêle ON  + allumé ≥ grace_minutes → extinction fin de plage + notification push
```

### Paramètres clés

| Paramètre | Rôle |
|---|---|
| `temp_on` | Seuil d'allumage (température ressentie, °C) |
| `temp_off` | Seuil d'extinction (température ressentie, °C) |
| `min_on_minutes` | Durée minimale de fonctionnement avant extinction par température |
| `end_of_schedule_grace_minutes` | Délai avant extinction après la fin de la plage horaire |
| `clim_comfort_hysteresis` | Marge (°C) au-dessus du seuil de confort clim avant de repasser du poêle à la clim |
| `poele_no_start_before_end_minutes` | Pas d'allumage du poêle dans les N dernières minutes de la plage (clim à la place si possible) |
| `manual_off_suspend_hours` | Suspension du thermostat après extinction manuelle |
| `use_felt_temperature` | Utilise la température ressentie (corrigée par l'humidité) |

**Consigne de la clim** : quand l'appli allume la clim (thermostat ou bouton ON du dashboard), elle lui envoie `temp_off` comme consigne. Si `temp_off` est modifié (dashboard ou `/config`) pendant que cette clim chauffe, la nouvelle consigne lui est renvoyée aussitôt. Une clim allumée hors de l'appli (télécommande, appli Sensibo, HA) garde toujours sa propre consigne.

### Température ressentie

`ressenti = temp_réelle + (humidité - humidité_référence) × facteur_correction`

Exemple : 20°C, 65% d'humidité, référence 50%, facteur 0.05 → ressenti = 20 + (65-50) × 0.05 = **20.75°C**

### Gestion des échecs de sonde

Si la sonde est inaccessible ≥ 3 fois consécutives (~30 min), une alerte email est envoyée. Le poêle continue de s'éteindre à la fin des plages horaires (extinction de sécurité) mais ne peut plus être allumé automatiquement par la température.

### Démarrage optimisé

Allume le matin **le plus tard possible** pour atteindre la consigne d'allumage (`temp_on`) à l'**heure de confort**, et **jamais avant le début de plage** : la consommation ne peut pas augmenter, au pire elle reste la même (`modules/warmup.py`).

- **Heure de confort** : colonne « Confort » des plages dans `/config` (vide = début de plage + 45 min ; égale au début = pas de démarrage optimisé ce jour-là, tracé « désactivé » dans le journal ; avant le début : refusée).
- **Mode** (`THERMOSTAT.optimal_start.mode`) : `off`, `simulation` (par défaut : journal seulement, le thermostat allume comme avant) ou `pilot` (le thermostat attend l'heure de démarrage calculée). Marge de sécurité : `safety_margin_minutes` (15 min).
- **Apprentissage** : chaque nuit à 2h30 (et au démarrage), chaque montée en température du matin (départ entre 4h et 11h, un seul système en marche) des lectures encore présentes devient une ligne de `warmup_events`, jamais purgée. Mesures : latence (démarrage → +0,2 °C) et vitesse en °C/h (fin de latence → maximum atteint). Montées écartées : moins de 0,5 °C, moins de 20 min de montée, vitesse hors 0,1–6 °C/h.
- **Modèle** : par système, `durée = latence + écart ÷ vitesse(temp. extérieure)`. Vitesse moyenne pondérée des matins à ±4 °C (noyau gaussien σ = 2 °C), latence médiane. Moins de 3 matins proches : pas de prévision, allumage au début de plage comme avant.
- **Décision** (fenêtre début de plage → heure de confort) : démarrage au plus tard = heure de confort − durée − marge ; le thermostat allume au cycle où attendre le suivant ferait dépasser cette heure. Une erreur du module ne bloque jamais l'allumage.
- **Journal** (`optimal_start_log`, un matin par ligne) : besoin constaté, durée prévue, allumage réel et optimisé, durée réelle jusqu'à la consigne, température à l'heure de confort, chauffe évitée (en simulation : celle qui l'aurait été). Affiché sur `/statistics` (carte « Démarrage optimisé », avec la courbe apprise), sur 30 jours ou depuis le début de la saison (1er septembre). « Avancement de l'apprentissage » : par système, matins appris et premier matin, puis une barre par tranche de 2 °C extérieurs (−8 à 18 °C) avec le nombre de matins, en vert quand la prévision est possible à cette température. « Prévision juste » : matins où la durée réelle de montée (allumage → consigne d'allumage) est à 10 min ou moins de la durée prévue — l'indicateur pour décider du passage en pilotage.

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

### Alerte traqueur figé

Une entité `person` dont le téléphone n'envoie plus de position reste figée sur son dernier état (souvent `home`) : il suffit d'une personne figée à `home` pour que l'absence ne soit jamais détectée. Toutes les 30 min, `modules/presence_watch.py` vérifie le `last_updated` de chaque entité de `person_entities` ; au-delà de `tracker_stale_hours` heures (3 par défaut, 0 = désactivé), une notification ntfy « 📍 Traqueur figé » est envoyée, une seule fois par épisode, puis « ✅ Traqueur actif » quand les positions reviennent. Les alertes en cours sont mémorisées dans `data/tracker_alerts.json`.

Causes habituelles : localisation de l'app Home Assistant non réglée sur « Toujours autoriser », optimisation de batterie Android, app déconnectée du serveur. Un redémarrage de Home Assistant remet `last_updated` à jour pour toutes les entités : un traqueur figé peut alors être signalé « actif » à tort, puis de nouveau figé après le délai.

### Surveillance des sources de température mesurée

Météo-France ne servant qu'en secours d'Infoclimat, une clé refusée sur l'une ou l'autre passerait inaperçue. Toutes les 6 h (et 2 min après le démarrage), `modules/source_watch.py` teste chaque source configurée :
- identifiants refusés (clé Infoclimat liée à une IP publique qui a changé, Application ID ou abonnement Météo-France refusé) : notification ntfy « 🔑 … refuse les identifiants » immédiate, avec la marche à suivre ;
- autre panne : « ⚠️ … ne répond plus » si elle dure 2 tests de suite ;
- une seule alerte par épisode, puis « ✅ … répond de nouveau ».

Rappel « 📅 Abonnement Météo-France à renouveler » 30 j et 7 j avant `OBSERVATIONS.meteofrance_subscription_end`. État mémorisé dans `data/source_alerts.json`.

---

## Mode Absence

Permet de suspendre le thermostat pour une absence de plusieurs jours (vacances, déplacement), indépendamment de la géolocalisation.

### Fonctionnement

- Les dates de départ et de retour sont saisies depuis le dashboard (section **🏖️ Mode Absence**) ou via l'API.
- Tant que la date du jour est dans la plage configurée, le thermostat est **entièrement suspendu** : pas d'allumage, extinction immédiate si le poêle tourne.
- Une notification push est envoyée à l'extinction.
- L'absence peut être annulée à tout moment depuis le dashboard.

### États affichés

| État | Affichage |
|---|---|
| Aucune absence | Formulaire de saisie départ / retour |
| Absence programmée (future) | Badge orange + bouton annuler |
| Absence en cours | Badge bleu + bouton annuler |

### API

| Endpoint | Description |
|---|---|
| `GET /api/thermostat/vacation` | Retourne les dates programmées et l'état actif |
| `POST /api/thermostat/vacation` | Programme une absence `{ "start": "YYYY-MM-DD", "end": "YYYY-MM-DD" }` |
| `DELETE /api/thermostat/vacation` | Annule l'absence programmée |

---

## Notifications push

Les notifications push sont envoyées via **[Ntfy](https://ntfy.sh)**, auto-hébergeable sur NAS Synology.

### Événements notifiés

| Titre | Déclencheur |
|---|---|
| 🔥 Poêle allumé | Allumage automatique par le thermostat |
| ✅ Poêle éteint | Température cible atteinte |
| ⏹ Poêle éteint | Recommandation changée (clim ou aucun chauffage) |
| 🌙 Poêle éteint | Fin de plage horaire |
| 🚗 Poêle éteint | Absence confirmée (géolocalisation) |
| ✈️ Poêle éteint | Mode vacances actif |
| ⚠️ Poêle éteint | Extinction sécurité (sonde hors service) |
| 🪵 Poêle : remplissage à prévoir | 14 h de chauffe depuis le dernier remplissage (aussi envoyé par email) |

### Installation Ntfy sur NAS Synology

```bash
mkdir -p /volume1/docker/ntfy/data /volume1/docker/ntfy/cache
cat > /volume1/docker/ntfy/docker-compose.yml << 'EOF'
services:
  ntfy:
    image: binwiederhier/ntfy
    container_name: ntfy
    command: serve
    environment:
      - TZ=Europe/Paris
    volumes:
      - /volume1/docker/ntfy/data:/etc/ntfy
      - /volume1/docker/ntfy/cache:/var/cache/ntfy
    ports:
      - "8080:80"
    restart: unless-stopped
EOF
cd /volume1/docker/ntfy && sudo docker-compose up -d
```

### Sécurisation (authentification)

```bash
# Activer l'authentification
cat > /volume1/docker/ntfy/data/server.yml << 'EOF'
auth-file: /etc/ntfy/auth.db
auth-default-access: deny-all
EOF
sudo docker-compose restart

# Créer un utilisateur et un token
sudo docker exec -it ntfy ntfy user add --role=admin <utilisateur>
sudo docker exec -it ntfy ntfy token add <utilisateur>
```

### Configuration dans Heating Advisor

Accessible via `/config` → section **🔔 Notifications push** :

| Paramètre | Description |
|---|---|
| URL serveur | Ex. `https://ntfy-jg.mywire.org:8088` |
| Topic | Ex. `heating-advisor` |
| Token | Généré avec `ntfy token add` (chiffré en AES) |

---

## Double authentification (TOTP)

Optionnelle, activée depuis `/config` → **🛡️ Double authentification** : scanner le QR code avec Google Authenticator (ou Aegis, Bitwarden…), confirmer avec un code, puis noter les 8 codes de secours (affichés une seule fois, stockés hachés).

- À la connexion, le mot de passe ouvre seulement la page `/login/code` (5 min, 5 essais/min) ; un code déjà utilisé est refusé.
- « Se souvenir de cet appareil » : cookie signé valable 30 jours, révoqué par un changement de mot de passe, un changement de MFA ou « Déconnecter tous les appareils ».
- Secret TOTP chiffré au repos (`modules/crypto.py`) dans `config_override.json`.
- Téléphone perdu et plus de code de secours :
  ```bash
  sudo docker exec heating-advisor python scripts/disable_mfa.py && sudo docker restart heating-advisor
  ```

---

## Suivi électricité (IMAP)

Historise automatiquement la consommation électrique hebdomadaire à partir des emails "flash élec" envoyés par **[Lite](https://lite.eco)** (`bonjour@lite.eco`).

Sur le tableau de bord, la carte **⚡ Consommation électrique** affiche le dernier relevé hebdomadaire et, à droite, un mini-graphe de l'évolution sur les 12 derniers mois (moyenne en pointillés, clic → `/electricity`).

Le compte Gmail personnel utilisé au quotidien est protégé par la **Protection avancée Google (APP)**, qui bloque tout accès OAuth aux scopes sensibles (dont `gmail.readonly`) pour les apps non vérifiées par Google — y compris ses propres apps OAuth en mode Test, sans possibilité de "continuer quand même". La vérification complète (CASA + domaine + politique de confidentialité) est disproportionnée pour un usage strictement personnel. La solution retenue : un **filtre Gmail transfère les mails Lite vers une boîte tierce** (ex. adresse FAI, non soumise à la Protection avancée), consultée en **IMAP standard** — sans OAuth, sans dépendance externe.

### Sécurité

- Mot de passe IMAP chiffré au repos (AES via `modules/crypto.py`, comme les autres secrets de l'app).
- Connexion en lecture seule (`SELECT ... readonly=True`) — aucun mail n'est jamais marqué lu ni modifié.
- Le filtre expéditeur/sujet est appliqué côté application ; seuls les mails correspondants sont parsés et historisés.

### Configuration côté boîte tierce (setup manuel, une fois)

1. Créer un filtre Gmail sur le compte principal : `from:bonjour@lite.eco subject:"flash élec"` → action **Transférer à** l'adresse de la boîte tierce (à valider via le code de confirmation envoyé par Gmail).
2. Sur la boîte tierce, activer l'accès IMAP (souvent désactivé par défaut — ex. chez Orange : Webmail → Paramètres → Gestion du courrier → accès POP/IMAP).
3. Noter host/port IMAP, l'identifiant (adresse complète) et le mot de passe (ou mot de passe applicatif si la double authentification est activée sur la boîte tierce).

⚠️ Un transfert Gmail réemballe le mail (`Fwd:`, contenu original en blockquote). En pratique le header `X-CampaignID` de Lite (période du relevé) est conservé par le filtre de transfert (vérifié en prod, novembre 2026). S'il venait à manquer, la consommation (kWh) reste extraite mais la période retombe sur la date d'import (`fetched_at`), pas sur celle de réception du mail.

Le relevé d'une semaine arrive le mardi suivant : la « Comparaison à date » de `/electricity` s'arrête donc à la dernière semaine reçue de l'année en cours, pas à la semaine ISO courante.

### Configuration dans Heating Advisor

Accessible via `/config` → section **⚡ Suivi électricité** :

| Paramètre | Description |
|---|---|
| `imap_host` | Serveur IMAP de la boîte tierce (ex. `imap.orange.fr`) |
| `imap_port` | Port IMAP (défaut 993, SSL/TLS) |
| `imap_user` | Adresse email complète de la boîte tierce |
| `imap_password` | Mot de passe (ou mot de passe applicatif), chiffré |
| `sender_filter` | Expéditeur filtré (défaut `bonjour@lite.eco`) |
| `subject_filter` | Sous-chaîne du sujet filtrée (défaut `flash élec`) |
| `check_interval_hours` | Fréquence de vérification des nouveaux emails (défaut 24h) |
| `lookback_days` | Fenêtre de recherche IMAP à chaque synchronisation (défaut 2200 jours) |

Une fois les identifiants renseignés et enregistrés, cliquer sur **🔌 Tester la connexion** pour vérifier.

### Fonctionnement

Le job planifié (`electricity_job`, toutes les `check_interval_hours`) recherche les emails correspondant au filtre, extrait la consommation (kWh) et la période (via le header `X-CampaignID: WEEKLY_YYYY-MM-DD_YYYY-MM-DD`, si préservé), et historise chaque email une seule fois (dédup par `Message-ID` — le polling peut donc être plus fréquent que la fréquence d'envoi réelle sans créer de doublons). Une page dédiée **`/electricity`** affiche l'historique (graphique + indicateurs).

### Compteur Linky et comparatif des options

Les mails « flash élec » ne donnent qu'un total hebdomadaire. Pour savoir si Tempo est vraiment l'option la moins
chère, l'appli récupère la **courbe de charge** du compteur Linky (consommation toutes les 30 min) via la passerelle
[MyElectricalData](https://www.myelectricaldata.fr), puis facture chaque demi-heure selon les trois options du Tarif Bleu.

**Mise en place (une fois)** : créer un compte MyElectricalData et donner le consentement Enedis, vérifier que la
collecte de la courbe de charge est activée sur le compte Enedis, puis renseigner dans `/config` → **Compteur Linky**
le numéro PDL (14 chiffres ; espaces, tirets et points tolérés) et la clé d'accès (chiffrée au repos). **Tester l'accès** vérifie les valeurs saisies, même pas encore enregistrées, et affiche la date de fin du consentement.

**Récupération** (`linky_job`, chaque jour à 10h30, et dans la minute qui suit un enregistrement de `/config`) :
- Enedis limite la courbe de charge à 7 jours par appel et 24 mois d'historique ; MyElectricalData à 50 appels/jour
  sans son cache (non utilisé : les données ne font que transiter). L'appli s'arrête à 40 appels par jour et attend
  1 s entre deux appels.
- Premier lancement : les 7 derniers jours, puis l'historique en remontant jusqu'à `LINKY.backfill_days` (365 j),
  soit environ 2 jours de récupération. Une réponse vide ou une erreur définitive marque la fin de l'historique
  disponible (collecte activée récemment) ; une panne passagère (réseau, quota, clé) est retentée le lendemain.
- État dans `data/linky_state.json` (dernier passage, dernière erreur, appels du jour, historique terminé).

**Calcul** (`/api/electricity/tariff-comparison`, carte « Tempo ou une autre option ? » en haut de `/electricity`) :
- Carte : verdict, coût total des trois options, **courbe des économies de Tempo mois par mois** (écart avec
  Heures Creuses et avec Base ; au-dessus de 0 = Tempo moins cher), répartition kWh par couleur et HP/HC, détail mensuel.
- 12 derniers mois disponibles ; couleurs Tempo passées lues sur `api-couleur-tempo.fr` (`joursTempo?periode=…`),
  gardées dans `data/tempo_history.json`.
- Le jour Tempo va de 6 h à 6 h : une heure creuse avant 6 h prend la couleur de la veille.
- Tarifs et abonnements de `TARIFF_OPTIONS` (config.py, grille EDF du 1er août 2026, 12 kVA) et `TEMPO_PRICES`,
  appliqués à toute la période. Heures creuses de l'option HP/HC supposées identiques à celles de Tempo (22h–6h).
  L'option Base n'est plus ouverte à la souscription au-delà de 6 kVA : comparaison indicative.

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
| `GET /api/warmup?range=30d\|season` | Démarrage optimisé : réglages, avancement de l'apprentissage, courbes apprises, montées mesurées, journal des matins et bilan (30 jours ou saison) |
| `GET /api/reports/season` | Bilan de la saison : coûts poêle + clim, économies vs « tout clim » / « tout poêle », début du suivi de la clim |
| `GET /api/weather/conditions` | État du ciel (Open-Meteo, cache 15 min) pour l'illustration de la carte météo |
| `GET /api/thermostat/state` | État courant du thermostat |
| `GET /api/thermostat/diagnose` | Diagnostic complet (temp, sonde, présence, scheduler) |
| `POST /api/thermostat/toggle` | Active/désactive le thermostat |
| `POST /api/thermostat/resume` | Annule la suspension du thermostat |
| `POST /api/thermostat/check-now` | Force une évaluation immédiate du thermostat |
| `POST /api/thermostat/thresholds` | Modifie les seuils `{ "temp_on": 19.5, "temp_off": 21 }` (widget « Température de confort » du dashboard) |
| `GET /api/thermostat/vacation` | Dates d'absence programmées |
| `POST /api/thermostat/vacation` | Programme une absence |
| `DELETE /api/thermostat/vacation` | Annule l'absence programmée |
| `POST /api/ui/background-theme` | Thème du fond animé `{ "background_theme": "sky" }`, enregistré au clic dans `/config` |
| `POST /api/poele/refill-reset` | Remplissage `{ "bags": 0.5 }` (1 par défaut) : remet à 0 le compteur et enregistre le cycle de mesure |
| `POST /api/poele/refill-apply` | Applique la durée par sac mesurée (heures par sac + débit du poêle) |
| `POST /api/config` | Sauvegarde la configuration depuis l'interface |
| `POST /api/notify/test` | Déclenche manuellement l'email de recommandation |
| `GET /electricity` | Page dédiée suivi électricité |
| `GET /api/electricity/history?weeks=N` | Historique des lectures des N dernières semaines, avec la part estimée de la clim |
| `GET /api/electricity/status` | Statut de connexion IMAP + dernière lecture |
| `POST /api/electricity/sync-now` | Déclenche manuellement la synchronisation IMAP |
| `POST /api/electricity/imap/test` | Teste la connexion IMAP avec les identifiants enregistrés |

---

## Icônes

Icônes au trait [Lucide](https://lucide.dev) (licence ISC) regroupées dans le sprite `static/icons.svg`. Dans les templates : `{{ icon('flame') }}` (fonction globale Jinja définie dans `app.py`) ; en JavaScript : `icon('flame')` (défini dans `base.html`), qui renvoie le même `<svg class="ic ic-flame">`. Les icônes prennent la couleur du texte ; flamme / granulés / radiateur en orangé, flocon en bleu, éclair et alerte en jaune (`style.css`, bloc « Icônes au trait »). Pour en ajouter une : copier le contenu du SVG de `lucide-static` dans un nouveau `<symbol id="nom">`. Les emails et notifications push gardent leurs emojis.

## Configuration

Tous les paramètres sont modifiables via l'interface `/config` sans rebuild Docker. Les valeurs sont sauvegardées dans `data/config_override.json` et surchargent `config.py` au démarrage.

### Blocs principaux

**`LOCATION`** — Localisation et météo
| Clé | Description |
|---|---|
| `city`, `postal_code` | Ville et code postal |
| `latitude`, `longitude` | Coordonnées GPS |
| `nas_ip`, `nas_port` | Adresse du NAS pour les liens dans les emails |
| `public_url` | URL publique si exposé sur Internet |

**`UI`** — Apparence
| Clé | Description |
|---|---|
| `background_theme` | Fond animé, ordinateur uniquement (souris + écran ≥ 1024 px, jamais sur mobile ni avec « réduire les animations », en pause quand l'onglet est masqué). Choix par vignettes dans `/config` → Apparence, appliqué et enregistré dès le clic (`POST /api/ui/background-theme`). `aurora` (défaut) : halos qui dérivent aux couleurs de la situation. `sky` : teinte d'aube / jour / crépuscule / nuit et soleil ou lune sur leur course, d'après le lever et le coucher calculés pour `LOCATION`. `weather` : pluie, orage, neige, brume, nuages ou rayons de soleil selon `/api/weather/conditions`. `thermal` : champ de particules bleu (trop frais), vert (confort) ou orange (trop chaud) selon le ressenti et les seuils du thermostat, plus intense quand l'écart grandit. `fireside` : lueur qui vacille et braises en continu. `auto` : coin du feu le soir poêle allumé, météo s'il pleut / neige / brume, sinon ciel. `none` : fond fixe. Sur tous les thèmes : braises quand le poêle tourne, halo rouge qui respire en Tempo rouge, étoile filante la nuit. **Jour rouge, prioritaire** : un jour Tempo rouge, le thème « Jour rouge » remplace le thème choisi (sauf `none`) : voiles cramoisis, étincelles, lueur qui respire et gyrophare pendant les heures pleines (`HP_START`–`HP_END`). La couleur du jour vient de l'accueil (carte Tempo puis rafraîchissement AJAX), mémorisée jusqu'au prochain 6 h pour les autres pages. Ancien réglage `background_animation: false` → `none`. Moteur : `static/background.js`. |

**`OBSERVATIONS`** — Stations de mesure de la température extérieure
| Clé | Description |
|---|---|
| `meteofrance_application_id` | « Application ID » du portail API Météo-France (chiffré) |
| `meteofrance_stations` | Identifiants de stations à 8 chiffres, par ordre de préférence |
| `meteofrance_subscription_end` | Fin de l'abonnement à l'API (AAAA-MM-JJ) : rappel ntfy 30 j et 7 j avant |
| `infoclimat_token` | Clé d'API open data Infoclimat (chiffrée) |
| `infoclimat_stations` | Identifiants de stations Infoclimat (ex. `ME113`, `00089`) |
| `max_age_minutes` | Âge maximal d'une mesure acceptée (défaut 90) |

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
| `tracker_stale_hours` | Alerte ntfy si une entité `person` n'est plus mise à jour depuis N heures (0 = désactivé) |
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
| `clim_entity_id` | Entité climate de la clim (Sensibo) |

**`EMAIL`**
| Clé | Description |
|---|---|
| `enabled` | Active les notifications email |
| `smtp_host` / `smtp_port` | Serveur SMTP |
| `sender` / `recipients` | Expéditeur et destinataires |
| `app_password` | Mot de passe applicatif (chiffré en AES) |
| `notify_hour` / `notify_minute` | Heure d'envoi automatique de la recommandation J+1 |

**`NTFY`** — Notifications push
| Clé | Description |
|---|---|
| `enabled` | Active les notifications push |
| `url` | URL du serveur Ntfy (ex. `https://ntfy-jg.mywire.org:8088`) |
| `topic` | Topic Ntfy (ex. `heating-advisor`) |
| `token` | Token d'accès Ntfy (chiffré en AES) |

**`ELECTRICITY`** — Suivi électricité (IMAP)
| Clé | Description |
|---|---|
| `enabled` | Active le suivi de consommation électrique |
| `imap_host` | Serveur IMAP de la boîte tierce |
| `imap_port` | Port IMAP (défaut 993) |
| `imap_user` | Adresse email complète de la boîte tierce |
| `imap_password` | Mot de passe IMAP (chiffré en AES) |
| `sender_filter` | Expéditeur des emails filtrés |
| `subject_filter` | Sous-chaîne du sujet filtrée |
| `check_interval_hours` | Fréquence de vérification des nouveaux emails |
| `lookback_days` | Fenêtre de recherche IMAP à chaque synchronisation |

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

> ⚠️ Toujours lancer la mise à jour depuis `/volume1/docker/heating-advisor` pour que le bind mount `./data` pointe vers le bon répertoire.

### En local (développement)

```bash
pip install -r requirements.txt
python app.py
# → http://localhost:5000
```

Le répertoire `data/` est monté en volume Docker — la configuration, l'historique et l'état du thermostat sont **persistés entre les redémarrages**.

### Sauvegardes

Chaque nuit à 3h30, `modules/backup.py` copie le contenu de `data/` dans `data/backups/AAAA-MM-JJ/` : les bases SQLite via `sqlite3.Connection.backup()` (copie cohérente à chaud), les fichiers JSON et les clés (`secret.key`, `.secret_key`) tels quels. Dossiers en `0700`, fichiers en `0600`. Les sauvegardes de 7 jours et plus sont supprimées ; le résultat est journalisé (`Sauvegarde data/ → …`).

Pour restaurer : arrêter le conteneur, recopier les fichiers voulus de `data/backups/AAAA-MM-JJ/` vers `data/` (supprimer d'éventuels `history.db-wal` / `history.db-shm`), relancer.

> ⚠️ Ces copies sont sur le même disque que l'original : elles protègent d'une corruption ou d'une erreur de manipulation, **pas d'une panne du NAS**. Inclure `/volume1/docker/heating-advisor/data` dans une tâche **Hyper Backup** (vers un disque USB ou un stockage distant).
