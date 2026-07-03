# Roborock Custom Cloud pour Home Assistant

Intégration Home Assistant custom pour les aspirateurs Roborock, avec un support
avancé du **Q10 S5+** (protocole B01, `roborock.vacuum.ss07`) qui va au-delà de
l'intégration native de Home Assistant 2026.7 :

- 🗺️ **Carte rendue en direct** (entité `image`), poussée par le robot
- 🤖 **Position du robot et trajet de nettoyage** dessinés sur la carte pendant
  une session
- 🧹 **Nettoyage par pièces** (`CLEAN_AREA` dans l'interface vacuum de HA,
  service `vacuum.roborock_clean_rooms`)
- 💧 Niveau d'eau et mode serpillière
- 📊 Capteurs : progression du nettoyage, surface et durée de la session,
  totaux (surface, durée, nombre de sessions), batterie, état
- 🔧 Service `roborock_b01_send` pour envoyer n'importe quelle commande B01 (DPS)

Basée sur [python-roborock](https://github.com/Python-roborock/python-roborock)
5.22.0 — la même version que l'intégration native HA 2026.7, pour éviter tout
conflit de dépendances.

## Installation (HACS)

1. HACS → menu ⋮ → **Dépôts personnalisés**
2. URL : `https://github.com/pierrec18/ha_roborock_custom` — Catégorie : **Intégration**
3. Installer « Roborock Custom Cloud », puis redémarrer Home Assistant
4. Paramètres → Appareils et services → **Ajouter une intégration** → « Roborock Custom Cloud »

### Authentification

Au premier écran, saisissez l'email de votre compte Roborock :

- Si l'intégration **native** Roborock est déjà configurée avec ce compte, le
  token est réutilisé automatiquement — laissez le mot de passe vide.
- Sinon, saisissez le mot de passe (2FA par code email géré).

> ⚠️ Évitez de faire tourner l'intégration native et celle-ci en parallèle sur
> le même compte : le cloud Roborock limite les requêtes (« maximum requests
> for home data »). Désactivez l'intégration native après l'ajout.

## Services

| Service | Description |
|---|---|
| `vacuum.roborock_clean_rooms` | Nettoyer des pièces (`room_ids`, `repeat` — repeat ignoré en B01) |
| `vacuum.roborock_set_water_level` | Niveau d'eau de la serpillière |
| `vacuum.roborock_set_clean_mode` | Mode de nettoyage (aspiration/serpillière) |
| `vacuum.roborock_b01_send` | Commande B01 brute (`command`, `params`) |
| `vacuum.roborock_b01_request_dps` | Forcer un rafraîchissement complet des DPS |

Exemple :

```yaml
service: vacuum.roborock_clean_rooms
target:
  entity_id: vacuum.mon_robot_vacuum
data:
  room_ids: [2, 3]
```

Les identifiants de pièces sont visibles dans l'attribut `rooms` de l'entité
vacuum (poussés par le robot avec sa carte).

## Notes techniques

- Le Q10 est **entièrement push-driven** : pas de requête get-map synchrone,
  un `REQUEST_DPS` incite le robot à publier carte et état.
- La position live n'est émise que **pendant une session de nettoyage**.
- Le tracé du trajet utilise une hypothèse de calibration (coordonnées de
  trace = cellules de grille) protégée par un garde-fou : si les points ne
  correspondent pas à la grille, la carte est affichée sans overlay et les
  bornes sont loguées en debug pour calibration.

## Licence / statut

Projet personnel, non affilié à Roborock. Testé uniquement sur un Q10 S5+
(ss07/B01) et Home Assistant 2026.7.
