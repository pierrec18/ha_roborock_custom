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

# Les coordonnees de trace B01 ne sont pas documentees (int16 signes, origine
# inconnue — observe: valeurs negatives, donc pas le coin de la grille). On
# teste plusieurs transformations candidates et on garde celle dont les points
# tombent majoritairement sur du sol (pixels de piece, ni exterieur ni mur).
_MIN_FLOOR_SCORE = 0.5

# Couleurs du rendu de B01Q10MapParser (voir _build_palette dans la lib).
_OUTSIDE_COLOR = (28, 30, 38)
_WALL_COLOR = (235, 235, 240)

_PATH_COLOR = (255, 255, 255, 170)
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


def _pixel_transforms(grid_w: int, grid_h: int, scale_x: float, scale_y: float):
    """Candidate trace→pixel transforms: origine {coin, centre} × y {inverse, direct}.

    Le rendu de la lib retourne l'image verticalement (FLIP_TOP_BOTTOM), d'ou
    les variantes en y. Retourne {nom: fonction (x, y) -> (px, py)}.
    """
    half_w = grid_w / 2
    half_h = grid_h / 2

    def make(offset_x: float, offset_y: float, flip_y: bool):
        def to_pixel(x: float, y: float) -> tuple[float, float]:
            gx = x + offset_x
            gy = y + offset_y
            py = (grid_h - 1 - gy + 0.5) if flip_y else (gy + 0.5)
            return ((gx + 0.5) * scale_x, py * scale_y)

        return to_pixel

    return {
        "corner_yflip": make(0, 0, True),
        "corner_ydirect": make(0, 0, False),
        "center_yflip": make(half_w, half_h, True),
        "center_ydirect": make(half_w, half_h, False),
    }


def _floor_score(img: Image.Image, pixels: list[tuple[float, float]]) -> float:
    """Fraction of points landing on floor pixels (inside a room)."""
    if not pixels:
        return 0.0
    rgb = img.convert("RGB")
    hits = 0
    for px, py in pixels:
        if not (0 <= px < rgb.width and 0 <= py < rgb.height):
            continue
        color = rgb.getpixel((int(px), int(py)))
        if color != _OUTSIDE_COLOR and color != _WALL_COLOR:
            hits += 1
    return hits / len(pixels)


def _compose_map(snapshot: MapSnapshot) -> bytes | None:
    """Draw the live path and robot position onto the rendered map PNG.

    Runs in the executor (PIL is CPU-bound). The trace→grid transform is
    auto-calibrated: candidate transforms are scored by how many path points
    land on floor pixels, and the overlay is skipped when none fits.
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

    best_name: str | None = None
    best_pixels: list[tuple[float, float]] = []
    best_score = 0.0
    for name, to_pixel in _pixel_transforms(grid_w, grid_h, scale_x, scale_y).items():
        pixels = [to_pixel(x, y) for x, y in snapshot.path]
        score = _floor_score(img, pixels)
        if score > best_score:
            best_name, best_pixels, best_score = name, pixels, score

    if best_name is None or best_score < _MIN_FLOOR_SCORE:
        xs = [p[0] for p in snapshot.path]
        ys = [p[1] for p in snapshot.path]
        _LOGGER.debug(
            "Aucune transformation trace->grille fiable (meilleur score %.2f): "
            "x=[%s..%s] y=[%s..%s] vs grille %sx%s",
            best_score, min(xs), max(xs), min(ys), max(ys), grid_w, grid_h,
        )
        return snapshot.image

    _LOGGER.debug(
        "Transformation trace->grille '%s' (score sol %.2f, %s points)",
        best_name, best_score, len(snapshot.path),
    )

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    if len(best_pixels) >= 2:
        draw.line(best_pixels, fill=_PATH_COLOR, width=max(2, int(scale_x)))

    if snapshot.robot_position is not None and best_pixels:
        # La position du robot est le dernier point du trajet.
        cx, cy = best_pixels[-1]
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
            return await self.hass.async_add_executor_job(_compose_map, snapshot)
        except Exception as err:  # noqa: BLE001 - image fetch must not raise
            _LOGGER.debug("Lecture de la carte impossible pour %s: %s", self._duid, err)
            return None
