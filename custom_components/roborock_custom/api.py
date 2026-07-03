"""Runtime API wrapper for Roborock devices."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any

import aiohttp
from roborock import RoborockCommand
from roborock.data import UserData
from roborock.data.b01_q10.b01_q10_code_mappings import B01_Q10_DP, YXCleanType, YXFanLevel, YXWaterLevel
from roborock.devices.cache import InMemoryCache
from roborock.devices.device import RoborockDevice
from roborock.devices.device_manager import DeviceManager, UserParams, create_device_manager
from roborock.exceptions import RoborockException, RoborockInvalidCredentials

_LOGGER = logging.getLogger(__name__)

_DISCOVERY_INTERVAL_SECONDS = 300
_B01_STATUS_SETTLE_SECONDS = 0.8
_B01_MAP_WAIT_ATTEMPTS = 10
_B01_MAP_WAIT_INTERVAL_SECONDS = 0.5
_HOME_DATA_RATE_LIMIT_MARKER = "maximum requests for home data"


class RoborockAuthenticationError(Exception):
    """Raised when the stored cloud session is no longer valid."""


class RoborockConnectionError(Exception):
    """Raised for non-auth cloud connectivity failures."""


@dataclass(slots=True)
class DeviceSnapshot:
    """Flattened device data used by Home Assistant entities."""

    duid: str
    name: str
    model: str
    pv: str | None
    online: bool | None
    firmware: str | None
    protocol: str
    status: dict[str, Any]


class RoborockCloudApi:
    """Manages one Roborock account session and device operations."""

    def __init__(
        self,
        username: str,
        user_data_raw: dict[str, Any],
        base_url: str | None = None,
        session: aiohttp.ClientSession | None = None,
        on_mqtt_unauthorized: Callable[[], None] | None = None,
    ) -> None:
        self._username = username
        user_data = UserData.from_dict(user_data_raw)
        if user_data is None:
            raise ValueError("Invalid user_data in config entry")
        self._user_data = user_data
        self._base_url = base_url
        self._session = session
        self._on_mqtt_unauthorized = on_mqtt_unauthorized

        self._manager: DeviceManager | None = None
        self._cache = InMemoryCache()
        self._devices: dict[str, RoborockDevice] = {}
        self._status_cache: dict[str, dict[str, Any]] = {}
        self._last_discovery = 0.0
        self._mqtt_unauthorized = False
        self._lock = asyncio.Lock()

    async def async_connect(self) -> None:
        """Initialize manager and discover devices if needed."""
        async with self._lock:
            await self._ensure_manager_locked()
            self._devices = {d.duid: d for d in await self._manager.get_devices()}  # type: ignore[union-attr]

    async def async_disconnect(self) -> None:
        """Close all active Roborock connections."""
        async with self._lock:
            if self._manager is not None:
                await self._manager.close()
            self._manager = None
            self._devices = {}

    async def async_refresh_snapshots(self, *, force_discovery: bool = False) -> dict[str, DeviceSnapshot]:
        """Refresh status for all known devices."""
        async with self._lock:
            if self._mqtt_unauthorized:
                raise RoborockAuthenticationError("MQTT session unauthorized")
            await self._ensure_manager_locked()
            manager = self._manager
            if manager is None:
                raise RoborockConnectionError("Device manager non initialise")

            now = time.monotonic()
            should_discover = force_discovery or (now - self._last_discovery) >= _DISCOVERY_INTERVAL_SECONDS
            if should_discover:
                try:
                    # Normal polling must rely on cached home data to avoid cloud API rate limits.
                    await manager.discover_devices(prefer_cache=not force_discovery)
                    self._last_discovery = now
                except RoborockException as err:
                    if self._is_home_data_rate_limited(err) and self._devices:
                        _LOGGER.warning(
                            "Home data rate limited by Roborock cloud, reusing cached device list: %s",
                            err,
                        )
                        self._last_discovery = now
                    else:
                        self._raise_wrapped_error(err)

            self._devices = {d.duid: d for d in await manager.get_devices()}
            snapshots: dict[str, DeviceSnapshot] = {}

            for device in self._devices.values():
                status: dict[str, Any]
                try:
                    status = await self._refresh_status(device)
                    self._status_cache[device.duid] = status
                except RoborockException as err:
                    self._raise_if_auth_error(err)
                    status = dict(self._status_cache.get(device.duid, {}))
                    status["last_error"] = str(err)
                    _LOGGER.debug("Status refresh failed for %s: %s", device.duid, err)

                snapshots[device.duid] = DeviceSnapshot(
                    duid=device.duid,
                    name=device.name,
                    model=device.product.model,
                    pv=getattr(device.device_info, "pv", None),
                    online=getattr(device.device_info, "online", None),
                    firmware=getattr(device.device_info, "fv", None),
                    protocol=self._protocol_for(device),
                    status=status,
                )

            return snapshots

    async def async_send_command(self, duid: str, command: str) -> None:
        """Send core vacuum commands to a device."""
        command = command.strip().lower()
        async with self._lock:
            device = await self._get_device_locked(duid)

            if device.v1_properties is not None:
                mapping = {
                    "start": RoborockCommand.APP_START,
                    "pause": RoborockCommand.APP_PAUSE,
                    "stop": RoborockCommand.APP_STOP,
                    "dock": RoborockCommand.APP_CHARGE,
                    "return_home": RoborockCommand.APP_CHARGE,
                }
                if command not in mapping:
                    raise ValueError(f"Commande non supportee: {command}")
                try:
                    await device.v1_properties.command.send(mapping[command])
                except RoborockException as err:
                    self._raise_wrapped_error(err)
                return

            if device.b01_q10_properties is not None:
                vacuum = device.b01_q10_properties.vacuum
                try:
                    if command == "start":
                        await vacuum.start_clean()
                    elif command == "pause":
                        await vacuum.pause_clean()
                    elif command == "resume":
                        await vacuum.resume_clean()
                    elif command == "stop":
                        await vacuum.stop_clean()
                    elif command in {"dock", "return_home"}:
                        await vacuum.return_to_dock()
                    else:
                        raise ValueError(f"Commande non supportee: {command}")
                except RoborockException as err:
                    self._raise_wrapped_error(err)
                return

            raise ValueError("Type d'appareil non supporte")

    async def async_send_custom_command(
        self,
        duid: str,
        command: str,
        params: Any = None,
    ) -> None:
        """Handle vacuum.send_command commands."""
        command = command.strip().lower()
        if command in {"start", "pause", "resume", "stop", "dock", "return_home"}:
            await self.async_send_command(duid, command)
            return

        if command in {"set_suction", "set_fan_speed"}:
            level = self._extract_single_value(params, {"level", "fan_speed", "value"})
            await self.async_set_suction(duid, level)
            return

        if command in {"set_water_level", "set_mop_water"}:
            level = self._extract_single_value(params, {"level", "water_level", "value"})
            await self.async_set_water_level(duid, level)
            return

        if command in {"set_mop_mode", "set_clean_mode"}:
            mode = self._extract_single_value(params, {"mode", "clean_mode", "value"})
            await self.async_set_clean_mode(duid, mode)
            return

        if command in {"clean_rooms", "clean_segments"}:
            room_ids, repeat = self._extract_room_clean_params(params)
            await self.async_clean_rooms(duid, room_ids=room_ids, repeat=repeat)
            return

        if command in {"b01_send", "b01_command", "b01_raw"}:
            raw_command, raw_params = self._extract_b01_command_payload(params)
            await self.async_send_b01_command(duid, raw_command, raw_params)
            return

        if command in {
            "request_dps",
            "refresh_dps",
            "b01_request_dps",
            "b01_refresh",
            "reset_main_brush",
            "reset_side_brush",
            "reset_filter",
            "reset_rag_life",
            "reset_mop",
            "reset_sensor",
            "map_reset",
            "seek",
        }:
            b01_aliases: dict[str, tuple[B01_Q10_DP, Any]] = {
                "request_dps": (B01_Q10_DP.REQUEST_DPS, {}),
                "refresh_dps": (B01_Q10_DP.REQUEST_DPS, {}),
                "b01_request_dps": (B01_Q10_DP.REQUEST_DPS, {}),
                "b01_refresh": (B01_Q10_DP.REQUEST_DPS, {}),
                "reset_main_brush": (B01_Q10_DP.RESET_MAIN_BRUSH, {}),
                "reset_side_brush": (B01_Q10_DP.RESET_SIDE_BRUSH, {}),
                "reset_filter": (B01_Q10_DP.RESET_FILTER, {}),
                "reset_rag_life": (B01_Q10_DP.RESET_RAG_LIFE, {}),
                "reset_mop": (B01_Q10_DP.RESET_RAG_LIFE, {}),
                "reset_sensor": (B01_Q10_DP.RESET_SENSOR, {}),
                "map_reset": (B01_Q10_DP.MAP_RESET, {}),
                "seek": (B01_Q10_DP.SEEK, {}),
            }
            dp, default_params = b01_aliases[command]
            await self.async_send_b01_command(duid, dp, params if params is not None else default_params)
            return

        async with self._lock:
            device = await self._get_device_locked(duid)

            if command in {"locate", "find_me"}:
                if device.v1_properties is not None:
                    try:
                        await device.v1_properties.command.send(RoborockCommand.FIND_ME)
                    except RoborockException as err:
                        self._raise_wrapped_error(err)
                    return
                if device.b01_q10_properties is not None:
                    try:
                        await device.b01_q10_properties.command.send(B01_Q10_DP.SEEK, {})
                    except RoborockException as err:
                        self._raise_wrapped_error(err)
                    return

            if command in {"empty", "empty_dustbin"} and device.b01_q10_properties is not None:
                try:
                    await device.b01_q10_properties.vacuum.empty_dustbin()
                except RoborockException as err:
                    self._raise_wrapped_error(err)
                return

        try:
            await self.async_send_b01_command(duid, command, params)
            return
        except ValueError:
            pass

        raise ValueError(f"Commande personnalisee non supportee: {command}")

    async def async_set_suction(self, duid: str, level: Any) -> None:
        """Set suction level."""
        async with self._lock:
            device = await self._get_device_locked(duid)
            if device.v1_properties is not None:
                status = device.v1_properties.status
                await status.refresh()
                code = self._resolve_code_from_mapping(status.fan_speed_mapping, level, "suction")
                try:
                    await device.v1_properties.command.send(RoborockCommand.SET_CUSTOM_MODE, [code])
                except RoborockException as err:
                    self._raise_wrapped_error(err)
                return

            if device.b01_q10_properties is not None:
                fan_level = self._parse_b01_enum(YXFanLevel, level, "suction")
                try:
                    await device.b01_q10_properties.vacuum.set_fan_level(fan_level)
                except RoborockException as err:
                    self._raise_wrapped_error(err)
                return

            raise ValueError("Type d'appareil non supporte")

    async def async_set_water_level(self, duid: str, level: Any) -> None:
        """Set mop water level."""
        async with self._lock:
            device = await self._get_device_locked(duid)
            if device.v1_properties is not None:
                status = device.v1_properties.status
                await status.refresh()
                code = self._resolve_code_from_mapping(status.water_mode_mapping, level, "water_level")
                try:
                    await device.v1_properties.command.send(RoborockCommand.SET_WATER_BOX_CUSTOM_MODE, [code])
                except RoborockException as err:
                    self._raise_wrapped_error(err)
                return

            if device.b01_q10_properties is not None:
                water_level = self._parse_b01_enum(YXWaterLevel, level, "water_level")
                try:
                    await device.b01_q10_properties.command.send(B01_Q10_DP.WATER_LEVEL, water_level.code)
                except RoborockException as err:
                    self._raise_wrapped_error(err)
                return

            raise ValueError("Type d'appareil non supporte")

    async def async_set_clean_mode(self, duid: str, mode: Any) -> None:
        """Set mop/clean mode."""
        async with self._lock:
            device = await self._get_device_locked(duid)
            if device.v1_properties is not None:
                status = device.v1_properties.status
                await status.refresh()
                code = self._resolve_code_from_mapping(status.mop_route_mapping, mode, "mop_mode")
                try:
                    await device.v1_properties.command.send(RoborockCommand.SET_MOP_MODE, [code])
                except RoborockException as err:
                    self._raise_wrapped_error(err)
                return

            if device.b01_q10_properties is not None:
                clean_mode = self._parse_b01_enum(YXCleanType, mode, "clean_mode")
                try:
                    await device.b01_q10_properties.vacuum.set_clean_mode(clean_mode)
                except RoborockException as err:
                    self._raise_wrapped_error(err)
                return

            raise ValueError("Type d'appareil non supporte")

    async def async_clean_rooms(self, duid: str, room_ids: list[int], repeat: int = 1) -> None:
        """Start room/segment clean for V1 and B01/Q10 devices."""
        if not room_ids:
            raise ValueError("Aucune piece fournie")
        repeat = max(1, min(3, int(repeat)))

        async with self._lock:
            device = await self._get_device_locked(duid)

            if device.b01_q10_properties is not None:
                if repeat != 1:
                    _LOGGER.debug("Parametre repeat ignore sur B01/Q10 (non supporte par dpStartClean)")
                try:
                    await device.b01_q10_properties.vacuum.clean_segments(room_ids)
                except RoborockException as err:
                    self._raise_wrapped_error(err)
                return

            if device.v1_properties is None:
                raise ValueError("Nettoyage par piece non supporte sur ce modele/protocole")

            candidates: list[Any] = [
                [room_ids, repeat],
                room_ids,
                {"segments": room_ids, "repeat": repeat},
                [{"segments": room_ids, "repeat": repeat}],
            ]
            last_error: Exception | None = None
            for payload in candidates:
                try:
                    await device.v1_properties.command.send(RoborockCommand.APP_SEGMENT_CLEAN, payload)
                    return
                except (RoborockException, ValueError) as err:
                    last_error = err

            if last_error is not None:
                if isinstance(last_error, RoborockException):
                    self._raise_wrapped_error(last_error)
                raise ValueError(f"Echec nettoyage pieces: {last_error}") from last_error
            raise ValueError("Echec nettoyage pieces")

    async def async_get_segments(self, duid: str) -> list[dict[str, str | None]]:
        """Return cleanable segments in Home Assistant clean-area format."""
        async with self._lock:
            device = await self._get_device_locked(duid)

            segments: list[dict[str, str | None]] = []

            if device.b01_q10_properties is not None:
                rooms = await self._get_b01_map_rooms_locked(device)
                for room in rooms:
                    segment_id = str(room.id)
                    segments.append(
                        {
                            "id": segment_id,
                            "name": room.name or f"Room {segment_id}",
                            "group": None,
                        }
                    )
                segments.sort(key=lambda item: (item["name"] or "", item["id"] or ""))
                return segments

            if device.v1_properties is None:
                raise ValueError("Segments non supportes sur ce modele/protocole")

            try:
                await device.v1_properties.rooms.refresh()
            except RoborockException as err:
                self._raise_wrapped_error(err)

            for room in device.v1_properties.rooms.rooms or []:
                segment_id = str(room.segment_id)
                name = room.name or f"Room {segment_id}"
                segments.append(
                    {
                        "id": segment_id,
                        "name": name,
                        "group": None,
                    }
                )

            segments.sort(key=lambda item: (item["name"] or "", item["id"] or ""))
            return segments

    async def _get_b01_map_rooms_locked(self, device: RoborockDevice) -> list[Any]:
        """Return the Q10 map rooms, nudging the device to push its map if empty.

        The Q10 has no synchronous get-map request: REQUEST_DPS makes the device
        publish its current map, which the library's subscribe loop parses into
        the map trait asynchronously.
        """
        props = device.b01_q10_properties
        if props is None:
            return []
        if props.map.rooms:
            return list(props.map.rooms)
        try:
            await props.refresh()
        except RoborockException as err:
            self._raise_wrapped_error(err)
        for _ in range(_B01_MAP_WAIT_ATTEMPTS):
            await asyncio.sleep(_B01_MAP_WAIT_INTERVAL_SECONDS)
            if props.map.rooms:
                break
        return list(props.map.rooms)

    async def async_send_b01_command(self, duid: str, command: B01_Q10_DP | str | int, params: Any = None) -> None:
        """Send any B01 command for B01/Q10 devices."""
        dp = self._resolve_b01_dp(command)
        normalized_params = self._normalize_b01_params(params)

        async with self._lock:
            device = await self._get_device_locked(duid)
            if device.b01_q10_properties is None:
                raise ValueError("Commande B01 non supportee sur ce modele/protocole")
            try:
                await device.b01_q10_properties.command.send(dp, normalized_params)
            except RoborockException as err:
                self._raise_wrapped_error(err)

    async def async_request_b01_dps(self, duid: str) -> None:
        """Request a full B01 DPS update."""
        await self.async_send_b01_command(duid, B01_Q10_DP.REQUEST_DPS, {})

    async def async_get_map_image(self, duid: str) -> bytes | None:
        """Return the latest rendered map image (PNG) for B01/Q10 devices."""
        async with self._lock:
            device = await self._get_device_locked(duid)
            props = device.b01_q10_properties
            if props is None:
                return None
            return props.map.image_content

    async def async_add_map_listener(self, duid: str, callback: Callable[[], None]) -> Callable[[], None]:
        """Register a callback fired when new map content is pushed.

        Returns an unsubscribe callable. No-op unsubscribe for devices without map support.
        """
        async with self._lock:
            device = await self._get_device_locked(duid)
            props = device.b01_q10_properties
            if props is None:
                return lambda: None
            return props.map.add_update_listener(callback)

    def supports_map(self, duid: str) -> bool:
        """Return True if the device exposes a rendered map (B01/Q10)."""
        device = self._devices.get(duid)
        return device is not None and device.b01_q10_properties is not None

    async def _ensure_manager_locked(self) -> None:
        if self._mqtt_unauthorized:
            raise RoborockAuthenticationError("MQTT session unauthorized")
        if self._manager is not None:
            return
        params = UserParams(
            username=self._username,
            user_data=self._user_data,
            base_url=self._base_url,
        )
        try:
            self._manager = await create_device_manager(
                params,
                cache=self._cache,
                session=self._session,
                mqtt_session_unauthorized_hook=self._mqtt_unauthorized_hook,
            )
            self._last_discovery = time.monotonic()
            self._mqtt_unauthorized = False
        except RoborockException as err:
            self._raise_wrapped_error(err)

    async def _get_device_locked(self, duid: str) -> RoborockDevice:
        await self._ensure_manager_locked()
        manager = self._manager
        if manager is None:
            raise RoborockConnectionError("Device manager non initialise")

        device = self._devices.get(duid)
        if device is not None:
            return device

        try:
            await manager.discover_devices(prefer_cache=False)
        except RoborockException as err:
            self._raise_wrapped_error(err)
        self._devices = {d.duid: d for d in await manager.get_devices()}
        device = self._devices.get(duid)
        if device is None:
            raise ValueError(f"Aucun appareil pour duid={duid}")
        return device

    async def _refresh_status(self, device: RoborockDevice) -> dict[str, Any]:
        if device.v1_properties is not None:
            status = device.v1_properties.status
            await status.refresh()
            payload = status.as_dict()
            payload["state_name"] = status.state_name
            payload["error_code_name"] = status.error_code_name
            payload["square_meter_clean_area"] = status.square_meter_clean_area
            payload["fan_speed_name"] = status.fan_speed_name
            payload["fan_speed_options"] = list(status.fan_speed_mapping.values())
            payload["water_mode_name"] = status.water_mode_name
            payload["water_mode_options"] = list(status.water_mode_mapping.values())
            payload["mop_mode_name"] = status.mop_route_name
            payload["mop_mode_options"] = list(status.mop_route_mapping.values())

            rooms_payload: list[dict[str, Any]] = []
            try:
                await device.v1_properties.rooms.refresh()
                for room in device.v1_properties.rooms.rooms or []:
                    rooms_payload.append(
                        {
                            "segment_id": room.segment_id,
                            "name": room.name,
                            "iot_id": room.iot_id,
                        }
                    )
            except Exception as err:
                _LOGGER.debug("Rooms refresh failed for %s: %s", device.duid, err)
            payload["rooms"] = rooms_payload
            return payload

        if device.b01_q10_properties is not None:
            props = device.b01_q10_properties
            await props.refresh()
            await asyncio.sleep(_B01_STATUS_SETTLE_SECONDS)
            status = props.status
            payload = status.as_dict()
            payload["state_name"] = status.status.value if status.status is not None else None
            clean_area = status.clean_area
            if isinstance(clean_area, (int, float)) and clean_area > 10000:
                payload["square_meter_clean_area"] = round(float(clean_area) / 1_000_000, 2)
            payload["fan_speed_name"] = status.fan_level.value if status.fan_level is not None else None
            payload["fan_speed_options"] = [lvl.value for lvl in YXFanLevel if lvl.name != "UNKNOWN"]
            payload["water_mode_name"] = status.water_level.value if status.water_level is not None else None
            payload["water_mode_options"] = [lvl.value for lvl in YXWaterLevel if lvl.name != "UNKNOWN"]
            payload["mop_mode_name"] = status.clean_mode.value if status.clean_mode is not None else None
            payload["mop_mode_options"] = [mode.value for mode in YXCleanType if mode.name != "UNKNOWN"]
            payload["rooms"] = [
                {"segment_id": room.id, "name": room.name or f"Room {room.id}"}
                for room in props.map.rooms
            ]
            return payload

        raise ValueError("Type d'appareil non supporte")

    def _raise_wrapped_error(self, err: RoborockException) -> None:
        self._raise_if_auth_error(err)
        raise RoborockConnectionError(str(err)) from err

    def _raise_if_auth_error(self, err: RoborockException) -> None:
        if isinstance(err, RoborockInvalidCredentials):
            self._mqtt_unauthorized_hook()
            raise RoborockAuthenticationError(str(err)) from err
        message = str(err).lower()
        if "invalid credentials" in message or "response code: 2010" in message:
            self._mqtt_unauthorized_hook()
            raise RoborockAuthenticationError(str(err)) from err

    def _is_home_data_rate_limited(self, err: RoborockException) -> bool:
        return _HOME_DATA_RATE_LIMIT_MARKER in str(err).lower()

    def _protocol_for(self, device: RoborockDevice) -> str:
        if device.v1_properties is not None:
            return "v1"
        if device.b01_q10_properties is not None:
            return "b01_q10"
        return "unknown"

    def _mqtt_unauthorized_hook(self) -> None:
        if self._mqtt_unauthorized:
            return
        self._mqtt_unauthorized = True
        if self._on_mqtt_unauthorized is not None:
            self._on_mqtt_unauthorized()

    def _resolve_code_from_mapping(self, mapping: dict[int, str], raw: Any, label: str) -> int:
        if isinstance(raw, bool):
            raise ValueError(f"{label} invalide: {raw}")
        if isinstance(raw, int):
            if raw in mapping:
                return raw
            raise ValueError(f"{label} invalide: {raw}")

        if isinstance(raw, str):
            value = raw.strip().lower()
            if not value:
                raise ValueError(f"{label} manquant")
            if value.isdigit():
                numeric = int(value)
                if numeric in mapping:
                    return numeric
            for code, name in mapping.items():
                if str(name).strip().lower() == value:
                    return code
        raise ValueError(f"{label} invalide: {raw}")

    def _parse_b01_enum(self, enum_cls, raw: Any, label: str):
        if isinstance(raw, bool):
            raise ValueError(f"{label} invalide: {raw}")
        if isinstance(raw, int):
            enum_value = enum_cls.from_code_optional(raw)
            if enum_value is not None:
                return enum_value
            raise ValueError(f"{label} invalide: {raw}")

        if isinstance(raw, str):
            value = raw.strip().lower()
            if not value:
                raise ValueError(f"{label} manquant")
            if value.isdigit():
                enum_value = enum_cls.from_code_optional(int(value))
                if enum_value is not None:
                    return enum_value
            try:
                return enum_cls.from_value(value)
            except Exception:
                pass
            try:
                return enum_cls.from_name(value)
            except Exception:
                pass
        raise ValueError(f"{label} invalide: {raw}")

    def _extract_single_value(self, params: dict[str, Any] | list[Any] | None, keys: set[str]) -> Any:
        if isinstance(params, list) and params:
            return params[0]
        if isinstance(params, dict):
            for key in keys:
                if key in params:
                    return params[key]
        raise ValueError("Parametre manquant")

    def _extract_room_clean_params(self, params: dict[str, Any] | list[Any] | None) -> tuple[list[int], int]:
        repeat = 1
        room_values: Any = None
        if isinstance(params, dict):
            room_values = params.get("rooms", params.get("room_ids", params.get("segments")))
            if "repeat" in params:
                repeat = int(params["repeat"])
        elif isinstance(params, list):
            if params and all(isinstance(item, (int, str)) for item in params):
                room_values = params
            elif len(params) >= 1 and isinstance(params[0], list):
                room_values = params[0]
                if len(params) > 1 and isinstance(params[1], (int, str)):
                    repeat = int(params[1])
        if room_values is None:
            raise ValueError("Parametre rooms/room_ids/segments manquant")

        room_ids: list[int] = []
        for value in room_values:
            try:
                room_ids.append(int(value))
            except (TypeError, ValueError) as err:
                raise ValueError(f"room id invalide: {value}") from err
        if not room_ids:
            raise ValueError("Aucune piece fournie")
        return room_ids, repeat

    def _resolve_b01_dp(self, raw: B01_Q10_DP | str | int) -> B01_Q10_DP:
        if isinstance(raw, B01_Q10_DP):
            return raw
        if isinstance(raw, bool):
            raise ValueError(f"Commande B01 invalide: {raw}")
        if isinstance(raw, int):
            dp = B01_Q10_DP.from_code_optional(raw)
            if dp is None:
                raise ValueError(f"Commande B01 inconnue: {raw}")
            return dp
        if not isinstance(raw, str):
            raise ValueError(f"Commande B01 invalide: {raw}")

        value = raw.strip()
        if not value:
            raise ValueError("Commande B01 manquante")
        if value.isdigit() or (value.startswith("-") and value[1:].isdigit()):
            numeric = int(value)
            dp = B01_Q10_DP.from_code_optional(numeric)
            if dp is None:
                raise ValueError(f"Commande B01 inconnue: {value}")
            return dp

        normalized = value.replace("-", "_").replace(" ", "_")
        normalized_no_prefix = normalized
        lowered = normalized.lower()
        if lowered.startswith("b01_"):
            normalized_no_prefix = normalized[4:]
        elif lowered.startswith("dp_"):
            normalized_no_prefix = normalized[3:]

        candidates = [
            value,
            normalized,
            normalized_no_prefix,
            normalized.upper(),
            normalized_no_prefix.upper(),
            normalized.lower(),
            normalized_no_prefix.lower(),
        ]
        seen: set[str] = set()
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            try:
                return B01_Q10_DP.from_name(candidate)
            except ValueError:
                pass
            try:
                return B01_Q10_DP.from_value(candidate)
            except ValueError:
                pass

        raise ValueError(f"Commande B01 inconnue: {raw}")

    def _normalize_b01_params(self, params: Any) -> Any:
        if params is None:
            return None
        if isinstance(params, str):
            text = params.strip()
            if not text:
                return None
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
        if isinstance(params, (dict, list, int, float, bool)):
            return params
        raise ValueError(f"Parametres B01 invalides: {params}")

    def _extract_b01_command_payload(self, params: Any) -> tuple[B01_Q10_DP | str | int, Any]:
        if not isinstance(params, dict):
            raise ValueError("Parametres invalides pour b01_send, attendu {'command': ..., 'params': ...}")
        command = params.get("command", params.get("dp", params.get("code")))
        if command is None:
            raise ValueError("Parametre B01 manquant: command/dp/code")
        command_params = params.get("params", params.get("value", params.get("payload")))
        return command, command_params
