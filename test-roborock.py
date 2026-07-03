#!/usr/bin/env python3

import argparse
import asyncio
import contextlib
import json
import os
import time
from pathlib import Path
from typing import Any

from roborock import RoborockCommand
from roborock.data.b01_q10.b01_q10_code_mappings import B01_Q10_DP
from roborock.devices.device import RoborockDevice
from roborock.devices.device_manager import UserParams, create_device_manager
from roborock.devices.rpc.b01_q10_channel import stream_decoded_responses
from roborock.exceptions import RoborockException, RoborockInvalidCode
from roborock.web_api import RoborockApiClient


AUTH_STATE_PATH = Path(".roborock-auth.json")
COMMANDS = ("start", "pause", "stop", "dock")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Script de test Roborock (python-roborock >= 4.x).",
    )
    parser.add_argument(
        "action",
        choices=["send-code", "debug-home", "b01-map-debug", "session", "list", "status", *COMMANDS],
        help="Action a executer.",
    )
    parser.add_argument(
        "--username",
        default=os.environ.get("ROBOROCK_USERNAME", "").strip(),
        help="Email Roborock (ou ROBOROCK_USERNAME).",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("ROBOROCK_PASSWORD", "").strip(),
        help="Mot de passe Roborock (ou ROBOROCK_PASSWORD).",
    )
    parser.add_argument(
        "--code",
        default=os.environ.get("ROBOROCK_CODE", "").strip(),
        help="Code OTP email Roborock (ou ROBOROCK_CODE).",
    )
    parser.add_argument(
        "--device",
        default="",
        help="Nom ou DUID du robot a utiliser.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Affiche le status en JSON brut.",
    )
    parser.add_argument(
        "--listen-seconds",
        type=int,
        default=12,
        help="Duree d'ecoute pour b01-map-debug.",
    )
    parser.add_argument(
        "--max-events",
        type=int,
        default=40,
        help="Nombre max d'evenements affiches pour b01-map-debug.",
    )
    parser.add_argument(
        "--deep-probe",
        action="store_true",
        help="Pour b01-map-debug: tente des variantes de requete MULTI_MAP avec map_id.",
    )
    parser.add_argument(
        "--probe-map-id",
        default="",
        help="Pour b01-map-debug: map_id specifique a sonder (sinon le premier detecte).",
    )
    return parser


def normalize_code(code: str) -> str:
    return "".join(code.split())


def require_username(username: str) -> None:
    if username:
        return
    raise SystemExit("Email manquant. Definis ROBOROCK_USERNAME ou passe --username.")


def require_auth(password: str, code: str) -> None:
    if password or code:
        return
    raise SystemExit(
        "Authentification incomplete. Definis ROBOROCK_PASSWORD/ROBOROCK_CODE "
        "ou passe --password/--code."
    )


def load_auth_state() -> dict[str, Any]:
    if not AUTH_STATE_PATH.exists():
        return {}
    return json.loads(AUTH_STATE_PATH.read_text())


def save_auth_state(state: dict[str, Any]) -> None:
    AUTH_STATE_PATH.write_text(json.dumps(state, indent=2))


def store_device_identifier(username: str, device_identifier: str) -> None:
    state = load_auth_state()
    state[username] = {"device_identifier": device_identifier}
    save_auth_state(state)


def get_stored_device_identifier(username: str) -> str | None:
    state = load_auth_state()
    user = state.get(username)
    if not isinstance(user, dict):
        return None
    value = user.get("device_identifier")
    if isinstance(value, str) and value:
        return value
    return None


async def request_new_code_and_read_input(web_api: RoborockApiClient) -> str:
    print("Demande d'envoi d'un nouveau code email...")
    await web_api.request_code()
    entered = normalize_code(input("Entre le code recu par email: ").strip())
    if not entered:
        raise SystemExit("Code vide.")
    return entered


async def login(web_api: RoborockApiClient, username: str, password: str, code: str):
    if code:
        device_identifier = get_stored_device_identifier(username)
        if not device_identifier:
            raise SystemExit(
                "Aucun identifiant 2FA sauvegarde.\n"
                "Lance d'abord: ./venv311/bin/python test-roborock.py send-code"
            )
        web_api._device_identifier = device_identifier
        print("Connexion au cloud Roborock avec code email...")
        try:
            return await web_api.code_login(normalize_code(code))
        except RoborockInvalidCode:
            print("Code invalide ou expire.")
            retry_code = await request_new_code_and_read_input(web_api)
            try:
                return await web_api.code_login(retry_code)
            except RoborockInvalidCode as exc:
                raise SystemExit("Code invalide une deuxieme fois.") from exc

    print("Connexion au cloud Roborock...")
    try:
        return await web_api.pass_login(password)
    except RoborockException as exc:
        message = str(exc)
        if "response code: 2031" in message:
            print("Validation en deux etapes requise.")
            otp_code = await request_new_code_and_read_input(web_api)
            return await web_api.code_login(otp_code)
        raise


