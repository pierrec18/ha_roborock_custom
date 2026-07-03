#!/usr/bin/env python3
"""Debug local carte + position Q10 (B01/ss07) avec python-roborock 5.23.x.

Se connecte au cloud Roborock avec le token de la config entry HA, attend le
push carte + trace du robot, sauvegarde les donnees brutes, et rend la carte
avec le trajet/position selon les 4 transformations candidates pour
identifier visuellement la bonne.

Usage:
    python map_debug.py <chemin_entry_data.json> [duree_attente_s]
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from roborock.data import UserData
from roborock.devices.device_manager import UserParams, create_device_manager

OUT_DIR = Path(__file__).parent / "map_debug_out"

PATH_COLOR = (30, 30, 30, 200)
ROBOT_FILL = (66, 165, 245, 255)
ROBOT_OUTLINE = (20, 20, 20, 255)


def transforms(grid_w: int, grid_h: int):
    half_w, half_h = grid_w / 2, grid_h / 2

    def make(ox, oy, flip):
        def f(x, y):
            gx, gy = x + ox, y + oy
            gy_img = (grid_h - 1 - gy) if flip else gy
            return gx, gy_img

        return f

    return {
        "corner_yflip": make(0, 0, True),
        "corner_ydirect": make(0, 0, False),
        "center_yflip": make(half_w, half_h, True),
        "center_ydirect": make(half_w, half_h, False),
    }


def render_candidates(image_png: bytes, grid_w: int, grid_h: int, path, robot) -> Image.Image:
    base = Image.open(io.BytesIO(image_png)).convert("RGBA")
    scale_x = base.width / grid_w
    scale_y = base.height / grid_h

    tiles = []
    for name, tf in transforms(grid_w, grid_h).items():
        tile = base.copy()
        draw = ImageDraw.Draw(tile)
        pixels = []
        inside = 0
        for x, y in path:
            gx, gy = tf(x, y)
            px, py = (gx + 0.5) * scale_x, (gy + 0.5) * scale_y
            pixels.append((px, py))
            if 0 <= px < tile.width and 0 <= py < tile.height:
                inside += 1
        if len(pixels) >= 2:
            draw.line(pixels, fill=PATH_COLOR, width=3)
        if robot is not None:
            gx, gy = tf(*robot)
            cx, cy = (gx + 0.5) * scale_x, (gy + 0.5) * scale_y
            r = 10
            draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=ROBOT_FILL, outline=ROBOT_OUTLINE, width=3)
        label = f"{name}  ({inside}/{len(path)} pts dans l'image)"
        draw.rectangle((0, 0, tile.width, 28), fill=(255, 255, 255, 230))
        draw.text((8, 6), label, fill=(0, 0, 0), font=ImageFont.load_default(size=18))
        tiles.append(tile)

    w, h = tiles[0].size
    grid = Image.new("RGBA", (w * 2 + 12, h * 2 + 12), (255, 255, 255, 255))
    for i, tile in enumerate(tiles):
        grid.paste(tile, ((i % 2) * (w + 12), (i // 2) * (h + 12)))
    return grid


async def main() -> int:
    entry_path = Path(sys.argv[1])
    wait_seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0
    start_clean = "--start-clean" in sys.argv
    min_points = 40
    entry = json.loads(entry_path.read_text())
    user_data = UserData.from_dict(entry["user_data"])

    OUT_DIR.mkdir(exist_ok=True)
    params = UserParams(
        username=entry["username"],
        user_data=user_data,
        base_url=entry.get("base_url"),
    )
    manager = await create_device_manager(params)
    started = False
    try:
        devices = await manager.get_devices()
        device = next(d for d in devices if d.b01_q10_properties is not None)
        props = device.b01_q10_properties
        print(f"Appareil: {device.name} ({device.product.model})")

        got_update = asyncio.Event()
        props.map.add_update_listener(got_update.set)

        if start_clean:
            print("Lancement d'un nettoyage de test (arret automatique apres capture)...")
            await props.vacuum.start_clean()
            started = True

        deadline = asyncio.get_event_loop().time() + wait_seconds
        while asyncio.get_event_loop().time() < deadline:
            await props.refresh()
            try:
                await asyncio.wait_for(got_update.wait(), timeout=8)
            except TimeoutError:
                pass
            got_update.clear()
            have_map = props.map.image_content is not None
            n_points = len(props.map.path)
            print(f"  carte={'OK' if have_map else '...'} trace={n_points} pts")
            if have_map and n_points >= (min_points if start_clean else 1):
                break

        if started:
            print("Arret du nettoyage et retour au dock...")
            try:
                await props.vacuum.stop_clean()
                await asyncio.sleep(2)
                await props.vacuum.return_to_dock()
            except Exception as err:  # noqa: BLE001
                print(f"!! Echec stop/dock: {err} — renvoyez-le au dock via l'appli si besoin")

        m = props.map
        if m.image_content is None:
            print("Pas de carte recue.")
            return 1

        img_data = m.map_data.image if m.map_data else None
        dims = getattr(img_data, "dimensions", None)
        grid_w = getattr(dims, "width", None)
        grid_h = getattr(dims, "height", None)
        summary = {
            "grid_width": grid_w,
            "grid_height": grid_h,
            "rooms": [{"id": r.id, "name": r.name, "pixel_value": r.pixel_value} for r in m.rooms],
            "path": [[p.x, p.y] for p in m.path],
            "robot_position": [m.robot_position.x, m.robot_position.y] if m.robot_position else None,
        }
        (OUT_DIR / "capture.json").write_text(json.dumps(summary, indent=1))
        (OUT_DIR / "map_plain.png").write_bytes(m.image_content)
        print(f"Grille {grid_w}x{grid_h}, {len(m.rooms)} pieces, {len(m.path)} points de trace")
        if m.path:
            xs = [p.x for p in m.path]
            ys = [p.y for p in m.path]
            print(f"Bornes trace: x=[{min(xs)}..{max(xs)}] y=[{min(ys)}..{max(ys)}]")
            candidates = render_candidates(
                m.image_content, grid_w, grid_h,
                [(p.x, p.y) for p in m.path],
                (m.robot_position.x, m.robot_position.y) if m.robot_position else None,
            )
            candidates.convert("RGB").save(OUT_DIR / "candidates.png")
            print(f"Rendu des 4 candidates: {OUT_DIR / 'candidates.png'}")
        else:
            print("Pas de trace (le robot n'emet sa position que pendant un nettoyage).")
        return 0
    finally:
        await manager.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
