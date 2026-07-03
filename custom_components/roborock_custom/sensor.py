"""Sensor platform for Roborock custom integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory, PERCENTAGE, UnitOfArea, UnitOfTime
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import RoborockConfigEntry, RoborockRuntimeData
from .api import DeviceSnapshot
from .const import DOMAIN
from .coordinator import RoborockDataUpdateCoordinator

# Surfaces B01 remontees en mm2 au-dela de ce seuil (meme heuristique que api.py).
_AREA_MM2_THRESHOLD = 10000


async def async_setup_entry(hass, entry: RoborockConfigEntry, async_add_entities) -> None:
    """Set up Roborock sensors."""
    runtime_data = entry.runtime_data
    entities: list[SensorEntity] = []
    for duid, snapshot in runtime_data.coordinator.data.items():
        entities.append(RoborockBatterySensor(runtime_data, duid))
        entities.append(RoborockStateSensor(runtime_data, duid))
        entities.append(RoborockProtocolSensor(runtime_data, duid))
        entities.append(RoborockMopWaterLevelSensor(runtime_data, duid))
        entities.append(RoborockCleanAreaSensor(runtime_data, duid))
        entities.append(RoborockCleanTimeSensor(runtime_data, duid))
        if snapshot.protocol == "b01_q10":
            entities.append(RoborockCleaningProgressSensor(runtime_data, duid))
            entities.append(RoborockTotalCleanAreaSensor(runtime_data, duid))
            entities.append(RoborockTotalCleanTimeSensor(runtime_data, duid))
            entities.append(RoborockTotalCleanCountSensor(runtime_data, duid))
    async_add_entities(entities)


def _area_m2(raw: Any) -> float | None:
    """Normalize a Roborock area value to square meters."""
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        return None
    value = float(raw)
    if value > _AREA_MM2_THRESHOLD:
        return round(value / 1_000_000, 2)
    return round(value, 2)


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


class RoborockCleaningProgressSensor(RoborockBaseSensor):
    """Progress of the current cleaning session."""

    _attr_name = "Cleaning Progress"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, runtime_data: RoborockRuntimeData, duid: str) -> None:
        super().__init__(runtime_data, duid)
        self._attr_unique_id = f"{duid}_cleaning_progress"

    @property
    def native_value(self):
        snapshot = self._snapshot
        if snapshot is None:
            return None
        value = snapshot.status.get("cleaning_progress")
        return int(value) if isinstance(value, (int, float)) else None


class RoborockCleanAreaSensor(RoborockBaseSensor):
    """Cleaned area of the current/last session."""

    _attr_name = "Clean Area"
    _attr_native_unit_of_measurement = UnitOfArea.SQUARE_METERS
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, runtime_data: RoborockRuntimeData, duid: str) -> None:
        super().__init__(runtime_data, duid)
        self._attr_unique_id = f"{duid}_clean_area"

    @property
    def native_value(self):
        snapshot = self._snapshot
        if snapshot is None:
            return None
        value = snapshot.status.get("square_meter_clean_area")
        if value is None:
            value = _area_m2(snapshot.status.get("clean_area"))
        return value


class RoborockCleanTimeSensor(RoborockBaseSensor):
    """Duration of the current/last cleaning session."""

    _attr_name = "Clean Time"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_suggested_unit_of_measurement = UnitOfTime.MINUTES
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, runtime_data: RoborockRuntimeData, duid: str) -> None:
        super().__init__(runtime_data, duid)
        self._attr_unique_id = f"{duid}_clean_time"

    @property
    def native_value(self):
        snapshot = self._snapshot
        if snapshot is None:
            return None
        value = snapshot.status.get("clean_time")
        return int(value) if isinstance(value, (int, float)) else None


class RoborockTotalCleanAreaSensor(RoborockBaseSensor):
    """Lifetime cleaned area."""

    _attr_name = "Total Clean Area"
    _attr_native_unit_of_measurement = UnitOfArea.SQUARE_METERS
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, runtime_data: RoborockRuntimeData, duid: str) -> None:
        super().__init__(runtime_data, duid)
        self._attr_unique_id = f"{duid}_total_clean_area"

    @property
    def native_value(self):
        snapshot = self._snapshot
        if snapshot is None:
            return None
        return _area_m2(snapshot.status.get("total_clean_area"))


class RoborockTotalCleanTimeSensor(RoborockBaseSensor):
    """Lifetime cleaning duration."""

    _attr_name = "Total Clean Time"
    _attr_device_class = SensorDeviceClass.DURATION
    # Le Q10 remonte le total en minutes (verifie: 6275 ~ 104,6 h, coherent
    # avec l'integration native), contrairement a clean_time en secondes.
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_suggested_unit_of_measurement = UnitOfTime.HOURS
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, runtime_data: RoborockRuntimeData, duid: str) -> None:
        super().__init__(runtime_data, duid)
        self._attr_unique_id = f"{duid}_total_clean_time"

    @property
    def native_value(self):
        snapshot = self._snapshot
        if snapshot is None:
            return None
        value = snapshot.status.get("total_clean_time")
        return int(value) if isinstance(value, (int, float)) else None


class RoborockTotalCleanCountSensor(RoborockBaseSensor):
    """Lifetime number of cleaning sessions."""

    _attr_name = "Total Clean Count"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, runtime_data: RoborockRuntimeData, duid: str) -> None:
        super().__init__(runtime_data, duid)
        self._attr_unique_id = f"{duid}_total_clean_count"

    @property
    def native_value(self):
        snapshot = self._snapshot
        if snapshot is None:
            return None
        value = snapshot.status.get("total_clean_count")
        return int(value) if isinstance(value, (int, float)) else None
