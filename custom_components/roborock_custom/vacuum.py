"""Vacuum platform for Roborock custom integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.vacuum import Segment, StateVacuumEntity, VacuumActivity, VacuumEntityFeature
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_platform
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import RoborockConfigEntry, RoborockRuntimeData
from .api import DeviceSnapshot
from .const import DOMAIN
from .coordinator import RoborockDataUpdateCoordinator


async def async_setup_entry(hass, entry: RoborockConfigEntry, async_add_entities) -> None:
    """Set up Roborock vacuum entities."""
    runtime_data = entry.runtime_data
    entities = [RoborockVacuumEntity(runtime_data, duid) for duid in runtime_data.coordinator.data]
    async_add_entities(entities)

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        "roborock_clean_rooms",
        {
            vol.Required("room_ids"): [vol.Coerce(int)],
            vol.Optional("repeat", default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=3)),
        },
        "async_clean_rooms_service",
    )
    platform.async_register_entity_service(
        "roborock_set_water_level",
        {vol.Required("level"): str},
        "async_set_water_level_service",
    )
    platform.async_register_entity_service(
        "roborock_set_clean_mode",
        {vol.Required("mode"): str},
        "async_set_clean_mode_service",
    )
    platform.async_register_entity_service(
        "roborock_b01_send",
        {
            vol.Required("command"): vol.Any(str, int),
            vol.Optional("params"): vol.Any(dict, list, str, int, float, bool),
            vol.Optional("refresh_after", default=True): bool,
        },
        "async_b01_send_service",
    )
    platform.async_register_entity_service(
        "roborock_b01_request_dps",
        {},
        "async_b01_request_dps_service",
    )


class RoborockVacuumEntity(CoordinatorEntity[RoborockDataUpdateCoordinator], StateVacuumEntity):
    """Representation of one Roborock vacuum."""

    _attr_has_entity_name = True
    _attr_name = "Vacuum"
    _attr_supported_features = (
        VacuumEntityFeature.START
        | VacuumEntityFeature.PAUSE
        | VacuumEntityFeature.STOP
        | VacuumEntityFeature.RETURN_HOME
        | VacuumEntityFeature.FAN_SPEED
        | VacuumEntityFeature.STATE
        | VacuumEntityFeature.SEND_COMMAND
    )

    def __init__(self, runtime_data: RoborockRuntimeData, duid: str) -> None:
        super().__init__(runtime_data.coordinator)
        self._runtime_data = runtime_data
        self._duid = duid
        self._attr_unique_id = f"{duid}_vacuum"

    @property
    def _snapshot(self) -> DeviceSnapshot | None:
        return self.coordinator.data.get(self._duid)

    @property
    def available(self) -> bool:
        return super().available and self._snapshot is not None

    @property
    def supported_features(self) -> VacuumEntityFeature:
        features = self._attr_supported_features
        snapshot = self._snapshot
        if snapshot is not None:
            if snapshot.protocol == "v1":
                features |= VacuumEntityFeature.CLEAN_AREA
            elif snapshot.protocol == "b01_q10" and snapshot.status.get("rooms"):
                # B01/Q10: n'activer CLEAN_AREA que lorsque la carte a fourni des pieces.
                features |= VacuumEntityFeature.CLEAN_AREA
        return features

    @property
    def activity(self) -> VacuumActivity | None:
        snapshot = self._snapshot
        if snapshot is None:
            return None
        return _map_activity(snapshot.status.get("state_name"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        snapshot = self._snapshot
        if snapshot is None:
            return {}
        status = snapshot.status
        attrs: dict[str, Any] = {
            "duid": snapshot.duid,
            "protocol": snapshot.protocol,
            "model": snapshot.model,
            "pv": snapshot.pv,
            "online": snapshot.online,
            "raw_state": status.get("state_name") or status.get("state") or status.get("status"),
            "error": status.get("error_code_name"),
            "battery": status.get("battery"),
            "water_mode": status.get("water_mode_name"),
            "water_mode_options": status.get("water_mode_options", []),
            "mop_mode": status.get("mop_mode_name"),
            "mop_mode_options": status.get("mop_mode_options", []),
            "rooms": status.get("rooms", []),
            "last_error": status.get("last_error"),
        }
        clean_area = status.get("square_meter_clean_area")
        if clean_area is None and isinstance(status.get("clean_area"), (int, float)):
            raw_area = float(status["clean_area"])
            clean_area = round(raw_area / 1_000_000, 2) if raw_area > 10000 else raw_area
        if clean_area is not None:
            attrs["clean_area_m2"] = clean_area
        if isinstance(status.get("clean_time"), int):
            attrs["clean_time_min"] = int(status["clean_time"] / 60)
        return attrs

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

    async def async_start(self) -> None:
        await self._async_command("start")

    async def async_pause(self) -> None:
        await self._async_command("pause")

    async def async_stop(self, **kwargs: Any) -> None:
        await self._async_command("stop")

    async def async_return_to_base(self, **kwargs: Any) -> None:
        await self._async_command("return_home")

    async def async_send_command(self, command: str, params: dict[str, Any] | list[Any] | None = None, **kwargs: Any) -> None:
        try:
            await self._runtime_data.api.async_send_custom_command(self._duid, command, params)
        except Exception as err:
            raise HomeAssistantError(f"Commande impossible: {err}") from err
        await self.coordinator.async_request_refresh()

    @property
    def fan_speed(self) -> str | None:
        snapshot = self._snapshot
        if snapshot is None:
            return None
        value = snapshot.status.get("fan_speed_name")
        return str(value) if value is not None else None

    @property
    def fan_speed_list(self) -> list[str]:
        snapshot = self._snapshot
        if snapshot is None:
            return []
        options = snapshot.status.get("fan_speed_options")
        if not isinstance(options, list):
            return []
        return [str(value) for value in options]

    async def async_set_fan_speed(self, fan_speed: str, **kwargs: Any) -> None:
        try:
            await self._runtime_data.api.async_set_suction(self._duid, fan_speed)
        except Exception as err:
            raise HomeAssistantError(f"Reglage aspiration impossible: {err}") from err
        await self.coordinator.async_request_refresh()

    async def async_get_segments(self) -> list[Segment]:
        try:
            raw_segments = await self._runtime_data.api.async_get_segments(self._duid)
        except Exception as err:
            raise HomeAssistantError(f"Recuperation des pieces impossible: {err}") from err

        segments = [
            Segment(
                id=str(item["id"]),
                name=str(item["name"]),
                group=(str(item["group"]) if item.get("group") else None),
            )
            for item in raw_segments
            if item.get("id") and item.get("name")
        ]
        self._check_segments_changed(segments)
        return segments

    async def async_clean_segments(self, segment_ids: list[str], **kwargs: Any) -> None:
        if not segment_ids:
            raise HomeAssistantError("Aucune piece fournie")
        try:
            room_ids = [int(segment_id) for segment_id in segment_ids]
        except (TypeError, ValueError) as err:
            raise HomeAssistantError(f"Segment ID invalide: {segment_ids}") from err

        repeat = int(kwargs.get("repeat", 1))
        try:
            await self._runtime_data.api.async_clean_rooms(self._duid, room_ids=room_ids, repeat=repeat)
        except Exception as err:
            raise HomeAssistantError(f"Nettoyage des pieces impossible: {err}") from err
        await self.coordinator.async_request_refresh()

    async def async_clean_rooms_service(self, room_ids: list[int], repeat: int = 1) -> None:
        try:
            await self._runtime_data.api.async_clean_rooms(self._duid, room_ids=room_ids, repeat=repeat)
        except Exception as err:
            raise HomeAssistantError(f"Nettoyage par pieces impossible: {err}") from err
        await self.coordinator.async_request_refresh()

    async def async_set_water_level_service(self, level: str) -> None:
        try:
            await self._runtime_data.api.async_set_water_level(self._duid, level)
        except Exception as err:
            raise HomeAssistantError(f"Reglage eau impossible: {err}") from err
        await self.coordinator.async_request_refresh()

    async def async_set_clean_mode_service(self, mode: str) -> None:
        try:
            await self._runtime_data.api.async_set_clean_mode(self._duid, mode)
        except Exception as err:
            raise HomeAssistantError(f"Reglage mode impossible: {err}") from err
        await self.coordinator.async_request_refresh()

    async def async_b01_send_service(self, command: str | int, params: Any = None, refresh_after: bool = True) -> None:
        try:
            await self._runtime_data.api.async_send_b01_command(self._duid, command, params)
        except Exception as err:
            raise HomeAssistantError(f"Commande B01 impossible: {err}") from err
        if refresh_after:
            await self.coordinator.async_request_refresh()

    async def async_b01_request_dps_service(self) -> None:
        try:
            await self._runtime_data.api.async_request_b01_dps(self._duid)
        except Exception as err:
            raise HomeAssistantError(f"Rafraichissement DPS B01 impossible: {err}") from err
        await self.coordinator.async_request_refresh()

    async def _async_command(self, command: str) -> None:
        try:
            await self._runtime_data.api.async_send_command(self._duid, command)
        except Exception as err:
            raise HomeAssistantError(f"Commande '{command}' impossible: {err}") from err
        await self.coordinator.async_request_refresh()

    def _handle_coordinator_update(self) -> None:
        self._check_segments_changed(self._segments_from_snapshot())
        super()._handle_coordinator_update()

    def _segments_from_snapshot(self) -> list[Segment]:
        snapshot = self._snapshot
        if snapshot is None:
            return []
        rooms = snapshot.status.get("rooms")
        if not isinstance(rooms, list):
            return []
        segments: list[Segment] = []
        for room in rooms:
            if not isinstance(room, dict):
                continue
            segment_id = room.get("segment_id")
            name = room.get("name")
            if segment_id is None or name is None:
                continue
            segments.append(Segment(id=str(segment_id), name=str(name), group=None))
        return segments

    def _check_segments_changed(self, current_segments: list[Segment]) -> None:
        if not current_segments:
            return

        last_seen_segments = getattr(self, "last_seen_segments", None)
        if not last_seen_segments:
            return

        def _signature(segments: list[Segment]) -> set[tuple[str, str, str | None]]:
            return {(segment.id, segment.name, segment.group) for segment in segments}

        if _signature(current_segments) == _signature(last_seen_segments):
            return

        create_issue = getattr(self, "async_create_segments_issue", None)
        if callable(create_issue):
            create_issue()


def _map_activity(state_name: str | None) -> VacuumActivity:
    if not state_name:
        return VacuumActivity.IDLE

    state = state_name.lower()
    if "error" in state or "fault" in state:
        return VacuumActivity.ERROR
    if state in {
        # Valeurs YXDeviceState (python-roborock 5.22, B01/Q10)
        "cleaning",
        "sweeping",
        "mopping",
        "sweep_and_mop",
        "mapping",
        "saving_map",
        "relocating",
        "transitioning",
        "emptying_the_bin",
        "remote_control_active",
        # Valeurs V1 / historiques
        "spot_cleaning",
        "zoned_cleaning",
        "segment_cleaning",
        "washing_the_mop",
        "cleaningstate",
        "creatingmapstate",
        "robotsweeping",
        "robotmoping",
        "robotsweepandmoping",
        "dusting",
    }:
        return VacuumActivity.CLEANING
    if state in {"paused", "pausestate"}:
        return VacuumActivity.PAUSED
    if state in {
        "returning_home",
        "going_to_target",
        "docking",
        "tochargestate",
        "robotwaitcharge",
        "back_to_dock_washing_duster",
    }:
        return VacuumActivity.RETURNING
    if "charging" in state or state in {
        "chargingstate",
        "standbystate",
        "waiting_to_charge",
    }:
        return VacuumActivity.DOCKED
    return VacuumActivity.IDLE
