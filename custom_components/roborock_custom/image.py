"""Map image platform for Roborock custom integration (B01/Q10)."""

from __future__ import annotations

import io
import logging
from typing import Any

from PIL import Image, ImageDraw

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import RoborockConfigEntry, RoborockRuntimeData
from .api import DeviceSnapshot, MapSnapshot
from .const import DOMAIN
from .coordinator import RoborockDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# Geometrie des traces B01/Q10, etablie par capture live (voir map_debug.py):
# les coordonnees de trace sont en centimetres, les cellules de la grille font
# 5 cm, et l'orientation est (gx = y/5 + offset_x, gy = -x/5 + offset_y) — le
# rendu de la lib etant deja retourne verticalement, ces offsets sont en espace
# image. L'origine (0,0) des traces est propre a chaque carte, donc l'offset est
# calcule par balayage (score = fraction de points tombant sur du sol) et mis en
# cache tant que les dimensions de la grille ne changent pas.
_TRACE_UNIT_CM = 5.0
_MIN_FLOOR_SCORE = 0.6
# Trop peu de points -> l'offset est sous-contraint (beaucoup de positions
# donnent un score parfait); attendre d'avoir un trajet significatif avant de
# figer une calibration.
_MIN_CALIB_POINTS = 15

# Couleurs du rendu de B01Q10MapParser (voir _build_palette dans la lib).
_OUTSIDE_COLOR = (28, 30, 38)
_WALL_COLOR = (235, 235, 240)

_PATH_COLOR = (255, 255, 255, 200)
_ROBOT_FILL = (66, 165, 245, 255)
_ROBOT_OUTLINE = (255, 255, 255, 255)


async def async_setup_entry(hass: HomeAssistant, entry: RoborockConfigEntry, async_add_entities) -> None:
    """Set up Roborock map image entities."""
    runtime_data = entry.runtime_data
    entities = [
        RoborockMapImageEntity(hass, runtime_data, duid)
        for duid, snapshot in runtime_data.coordinator.data.items()
        if snapshot.protocol == "b01_q10"
    ]
    async_add_entities(entities)


def _base_grid_coords(path: list[tuple[int, int]]) -> list[tuple[float, float]]:
    """Trace (cm) -> coordonnees grille sans offset (orientation Q10 validee)."""
    return [(y / _TRACE_UNIT_CM, -x / _TRACE_UNIT_CM) for x, y in path]


