# Apprentissage Automatique du COP

## Vue d'ensemble

Le système d'apprentissage automatique du COP surveille en continu l'état de la climatisation via Home Assistant et lance automatiquement des cycles d'échantillonnage lorsque la clim démarre en mode chauffage.

**Problème résolu** : Éviter d'avoir à cliquer manuellement sur "Clim ON" à chaque fois que vous voulez enregistrer un point COP.

## Comment ça fonctionne

### 1. Surveillance en arrière-plan

Un worker (thread daemon) surveille l'état de la clim toutes les 45 secondes (configurable) :
- Détecte la transition `OFF` → `heat`
- Vérifie les pré-conditions (voir ci-dessous)
- Attend 5 minutes de stabilisation
- Lance un échantillonnage de 2 minutes

### 2. Validation statistique multi-niveaux

Pour éviter de fausser la courbe COP avec d'autres appareils (plaque de cuisson, four, voiture électrique), le système applique **4 validations** :

#### ✅ **Pré-checks** (avant de démarrer)
- Ballon eau chaude < 50W (pas en chauffe)
- Capteurs Shelly disponibles
- Température extérieure disponible
- Intervalle min respecté (2h par défaut)
- Pas dans les heures blackout (11h30-14h, 18h30-21h)
- Dans le planning chauffage (si configuré)

#### ✅ **Pendant l'échantillonnage**
- Variance des échantillons < 200W
- Pas de saut > 500W entre deux mesures

#### ✅ **Post-validation**
- COP calculé à ±50% du COP théorique
- COP calculé à ±30% de l'historique (si existe)
- Puissance clim dans les limites (500-4500W)

#### ❌ **Rejet automatique**
Si une validation échoue :
- L'échantillonnage est rejeté
- Le tag erroné est supprimé
- La raison est loggée dans l'historique

## Configuration

### Activer l'auto-learning

Dans `config.py`, section `COP_LEARNING` :

```python
"auto_learning": {
    "enabled": True,  # ⬅️ Passer à True pour activer

    # Surveillance
    "polling_interval_seconds": 45,
    "cooldown_after_startup_minutes": 5,
    "min_interval_between_samples_hours": 2,

    # Sécurité
    "heater_power_threshold": 50,
    "only_during_thermostat_schedule": True,
    "blackout_hours": [
        {"start": "11:30", "end": "14:00"},  # déjeuner
        {"start": "18:30", "end": "21:00"},  # dîner
    ],

    # Validation statistique
    "validation": {
        "max_variance_watts": 200,
        "max_power_jump_watts": 500,
        "max_deviation_from_theoretical": 0.5,  # 50%
        "max_deviation_from_history": 0.3,      # 30%
    }
}
```

### Ajuster les paramètres

**Pour réduire les faux positifs** (rejets excessifs) :
- Augmenter `max_variance_watts` (ex: 300)
- Augmenter `max_power_jump_watts` (ex: 700)
- Augmenter `max_deviation_from_theoretical` (ex: 0.7 = 70%)

**Pour renforcer la validation** (plus strict) :
- Diminuer les seuils ci-dessus
- Ajouter des plages dans `blackout_hours`
- Augmenter `min_interval_between_samples_hours`

## Interface web

### Page `/cop-learning`

Nouvelle section **🤖 Apprentissage Automatique** :

#### Indicateurs
- 🟢 **Actif** : Worker en surveillance
- 🟡 **Échantillonnage** : Mesure en cours
- 🔴 **Désactivé** : Worker arrêté

#### Statistiques
- **Détections** : Nombre de fois où la clim a démarré
- **Démarrés** : Échantillonnages lancés
- **✅ Validés** : Mesures acceptées
- **❌ Rejetés** : Mesures rejetées

#### Actions
- **Bouton ON/OFF** : Activer/désactiver le worker
- **📋 Historique rejets** : Voir les 20 derniers rejets avec raisons

## Exemples de rejets

### ❌ Variance trop élevée (345W)
**Cause probable** : Plaque de cuisson ou four allumé pendant l'échantillonnage
**Solution** : Ajuster les horaires `blackout_hours`

