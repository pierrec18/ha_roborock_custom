"""Camera platform for Roborock custom integration (B01/Q10).

Expose la carte des pièces comme entité camera, pour la carte Lovelace
`xiaomi-vacuum-map-card` : nettoyage par pièce en cliquant sur la carte.

- L'image est la carte des pièces remise à l'endroit (voir map_render).
- L'attribut `rooms` donne, par pièce, son id et sa position en pixels sur
  l'image → sert à générer les `predefined_selections` de la carte Lovelace
  (calibration `identity` : coordonnées image, pas besoin de la transformation
  trace→carte). Le clic appelle le service `vacuum.roborock_clean_rooms`.
- La position/trajet du robot ne sont dessinés que si une calibration manuelle
  est fournie dans les options (`map_calibration`).
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.camera import Camera
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import RoborockConfigEntry, RoborockRuntimeData
from .api import DeviceSnapshot, MapSnapshot
from .const import DOMAIN
from .coordinator import RoborockDataUpdateCoordinator
from . import map_render

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: RoborockConfigEntry, async_add_entities) -> None:
    """Set up Roborock map camera entities."""
    runtime_data = entry.runtime_data
    calibration = map_render.calibration_from_options(dict(entry.options))
    entities = [
        RoborockMapCamera(runtime_data, duid, calibration)
        for duid, snapshot in runtime_data.coordinator.data.items()
        if snapshot.protocol == "b01_q10"
    ]
    async_add_entities(entities)


class RoborockMapCamera(CoordinatorEntity[RoborockDataUpdateCoordinator], Camera):
    """Rooms map camera for the interactive Lovelace card."""

    _attr_has_entity_name = True
    _attr_name = "Map camera"
    _attr_content_type = "image/png"
    _attr_frame_interval = 1.0

    def __init__(
        self,
        runtime_data: RoborockRuntimeData,
        duid: str,
        calibration: map_render.TraceCalibration | None,
    ) -> None:
        CoordinatorEntity.__init__(self, runtime_data.coordinator)
        Camera.__init__(self)
        self._runtime_data = runtime_data
        self._duid = duid
        self._calibration = calibration
        self._attr_unique_id = f"{duid}_map_camera"
        self._last_map: MapSnapshot | None = None
        self._regions: list[dict] = []

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
        attrs: dict[str, Any] = {
            "rooms": self._regions,
            "calibrated": self._calibration is not None,
        }
        last_map = self._last_map
        if last_map is not None:
            attrs["path_points"] = len(last_map.path)
            if last_map.robot_position is not None:
                # Coordonnees brutes pour la calibration manuelle.
                attrs["robot_raw_x"] = last_map.robot_position[0]
                attrs["robot_raw_y"] = last_map.robot_position[1]
        return attrs

    async def async_added_to_hass(self) -> None:
        """Subscribe to pushed map updates from the library's map trait."""
        await super().async_added_to_hass()
        unsub = await self._runtime_data.api.async_add_map_listener(self._duid, self.async_write_ha_state)
        self.async_on_remove(unsub)

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        try:
            snapshot = await self._runtime_data.api.async_get_map_snapshot(self._duid)
            if snapshot is None or snapshot.image is None:
                return None
            self._last_map = snapshot
            image, regions = await self.hass.async_add_executor_job(self._render, snapshot)
            self._regions = regions
            return image
        except Exception as err:  # noqa: BLE001 - image fetch must not raise
            _LOGGER.debug("Rendu carte camera impossible pour %s: %s", self._duid, err)
            return None

    def _render(self, snapshot: MapSnapshot) -> tuple[bytes, list[dict]]:
        """Rendu + régions de pièces (exécuté dans l'executor)."""
        image = map_render.render_with_overlay(
            snapshot.image,
            snapshot.grid_width,
            snapshot.grid_height,
            snapshot.path,
            snapshot.robot_position,
            self._calibration,
        )
        regions = map_render.room_regions(snapshot.image, snapshot.rooms)
        return image, regions
