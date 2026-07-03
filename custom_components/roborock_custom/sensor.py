"""Sensor platform for Roborock custom integration."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory, PERCENTAGE
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import RoborockConfigEntry, RoborockRuntimeData
from .api import DeviceSnapshot
from .const import DOMAIN
from .coordinator import RoborockDataUpdateCoordinator


async def async_setup_entry(hass, entry: RoborockConfigEntry, async_add_entities) -> None:
    """Set up Roborock sensors."""
    runtime_data = entry.runtime_data
    entities: list[SensorEntity] = []
    for duid in runtime_data.coordinator.data:
        entities.append(RoborockBatterySensor(runtime_data, duid))
        entities.append(RoborockStateSensor(runtime_data, duid))
        entities.append(RoborockProtocolSensor(runtime_data, duid))
        entities.append(RoborockMopWaterLevelSensor(runtime_data, duid))
    async_add_entities(entities)


class RoborockBaseSensor(CoordinatorEntity[RoborockDataUpdateCoordinator], SensorEntity):
    """Base class for Roborock coordinator sensors."""

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


class RoborockBatterySensor(RoborockBaseSensor):
    """Battery level sensor."""

    _attr_name = "Battery"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, runtime_data: RoborockRuntimeData, duid: str) -> None:
        super().__init__(runtime_data, duid)
        self._attr_unique_id = f"{duid}_battery"

    @property
    def native_value(self):
        snapshot = self._snapshot
        if snapshot is None:
            return None
        battery = snapshot.status.get("battery")
        return int(battery) if isinstance(battery, (int, float)) else None


class RoborockStateSensor(RoborockBaseSensor):
    """Raw Roborock state sensor for diagnostics."""

    _attr_name = "State"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, runtime_data: RoborockRuntimeData, duid: str) -> None:
        super().__init__(runtime_data, duid)
        self._attr_unique_id = f"{duid}_state"

    @property
    def native_value(self):
        snapshot = self._snapshot
        if snapshot is None:
            return None
        state = snapshot.status.get("state_name") or snapshot.status.get("state") or snapshot.status.get("status")
        if state is None:
            return "unknown"
        return str(state)


class RoborockProtocolSensor(RoborockBaseSensor):
    """Device protocol sensor."""

    _attr_name = "Protocol"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, runtime_data: RoborockRuntimeData, duid: str) -> None:
        super().__init__(runtime_data, duid)
        self._attr_unique_id = f"{duid}_protocol"

    @property
    def native_value(self):
        snapshot = self._snapshot
        if snapshot is None:
            return None
        return snapshot.protocol


class RoborockMopWaterLevelSensor(RoborockBaseSensor):
    """Current mop water level."""

    _attr_name = "Mop Water Level"

    def __init__(self, runtime_data: RoborockRuntimeData, duid: str) -> None:
        super().__init__(runtime_data, duid)
        self._attr_unique_id = f"{duid}_mop_water_level"

    @property
    def native_value(self):
        snapshot = self._snapshot
        if snapshot is None:
            return None
        value = snapshot.status.get("water_mode_name")
        if value is None:
            return "unknown"
        return str(value)
