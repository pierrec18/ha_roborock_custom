"""Tests du module de rendu carte (pur, sans Home Assistant).

Utilise une capture réelle du Q10 S5+ (tests/fixtures/): PNG rendu par la lib +
capture.json (pièces, trajet, position). Exécuter: pytest tests/ -q
"""

import importlib.util
import io
import json
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

FIXTURES = Path(__file__).parent / "fixtures"
MODULE = Path(__file__).parents[1] / "custom_components" / "roborock_custom" / "map_render.py"


def _load_map_render():
    spec = importlib.util.spec_from_file_location("map_render", MODULE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["map_render"] = mod  # requis pour que @dataclass résolve son module
    spec.loader.exec_module(mod)
    return mod


mr = _load_map_render()


@pytest.fixture
def capture():
    return json.loads((FIXTURES / "q10_capture.json").read_text())


@pytest.fixture
def base_png():
    return (FIXTURES / "q10_map.png").read_bytes()


def test_flip_is_vertical_mirror(base_png):
    flipped = mr.flipped_map_png(base_png)
    assert flipped[:8] == b"\x89PNG\r\n\x1a\n"
    orig = np.asarray(Image.open(io.BytesIO(base_png)).convert("RGB"))
    fl = np.asarray(Image.open(io.BytesIO(flipped)).convert("RGB"))
    assert np.array_equal(orig[::-1], fl)


def test_room_regions_found(base_png, capture):
    regions = mr.room_regions(base_png, capture["rooms"])
    assert len(regions) == len(capture["rooms"]) == 3
    ids = {r["id"] for r in regions}
    assert ids == {1, 2, 3}
    for r in regions:
        assert r["x1"] <= r["x"] <= r["x2"]
        assert r["y1"] <= r["y"] <= r["y2"]
        # centre dans les bornes de l'image
        assert 0 <= r["x"] and 0 <= r["y"]


def test_room3_is_on_the_right(base_png, capture):
    # Room3 (pièce du dock) doit être la plus à droite sur la carte retournée.
    regions = {r["id"]: r for r in mr.room_regions(base_png, capture["rooms"])}
    assert regions[3]["x"] > regions[1]["x"]  # pièce du dock à droite de la grande pièce


def test_overlay_off_without_calibration(base_png, capture):
    plain = mr.render_with_overlay(
        base_png, capture["grid_width"], capture["grid_height"],
        [(p[0], p[1]) for p in capture["path"]], tuple(capture["robot_position"]), None,
    )
    assert plain == mr.flipped_map_png(base_png)


def test_overlay_on_with_calibration_changes_image(base_png, capture):
    cal = mr.TraceCalibration(unit=12.5, off_x=187, off_y=124)
    over = mr.render_with_overlay(
        base_png, capture["grid_width"], capture["grid_height"],
        [(p[0], p[1]) for p in capture["path"]], tuple(capture["robot_position"]), cal,
    )
    assert over != mr.flipped_map_png(base_png)
    assert over[:8] == b"\x89PNG\r\n\x1a\n"


def test_calibration_from_options():
    assert mr.calibration_from_options(None) is None
    assert mr.calibration_from_options({}) is None
    assert mr.calibration_from_options({"map_calibration": {"unit": 0, "off_x": 1, "off_y": 1}}) is None
    cal = mr.calibration_from_options({"map_calibration": {"unit": 12.5, "off_x": 187, "off_y": 124}})
    assert cal is not None
    assert cal.unit == 12.5 and cal.sign_x == -1 and cal.sign_y == -1
    cal2 = mr.calibration_from_options(
        {"map_calibration": {"unit": 5, "off_x": 0, "off_y": 0, "sign_x": 1, "sign_y": 1}}
    )
    assert cal2.sign_x == 1 and cal2.sign_y == 1


def test_empty_path_returns_plain_map(base_png, capture):
    cal = mr.TraceCalibration(unit=12.5, off_x=187, off_y=124)
    out = mr.render_with_overlay(base_png, capture["grid_width"], capture["grid_height"], [], None, cal)
    assert out == mr.flipped_map_png(base_png)