### ❌ Saut de puissance détecté (1200W)
**Cause probable** : Appareil démarré pendant l'échantillonnage
**Solution** : Normal, le système a bien détecté et rejeté

### ❌ COP=4.2 trop éloigné du théorique (2.8, écart 50%)
**Cause probable** : Consommation base mal calibrée OU mesure réellement anormale
**Solution** : Vérifier le profil de base sur la page COP Learning

### ❌ Ballon en chauffe (1250W > 50W)
**Cause probable** : Le ballon d'eau chaude chauffe en même temps que la clim
**Solution** : Normal, le système a bien détecté et rejeté

### ❌ Période blackout (18:30-21:00)
**Cause probable** : La clim a démarré pendant les heures de cuisine
**Solution** : Normal, évite les faux positifs dus aux plaques/four

## API

### `GET /api/cop/auto-learning/status`
Retourne le statut du worker :
```json
{
  "enabled": true,
  "running": true,
  "last_check": "2024-01-15T14:30:00",
  "last_clim_state": "heat",
  "current_sampling_task_id": null,
  "stats": {
    "total_detections": 12,
    "total_samples_started": 8,
    "total_samples_successful": 6,
    "total_samples_rejected": 2
  }
}
```

### `POST /api/cop/auto-learning/toggle`
Active/désactive le worker :
```json
{"enable": true}
```

### `GET /api/cop/auto-learning/history?limit=20`
Retourne l'historique des rejets :
```json
{
  "history": [
    {
      "timestamp": "2024-01-15T14:25:00",
      "outdoor_temp": 5.2,
      "reason_code": "validation_failed",
      "reason_message": "Variance trop élevée (345W² > 200W²)"
    }
  ]
}
```

## Logs

Les événements importants sont loggés dans la console :

```
🤖 Worker auto-learning COP démarré
🔥 Transition détectée : off → heat
⏳ Échantillonnage programmé dans 5 min (stabilisation clim)
✅ Échantillonnage démarré : task_id=abc123
✅ Échantillonnage abc123 validé : COP=2.63 à 5.1°C
📝 Rejet enregistré : validation_failed - Variance trop élevée (345W² > 200W²)
```

## Dépannage

### Le worker ne démarre pas

**Vérifier** :
1. `COP_LEARNING.enabled = True`
2. `COP_LEARNING.auto_learning.enabled = True`
3. `HOME_ASSISTANT.clim_entity_id` configuré
4. Home Assistant accessible

**Voir les logs** au démarrage de l'app.

### Trop de rejets

**Solutions** :
1. Assouplir les seuils de validation dans `config.py`
2. Ajouter plus de plages dans `blackout_hours`
3. Augmenter `min_interval_between_samples_hours` pour éviter mesures rapprochées
4. Vérifier que le profil de base est bien calibré

### Aucune détection

**Vérifier** :
1. La clim est bien configurée dans Home Assistant
2. L'état de la clim change bien de `off` à `heat`
3. Le worker est bien actif (voir page `/cop-learning`)
4. Les logs de l'application

## Bonnes pratiques

### Phase d'apprentissage initial (1-2 semaines)

1. **Activer l'auto-learning** avec paramètres par défaut
2. **Surveiller les rejets** quotidiennement
3. **Ajuster les seuils** si trop de rejets
4. **Vérifier la courbe** après 10-15 mesures validées

### Utilisation quotidienne

1. **Laisser tourner** en arrière-plan
2. **Consulter l'historique** une fois par semaine
3. **Comparer** courbe théorique vs apprise
4. **Basculer** sur la courbe apprise quand confiance > 60%

### Cas particuliers

- **Jour Tempo Rouge** : Le worker reste actif mais ne devrait pas détecter (clim éteinte)
- **Vacances** : Vous pouvez désactiver temporairement l'auto-learning
- **Travaux électriques** : Désactiver pendant les travaux pour éviter faux positifs

## Évolutions futures possibles

- [ ] Détection d'autres appareils via capteurs HA dédiés
- [ ] Apprentissage par Machine Learning des patterns de consommation
- [ ] Notification push lors de rejets répétés
- [ ] Export des données pour analyse externe
- [ ] Intégration webhook Home Assistant (au lieu de polling)
