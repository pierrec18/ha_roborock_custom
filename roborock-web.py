#!/usr/bin/env python3

import argparse
import asyncio
import base64
import contextlib
import json
from pathlib import Path
from typing import Any

from aiohttp import web
from roborock import RoborockCommand
from roborock.data import UserData
from roborock.data.b01_q10.b01_q10_code_mappings import B01_Q10_DP
from roborock.devices.device import RoborockDevice
from roborock.devices.device_manager import DeviceManager, UserParams, create_device_manager
from roborock.devices.rpc.b01_q10_channel import stream_decoded_responses
from roborock.exceptions import RoborockException, RoborockInvalidCode
from roborock.web_api import RoborockApiClient

ROOT = Path(__file__).parent
AUTH_STATE_PATH = ROOT / ".roborock-auth.json"
SESSION_PATH = ROOT / ".roborock-web-session.json"
INDEX_PATH = ROOT / "webui" / "index.html"


class NeedsTwoFactorError(Exception):
    pass


def normalize_code(code: str) -> str:
    return "".join(code.split())


def json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2))


def extract_b01_maps(multi_map_value: Any) -> list[dict[str, Any]]:
    if not isinstance(multi_map_value, dict):
        return []
    data = multi_map_value.get("data")
    if not isinstance(data, list):
        return []
    maps: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        map_id = item.get("id")
        if not isinstance(map_id, str) or not map_id:
            continue
        maps.append(
            {
                "id": map_id,
                "name": item.get("name"),
                "timestamp": item.get("timestamp"),
            }
        )
    return maps


