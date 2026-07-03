"""Map image platform for Roborock custom integration (B01/Q10)."""

from __future__ import annotations

import logging

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import RoborockConfigEntry, RoborockRuntimeData
from .api import DeviceSnapshot
from .const import DOMAIN
from .coordinator import RoborockDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: RoborockConfigEntry, async_add_entities) -> None:
    """Set up Roborock map image entities."""
    runtime_data = entry.runtime_data
    entities = [
        RoborockMapImageEntity(hass, runtime_data, duid)
        for duid, snapshot in runtime_data.coordinator.data.items()
        if snapshot.protocol == "b01_q10"
    ]
    async_add_entities(entities)


class RoborockMapImageEntity(CoordinatorEntity[RoborockDataUpdateCoordinator], ImageEntity):
    """Rendered map (PNG) pushed by a B01/Q10 device."""

    _attr_has_entity_name = True
    _attr_name = "Map"
    _attr_content_type = "image/png"

    def __init__(self, hass: HomeAssistant, runtime_data: RoborockRuntimeData, duid: str) -> None:
        CoordinatorEntity.__init__(self, runtime_data.coordinator)
        ImageEntity.__init__(self, hass)
        self._runtime_data = runtime_data
        self._duid = duid
        self._attr_unique_id = f"{duid}_map"

    @property
    def _snapshot(self) -> DeviceSnapshot | None:
        return self.coordinator.data.get(self._duid)

    @property
    def available(self) -> bool:
        return super().available and self._snapshot is not None

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._duid)}, manufacturer="Roborock")

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
            return await self._runtime_data.api.async_get_map_image(self._duid)
        except Exception as err:  # noqa: BLE001 - image fetch must not raise
            _LOGGER.debug("Lecture de la carte impossible pour %s: %s", self._duid, err)
            return None