def _densify(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Interpole le long du trajet pour que le score detecte les traversees de mur."""
    if len(points) < 2:
        return list(points)
    dense: list[tuple[float, float]] = []
    for (ax, ay), (bx, by) in zip(points, points[1:]):
        steps = max(2, int(max(abs(bx - ax), abs(by - ay)) * 2))
        for i in range(steps + 1):
            f = i / steps
            dense.append((ax + (bx - ax) * f, ay + (by - ay) * f))
    return dense


def _build_floor_lookup(img: Image.Image, grid_w: int, grid_h: int):
    """Retourne une fonction (gx, gy) -> bool disant si la cellule est du sol."""
    rgb = img.convert("RGB")
    scale_x = rgb.width / grid_w
    scale_y = rgb.height / grid_h
    px = rgb.load()

    def is_floor(gx: int, gy: int) -> bool:
        if not (0 <= gx < grid_w and 0 <= gy < grid_h):
            return False
        color = px[int((gx + 0.5) * scale_x), int((gy + 0.5) * scale_y)]
        return color != _OUTSIDE_COLOR and color != _WALL_COLOR

    return is_floor


def _find_offset(base: list[tuple[float, float]], grid_w: int, grid_h: int, is_floor) -> tuple[int, int, float]:
    """Balaye l'offset (borne au bounding box du trajet) maximisant le score sol."""
    dense = _densify(base)
    xs = [b[0] for b in base]
    ys = [b[1] for b in base]
    dx_lo, dx_hi = int(-min(xs)), int(grid_w - 1 - max(xs))
    dy_lo, dy_hi = int(-min(ys)), int(grid_h - 1 - max(ys))

    best = (0, 0, 0.0)
    for dx in range(dx_lo, dx_hi + 1):
        for dy in range(dy_lo, dy_hi + 1):
            hits = sum(1 for bx, by in dense if is_floor(round(bx + dx), round(by + dy)))
            score = hits / len(dense)
            if score > best[2]:
                best = (dx, dy, score)
    return best


def _compose_map(snapshot: MapSnapshot, calib_cache: dict) -> bytes | None:
    """Draw the live path and robot position onto the rendered map PNG.

    Runs in the executor (PIL is CPU-bound). The per-map offset is searched once
    and cached in ``calib_cache`` (keyed by grid dimensions); the overlay is
    skipped when the trace does not fit the floor (guard against a bad map).
    """
    if snapshot.image is None:
        return None
    if not snapshot.path or not snapshot.grid_width or not snapshot.grid_height:
        return snapshot.image

    grid_w = snapshot.grid_width
    grid_h = snapshot.grid_height
    img = Image.open(io.BytesIO(snapshot.image)).convert("RGBA")
    scale_x = img.width / grid_w
    scale_y = img.height / grid_h

    base = _base_grid_coords(snapshot.path)
    is_floor = _build_floor_lookup(img, grid_w, grid_h)

    cache_key = (grid_w, grid_h)
    cached = calib_cache.get(cache_key)
    offset: tuple[int, int] | None = None
    if cached is not None:
        # Reutiliser l'offset connu s'il place encore le trajet courant sur le sol.
        dx, dy = cached
        dense = _densify(base)
        hits = sum(1 for bx, by in dense if is_floor(round(bx + dx), round(by + dy)))
        if hits / len(dense) >= _MIN_FLOOR_SCORE:
            offset = cached

    if offset is None:
        if len(base) < _MIN_CALIB_POINTS:
            # Pas encore assez de trajet pour calibrer de maniere fiable.
            return snapshot.image
        dx, dy, score = _find_offset(base, grid_w, grid_h, is_floor)
        if score < _MIN_FLOOR_SCORE:
            xs = [p[0] for p in snapshot.path]
            ys = [p[1] for p in snapshot.path]
            _LOGGER.debug(
                "Calibration trace->grille echouee (meilleur score %.2f): "
                "x=[%s..%s] y=[%s..%s] vs grille %sx%s",
                score, min(xs), max(xs), min(ys), max(ys), grid_w, grid_h,
            )
            return snapshot.image
        offset = (dx, dy)
        calib_cache.clear()
        calib_cache[cache_key] = offset
        _LOGGER.debug("Calibration trace->grille offset=%s (score %.2f)", offset, score)

    dx, dy = offset
    pixels = [((bx + dx + 0.5) * scale_x, (by + dy + 0.5) * scale_y) for bx, by in base]

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    if len(pixels) >= 2:
        draw.line(pixels, fill=_PATH_COLOR, width=max(2, int(scale_x)))

    if snapshot.robot_position is not None and pixels:
        # La position du robot est le dernier point du trajet.
        cx, cy = pixels[-1]
        radius = max(4.0, scale_x * 1.6)
        draw.ellipse(
            (cx - radius, cy - radius, cx + radius, cy + radius),
            fill=_ROBOT_FILL,
            outline=_ROBOT_OUTLINE,
            width=max(1, int(radius / 4)),
        )

    composed = Image.alpha_composite(img, overlay)
    buffer = io.BytesIO()
    composed.convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()


class RoborockMapImageEntity(CoordinatorEntity[RoborockDataUpdateCoordinator], ImageEntity):
    """Rendered map (PNG) pushed by a B01/Q10 device, with live path overlay."""

    _attr_has_entity_name = True
    _attr_name = "Map"
    _attr_content_type = "image/png"

    def __init__(self, hass: HomeAssistant, runtime_data: RoborockRuntimeData, duid: str) -> None:
        CoordinatorEntity.__init__(self, runtime_data.coordinator)
        ImageEntity.__init__(self, hass)
        self._runtime_data = runtime_data
        self._duid = duid
        self._attr_unique_id = f"{duid}_map"
        self._last_map: MapSnapshot | None = None
        # offset de calibration trace->grille, mis en cache par dimensions de carte
        self._calib_cache: dict = {}

    @property
    def _snapshot(self) -> DeviceSnapshot | None:
        return self.coordinator.data.get(self._duid)

    @property
    def available(self) -> bool:
        return super().available and self._snapshot is not None

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._duid)}, manufacturer="Roborock")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        last_map = self._last_map
        if last_map is None:
            return {}
        attrs: dict[str, Any] = {"path_points": len(last_map.path)}
        if last_map.robot_position is not None:
            attrs["robot_position_x"] = last_map.robot_position[0]
            attrs["robot_position_y"] = last_map.robot_position[1]
        return attrs

    async def async_added_to_hass(self) -> None:
        """Subscribe to pushed map updates from the library's map trait."""
        await super().async_added_to_hass()
        unsub = await self._runtime_data.api.async_add_map_listener(self._duid, self._on_map_update)
        self.async_on_remove(unsub)
        # Le robot pousse la carte de maniere asynchrone; si une image est deja
        # en cache dans le trait, l'exposer immediatement.
        if await self._runtime_data.api.async_get_map_image(self._duid) is not None:
            self._attr_image_last_updated = dt_util.utcnow()

    def _on_map_update(self) -> None:
        self._attr_image_last_updated = dt_util.utcnow()
        self.async_write_ha_state()

    async def async_image(self) -> bytes | None:
        try:
            snapshot = await self._runtime_data.api.async_get_map_snapshot(self._duid)
            if snapshot is None:
                return None
            self._last_map = snapshot
            return await self.hass.async_add_executor_job(
                _compose_map, snapshot, self._calib_cache
            )
        except Exception as err:  # noqa: BLE001 - image fetch must not raise
            _LOGGER.debug("Lecture de la carte impossible pour %s: %s", self._duid, err)
            return None
