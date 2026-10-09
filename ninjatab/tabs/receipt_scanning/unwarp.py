"""UVDoc page unwarping, run in-process with cv2.dnn: no extra dependency.

PaddleX's UVDoc resizes the page to 712x488, runs a CNN that predicts a
backward map (where each output pixel comes from, 45x31 points in [-1, 1]),
resizes that map to full size and grid-samples the page. models/uvdoc.onnx is
only the CNN (exported by experiments/preprocess/export_uvdoc.py); both resizes
and the sampling are done here with cv2.remap on uint8, the same maths as
Paddle's align_corners bilinear resize and zero-padded grid_sample.

Pure cv2/numpy with no Django coupling, like deskew.py.

    python ninjatab/tabs/receipt_scanning/unwarp.py    # self-check
"""

import functools
import threading
from pathlib import Path

import cv2
import numpy as np

MODEL = Path(__file__).with_name("models") / "uvdoc.onnx"
NET_H, NET_W = 712, 488

# ponytail: global lock; the droplet has 1 vCPU and a cv2.dnn Net isn't
# thread-safe. A Net per thread if scans ever run on more cores.
_lock = threading.Lock()


@functools.cache
def _net() -> cv2.dnn.Net:
    # Loaded on first scan, not at boot: ~32 MB per gunicorn worker.
    return cv2.dnn.readNetFromONNX(str(MODEL))


# Both resizes are bilinear with align_corners=True (corner samples land on
# corner pixels), done exactly. cv2.resize is half-pixel, and cv2.remap snaps
# positions to 1/32 px - a few pixels once the 45x31 map is stretched to a
# photo, which measured 12 grey levels off Paddle's output.

def _ac(n_src: int, n_dst: int) -> tuple[np.ndarray, np.ndarray]:
    """For each output index: the source index below it and the weight of the one above."""
    pos = np.linspace(0, n_src - 1, n_dst)
    i0 = np.minimum(pos.astype(np.int64), n_src - 2)
    return i0, (pos - i0).astype(np.float32)


def _ac_matrix(n_src: int, n_dst: int) -> np.ndarray:
    i0, f = _ac(n_src, n_dst)
    m = np.zeros((n_dst, n_src), np.float32)
    m[np.arange(n_dst), i0] = 1 - f
    m[np.arange(n_dst), i0 + 1] = f
    return m


def _downscale(image: np.ndarray) -> np.ndarray:
    """uint8 HxWx3 -> float32 1x3xNET_HxNET_W in [0, 1]. Gathers the two
    neighbours per row, then per column: the output is small, the input isn't."""
    i, f = _ac(image.shape[0], NET_H)
    f = f[:, None, None]
    rows = image[i] * (1 - f) + image[i + 1] * f
    j, g = _ac(image.shape[1], NET_W)
    g = g[None, :, None]
    small = rows[:, j] * (1 - g) + rows[:, j + 1] * g
    return (small / 255.0).transpose(2, 0, 1)[None].astype(np.float32)


def unwarp_image(image: np.ndarray) -> np.ndarray:
    """Unwarp a BGR uint8 page. Same size out; pixels mapped from outside the
    page come out black, as in Paddle."""
    h, w = image.shape[:2]
    with _lock:
        net = _net()
        net.setInput(_downscale(image))
        bm = net.forward()[0]  # (2, 45, 31): x then y, in [-1, 1]
    # Upsample the map: two small matrix products per channel.
    my, mx = _ac_matrix(bm.shape[1], h), _ac_matrix(bm.shape[2], w).T
    map_x = my @ bm[0] @ mx
    map_x += 1
    map_x *= (w - 1) / 2
    map_y = my @ bm[1] @ mx
    map_y += 1
    map_y *= (h - 1) / 2
    return cv2.remap(image, map_x, map_y, cv2.INTER_LINEAR,
                     borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0))


def _demo() -> None:
    # A flat page of text lines must come back the same
    # size, still mostly white paper, with the text still there.
    page = np.full((1200, 800, 3), 255, np.uint8)
    for y in range(100, 1100, 50):
        cv2.putText(page, "ITEM 12.50", (80, y), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 0), 2)
    out = unwarp_image(page)
    assert out.shape == page.shape and out.dtype == np.uint8, out.shape
    assert out.mean() > 200, out.mean()
    assert (out < 128).mean() > 0.01, "text lost"
    # align_corners: corners exact, and a linear ramp stays linear.
    ramp = np.arange(12, dtype=np.float32).reshape(3, 4)
    up = _ac_matrix(3, 5) @ ramp @ _ac_matrix(4, 7).T
    assert up[0, 0] == 0 and up[-1, -1] == 11 and up[0, -1] == 3, up
    assert np.allclose(up[2, 1], 4 + 0.5), up
    print("unwarp ok")


if __name__ == "__main__":
    _demo()
