# Roborock Custom Cloud pour Home Assistant

Intégration Home Assistant custom pour les aspirateurs Roborock, avec un support
avancé du **Q10 S5+** (protocole B01, `roborock.vacuum.ss07`) qui va au-delà de
l'intégration native de Home Assistant 2026.7 :

- 🗺️ **Carte des pièces** (entités `image` et `camera`), poussée par le robot,
  remise à l'endroit pour correspondre à l'appli Roborock
- 🧹 **Nettoyage par pièces interactif** : carte Lovelace cliquable
  (xiaomi-vacuum-map-card) — cliquez une pièce → elle se nettoie. Aussi via
  `CLEAN_AREA` (UI vacuum) et le service `vacuum.roborock_clean_rooms`
- 💧 Niveau d'eau et mode serpillière
- 📊 Capteurs : progression du nettoyage, surface et durée de la session,
  totaux (surface, durée, nombre de sessions), batterie, état, **erreur décodée**
- 🤖 **Position/trajet du robot** sur la carte — via calibration manuelle
  (voir plus bas ; désactivé par défaut)
- 🔧 Service `roborock_b01_send` pour envoyer n'importe quelle commande B01 (DPS)

Basée sur [python-roborock](https://github.com/Python-roborock/python-roborock)
**5.27.0**. ⚠️ L'intégration native HA 2026.7 embarque python-roborock 5.22.0 ;
gardez-la **désactivée** tant que cette extension pinne une version différente
(sinon conflit de dépendances). Les deux ne doivent de toute façon pas interroger
le cloud Roborock en parallèle (rate-limit).

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
vacuum et de l'entité `camera` (poussés par le robot avec sa carte).

## Carte interactive (clic pour nettoyer une pièce)

L'entité `camera.<robot>_map_camera` fournit la carte des pièces et expose, dans
son attribut `rooms`, la position en pixels de chaque pièce. On l'utilise avec la
carte Lovelace [xiaomi-vacuum-map-card](https://github.com/PiotrMachowski/lovelace-xiaomi-vacuum-map-card)
(installable via HACS).

Comme la sélection se fait par **pièce** (pas par coordonnées), on utilise la
calibration `identity` (coordonnées image) — pas besoin de la transformation
trace→carte. Exemple de carte (adaptez les `id` et `outline`/`label` aux valeurs
de l'attribut `rooms` de VOTRE camera) :

```yaml
type: custom:xiaomi-vacuum-map-card
entity: vacuum.roborock_q10_s5_vacuum
map_source:
  camera: camera.roborock_q10_s5_map_camera
calibration_source:
  identity: true
map_modes:
  - name: Pièces
    selection_type: PREDEFINED
    max_selections: 4
    repeats_type: NONE
    service_call_schema:
      service: vacuum.roborock_clean_rooms
      service_data:
        entity_id: vacuum.roborock_q10_s5_vacuum
        room_ids: "[[selection]]"
    predefined_selections:
      - id: 1
        outline: [[8, 8], [503, 8], [503, 515], [8, 515]]
        label: { text: "Pièce1", x: 255, y: 261 }
      - id: 2
        outline: [[408, 264], [607, 264], [607, 503], [408, 503]]
        label: { text: "Pièce2", x: 507, y: 383 }
      - id: 3
        outline: [[528, 124], [751, 124], [751, 499], [528, 499]]
        label: { text: "Pièce3", x: 639, y: 311 }
```

> Astuce : ouvrez les outils de développement → États → `camera.<robot>_map_camera`
> et copiez l'attribut `rooms` ; chaque entrée donne `id`, `name`, le centre
> (`x`,`y`) et le cadre (`x1`,`y1`,`x2`,`y2`) à reporter dans `outline`/`label`.

## Position/trajet du robot sur la carte (calibration manuelle)

La transformation coordonnées-robot → pixels-carte **n'est pas dérivable** pour le
Q10 (le paquet carte ne contient aucune origine ; le projet de référence
`roborock-qseries-map-bridge` utilise lui aussi une calibration manuelle). Par
défaut, **aucun trajet/robot n'est dessiné** (on évite un overlay faux).

Pour l'activer, fournissez une calibration dans les options de l'intégration
(Paramètres → Appareils et services → Roborock Custom Cloud → Configurer →
« Calibration carte ») au format JSON :

```json
{ "unit": 12.5, "off_x": 187, "off_y": 124, "sign_x": -1, "sign_y": -1 }
```

Pour la déterminer (établi sur le Q10 testé) : orientation `sign_x=-1, sign_y=-1`,
`unit` ≈ 12 (unités de trace par cellule), `off_x/off_y` propres à votre carte.
Repérez `robot_raw_x`/`robot_raw_y` (attributs de la camera) quand le robot est à
une position connue, et ajustez `off_x/off_y` pour aligner le point sur la carte.

## Notes techniques

- Le Q10 est **entièrement push-driven** : pas de requête get-map synchrone,
  un `REQUEST_DPS` incite le robot à publier carte et état.
- Le rendu de la lib est **retourné verticalement** par rapport à la réalité ;
  l'extension le remet à l'endroit (voir `map_render.py`).
- La position/trajet ne sont émis que **pendant une session de nettoyage**.

## Licence / statut

Projet personnel, non affilié à Roborock. Testé uniquement sur un Q10 S5+
(ss07/B01) et Home Assistant 2026.7 / python-roborock 5.27.0.