def format_device(device: RoborockDevice) -> str:
    online = getattr(device.device_info, "online", None)
    pv = getattr(device.device_info, "pv", "unknown")
    return (
        f"- {device.name} | duid={device.duid} | model={device.product.model} | "
        f"pv={pv} | online={online}"
    )


def select_device(devices: list[RoborockDevice], selector: str) -> RoborockDevice:
    if not devices:
        raise SystemExit("Aucun appareil trouve.")

    if not selector:
        if len(devices) == 1:
            return devices[0]
        raise SystemExit(
            "Plusieurs appareils trouves. Relance avec --device.\n"
            + "\n".join(format_device(device) for device in devices)
        )

    lowered = selector.lower()
    matches = [d for d in devices if d.duid == selector or lowered in d.name.lower()]
    if not matches:
        raise SystemExit(
            "Aucun appareil ne correspond a --device.\n"
            + "\n".join(format_device(device) for device in devices)
        )
    if len(matches) > 1:
        raise SystemExit(
            "Plusieurs appareils correspondent a --device.\n"
            + "\n".join(format_device(device) for device in matches)
        )
    return matches[0]


def read_status_dict(device: RoborockDevice) -> dict[str, Any]:
    if device.v1_properties is not None:
        return device.v1_properties.status.as_dict()
    if device.b01_q10_properties is not None:
        return device.b01_q10_properties.status.as_dict()
    raise SystemExit("Statut non supporte pour ce type d'appareil.")


def format_status(status: dict[str, Any]) -> str:
    battery = status.get("battery")
    state = (
        status.get("state_name")
        or status.get("state")
        or status.get("status")
        or "inconnu"
    )
    clean_time = status.get("clean_time")
    clean_area = status.get("square_meter_clean_area")
    if clean_area is None and isinstance(status.get("clean_area"), (int, float)):
        clean_area = round(float(status["clean_area"]) / 1000000, 2)

    lines = [
        f"Etat: {state}",
        f"Batterie: {battery if battery is not None else 'inconnue'}%",
    ]
    if clean_area is not None:
        lines.append(f"Surface session: {clean_area} m2")
    if isinstance(clean_time, int) and clean_time > 0:
        lines.append(f"Duree session: {int(clean_time / 60)} min")
    if status.get("error_code_name"):
        lines.append(f"Erreur: {status['error_code_name']}")
    return "\n".join(lines)


async def refresh_status(device: RoborockDevice) -> None:
    if device.v1_properties is not None:
        await device.v1_properties.status.refresh()
        return
    if device.b01_q10_properties is not None:
        await device.b01_q10_properties.refresh()
        await asyncio.sleep(1.0)
        return
    raise SystemExit("Refresh status non supporte pour ce type d'appareil.")


async def execute_action(device: RoborockDevice, action: str) -> Any:
    if device.v1_properties is not None:
        mapping = {
            "start": RoborockCommand.APP_START,
            "pause": RoborockCommand.APP_PAUSE,
            "stop": RoborockCommand.APP_STOP,
            "dock": RoborockCommand.APP_CHARGE,
        }
        return await device.v1_properties.command.send(mapping[action])

    if device.b01_q10_properties is not None:
        vacuum = device.b01_q10_properties.vacuum
        if action == "start":
            return await vacuum.start_clean()
        if action == "pause":
            return await vacuum.pause_clean()
        if action == "stop":
            return await vacuum.stop_clean()
        if action == "dock":
            return await vacuum.return_to_dock()
    raise SystemExit("Action non supportee pour ce type d'appareil.")


def _summarize_value(value: Any, max_length: int = 220) -> str:
    try:
        rendered = json.dumps(value, ensure_ascii=False)
    except TypeError:
        rendered = repr(value)
    if len(rendered) <= max_length:
        return rendered
    return rendered[: max_length - 3] + "..."


def _format_b01_event(event: dict[B01_Q10_DP, Any]) -> str:
    parts: list[str] = []
    for dp, value in sorted(event.items(), key=lambda item: item[0].code):
        parts.append(f"{dp.name}({dp.code})={_summarize_value(value)}")
    return " | ".join(parts)