class RoborockWebService:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._username: str | None = None
        self._user_data: UserData | None = None
        self._manager: DeviceManager | None = None
        self._devices: dict[str, RoborockDevice] = {}

    async def startup(self) -> None:
        await self._load_session_from_disk()
        if self._username and self._user_data:
            with contextlib.suppress(Exception):
                await self._connect_manager()

    async def shutdown(self) -> None:
        async with self._lock:
            await self._close_manager()

    async def state(self) -> dict[str, Any]:
        async with self._lock:
            if self._manager and not self._devices:
                self._devices = {d.duid: d for d in await self._manager.get_devices()}
            return {
                "logged_in": bool(self._username and self._user_data),
                "username": self._username,
                "connected": self._manager is not None,
                "devices": [self._format_device(d) for d in self._devices.values()],
            }

    async def send_code(self, username: str) -> None:
        username = username.strip()
        if not username:
            raise ValueError("username manquant")
        web_api = RoborockApiClient(username)
        self._store_device_identifier(username, web_api._device_identifier)
        await web_api.request_code()

    async def login(self, username: str, password: str, code: str) -> dict[str, Any]:
        username = username.strip()
        password = password or ""
        code = normalize_code(code or "")
        if not username:
            raise ValueError("username manquant")
        if not password and not code:
            raise ValueError("password ou code requis")

        web_api = RoborockApiClient(username)
        user_data: UserData

        if code:
            device_identifier = self._get_stored_device_identifier(username)
            if not device_identifier:
                raise ValueError("Aucun identifiant 2FA. Demande un code d'abord.")
            web_api._device_identifier = device_identifier
            try:
                user_data = await web_api.code_login(code)
            except RoborockInvalidCode as exc:
                raise ValueError("Code invalide ou expire") from exc
        else:
            try:
                user_data = await web_api.pass_login(password)
            except RoborockException as exc:
                if "response code: 2031" in str(exc):
                    raise NeedsTwoFactorError("2FA requis, envoie un code email") from exc
                raise

        async with self._lock:
            self._username = username
            self._user_data = user_data
            self._persist_session_to_disk()
            await self._connect_manager()

        return {"username": username}

    async def logout(self) -> None:
        async with self._lock:
            await self._close_manager()
            self._username = None
            self._user_data = None
            self._persist_session_to_disk()

    async def refresh_devices(self) -> list[dict[str, Any]]:
        async with self._lock:
            await self._ensure_manager()
            if not self._manager:
                return []
            await self._manager.discover_devices(prefer_cache=False)
            self._devices = {d.duid: d for d in await self._manager.get_devices()}
            return [self._format_device(d) for d in self._devices.values()]

    async def status(self, device_id: str) -> dict[str, Any]:
        async with self._lock:
            device = await self._get_device(device_id)
            if device.v1_properties is not None:
                await device.v1_properties.status.refresh()
                return device.v1_properties.status.as_dict()
            if device.b01_q10_properties is not None:
                await device.b01_q10_properties.refresh()
                await asyncio.sleep(0.8)
                return device.b01_q10_properties.status.as_dict()
            raise ValueError("Status non supporte sur ce device")

    async def command(self, device_id: str, command: str) -> Any:
        command = command.strip().lower()
        async with self._lock:
            device = await self._get_device(device_id)
            if device.v1_properties is not None:
                mapping = {
                    "start": RoborockCommand.APP_START,
                    "pause": RoborockCommand.APP_PAUSE,
                    "stop": RoborockCommand.APP_STOP,
                    "dock": RoborockCommand.APP_CHARGE,
                }
                if command not in mapping:
                    raise ValueError(f"Commande inconnue: {command}")
                response = await device.v1_properties.command.send(mapping[command])
                return json_safe(response)

            if device.b01_q10_properties is not None:
                vacuum = device.b01_q10_properties.vacuum
                if command == "start":
                    await vacuum.start_clean()
                elif command == "pause":
                    await vacuum.pause_clean()
                elif command == "stop":
                    await vacuum.stop_clean()
                elif command == "dock":
                    await vacuum.return_to_dock()
                elif command == "empty":
                    await vacuum.empty_dustbin()
                else:
                    raise ValueError(f"Commande inconnue: {command}")
                return {"ack": True}
            raise ValueError("Commande non supportee pour ce device")

    async def map_data(self, device_id: str) -> dict[str, Any]:
        async with self._lock:
            device = await self._get_device(device_id)
            if device.v1_properties is not None:
                await device.v1_properties.map_content.refresh()
                trait = device.v1_properties.map_content
                image_base64 = None
                if trait.image_content:
                    image_base64 = base64.b64encode(trait.image_content).decode()
                return {
                    "mode": "v1",
                    "image_base64": image_base64,
                    "map_summary": self._summarize_v1_map_data(trait.map_data),
                }

            if device.b01_q10_properties is not None:
                multimap_value, event = await self._query_b01_dp(
                    device,
                    send_dp=B01_Q10_DP.MULTI_MAP,
                    send_params={},
                    expected_dp=B01_Q10_DP.MULTI_MAP,
                    timeout=10,
                )
                maps = extract_b01_maps(multimap_value)
                probes: list[dict[str, Any]] = []
                for map_item in maps[:3]:
                    map_id = map_item["id"]
                    for params in (
                        {"op": "get", "id": map_id},
                        {"op": "detail", "id": map_id},
                        {"op": "data", "id": map_id},
                    ):
                        value, raw = await self._query_b01_dp(
                            device,
                            send_dp=B01_Q10_DP.MULTI_MAP,
                            send_params=params,
                            expected_dp=B01_Q10_DP.MULTI_MAP,
                            timeout=4,
                        )
                        probes.append(
                            {
                                "map_id": map_id,
                                "params": params,
                                "value": value,
                                "raw_event": self._event_to_jsonable(raw),
                            }
                        )
                return {
                    "mode": "b01_experimental",
                    "maps": maps,
                    "list_value": multimap_value,
                    "list_event": self._event_to_jsonable(event),
                    "probes": probes,
                    "note": (
                        "B01/Q10 ne fournit pas une image de carte standard via python-roborock "
                        "aujourd'hui. Donnees brutes exposees pour reverse engineering."
                    ),
                }
            raise ValueError("Map non supportee pour ce device")

    async def _load_session_from_disk(self) -> None:
        data = load_json(SESSION_PATH)
        username = data.get("username")
        user_data_raw = data.get("user_data")
        if not isinstance(username, str) or not username:
            return
        if not isinstance(user_data_raw, dict):
            return
        with contextlib.suppress(Exception):
            self._username = username
            self._user_data = UserData.from_dict(user_data_raw)

    def _persist_session_to_disk(self) -> None:
        payload: dict[str, Any] = {"username": None, "user_data": None}
        if self._username and self._user_data:
            payload = {
                "username": self._username,
                "user_data": self._user_data.as_dict(),
            }
        save_json(SESSION_PATH, payload)

    def _store_device_identifier(self, username: str, device_identifier: str) -> None:
        state = load_json(AUTH_STATE_PATH)
        state[username] = {"device_identifier": device_identifier}
        save_json(AUTH_STATE_PATH, state)

    def _get_stored_device_identifier(self, username: str) -> str | None:
        state = load_json(AUTH_STATE_PATH)
        user_state = state.get(username)
        if not isinstance(user_state, dict):
            return None
        value = user_state.get("device_identifier")
        if isinstance(value, str) and value:
            return value
        return None

    async def _connect_manager(self) -> None:
        await self._close_manager()
        if not self._username or not self._user_data:
            return
        self._manager = await create_device_manager(
            UserParams(username=self._username, user_data=self._user_data),
        )
        self._devices = {d.duid: d for d in await self._manager.get_devices()}

    async def _close_manager(self) -> None:
        if self._manager is not None:
            await self._manager.close()
        self._manager = None
        self._devices = {}

    async def _ensure_manager(self) -> None:
        if self._manager is not None:
            return
        if not self._username or not self._user_data:
            raise ValueError("Aucune session connectee")
        await self._connect_manager()

    async def _get_device(self, device_id: str) -> RoborockDevice:
        await self._ensure_manager()
        if not self._manager:
            raise ValueError("Session non connectee")
        if device_id not in self._devices:
            await self._manager.discover_devices(prefer_cache=False)
            self._devices = {d.duid: d for d in await self._manager.get_devices()}
        device = self._devices.get(device_id)
        if device is None:
            raise ValueError(f"Device inconnu: {device_id}")
        return device

    def _format_device(self, device: RoborockDevice) -> dict[str, Any]:
        protocol = "unknown"
        if device.v1_properties is not None:
            protocol = "v1"
        elif device.b01_q10_properties is not None:
            protocol = "b01_q10"
        return {
            "duid": device.duid,
            "name": device.name,
            "model": device.product.model,
            "pv": getattr(device.device_info, "pv", None),
            "online": getattr(device.device_info, "online", None),
            "protocol": protocol,
        }

    def _summarize_v1_map_data(self, map_data: Any) -> dict[str, Any] | None:
        if map_data is None:
            return None
        summary: dict[str, Any] = {}
        charger = getattr(map_data, "charger", None)
        if charger is not None and hasattr(charger, "as_dict"):
            summary["charger"] = charger.as_dict()
        vacuum_position = getattr(map_data, "vacuum_position", None)
        if vacuum_position is not None and hasattr(vacuum_position, "as_dict"):
            summary["vacuum_position"] = vacuum_position.as_dict()
        zones = getattr(map_data, "zones", None)
        if isinstance(zones, list):
            summary["zones_count"] = len(zones)
        image = getattr(map_data, "image", None)
        image_data = getattr(image, "data", None)
        image_size = getattr(image_data, "size", None)
        if image_size:
            summary["image_size"] = list(image_size)
        return summary

    def _event_to_jsonable(self, event: dict[B01_Q10_DP, Any] | None) -> dict[str, Any] | None:
        if event is None:
            return None
        payload: dict[str, Any] = {}
        for dp, value in event.items():
            payload[f"{dp.name}({dp.code})"] = json_safe(value)
        return payload

    async def _query_b01_dp(
        self,
        device: RoborockDevice,
        send_dp: B01_Q10_DP,
        send_params: Any,
        expected_dp: B01_Q10_DP,
        timeout: float = 8.0,
    ) -> tuple[Any, dict[B01_Q10_DP, Any] | None]:
        if device.b01_q10_properties is None:
            return None, None

        command = device.b01_q10_properties.command
        channel = command._channel

        async def wait_event() -> dict[B01_Q10_DP, Any] | None:
            async for decoded in stream_decoded_responses(channel):
                if expected_dp in decoded:
                    return decoded
            return None

        waiter = asyncio.create_task(wait_event())
        await asyncio.sleep(0)
        with contextlib.suppress(Exception):
            await command.send(send_dp, params=send_params)

        try:
            event = await asyncio.wait_for(waiter, timeout=timeout)
        except asyncio.TimeoutError:
            waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await waiter
            return None, None
        if event is None:
            return None, None
        return event.get(expected_dp), event


