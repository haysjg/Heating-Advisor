# Apprentissage COP Réel - Documentation

## Vue d'ensemble

Le système d'apprentissage du COP réel permet de mesurer les performances réelles de votre climatisation en conditions réelles d'utilisation, plutôt que de se fier uniquement aux valeurs théoriques du fabricant.

## Comment ça fonctionne ?

### Principe

1. **Tagging manuel** : Vous appuyez sur "Clim ON" quand vous allumez la climatisation, et "Clim OFF" quand vous l'éteignez
2. **Déduction de la consommation** : Le système calcule la consommation de la clim par différence :
   ```
   Consommation clim = Total maison - Ballon eau chaude - Consommation de base
   ```
3. **Calcul du COP** : À partir de la consommation électrique mesurée et de la puissance thermique nominale, le COP réel est calculé
4. **Construction de la courbe** : Progressivement, une courbe COP en fonction de la température extérieure est construite

### Prérequis

- **Shelly EM** configuré avec :
  - Channel 1 : Consommation totale de la maison
  - Channel 2 : Consommation du ballon d'eau chaude
- **Home Assistant** configuré et fonctionnel
- **Patience** : 20-30 tags répartis sur différentes températures nécessaires pour une courbe fiable

## Configuration

### 1. Activer la fonctionnalité

Dans `config.py` ou via l'interface de configuration :

```python
COP_LEARNING = {
    "enabled": True,  # Activer la fonctionnalité
    "shelly_total_power_entity_id": "sensor.conso_globale_channel_1_power",
    "shelly_heater_power_entity_id": "sensor.conso_globale_channel_2_power",
    "nominal_thermal_kw": 4.0,  # Puissance thermique nominale de votre clim
    "min_samples_per_bin": 3,   # Minimum d'échantillons par tranche de température
    "confidence_threshold": 0.6,  # Seuil de confiance pour auto-switch
    "auto_switch_to_learned": False,  # Utiliser automatiquement la courbe apprise
    "temp_bin_size": 5,  # Taille des tranches de température (°C)
    "min_ac_power": 500,  # Puissance minimum acceptable (W)
    "max_ac_power": 3000,  # Puissance maximum acceptable (W)
}
```

### 2. Vérifier les entités Home Assistant

Assurez-vous que les entités Shelly sont correctement configurées et renvoient des valeurs en Watts.

## Utilisation

### Interface Web

Accédez à l'interface via le lien **🎓 COP Learning** dans la navigation.

### Workflow de tagging

1. **Préparez-vous** :
   - Assurez-vous que la maison est en consommation "normale" (pas de four, lave-linge, etc.)
   - Vérifiez que le ballon d'eau chaude n'est pas en chauffe

2. **Allumage clim** :
   - Allumez votre climatisation
   - **Attendez 2-3 minutes** que la consommation se stabilise
   - Cliquez sur "✓ Clim ON"
   - Ajoutez des notes si besoin (ex: "mode chauffage 22°C")

3. **Extinction clim** :
   - Éteignez votre climatisation
   - **Attendez 1-2 minutes**
   - Cliquez sur "✗ Clim OFF"

### Profil de consommation de base

Le système apprend automatiquement la consommation de base de votre maison pour chaque heure de la journée (0-23h) à partir des tags OFF.

**Calibration manuelle** : Si les valeurs semblent incorrectes, vous pouvez calibrer manuellement :
- Pour une heure spécifique : entrez les Watts et l'heure (0-23)
- Pour toutes les heures : entrez les Watts uniquement (laissez l'heure vide)

## Interprétation des résultats

### Score de confiance

Le score de confiance (0-100%) indique la fiabilité des données collectées :

- **< 30%** : Données insuffisantes, courbe théorique utilisée
- **30-60%** : Données en cours de collecte, blend progressif théorique/appris
- **> 60%** : Données fiables, possibilité d'utiliser la courbe apprise

Le score est basé sur :
- Nombre de mesures valides (max à 50 mesures = 100%)
- Couverture des températures (7 bins de -10°C à +20°C)
- Cohérence des mesures (faible écart-type)
- Durée de collecte (max à 30 jours)

### Graphiques

1. **Courbes COP** : Compare votre courbe théorique (bleu) avec la courbe apprise (vert)
   - Écart moyen affiché sous le graphique
   - Permet de voir si votre clim performe mieux ou moins bien que prévu

2. **Profil de base** : Consommation horaire de base
   - Vert : heures avec données collectées
   - Gris : heures avec valeur par défaut (200W)

### Table des tags récents

- **Tags ON valides** : COP calculé affiché
- **Tags ON invalides** : Problème détecté (puissance hors limites, négative, etc.)
- **Tags OFF** : Mise à jour du profil de base

## Bonnes pratiques

### À faire ✅

- Tagguer systématiquement chaque allumage/extinction
- Varier les conditions (températures extérieures différentes)
- Attendre la stabilisation de la consommation avant de tagguer ON
- Tagguer à différentes heures de la journée
- Vérifier que les capteurs Shelly fonctionnent correctement

### À éviter ❌

- Tagguer pendant l'utilisation d'appareils énergivores (four, lave-linge, sèche-linge)
- Tagguer immédiatement après allumage/extinction (attendre stabilisation)
- Tagguer si le ballon d'eau chaude est en chauffe
- Négliger des températures extérieures (besoin de données sur toute la plage)

## Stratégie d'utilisation de la courbe apprise

Le système propose 3 modes :

1. **Mode théorique pur** (confiance < 30%) :
   - Courbe théorique uniquement
   - Sécurité maximale

2. **Mode blend pondéré** (30% ≤ confiance < seuil) :
   - Mélange progressif théorique + appris
   - Transition douce et sécurisée
   - Facteur de blend = (confiance - 0.3) / 0.7

3. **Mode appris pur** (confiance ≥ seuil ET auto_switch activé) :
   - Courbe apprise uniquement
   - Performance optimale

## Maintenance

### Purge automatique

- Les tags et mesures > 90 jours sont automatiquement supprimés (lundi 3h20)
- Le profil de base et la courbe apprise sont conservés

### Effacement manuel

Deux options disponibles :
1. **Effacer données (garder calibration)** : Supprime tags/mesures/courbe, conserve le profil de base
2. **Tout effacer** : Remet tout à zéro

## Dépannage

### "Capteurs Shelly indisponibles"

- Vérifiez que Home Assistant est accessible
- Vérifiez que les entités Shelly sont correctes dans la config
- Vérifiez que les entités renvoient des valeurs numériques

### "Puissance clim négative"

- La consommation de base est probablement surestimée
- Calibrez manuellement le profil de base à une valeur plus faible
- Vérifiez qu'aucun appareil énergivore ne s'est éteint entre le dernier tag OFF et ce tag ON

### "Puissance clim trop faible/élevée"

- Vérifiez que la clim est réellement allumée/éteinte
- Ajustez `min_ac_power` / `max_ac_power` dans la config si nécessaire
- Vérifiez qu'aucun autre appareil ne perturbe la mesure

### COP aberrant

- Vérifiez la puissance thermique nominale dans la config
- Attendez plus longtemps la stabilisation avant de tagguer
- Supprimez les mesures suspectes en effaçant les données et recommencez

## Support

Pour toute question ou problème :
- Consultez les logs de l'application
- Vérifiez la page de diagnostic (`/api/thermostat/diagnose`)
- Créez une issue sur le dépôt GitHub
