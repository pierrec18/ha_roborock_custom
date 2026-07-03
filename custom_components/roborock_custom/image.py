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

# Fraction minimale de points du trajet devant tomber dans la grille pour que
# l'overlay soit dessine. Les coordonnees de trace B01 ne sont pas documentees;
# l'hypothese (unites = cellules de grille, origine partagee) est verifiee a
# chaque rendu et l'overlay est saute si elle ne tient pas.
_MIN_IN_BOUNDS_RATIO = 0.9

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


def _compose_map(snapshot: MapSnapshot) -> bytes | None:
    """Draw the live path and robot position onto the rendered map PNG.

    Runs in the executor (PIL is CPU-bound). Returns the plain map when there
    is no live trace or when the trace does not line up with the grid.
    """
    if snapshot.image is None:
        return None
    if not snapshot.path or not snapshot.grid_width or not snapshot.grid_height:
        return snapshot.image

    grid_w = snapshot.grid_width
    grid_h = snapshot.grid_height
    in_bounds = [p for p in snapshot.path if 0 <= p[0] < grid_w and 0 <= p[1] < grid_h]
    if len(in_bounds) / len(snapshot.path) < _MIN_IN_BOUNDS_RATIO:
        xs = [p[0] for p in snapshot.path]
        ys = [p[1] for p in snapshot.path]
        _LOGGER.debug(
            "Trace hors grille (calibration requise): x=[%s..%s] y=[%s..%s] vs grille %sx%s",
            min(xs), max(xs), min(ys), max(ys), grid_w, grid_h,
        )
        return snapshot.image

    img = Image.open(io.BytesIO(snapshot.image)).convert("RGBA")
    scale_x = img.width / grid_w
    scale_y = img.height / grid_h

    def to_pixel(point: tuple[int, int]) -> tuple[float, float]:
        # Le rendu de la lib retourne l'image verticalement (FLIP_TOP_BOTTOM):
        # la ligne y de la grille devient la ligne (grid_h - 1 - y) de l'image.
        x, y = point
        return ((x + 0.5) * scale_x, (grid_h - 1 - y + 0.5) * scale_y)

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    pixels = [to_pixel(p) for p in in_bounds]
    if len(pixels) >= 2:
        draw.line(pixels, fill=_PATH_COLOR, width=max(2, int(scale_x)))

    robot = snapshot.robot_position
    if robot is not None and 0 <= robot[0] < grid_w and 0 <= robot[1] < grid_h:
        cx, cy = to_pixel(robot)
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
