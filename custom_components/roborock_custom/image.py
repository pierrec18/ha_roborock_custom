"""Map image platform for Roborock custom integration (B01/Q10).

Affiche la carte des pièces (PNG remis à l'endroit). La position/trajet du robot
ne sont dessinés QUE si une calibration manuelle est fournie dans les options de
l'intégration (`map_calibration`) — sans quoi la carte reste nue. La transformation
trace→carte du Q10 n'étant pas dérivable, on ne dessine jamais un trajet non calibré.
Pour une carte interactive (clic pour nettoyer une pièce), voir l'entité camera.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import RoborockConfigEntry, RoborockRuntimeData
from .api import DeviceSnapshot, MapSnapshot
from .const import DOMAIN
from .coordinator import RoborockDataUpdateCoordinator
from . import map_render

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: RoborockConfigEntry, async_add_entities) -> None:
    """Set up Roborock map image entities."""
    runtime_data = entry.runtime_data
    calibration = map_render.calibration_from_options(dict(entry.options))
    entities = [
        RoborockMapImageEntity(hass, runtime_data, duid, calibration)
        for duid, snapshot in runtime_data.coordinator.data.items()
        if snapshot.protocol == "b01_q10"
    ]
    async_add_entities(entities)


class RoborockMapImageEntity(CoordinatorEntity[RoborockDataUpdateCoordinator], ImageEntity):
    """Rendered rooms map (PNG) pushed by a B01/Q10 device."""

    _attr_has_entity_name = True
    _attr_name = "Map"
    _attr_content_type = "image/png"

    def __init__(
        self,
        hass: HomeAssistant,
        runtime_data: RoborockRuntimeData,
        duid: str,
        calibration: map_render.TraceCalibration | None,
    ) -> None:
        CoordinatorEntity.__init__(self, runtime_data.coordinator)
        ImageEntity.__init__(self, hass)
        self._runtime_data = runtime_data
        self._duid = duid
        self._calibration = calibration
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
        # Coordonnees BRUTES (repere trace du robot) — utiles pour la calibration
        # manuelle: on relève ces valeurs quand le robot est à des points connus.
        attrs: dict[str, Any] = {
            "path_points": len(last_map.path),
            "calibrated": self._calibration is not None,
        }
        if last_map.robot_position is not None:
            attrs["robot_raw_x"] = last_map.robot_position[0]
            attrs["robot_raw_y"] = last_map.robot_position[1]
        return attrs

    async def async_added_to_hass(self) -> None:
        """Subscribe to pushed map updates from the library's map trait."""
        await super().async_added_to_hass()
        unsub = await self._runtime_data.api.async_add_map_listener(self._duid, self._on_map_update)
        self.async_on_remove(unsub)
        if await self._runtime_data.api.async_get_map_image(self._duid) is not None:
            self._attr_image_last_updated = dt_util.utcnow()

    def _on_map_update(self) -> None:
        self._attr_image_last_updated = dt_util.utcnow()
        self.async_write_ha_state()

    async def async_image(self) -> bytes | None:
        try:
            snapshot = await self._runtime_data.api.async_get_map_snapshot(self._duid)
            if snapshot is None or snapshot.image is None:
                return None
            self._last_map = snapshot
            return await self.hass.async_add_executor_job(
                map_render.render_with_overlay,
                snapshot.image,
                snapshot.grid_width,
                snapshot.grid_height,
                snapshot.path,
                snapshot.robot_position,
                self._calibration,
            )
        except Exception as err:  # noqa: BLE001 - image fetch must not raise
            _LOGGER.debug("Lecture de la carte impossible pour %s: %s", self._duid, err)
            return None
