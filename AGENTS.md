# AGENTS.md - Reprise projet Roborock / Home Assistant

Derniere mise a jour: 2026-07-03

## Objectif
Ce repo contient:
- des scripts de debug Roborock (`test-roborock.py`, `roborock-simple.py`, `roborock-web.py`)
- une integration Home Assistant custom:
  - `custom_components/roborock_custom`
  - cible: Home Assistant `2026.7.x`
  - dependance: `python-roborock==5.22.0` (meme version que l'integration native HA 2026.7,
    pour eviter tout conflit de dependances)

## Etat actuel
L'integration HA fonctionne avec:
- login cloud + reauth + 2FA email
- mecanisme reauth aligne sur integration native:
  - `mqtt_session_unauthorized_hook` => `entry.async_start_reauth(hass)`
  - session HTTP partagee Home Assistant (`async_get_clientsession`)
- commandes de base: start / pause / stop / dock
- aspiration (fan speed)
- commandes mop (niveau eau + mode)
- capteurs diagnostiques (battery/state/protocol)
- support CLEAN_AREA Home Assistant (segments/pieces) pour appareils V1 **et B01/Q10**
- entite image "Map" (carte PNG rendue, push-driven) pour B01/Q10
- position live + trajet dessines sur la carte (overlay PIL, voir ci-dessous)
- capteurs statistiques: progression (%), surface/duree session, totaux (B01)

Deploiement: via HACS (depot custom `pierrec18/ha_roborock_custom`, doit etre public);
fallback possible en rsync SSH vers `/homeassistant/custom_components/roborock_custom/`
(add-on "Advanced SSH & Web Terminal", host `homeassistant.local`).

Overlay position/trajet (image.py `_compose_map`) — GEOMETRIE ETABLIE EN LIVE:
- Coordonnees de trace 02 01 en CENTIMETRES; cellules de grille = 5 cm.
- Orientation validee (capture reelle, hypothese "A" choisie par l'utilisateur):
  `gx = y/5 + offset_x`, `gy = -x/5 + offset_y` (offsets en espace image, le
  rendu lib faisant deja un FLIP_TOP_BOTTOM).
- offset d'origine propre a chaque carte -> balaye (max fraction de points sur
  le sol), cache par (grid_w, grid_h). Sur la carte de test: offset ~ (113, 29)
  pour grille 186x133.
- Garde-fou: pas d'overlay tant que <15 points, ou si aucun offset n'atteint
  60% de points sur le sol (log DEBUG `Calibration trace->grille echouee`).
- Dimensions de grille = `map_data.image.dimensions.width/height` (PAS a la
  racine de ImageData).
- Outil de calibration: `map_debug.py` (connexion cloud locale via le token de
  la config entry HA; option `--start-clean` pour capturer un trajet reel).
  Sorties dans `map_debug_out/` (capture.json, candidates, rendu final).
- Le robot n'emet des paquets trace que PENDANT un nettoyage; carte + pieces
  sont poussees des le reveil/refresh.

Version courante: 0.4.0, python-roborock 5.23.1.

Points importants:
- Ton robot detecte est `Roborock Q10 S5+` (`model=roborock.vacuum.ss07`, `pv=B01`).
- Depuis `python-roborock` 5.20 (5.22 ici), le Q10 a un support carte + pieces:
  - `device.b01_q10_properties.map` (`MapContentTrait`): `image_content` (PNG),
    `rooms` (id + nom), `path`, `robot_position`. Purement push-driven:
    `REQUEST_DPS` pousse le robot a publier sa carte (pas de get-map synchrone).
  - `device.b01_q10_properties.vacuum.clean_segments([ids])`: nettoyage par pieces,
    verifie live sur hardware ss07 par les mainteneurs de la lib.
  - Le parametre `repeat` n'est pas supporte par `dpStartClean` en B01 (ignore).
- L'integration NATIVE de HA 2026.7 embarque python-roborock 5.22.0 mais ne cable
  PAS encore la carte ni CLEAN_AREA pour le Q10 (verifie dans le code 2026.7.0:
  `image.py` natif sans B01, `RoborockQ10Vacuum` sans CLEAN_AREA). D'ou l'interet
  de cette extension custom.

