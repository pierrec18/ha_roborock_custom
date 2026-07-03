"""Select platform for Roborock custom integration."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import RoborockConfigEntry, RoborockRuntimeData
from .api import DeviceSnapshot
from .const import DOMAIN
from .coordinator import RoborockDataUpdateCoordinator


async def async_setup_entry(hass, entry: RoborockConfigEntry, async_add_entities) -> None:
    """Set up Roborock select entities."""
    runtime_data = entry.runtime_data
    entities: list[SelectEntity] = []
    for duid in runtime_data.coordinator.data:
        entities.append(RoborockWaterLevelSelect(runtime_data, duid))
        entities.append(RoborockCleanModeSelect(runtime_data, duid))

    async_add_entities(entities)


class RoborockBaseSelect(CoordinatorEntity[RoborockDataUpdateCoordinator], SelectEntity):
    """Base class for Roborock select entities."""

    _attr_has_entity_name = True

    def __init__(self, runtime_data: RoborockRuntimeData, duid: str) -> None:
        super().__init__(runtime_data.coordinator)
        self._runtime_data = runtime_data
        self._duid = duid

    @property
    def _snapshot(self) -> DeviceSnapshot | None:
        return self.coordinator.data.get(self._duid)

    @property
    def available(self) -> bool:
        return super().available and self._snapshot is not None

    @property
    def device_info(self) -> DeviceInfo:
        snapshot = self._snapshot
        if snapshot is None:
            return DeviceInfo(identifiers={(DOMAIN, self._duid)}, manufacturer="Roborock")
        return DeviceInfo(
            identifiers={(DOMAIN, snapshot.duid)},
            manufacturer="Roborock",
            model=snapshot.model,
            name=snapshot.name,
            sw_version=snapshot.firmware,
        )


class RoborockWaterLevelSelect(RoborockBaseSelect):
    """Water level select for mop."""

    _attr_name = "Water Level"

    def __init__(self, runtime_data: RoborockRuntimeData, duid: str) -> None:
        super().__init__(runtime_data, duid)
        self._attr_unique_id = f"{duid}_water_level"

    @property
    def available(self) -> bool:
        return super().available and bool(self.options)

    @property
    def options(self) -> list[str]:
        snapshot = self._snapshot
        if snapshot is None:
            return []
        options = snapshot.status.get("water_mode_options")
        if not isinstance(options, list):
            return []
        return [str(value) for value in options]

    @property
    def current_option(self) -> str | None:
        snapshot = self._snapshot
        if snapshot is None:
            return None
        value = snapshot.status.get("water_mode_name")
        return str(value) if value is not None else None

    async def async_select_option(self, option: str) -> None:
        try:
            await self._runtime_data.api.async_set_water_level(self._duid, option)
        except Exception as err:
            raise HomeAssistantError(f"Reglage niveau d'eau impossible: {err}") from err
        await self.coordinator.async_request_refresh()


class RoborockCleanModeSelect(RoborockBaseSelect):
    """Mop/clean mode select."""

    _attr_name = "Clean Mode"

    def __init__(self, runtime_data: RoborockRuntimeData, duid: str) -> None:
        super().__init__(runtime_data, duid)
        self._attr_unique_id = f"{duid}_clean_mode"

    @property
    def available(self) -> bool:
        return super().available and bool(self.options)

    @property
    def options(self) -> list[str]:
        snapshot = self._snapshot
        if snapshot is None:
            return []
        options = snapshot.status.get("mop_mode_options")
        if not isinstance(options, list):
            return []
        return [str(value) for value in options]

    @property
    def current_option(self) -> str | None:
        snapshot = self._snapshot
        if snapshot is None:
            return None
        value = snapshot.status.get("mop_mode_name")
        return str(value) if value is not None else None

    async def async_select_option(self, option: str) -> None:
        try:
            await self._runtime_data.api.async_set_clean_mode(self._duid, option)
        except Exception as err:
            raise HomeAssistantError(f"Reglage mode de nettoyage impossible: {err}") from err
        await self.coordinator.async_request_refresh()
