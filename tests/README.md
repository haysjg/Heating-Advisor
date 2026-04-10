# Tests unitaires — Heating Advisor

Ce dossier contient les tests unitaires pour la logique métier du projet Heating Advisor.

## Structure

```
tests/
├── README.md                # Ce fichier
├── __init__.py
├── conftest.py              # Fixtures partagées (configs de test)
├── test_advisor.py          # Tests du moteur de décision (34 tests)
└── test_thermostat.py       # Tests du thermostat automatique (25 tests)
```

## Périmètre couvert

### ✅ Modules testés

- **`modules/advisor.py`** — Moteur de décision coût clim vs poêle
  - `interpolate_cop()` : interpolation COP sur courbe de température
  - `compute_clim_cost()` : calcul coût horaire climatisation
  - `compute_poele_cost()` : calcul coût horaire poêle
  - `make_recommendation()` : arbre de décision (jour rouge, confort, coûts)
  - `get_effective_cop_curve()` : blend courbe théorique/apprise
  - `analyze()` : analyse complète aujourd'hui
  - `analyze_tomorrow()` : analyse demain

- **`modules/thermostat.py`** — Thermostat automatique
  - `felt_temperature()` : température ressentie (correction humidité)
  - `is_in_schedule()` : vérification plage horaire
  - `is_on_vacation()` : mode vacances
  - `check_and_apply()` : logique de pilotage du poêle (allumage/extinction)

### ❌ Modules non testés (hors périmètre)

- `modules/weather.py`, `modules/tempo.py`, `modules/homeassistant.py` — Dépendent d'APIs externes
- `modules/cop_learning.py`, `modules/cop_sampling.py` — Nécessitent une base SQLite
- `app.py` — Routes Flask (tests d'intégration à venir)

## Lancer les tests

### Installation des dépendances de test

```bash
pip install -r requirements-dev.txt
```

### Exécuter tous les tests

```bash
pytest tests/ -v
```

### Exécuter un fichier de test spécifique

```bash
pytest tests/test_advisor.py -v
pytest tests/test_thermostat.py -v
```

### Exécuter un test spécifique

```bash
pytest tests/test_advisor.py::TestInterpolateCop::test_interpolation_between_points -v
```

### Options utiles

```bash
# Afficher les prints
pytest tests/ -v -s

# Arrêter au premier échec
pytest tests/ -x

# Afficher le résumé détaillé
pytest tests/ -v --tb=short

# Exécuter seulement les tests qui ont échoué
pytest tests/ --lf
```

## Fixtures disponibles

Définies dans `conftest.py` :

- **`base_config`** — Configuration complète Heating Advisor (CLIM, POELE, TEMPO_PRICES, etc.)
- **`base_thermostat_cfg`** — Configuration thermostat (schedule, seuils, etc.)
- **`base_ha_cfg`** — Configuration Home Assistant minimale

## Outils de test

- **pytest** — Framework de test
- **freezegun** — Figer le temps pour tester la logique horaire
- **unittest.mock** — Mocker les dépendances externes (HA, ntfy, fichiers d'état)

## Couverture de code (futur)

Pour mesurer la couverture :

```bash
pip install pytest-cov
pytest tests/ --cov=modules --cov-report=html
```

Ouvrir `htmlcov/index.html` pour voir le rapport.

## Règles métier testées

### Advisor

- **Jour ROUGE** → toujours le poêle (override absolu)
- **Confort insuffisant** (temp < 7°C) → poêle recommandé
- **Température ≥ cible** → pas de chauffage
- **Heures Creuses + NO_HEATING_AT_NIGHT** → pas de chauffage
- **Comparaison coûts** → le moins cher gagne (seuil 0.01 €/h pour "marginal")
- **COP plancher** → jamais < 1.0
- **Courbe COP apprise** → blend selon niveau de confiance

### Thermostat

- **Mode vacances** → éteint le poêle
- **Sonde en panne** → extinction de sécurité hors plage horaire
- **Allumage** → dans horaire + temp < seuil + recommandation="poele"
- **Extinction** → temp atteinte OU fin de plage OU reco changée
- **min_on_minutes** → durée minimale avant extinction
- **Détection manuelle** → synchro état + suspension N heures si extinction manuelle
- **Température ressentie** → correction selon humidité (±0.05°C par % d'écart)

## Maintenance

- Ajouter un test lorsqu'un bug est corrigé (régression)
- Maintenir les tests à jour lors des évolutions de la logique métier
- Garder les tests unitaires rapides (< 1s pour toute la suite)
