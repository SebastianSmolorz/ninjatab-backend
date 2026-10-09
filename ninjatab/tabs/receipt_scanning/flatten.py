"""`flat`: a minimal unwarp - straighten every text row along its own local slope.

The failure it targets is a row whose height drifts across the gap between
name and price, by enough to land the price on the neighbouring row. A global
rotation (deskew) fixes that only when the whole receipt shares one slope;
curl, fan-out and bows don't. UVDoc (unwarp.py) fixes those but crops 5-10%
top and bottom and once overcorrected a tilt; this is its partner in the
orig + UVDoc + flat scan (specs/receipt-preprocessing-rnd.md).

Uses only what deskew already finds - the text-line blobs - and no model:
  1. each blob gives a slope where it sits;
  2. a smooth slope field over the image: Gaussian-weighted average of nearby
     blobs, pulled to the median slope where blobs are sparse, refit once
     without the blobs it disagrees with (grain, logos);
  3. integrate the field along x from the text's middle column: how far each
     point of a row sits above or below that row's height in the middle;
  4. shift every column vertically by that amount (cv2.remap), on a canvas
     grown so no corner is pushed out of frame.
Rows whose ends drift less than GATE line heights are left alone, so straight
scans are untouched.

ponytail: vertical shift only, so a tilted image comes out sheared, not
rotated - its letters lean like italics. Mistral reads italics; add the
horizontal component if a case shows otherwise (the 21 deg Valeroso photo lost rows).

Pure cv2/numpy with no Django coupling, like deskew.py. Research harness:
experiments/preprocess/flatten.py.

    python -m ninjatab.tabs.receipt_scanning.flatten    # self-check
"""

import math

import cv2
import numpy as np

from . import deskew

GRID = 8           # working-copy pixels per slope-field cell
SIGMA_Y = 4.0      # line heights up/down one blob's slope reaches
SIGMA_X = 0.25     # share of the text width along the row it reaches
PRIOR = 1.0        # blob-weights pulling a sparse neighbourhood to the median slope
MAX_SLOPE = 0.58   # tan 30 deg: steeper is sideways text or not a text line
GATE = 0.5         # line heights of drift across a row below which nothing is done


def line_slopes(gray: np.ndarray):
    """Per text-line blob under MAX_SLOPE: (x, y, slope dy/dx, length) in
    deskew's working copy. Plus the median line height, the working copy's
    scale and the count of all line blobs, steep ones included."""
    rects, scale = deskew.detect_text_rects(gray)
    out = []
    for rect in rects:
        a, b, c, _ = cv2.boxPoints(rect)
        v = max(b - a, c - b, key=lambda e: math.hypot(*e))
        if v[0] < 0:
            v = -v
        if v[0] > 0 and abs(v[1]) <= MAX_SLOPE * v[0]:
            out.append((*rect[0], v[1] / v[0], max(rect[1])))
    height = float(np.median([min(r[1]) for r in rects])) if rects else 0.0
    return np.array(out, float).reshape(-1, 4), height, scale, len(rects)