## Arborescence importante
- `custom_components/roborock_custom/manifest.json`
- `custom_components/roborock_custom/config_flow.py`
- `custom_components/roborock_custom/__init__.py`
- `custom_components/roborock_custom/api.py`
- `custom_components/roborock_custom/vacuum.py`
- `custom_components/roborock_custom/sensor.py`
- `custom_components/roborock_custom/services.yaml`

## Setup dev local
Python conseille: `venv311` (Python 3.11)

Commandes utiles:
```bash
source venv311/bin/activate
python -m py_compile custom_components/roborock_custom/*.py
```

## Installation dans Home Assistant
1. Copier `custom_components/roborock_custom` dans `<HA_CONFIG>/custom_components/`.
2. Redemarrer Home Assistant.
3. Ajouter integration: `Roborock Custom Cloud`.
4. Login email/mot de passe; si 2FA, saisir le code email.

## Services exposes (entite vacuum)
Services custom:
- `vacuum.roborock_clean_rooms`
- `vacuum.roborock_set_water_level`
- `vacuum.roborock_set_clean_mode`

Exemples:
```yaml
service: vacuum.roborock_set_water_level
target:
  entity_id: vacuum.mon_robot_vacuum
data:
  level: middle
```

```yaml
service: vacuum.roborock_set_clean_mode
target:
  entity_id: vacuum.mon_robot_vacuum
data:
  mode: onlymop
```

```yaml
service: vacuum.roborock_clean_rooms
target:
  entity_id: vacuum.mon_robot_vacuum
data:
  room_ids: [16, 18]
  repeat: 1
```

## Support CLEAN_AREA (HA 2026.7)
L'entite vacuum implemente:
- `async_get_segments`
- `async_clean_segments`

Le flag `VacuumEntityFeature.CLEAN_AREA` est active:
- pour les protocoles V1 (toujours)
- pour B01/Q10 des que la carte a fourni des pieces (`snapshot.status["rooms"]` non vide)

Cote B01/Q10, les segments viennent de `map.rooms` (pousse par le robot). Si la liste
est vide, `async_get_segments` envoie un `REQUEST_DPS` et attend jusqu'a ~5s l'arrivee
du paquet carte.

## Entite carte (image.py, B01/Q10)
- Plateforme `image` ajoutee a `PLATFORMS` (const.py).
- `RoborockMapImageEntity` expose le PNG rendu par `MapContentTrait` de la lib.
- Mise a jour par listener push (`map.add_update_listener`), pas par polling.

## 2FA et erreurs frequentes
- Erreur `already_in_progress`: un flow HA est deja ouvert.
  - reprendre le flow courant au lieu de relancer "Ajouter integration".
- Erreur code invalide:
  - flow actuel aligne natif: envoi + validation v4 (`request_code_v4` / `code_login_v4`).
- Erreur `too many codes`:
  - rate-limit serveur Roborock, attendre avant nouvelle demande.

## Strategie pour reprise future (priorites)
1. Valider sur le vrai robot: entite image "Map", liste des pieces, `roborock_clean_rooms`
   et CLEAN_AREA (UI) sur le Q10.
2. Evaluer python-roborock 5.23.x (5.23.0 ajoute l'historique clean-record Q10);
   attention: rester aligne avec la version embarquee par HA natif si possible.
3. Exposer path/robot_position sur la carte (via `map.path` / `map.robot_position`,
   deja parses par la lib).
4. Ajouter tests (fixtures de status V1/B01 + parsing segments).
5. Ajouter diagnostics HA plus detaillees (etat brut DPS B01 optionnel).

## Notes de debug
Scripts existants:
- `test-roborock.py`: debug CLI cloud/device + mode `b01-map-debug`
- `roborock-web.py`: UI locale de test

Pour investigations map B01:
- observer `MULTI_MAP(61)` et payloads associes
- verifier si nouvelle version de `python-roborock` expose une carte/image ou segmentation B01
