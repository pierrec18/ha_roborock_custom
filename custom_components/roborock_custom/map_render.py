"""Rendu partagé de la carte Q10 (B01) — module pur, sans import Home Assistant.

Le rendu de python-roborock (`MapContentTrait.image_content`) est un PNG des
pièces, mais retourné verticalement par rapport à la réalité / à l'appli Roborock
(la lib applique un FLIP_TOP_BOTTOM). On ré-inverse donc l'image ici pour que la
carte ait la même orientation que l'appli.

Ce module fournit :
- `flipped_map_png()` : le PNG des pièces, remis à l'endroit ;
- `room_regions()` : pour chaque pièce, sa couleur, son cadre et son centre en
  pixels de l'image retournée (pour configurer la carte Lovelace) ;
- `render_with_overlay()` : optionnellement, superpose position/trajet du robot
  SI une calibration est fournie (sinon renvoie la carte nue).

La transformation trace→carte du Q10 n'est pas dérivable (aucune origine dans le
paquet carte) : `overlay` reste désactivé tant qu'une calibration manuelle n'a pas
été fournie via les options de l'intégration. Voir AGENTS.md.
"""

from __future__ import annotations

import colorsys
import io
from dataclasses import dataclass

from PIL import Image, ImageDraw

# Couleurs du renderer de la lib (b01_q10_map_parser._build_palette)
_OUTSIDE_COLOR = (28, 30, 38)
_WALL_COLOR = (235, 235, 240)
_WALL_THRESHOLD = 240

_PATH_COLOR = (255, 255, 255, 210)
_ROBOT_FILL = (66, 165, 245, 255)
_ROBOT_OUTLINE = (255, 255, 255, 255)


@dataclass(frozen=True)
class TraceCalibration:
    """Calibration manuelle trace→carte (sur l'image retournée).

    gx = sign_x * x/unit + off_x ; gy = sign_y * y/unit + off_y   (en cellules)
    Établi en live pour le Q10 testé : sign_x=-1, sign_y=-1, unit≈12 ; off_* propre
    à chaque carte. Aucune valeur par défaut n'est appliquée : sans calibration
    explicite, aucun overlay n'est dessiné.
    """

    unit: float
    off_x: float
    off_y: float
    sign_x: int = -1
    sign_y: int = -1


def calibration_from_options(options: dict | None) -> TraceCalibration | None:
    """Construit une TraceCalibration depuis entry.options, ou None si absente/invalide."""
    if not options:
        return None
    raw = options.get("map_calibration")
    if not isinstance(raw, dict):
        return None
    try:
        unit = float(raw["unit"])
        off_x = float(raw["off_x"])
        off_y = float(raw["off_y"])
    except (KeyError, TypeError, ValueError):
        return None
    if unit == 0:
        return None
    sign_x = -1 if int(raw.get("sign_x", -1)) < 0 else 1
    sign_y = -1 if int(raw.get("sign_y", -1)) < 0 else 1
    return TraceCalibration(unit=unit, off_x=off_x, off_y=off_y, sign_x=sign_x, sign_y=sign_y)


def _room_color(index: int) -> tuple[int, int, int]:
    """Couleur d'une pièce selon son rang de pixel_value (comme _build_palette)."""
    hue = (index * 0.139) % 1.0
    r, g, b = colorsys.hsv_to_rgb(hue, 0.5, 0.95)
    return (int(r * 255), int(g * 255), int(b * 255))


def _load_flipped(base_png: bytes) -> Image.Image:
    return Image.open(io.BytesIO(base_png)).convert("RGB").transpose(Image.FLIP_TOP_BOTTOM)


def flipped_map_png(base_png: bytes) -> bytes:
    """Renvoie le PNG des pièces remis à l'endroit (miroir vertical)."""
    buffer = io.BytesIO()
    _load_flipped(base_png).save(buffer, format="PNG")
    return buffer.getvalue()


def room_regions(base_png: bytes, rooms: list[dict]) -> list[dict]:
    """Cadre + centre en pixels (image retournée) de chaque pièce.

    `rooms` : liste de dicts {id, name, pixel_value}. Renvoie une liste
    {id, name, x, y (centre px), x1,y1,x2,y2 (cadre px)} pour les pièces trouvées.
    """
    img = _load_flipped(base_png)
    width, height = img.size
    px = img.load()

    values = sorted({int(r["pixel_value"]) for r in rooms if r.get("pixel_value")})
    color_for_value = {v: _room_color(i) for i, v in enumerate(values)}

    # Regrouper les pixels par couleur de pièce (un seul passage).
    from collections import defaultdict

    buckets: dict[tuple[int, int, int], list[int]] = defaultdict(list)
    color_set = {c: v for v, c in color_for_value.items()}
    for y in range(height):
        for x in range(width):
            c = px[x, y]
            if c in color_set:
                buckets[c].append(x)
                buckets[c].append(y)

    regions: list[dict] = []
    for room in rooms:
        value = int(room.get("pixel_value") or 0)
        color = color_for_value.get(value)
        if color is None:
            continue
        coords = buckets.get(color)
        if not coords:
            continue
        xs = coords[0::2]
        ys = coords[1::2]
        x1, x2 = min(xs), max(xs)
        y1, y2 = min(ys), max(ys)
        regions.append(
            {
                "id": room.get("id"),
                "name": room.get("name") or f"Room {room.get('id')}",
                "x": (x1 + x2) // 2,
                "y": (y1 + y2) // 2,
                "x1": x1,
                "y1": y1,
                "x2": x2,
                "y2": y2,
            }
        )
    return regions


def render_with_overlay(
    base_png: bytes,
    grid_w: int,
    grid_h: int,
    path: list[tuple[int, int]] | None = None,
    robot_position: tuple[int, int] | None = None,
    calibration: TraceCalibration | None = None,
) -> bytes:
    """Carte retournée, avec trajet + robot SI une calibration est fournie.

    Sans calibration (défaut), renvoie la carte nue : on ne dessine jamais un
    trajet non calibré (évite l'overlay faux de la v0.4.0).
    """
    if calibration is None or not path:
        return flipped_map_png(base_png)

    img = _load_flipped(base_png).convert("RGBA")
    scale_x = img.width / grid_w
    scale_y = img.height / grid_h
    cal = calibration

    def to_pixel(pt: tuple[int, int]) -> tuple[float, float]:
        gx = cal.sign_x * pt[0] / cal.unit + cal.off_x
        gy = cal.sign_y * pt[1] / cal.unit + cal.off_y
        return ((gx + 0.5) * scale_x, (gy + 0.5) * scale_y)

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    pts = [to_pixel(p) for p in path]
    if len(pts) >= 2:
        draw.line(pts, fill=_PATH_COLOR, width=max(2, int(scale_x)))
    if robot_position is not None:
        cx, cy = to_pixel(robot_position)
        r = max(4.0, scale_x * 1.6)
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=_ROBOT_FILL, outline=_ROBOT_OUTLINE, width=max(1, int(r / 4)))

    composed = Image.alpha_composite(img, overlay)
    buffer = io.BytesIO()
    composed.convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()