def _extract_map_ids_from_multimap_value(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return []
    data = value.get("data")
    if not isinstance(data, list):
        return []
    map_ids: list[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        map_id = item.get("id")
        if isinstance(map_id, str) and map_id:
            map_ids.append(map_id)
    return map_ids


def _build_multimap_probe_payloads(map_id: str) -> list[dict[str, Any]]:
    return [
        {"op": "list"},
        {"op": "get", "id": map_id},
        {"op": "detail", "id": map_id},
        {"op": "info", "id": map_id},
        {"op": "data", "id": map_id},
        {"id": map_id},
    ]


async def b01_map_debug(
    device: RoborockDevice,
    listen_seconds: int,
    max_events: int,
    deep_probe: bool,
    probe_map_id: str,
) -> None:
    if device.b01_q10_properties is None:
        raise SystemExit("b01-map-debug fonctionne uniquement pour les appareils B01/Q10.")
    if listen_seconds < 1:
        raise SystemExit("--listen-seconds doit etre >= 1")
    if max_events < 1:
        raise SystemExit("--max-events doit etre >= 1")

    command = device.b01_q10_properties.command
    channel = command._channel  # Internal API, used only for debug sniffing.

    stop_at = time.monotonic() + listen_seconds
    collected = 0
    observed_map_ids: set[str] = set()
    probe_started = False

    print(
        f"Ecoute B01 brute pendant {listen_seconds}s (max {max_events} events). "
        "Envoi de requetes map experimentales..."
    )
    requests: list[tuple[str, B01_Q10_DP, Any]] = [
        ("refresh all dps", B01_Q10_DP.REQUEST_DPS, {}),
        ("multi map", B01_Q10_DP.MULTI_MAP, {}),
        ("get carpet", B01_Q10_DP.GET_CARPET, {}),
        ("customer clean request", B01_Q10_DP.CUSTOMER_CLEAN_REQUEST, {}),
    ]

    async def _send_requests() -> None:
        for label, dp, params in requests:
            try:
                print(f"-> send {label}: {dp.name}({dp.code}) params={params}")
                await command.send(dp, params=params)
            except Exception as exc:
                print(f"-> erreur envoi {dp.name}: {exc}")
            await asyncio.sleep(0.5)

    sender = asyncio.create_task(_send_requests())
    prober: asyncio.Task[None] | None = None
    try:
        async for decoded in stream_decoded_responses(channel):
            now = time.strftime("%H:%M:%S")
            if decoded:
                print(f"[{now}] {_format_b01_event(decoded)}")
                collected += 1

                multi_map_value = decoded.get(B01_Q10_DP.MULTI_MAP)
                for map_id in _extract_map_ids_from_multimap_value(multi_map_value):
                    observed_map_ids.add(map_id)

                if deep_probe and not probe_started and observed_map_ids:
                    target_map_id = probe_map_id or sorted(observed_map_ids)[0]
                    probe_payloads = _build_multimap_probe_payloads(target_map_id)

                    async def _run_probe() -> None:
                        print(f"Deep probe active sur map_id={target_map_id}")
                        for payload in probe_payloads:
                            try:
                                print(f"-> probe MULTI_MAP(61) params={payload}")
                                await command.send(B01_Q10_DP.MULTI_MAP, params=payload)
                            except Exception as exc:
                                print(f"-> erreur probe MULTI_MAP: {exc}")
                            await asyncio.sleep(0.5)

                    prober = asyncio.create_task(_run_probe())
                    probe_started = True

            if collected >= max_events or time.monotonic() >= stop_at:
                break
    finally:
        sender.cancel()
        if prober is not None:
            prober.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sender
        if prober is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await prober

    print(f"Capture terminee: {collected} event(s) recus.")
    if observed_map_ids:
        print("Map IDs detectes:", ", ".join(sorted(observed_map_ids)))
    if collected == 0:
        print(
            "Aucun event brut recu dans la fenetre d'ecoute. "
            "Reessaie en augmentant --listen-seconds (ex: 25)."
        )


def _resolve_target_device(
    devices: list[RoborockDevice], selector: str, current: RoborockDevice | None
) -> RoborockDevice | None:
    if selector:
        return select_device(devices, selector)
    return current


async def run_interactive_session(devices: list[RoborockDevice]) -> None:
    current = devices[0] if len(devices) == 1 else None
    print("Session active. Commandes: list, use <device>, status [device], start/pause/stop/dock [device], quit")
    if current is not None:
        print(f"Appareil courant: {current.name} ({current.product.model})")

    while True:
        try:
            raw = input("robo> ").strip()
        except EOFError:
            print()
            break
        if not raw:
            continue

        parts = raw.split(maxsplit=1)
        command = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if command in {"quit", "exit"}:
            break
        if command == "help":
            print("Commandes: list, use <device>, status [device], start/pause/stop/dock [device], quit")
            continue
        if command == "list":
            for device in devices:
                prefix = "* " if current is not None and device.duid == current.duid else "- "
                print(prefix + format_device(device))
            continue
        if command == "use":
            if not arg:
                print("Usage: use <nom-ou-duid>")
                continue
            current = select_device(devices, arg)
            print(f"Appareil courant: {current.name} ({current.product.model})")
            continue
        if command == "status":
            target = _resolve_target_device(devices, arg, current)
            if target is None:
                print("Choisis un appareil avec 'use <device>' ou passe un argument a status.")
                continue
            await refresh_status(target)
            print(format_status(read_status_dict(target)))
            continue
        if command in COMMANDS:
            target = _resolve_target_device(devices, arg, current)
            if target is None:
                print(f"Choisis un appareil avec 'use <device>' ou passe un argument a {command}.")
                continue
            response = await execute_action(target, command)
            print(f"Commande {command} envoyee a {target.name}.")
            print(f"Reponse: {response}")
            continue

        print("Commande inconnue. Tape 'help'.")


async def run_action(args: argparse.Namespace) -> None:
    require_username(args.username)

    web_api = RoborockApiClient(args.username)
    if args.action == "send-code":
        store_device_identifier(args.username, web_api._device_identifier)
        print("Demande d'envoi du code email Roborock...")
        await web_api.request_code()
        print("Code demande. Relance ensuite avec --code.")
        return

    require_auth(args.password, args.code)
    user_data = await login(web_api, args.username, args.password, args.code)
    print("Connecte au cloud Roborock.")

    if args.action == "debug-home":
        home_v1 = await web_api.get_home_data(user_data)
        home_v2 = await web_api.get_home_data_v2(user_data)
        home_v3 = await web_api.get_home_data_v3(user_data)
        print(
            f"Compte: uid={user_data.uid}, nickname={user_data.nickname}, "
            f"region={user_data.region}, country={user_data.country}"
        )
        print(
            f"home v1: devices={len(home_v1.get_all_devices())}, products={len(home_v1.products)}"
        )
        print(
            f"home v2: devices={len(home_v2.get_all_devices())}, products={len(home_v2.products)}"
        )
        print(
            f"home v3: devices={len(home_v3.get_all_devices())}, products={len(home_v3.products)}"
        )
        for device in home_v3.get_all_devices():
            product = home_v3.product_map.get(device.product_id)
            model = product.model if product is not None else "unknown"
            print(f"- {device.name} | duid={device.duid} | model={model} | pv={device.pv}")
        return

    manager = await create_device_manager(UserParams(username=args.username, user_data=user_data))
    try:
        devices = await manager.get_devices()
        if args.action == "session":
            if not devices:
                print("Connexion OK, mais aucun appareil Roborock n'est associe a ce compte.")
                return
            await run_interactive_session(devices)
            return

        if args.action == "list":
            if not devices:
                print("Connexion OK, mais aucun appareil Roborock n'est associe a ce compte.")
                return
            print(f"{len(devices)} appareil(s) trouve(s):")
            for device in devices:
                print(format_device(device))
            return

        device = select_device(devices, args.device)
        print(f"Robot selectionne: {device.name} ({device.product.model})")

        if args.action == "b01-map-debug":
            await b01_map_debug(
                device,
                listen_seconds=args.listen_seconds,
                max_events=args.max_events,
                deep_probe=args.deep_probe,
                probe_map_id=args.probe_map_id.strip(),
            )
            return

        if args.action == "status":
            await refresh_status(device)
            status = read_status_dict(device)
            if args.json:
                print(json.dumps(status, indent=2, ensure_ascii=False))
            else:
                print(format_status(status))
            return

        response = await execute_action(device, args.action)
        print(f"Commande {args.action} envoyee.")
        print(f"Reponse: {response}")
    finally:
        await manager.close()


def main() -> None:
    args = build_parser().parse_args()
    asyncio.run(run_action(args))


if __name__ == "__main__":
    main()
