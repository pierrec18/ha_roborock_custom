#!/usr/bin/env python3
"""Analyse de calibration trace->grille Q10 (B01/ss07).

Explore TOUTES les hypothèses de géométrie plausibles à partir d'une capture
(`map_debug_out/capture.json` + `map_plain.png`) :
- 8 orientations (4 rotations x miroir),
- balayage d'unité (cm par cellule de grille),
- pour chaque candidate : meilleur offset via corrélation FFT (tous les offsets
  d'un coup), score = fraction des points (trajet densifié) tombant sur du sol,
  et mesure d'AMBIGUÏTÉ (offsets quasi-équivalents, étendue spatiale, 2e pic).

Sorties dans map_debug_out/ :
- calib_report.json : classement complet
- calib_top<N>.png : rendus côte à côte des meilleures candidates

Usage:
    python map_calibrate.py [capture.json] [--top 6]
"""

from __future__ import annotations

import io
import json
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

OUT_DIR = Path(__file__).parent / "map_debug_out"

# Couleurs du rendu de B01Q10MapParser
OUTSIDE = (28, 30, 38)
WALL = (235, 235, 240)

# Les 8 orientations: (x, y) -> (gx, gy) avant mise a l'echelle
ORIENTATIONS = {
    "(x,y)": lambda x, y: (x, y),
    "(x,-y)": lambda x, y: (x, -y),
    "(-x,y)": lambda x, y: (-x, y),
    "(-x,-y)": lambda x, y: (-x, -y),
    "(y,x)": lambda x, y: (y, x),
    "(y,-x)": lambda x, y: (y, -x),      # = hypothese "A" v0.4.0
    "(-y,x)": lambda x, y: (-y, x),
    "(-y,-x)": lambda x, y: (-y, -x),
}

UNITS = [2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 7.5, 8.0, 9.0, 9.5, 10.0, 10.5, 11.0, 12.0, 12.5, 15.0, 20.0]


def densify(points: list[tuple[float, float]], step: float = 0.5) -> list[tuple[float, float]]:
    """Interpole le long du trajet (en unites de grille) tous les ~step cellules."""
    if len(points) < 2:
        return list(points)
    dense: list[tuple[float, float]] = []
    for (ax, ay), (bx, by) in zip(points, points[1:]):
        dist = math.hypot(bx - ax, by - ay)
        n = max(1, int(dist / step))
        for i in range(n):
            f = i / n
            dense.append((ax + (bx - ax) * f, ay + (by - ay) * f))
    dense.append(points[-1])
    return dense


def floor_mask(img: Image.Image, grid_w: int, grid_h: int) -> np.ndarray:
    """Masque booleen (grid_h x grid_w) des cellules 'sol' (piece, ni mur ni exterieur)."""
    small = img.convert("RGB").resize((grid_w, grid_h), Image.Resampling.NEAREST)
    arr = np.asarray(small)
    outside = np.all(arr == OUTSIDE, axis=-1)
    wall = np.all(arr == WALL, axis=-1)
    return ~(outside | wall)


def score_all_offsets(mask: np.ndarray, cells: np.ndarray) -> tuple[np.ndarray, tuple[int, int]]:
    """Corrélation FFT: score(dy, dx) = nb de points sur le sol pour l'offset (dx, dy).

    cells: (N, 2) coordonnees (gx, gy) flottantes des points densifies, sans offset.
    Retourne (grille de scores normalisee, (off_x0, off_y0) tel que
    score[j, i] correspond a l'offset (i + off_x0, j + off_y0)).
    """
    gh, gw = mask.shape
    cx = np.round(cells[:, 0]).astype(int)
    cy = np.round(cells[:, 1]).astype(int)
    # Histogramme des points, decale pour etre positif
    x0, y0 = cx.min(), cy.min()
    hw = cx.max() - x0 + 1
    hh = cy.max() - y0 + 1
    hist = np.zeros((hh, hw))
    np.add.at(hist, (cy - y0, cx - x0), 1.0)

    # Correlation pleine via FFT: out[j, i] = sum hist[l, k] * mask[l + j, k + i]
    # avec j in [-(hh-1) .. gh-1], i in [-(hw-1) .. gw-1]
    fh = np.fft.rfft2(mask, s=(gh + hh - 1, gw + hw - 1))
    fk = np.fft.rfft2(hist[::-1, ::-1], s=(gh + hh - 1, gw + hw - 1))
    corr = np.fft.irfft2(fh * fk, s=(gh + hh - 1, gw + hw - 1))
    # corr[j, i] correspond au decalage (i - (hw - 1), j - (hh - 1)) applique a hist
    # dans le repere du masque; l'offset reel inclut le decalage d'histogramme:
    # offset = (i - (hw - 1) - x0 ... ) -> voir off_x0/off_y0 ci-dessous.
    scores = corr / len(cells)
    off_x0 = int(-(hw - 1) - x0)
    off_y0 = int(-(hh - 1) - y0)
    return scores, (off_x0, off_y0)


