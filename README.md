# Conseiller Chauffage 🏠

Compare en temps réel le coût de la **climatisation réversible** vs le **poêle à granulés**
en fonction de la température extérieure et des tarifs EDF Tempo.

## Stack

- **Python 3.12** + Flask
- **Météo** : scraping météociel.fr → fallback Open-Meteo
- **Tarifs** : API Tempo (api-couleur-tempo.fr)
- **Déploiement** : Docker / Docker Compose (NAS Synology)

## Démarrage rapide

### Sur le NAS Synology (Container Manager)

1. Télécharger et extraire le projet :
   ```bash
   cd /volume1/docker
   wget https://github.com/haysjg/Heating-Advisor/archive/refs/heads/main.tar.gz -O ha.tar.gz
   tar xzf ha.tar.gz && mv Heating-Advisor-main heating-advisor && rm ha.tar.gz
   ```
2. Lancer le conteneur :
   ```bash
   cd heating-advisor
   sudo docker-compose up -d --build
   ```
3. Accédez à `http://IP_NAS:8888`

### Mettre à jour le code sur le NAS

```bash
cd /volume1/docker/heating-advisor
wget https://github.com/haysjg/Heating-Advisor/archive/refs/heads/main.tar.gz -O ha.tar.gz
tar xzf ha.tar.gz --strip-components=1 && rm ha.tar.gz
sudo docker-compose up -d --build   # ⚠️ --build obligatoire pour recharger le code
```

### En local (développement)

```bash
pip install -r requirements.txt
python app.py
# → http://localhost:5000
```

## Configuration (`config.py`)

| Paramètre | Description |
|-----------|-------------|
| `LOCATION` | Ville et coordonnées GPS |
| `TEMPO_PRICES` | Tarifs EDF Tempo (mis à jour chaque saison) |
| `CLIM.cop_curve` | Courbe COP selon température extérieure |
| `POELE.pellet_price_per_kg` | Prix de vos granulés (€/kg) |
| `POELE.consumption_kg_per_hour` | Consommation horaire de votre poêle |
| `REFRESH_INTERVAL_MINUTES` | Fréquence d'actualisation (défaut : 30 min) |

## API

- `GET /` — Dashboard web
- `GET /api/data` — Données JSON complètes
- `GET /api/refresh` — Force le rechargement des données

## Personnalisation du COP (Mitsubishi MSZ-FA35VA)

Le COP par défaut est basé sur les données constructeur estimées.
Pour affiner, renseignez vos propres mesures dans `config.py` → `CLIM.cop_curve`.