async def read_json_body(request: web.Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def ok(data: dict[str, Any]) -> web.Response:
    return web.json_response({"ok": True, **data})


def fail(message: str, status: int = 400, **extra: Any) -> web.Response:
    return web.json_response({"ok": False, "error": message, **extra}, status=status)


async def handle_index(_: web.Request) -> web.Response:
    return web.Response(text=INDEX_PATH.read_text(), content_type="text/html")


async def handle_state(request: web.Request) -> web.Response:
    service: RoborockWebService = request.app["service"]
    try:
        return ok(await service.state())
    except Exception as exc:
        return fail(str(exc), status=500)


async def handle_send_code(request: web.Request) -> web.Response:
    service: RoborockWebService = request.app["service"]
    payload = await read_json_body(request)
    username = str(payload.get("username", "")).strip()
    try:
        await service.send_code(username)
        return ok({"message": "Code envoye"})
    except Exception as exc:
        return fail(str(exc))


async def handle_login(request: web.Request) -> web.Response:
    service: RoborockWebService = request.app["service"]
    payload = await read_json_body(request)
    username = str(payload.get("username", ""))
    password = str(payload.get("password", ""))
    code = str(payload.get("code", ""))
    try:
        data = await service.login(username=username, password=password, code=code)
        return ok(data)
    except NeedsTwoFactorError as exc:
        return fail(str(exc), status=401, requires_2fa=True)
    except Exception as exc:
        return fail(str(exc), status=401)


async def handle_logout(request: web.Request) -> web.Response:
    service: RoborockWebService = request.app["service"]
    await service.logout()
    return ok({"message": "Session fermee"})


async def handle_devices(request: web.Request) -> web.Response:
    service: RoborockWebService = request.app["service"]
    try:
        return ok({"devices": await service.refresh_devices()})
    except Exception as exc:
        return fail(str(exc))


async def handle_status(request: web.Request) -> web.Response:
    service: RoborockWebService = request.app["service"]
    device_id = str(request.query.get("device_id", "")).strip()
    try:
        return ok({"status": await service.status(device_id)})
    except Exception as exc:
        return fail(str(exc))


async def handle_command(request: web.Request) -> web.Response:
    service: RoborockWebService = request.app["service"]
    payload = await read_json_body(request)
    device_id = str(payload.get("device_id", "")).strip()
    command = str(payload.get("command", "")).strip()
    try:
        result = await service.command(device_id=device_id, command=command)
        return ok({"result": result})
    except Exception as exc:
        return fail(str(exc))


async def handle_map(request: web.Request) -> web.Response:
    service: RoborockWebService = request.app["service"]
    device_id = str(request.query.get("device_id", "")).strip()
    try:
        data = await service.map_data(device_id)
        return ok({"map": data})
    except Exception as exc:
        return fail(str(exc))


def create_app() -> web.Application:
    app = web.Application()
    service = RoborockWebService()
    app["service"] = service

    app.router.add_get("/", handle_index)
    app.router.add_get("/api/state", handle_state)
    app.router.add_post("/api/send-code", handle_send_code)
    app.router.add_post("/api/login", handle_login)
    app.router.add_post("/api/logout", handle_logout)
    app.router.add_post("/api/devices/refresh", handle_devices)
    app.router.add_get("/api/status", handle_status)
    app.router.add_post("/api/command", handle_command)
    app.router.add_get("/api/map", handle_map)

    async def on_startup(_: web.Application) -> None:
        await service.startup()

    async def on_cleanup(_: web.Application) -> None:
        await service.shutdown()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Roborock local web app")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=8080, help="Bind port")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    web.run_app(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