def analyze_candidate(mask: np.ndarray, path_cm: list[tuple[float, float]], orient, unit: float) -> dict:
    base = [orient(x, y) for x, y in path_cm]
    base = [(gx / unit, gy / unit) for gx, gy in base]
    dense = densify(base)
    cells = np.array(dense)
    span_x = cells[:, 0].max() - cells[:, 0].min()
    span_y = cells[:, 1].max() - cells[:, 1].min()
    gh, gw = mask.shape
    if span_x > gw * 1.2 or span_y > gh * 1.2:
        return {"score": 0.0, "reason": "trajet plus grand que la carte"}
    if span_x < 3 and span_y < 3:
        return {"score": 0.0, "reason": "trajet degenere (unite trop grande)"}

    scores, (ox0, oy0) = score_all_offsets(mask, cells)
    j, i = np.unravel_index(np.argmax(scores), scores.shape)
    best_score = float(scores[j, i])
    best_offset = (int(i) + ox0, int(j) + oy0)

    # Ambiguite: offsets a >= 98% du max
    near = np.argwhere(scores >= best_score * 0.98)
    extent = 0.0
    if len(near) > 1:
        ys, xs = near[:, 0], near[:, 1]
        extent = float(math.hypot(xs.max() - xs.min(), ys.max() - ys.min()))
    # 2e pic hors rayon 5 du meilleur
    masked = scores.copy()
    jj, ii = np.mgrid[0:scores.shape[0], 0:scores.shape[1]]
    masked[(jj - j) ** 2 + (ii - i) ** 2 <= 25] = 0
    second_ratio = float(masked.max() / best_score) if best_score > 0 else 0.0

    return {
        "score": round(best_score, 4),
        "offset": best_offset,
        "ambiguity_count": int(len(near)),
        "ambiguity_extent": round(extent, 1),
        "second_peak_ratio": round(second_ratio, 3),
    }


def render_candidate(img: Image.Image, grid_w: int, grid_h: int, path_cm, orient, unit, offset, label) -> Image.Image:
    tile = img.convert("RGBA").copy()
    draw = ImageDraw.Draw(tile)
    sx, sy = tile.width / grid_w, tile.height / grid_h
    pts = []
    for x, y in path_cm:
        gx, gy = orient(x, y)
        pts.append(((gx / unit + offset[0] + 0.5) * sx, (gy / unit + offset[1] + 0.5) * sy))
    if len(pts) >= 2:
        draw.line(pts, fill=(30, 30, 30, 220), width=3)
    if pts:
        cx, cy = pts[-1]
        draw.ellipse((cx - 9, cy - 9, cx + 9, cy + 9), fill=(66, 165, 245, 255), outline=(0, 0, 0, 255), width=2)
        dx, dy = pts[0]
        draw.ellipse((dx - 7, dy - 7, dx + 7, dy + 7), fill=(76, 175, 80, 255), outline=(0, 0, 0, 255), width=2)
    draw.rectangle((0, 0, tile.width, 26), fill=(255, 255, 255, 235))
    draw.text((6, 5), label, fill=(0, 0, 0), font=ImageFont.load_default(size=16))
    return tile


def main() -> int:
    argv = sys.argv[1:]
    top_n = 6
    if "--top" in argv:
        i = argv.index("--top")
        top_n = int(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]
    args = [a for a in argv if not a.startswith("--")]
    capture_path = Path(args[0]) if args else OUT_DIR / "capture.json"

    cap = json.loads(capture_path.read_text())
    png = (capture_path.parent / "map_plain.png").read_bytes()
    grid_w, grid_h = cap["grid_width"], cap["grid_height"]
    path_cm = [tuple(p) for p in cap["path"]]
    print(f"Capture: {len(path_cm)} points, grille {grid_w}x{grid_h}")

    img = Image.open(io.BytesIO(png))
    mask = floor_mask(img, grid_w, grid_h)
    print(f"Sol: {mask.sum()} cellules / {mask.size}")

    results = []
    for oname, orient in ORIENTATIONS.items():
        for unit in UNITS:
            r = analyze_candidate(mask, path_cm, orient, unit)
            r.update({"orientation": oname, "unit": unit})
            results.append(r)
    # Les candidates a tres forte ambiguite sont des artefacts (trajet retreci
    # qui rentre n'importe ou): les ecarter du classement principal.
    for r in results:
        r["degenerate"] = r.get("ambiguity_count", 0) > 40 or r["score"] == 0
    results.sort(key=lambda r: (not r["degenerate"], r["score"], -r.get("ambiguity_count", 9999)), reverse=True)

    (OUT_DIR / "calib_report.json").write_text(json.dumps(results, indent=1))
    print(f"\n{'orient':10} {'unit':>5} {'score':>6} {'ambig#':>6} {'etend':>6} {'2e pic':>6}  offset")
    for r in results[:12]:
        if r["score"] == 0:
            continue
        print(f"{r['orientation']:10} {r['unit']:>5} {r['score']:>6.3f} "
              f"{r.get('ambiguity_count', '-'):>6} {r.get('ambiguity_extent', '-'):>6} "
              f"{r.get('second_peak_ratio', '-'):>6}  {r.get('offset')}")

    # Rendu cote a cote des top N
    tiles = []
    for r in [x for x in results if not x["degenerate"]][:top_n]:
        if r["score"] == 0 or "offset" not in r:
            continue
        label = (f"{r['orientation']} u={r['unit']} s={r['score']:.2f} "
                 f"amb={r['ambiguity_count']}/{r['ambiguity_extent']} 2e={r['second_peak_ratio']}")
        tiles.append(render_candidate(img, grid_w, grid_h, path_cm,
                                      ORIENTATIONS[r["orientation"]], r["unit"], r["offset"], label))
    if tiles:
        w, h = tiles[0].size
        cols = 2
        rows = (len(tiles) + cols - 1) // cols
        sheet = Image.new("RGBA", (w * cols + 12, (h + 6) * rows), (255, 255, 255, 255))
        for k, t in enumerate(tiles):
            sheet.paste(t, ((k % cols) * (w + 12), (k // cols) * (h + 6)))
        out = OUT_DIR / f"calib_top{len(tiles)}.png"
        sheet.convert("RGB").save(out)
        print(f"\nRendus: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