def slope_field(pts: np.ndarray, shape: tuple, line_h: float) -> np.ndarray:
    """Slope at every GRID cell of a working copy of `shape`."""
    x, y, s, length = pts.T
    gy = (np.arange(-(-shape[0] // GRID)) + 0.5) * GRID
    gx = (np.arange(-(-shape[1] // GRID)) + 0.5) * GRID
    text_w = np.percentile(x + length / 2, 95) - np.percentile(x - length / 2, 5)
    weight = length / np.median(length)

    def fit(keep):
        # Separable Gaussian: field = sum_i wy*wx*w_i*s_i / sum_i wy*wx*w_i
        wy = np.exp(-0.5 * ((gy[:, None] - y[keep]) / (SIGMA_Y * line_h)) ** 2) * weight[keep]
        wx = np.exp(-0.5 * ((gx[:, None] - x[keep]) / (SIGMA_X * text_w)) ** 2)
        prior = np.median(s[keep])
        return ((wy * s[keep]) @ wx.T + PRIOR * prior) / (wy @ wx.T + PRIOR)

    field = fit(np.ones(len(s), bool))
    resid = np.abs(s - field[np.minimum(y // GRID, len(gy) - 1).astype(int),
                             np.minimum(x // GRID, len(gx) - 1).astype(int)])
    return fit(resid <= max(3 * 1.4826 * np.median(resid), math.tan(math.radians(1))))


def flatten(image: np.ndarray):
    """(flattened image or `image` itself, info). info["noop"] means `image`
    came back untouched; info["curves"] are row paths for drawing."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    pts, line_h, scale, n_rects = line_slopes(gray)
    info = {"blobs": len(pts), "noop": True, "drift_lh": 0.0, "median_deg": None}
    # Under 3/4 of line blobs near-horizontal: sideways text (Mistral reads it, a
    # shear would not help) or grain - 17400, a sideways receipt on a table, has 15/28.
    if len(pts) < max(deskew.MIN_VOTES, 0.75 * n_rects):
        return image, info
    h, w = image.shape[:2]
    field = slope_field(pts, (round(h * scale), round(w * scale)), line_h)
    # Height of each row relative to its height at the text's middle column.
    disp = np.cumsum(field, axis=1) * GRID
    disp -= disp[:, [min(int(np.median(pts[:, 0]) // GRID), disp.shape[1] - 1)]]

    # Drift = how far a row's ends part across the text, in line heights.
    x, y, _, length = pts.T
    def span(lo, hi, n):
        return slice(max(0, int(lo // GRID)), min(n, int(hi // GRID) + 1))
    cols = span(np.percentile(x - length / 2, 5), np.percentile(x + length / 2, 95), disp.shape[1])
    rows = span(np.percentile(y, 2), np.percentile(y, 98), disp.shape[0])
    inside = disp[rows, cols]
    info.update(drift_lh=round(float((inside.max(1) - inside.min(1)).max() / line_h), 2),
                median_deg=round(math.degrees(math.atan(np.median(pts[:, 2]))), 2))
    if info["drift_lh"] < GATE:
        return image, info

    full = cv2.resize(disp.astype(np.float32), (w, h), interpolation=cv2.INTER_LINEAR) / scale
    top, bottom = math.ceil(max(full.max(), 0)), math.ceil(max(-full.min(), 0))
    out_y = np.arange(h + top + bottom) - top
    map_y = (out_y[:, None] + full[np.clip(out_y, 0, h - 1)]).astype(np.float32)
    map_x = np.broadcast_to(np.arange(w, dtype=np.float32), map_y.shape)
    flat = cv2.remap(image, map_x, map_y, cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    xs = np.arange(cols.start, cols.stop) * GRID
    info.update(noop=False, top=top, curves=[
        [(int(cx / scale), int((r + 0.5) * GRID / scale + full[min(int((r + 0.5) * GRID / scale), h - 1), int(cx / scale)]))
         for cx in xs]
        for r in range(rows.start, rows.stop, max(1, int(2 * line_h // GRID)))])
    return flat, info


def _demo() -> None:
    """A synthetic receipt bent into a tilted smile comes out flat: before, its
    rows drift by about a line; after, flatten finds nothing left to fix."""
    page = deskew._synthetic_receipt(0.0)
    h, w = page.shape[:2]
    xx, yy = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    mid = 135  # the synthetic text spans x 40-230
    bend = 0.002 * (xx - mid) ** 2 + math.tan(math.radians(5)) * (xx - mid)
    warped = cv2.remap(page, xx, yy - bend, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    flat, before = flatten(warped)
    _, after = flatten(flat)
    print(f"warped: drift {before['drift_lh']} lines -> flattened: drift {after['drift_lh']} lines")
    assert not before["noop"] and before["drift_lh"] > 2 * GATE - 0.1, before
    assert after["noop"] and after["drift_lh"] < GATE, after
    print("flatten ok")


if __name__ == "__main__":
    _demo()
