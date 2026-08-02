"""Text-based image deskew.

`deskew_bytes` rotates a receipt photo so its text lines are horizontal. The
default method groups characters into text-line blobs and takes the median of
their angles; `method="projection"` selects the older profile search, which is
slower and misses skew that the blobs find.

Pure cv2/numpy with no Django coupling, so the labeller imports it directly off
MAIN_BACKEND_DIR rather than shelling out.
"""

import logging

import cv2
import numpy as np

logger = logging.getLogger("app")


def _binarize(gray: np.ndarray) -> np.ndarray:
    """Return a binary image where text pixels are white (255) on black.

    Otsu threshold, inverted so dark text becomes the foreground.

    A local (adaptive) threshold was measured here and rejected. It is better on
    shadowed receipts - one where Otsu called 24.4% of the image ink and
    reported +14.9 deg of skew on a straight receipt reads 4.0% and 0.0 deg -
    but it also invents small rotations on clean images. Over the 30 cases whose
    angle it changed: item-total F1 0.9468 -> 0.9093, names 0.9654 -> 0.9023,
    4 cases better and 9 worse. The saturation guard below fixes the same
    catastrophes without that cost.
    """
    _, thresh = cv2.threshold(
        gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
    )
    return thresh


def _score_angle(binary: np.ndarray, angle: float) -> float:
    """Rotate the binary image by `angle` and score how well text lines line up
    horizontally. Higher variance of the row-sum profile == sharper lines."""
    h, w = binary.shape
    matrix = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    rotated = cv2.warpAffine(
        binary, matrix, (w, h), flags=cv2.INTER_NEAREST, borderValue=0
    )
    row_sums = np.sum(rotated, axis=1, dtype=np.float64)
    return float(np.var(row_sums))


def _angle_projection(binary: np.ndarray, limit: float, step: float) -> float:
    """Coarse-to-fine projection-profile search for the skew angle in
    [-limit, +limit] degrees. Returns the angle that best aligns text rows, or
    0.0 when no angle is convincingly better than leaving the image alone."""
    # Downscale for speed; angle estimation does not need full resolution.
    scale = 1000.0 / max(binary.shape)
    if scale < 1.0:
        small = cv2.resize(
            binary, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST
        )
    else:
        small = binary

    def best_in(angles):
        return max(angles, key=lambda a: _score_angle(small, a))

    coarse = np.arange(-limit, limit + step, step)
    coarse_best = best_in(coarse)
    fine = np.arange(coarse_best - step, coarse_best + step + step / 10, step / 10)
    best = float(best_in(fine))

    if abs(best) > limit - SATURATION_MARGIN:
        logger.info(
            "Deskew declined: angle search saturated at %.2f deg (limit %.1f), "
            "which means no peak was found rather than a large skew", best, limit,
        )
        return 0.0
    return best


def _angle_minarea(binary: np.ndarray) -> float:
    """Estimate skew from the minimum-area rectangle enclosing all text pixels.
    Faster than projection but unreliable on noisy/crumpled receipts."""
    coords = np.column_stack(np.where(binary > 0))
    if coords.size == 0:
        return 0.0
    # cv2.minAreaRect wants (x, y) points.
    angle = cv2.minAreaRect(coords[:, ::-1].astype(np.float32))[-1]
    if angle < -45:
        angle += 90
    elif angle > 45:
        angle -= 90
    return float(angle)


def _character_mask(binary: np.ndarray) -> tuple[np.ndarray, float]:
    """Keep only character-sized connected components. Returns (mask, median
    character height), or (input, 0.0) when there is nothing to measure.

    Everything on a receipt photo that is not text - wood grain, table edges,
    fingers, the paper outline itself - survives a raw aspect-ratio filter once
    it has been smeared horizontally, and votes on the angle. Characters are
    distinguishable before smearing: they cluster tightly around one height and
    are never much wider than they are tall.
    """
    count, labels, stats, _ = cv2.connectedComponentsWithStats(binary, 8)
    if count < 2:
        return binary, 0.0

    heights = stats[1:, cv2.CC_STAT_HEIGHT]
    widths = stats[1:, cv2.CC_STAT_WIDTH]
    plausible = heights[(heights >= 3) & (heights <= binary.shape[0] // 10)]
    if plausible.size < MIN_BLOBS:
        return binary, 0.0
    median_h = float(np.median(plausible))

    keep = (
        (heights >= max(3, 0.5 * median_h))
        & (heights <= 3 * median_h)
        & (widths <= 8 * median_h)  # drops rules, paper edges and grain streaks
    )
    mask = np.isin(labels, np.flatnonzero(keep) + 1).astype(np.uint8) * 255
    return mask, median_h


def text_rects(binary: np.ndarray) -> list:
    """Rotated rects (cv2.minAreaRect tuples) around text lines in a binary image.

    Characters are isolated, then glued into lines with a horizontal close whose
    width scales with the text itself - a fraction of the image width merges
    nothing on a receipt photographed small in frame, and merges whole columns
    on a close-up. Blobs too small or too square to be a line of text are dropped.
    """
    mask, median_h = _character_mask(binary)
    kernel_w = max(9, int(1.5 * median_h)) if median_h else max(9, binary.shape[1] // 40)
    closed = cv2.morphologyEx(
        mask,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_w, 3)),
    )
    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    rects = [cv2.minAreaRect(c) for c in contours]
    return [r for r in rects if min(r[1]) >= 6 and max(r[1]) >= 3 * min(r[1])]


def _paper_box(gray: np.ndarray) -> tuple | None:
    """Bounding box (x, y, w, h) of the receipt: the largest bright region.
    None when the largest one is implausible as paper, in which case the caller
    should use the whole frame."""
    small = cv2.resize(gray, None, fx=0.25, fy=0.25, interpolation=cv2.INTER_AREA)
    _, paper = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    paper = cv2.morphologyEx(paper, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    count, _, stats, _ = cv2.connectedComponentsWithStats(paper, 8)
    if count < 2:
        return None
    largest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    x, y, w, h, area = stats[largest]
    if not 0.05 < area / small.size < 0.95:
        return None
    return x * 4, y * 4, w * 4, h * 4


def detect_text_rects(gray: np.ndarray) -> tuple[list, float]:
    """Find text-line rects in a grayscale image. Returns (rects, scale), where
    the rects are in the coordinates of the internally downscaled copy and
    `scale` is the factor that was applied.

    Two fallbacks, both scoped to this function so they do not disturb the
    binarisation the rest of the pipeline was tuned against (see `_binarize`):

    - The whole frame is used when cropping to the paper finds too little text,
      which covers receipts held against a bright background.
    - A local threshold is used when the global one finds too little text. On a
      full-frame photo of a receipt lying on a dark table, Otsu splits paper from
      background rather than ink from paper and finds no text at all: 58 of the
      221 corpus images, 49 of which the local threshold recovers.
    """
    scale = min(1.0, 1000.0 / max(gray.shape))
    if scale < 1.0:
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)

    box = _paper_box(gray)
    if box:
        x, y, w, h = box
        cropped = gray[y:y + h, x:x + w]
        rects = _threshold_and_find(cropped)
        if len(rects) >= MIN_BLOBS:
            # Shift back into whole-frame coordinates.
            return [((cx + x, cy + y), size, angle) for (cx, cy), size, angle in rects], scale

    return _threshold_and_find(gray), scale


def _threshold_and_find(gray: np.ndarray) -> list:
    """Text-line rects from a grayscale image, global threshold then local."""
    rects = text_rects(_binarize(gray))
    if len(rects) < MIN_BLOBS:
        local = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 31, 15
        )
        local_rects = text_rects(local)
        if len(local_rects) > len(rects):
            rects = local_rects
    return rects


def _angle_blobs(gray: np.ndarray, limit: float) -> float:
    """Median skew of the text-line blobs, in degrees. 0.0 when none qualify."""
    rects, _ = detect_text_rects(gray)
    angles = []
    for rect in rects:
        # minAreaRect's angle range differs between OpenCV versions ([-90, 0) vs
        # (0, 90]); folding into (-45, 45] normalises both.
        angle = (rect[2] + 45) % 90 - 45
        if abs(angle) < limit:
            angles.append(angle)
    if len(angles) < MIN_VOTES:
        # A median over a handful of blobs is not a skew estimate. One corpus
        # photo of a straight receipt on jeans yields a single speck-sized blob
        # at +12.8 deg; refusing is the safe failure, as with SATURATION_MARGIN.
        logger.info("Deskew declined: only %d text-line blobs found", len(angles))
        return 0.0
    return float(np.median(angles))


def _rotate(image: np.ndarray, angle: float) -> np.ndarray:
    """Rotate `image` by `angle` degrees about its center, expanding the canvas
    so nothing is clipped. New corners are filled with white."""
    h, w = image.shape[:2]
    center = (w / 2, h / 2)
    matrix = cv2.getRotationMatrix2D(center, angle, 1.0)

    cos, sin = abs(matrix[0, 0]), abs(matrix[0, 1])
    new_w = int(h * sin + w * cos)
    new_h = int(h * cos + w * sin)
    matrix[0, 2] += (new_w - w) / 2
    matrix[1, 2] += (new_h - h) / 2

    return cv2.warpAffine(
        image,
        matrix,
        (new_w, new_h),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(255, 255, 255),
    )

# Below this angle (degrees) the rotation is not worth the resampling cost.
MIN_ANGLE = 0.05

# Fewer text-line blobs than this means the binarisation, or the crop, is the
# problem - retry before settling for what was found.
MIN_BLOBS = 8

# How many blobs must vote before their median is believed as a skew angle.
#
# A receipt photographed small in a textured frame - a long till roll on a wood
# floor - has characters too small to survive downscaling, while the floor's
# grain and plank seams do survive and vote as one. The tell is the count: real
# receipts yield a median of 28 votes across the corpus, whereas both images
# that came out badly rotated yielded 11. Declining costs a correction on ~20
# images, all of them under 5 degrees.
MIN_VOTES = 15

# How close to the edge of the search range an answer may land before it is
# treated as a failure rather than a detection.
#
# When the projection profile has no real peak - a heavy shadow binarised as
# text, say - the score climbs monotonically and the search simply runs to the
# wall, returning whatever the largest angle happens to be. Four receipts in the
# corpus did exactly that, three of them pinned at the arithmetic maximum of
# +16.10 deg, one at +14.90; all four were close to straight already. A genuine
# skew peaks somewhere inside the range instead.
#
# Refusing to rotate is the safe failure: the image is still readable, which is
# more than can be said for one rotated 16 degrees for no reason.
SATURATION_MARGIN = 2.0


def detect_angle(
    image: np.ndarray, *, method: str = "blobs", limit: float = 15.0, step: float = 1.0
) -> float:
    """Detect the skew angle (degrees) of the text in a BGR image. Returns 0.0
    when no text-like pixels are found."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    binary = _binarize(gray)
    if not np.any(binary):
        return 0.0
    if method == "minarea":
        return _angle_minarea(binary)
    if method == "blobs":
        return _angle_blobs(gray, limit)
    return _angle_projection(binary, limit, step)


def deskew_image(
    image: np.ndarray, *, method: str = "blobs", limit: float = 15.0, step: float = 1.0
) -> tuple[np.ndarray, float]:
    """Deskew a BGR image array. Returns (deskewed_image, detected_angle). The
    image is returned unchanged when the detected skew is negligible."""
    angle = detect_angle(image, method=method, limit=limit, step=step)
    if abs(angle) < MIN_ANGLE:
        return image, angle
    return _rotate(image, angle), angle


def deskew_bytes(
    image_bytes: bytes, *, method: str = "blobs", limit: float = 15.0, step: float = 1.0
) -> tuple[bytes, float]:
    """Deskew encoded image bytes, returning (jpeg_bytes, detected_angle).

    Best-effort: if the bytes cannot be decoded, no text is found, or re-encoding
    fails, the original bytes are returned with angle 0.0 (never raises)."""
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    image = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if image is None:
        logger.warning("Deskew skipped: could not decode image (%d bytes)", len(image_bytes))
        return image_bytes, 0.0

    deskewed, angle = deskew_image(image, method=method, limit=limit, step=step)
    if abs(angle) < MIN_ANGLE:
        return image_bytes, angle

    ok, buf = cv2.imencode(".jpg", deskewed)
    if not ok:
        logger.warning("Deskew skipped: re-encode failed")
        return image_bytes, 0.0
    return buf.tobytes(), angle


def _synthetic_receipt(skew: float) -> np.ndarray:
    """A white page of black text lines, rotated by `skew` degrees."""
    image = np.full((700, 500, 3), 255, np.uint8)
    for i, y in enumerate(range(60, 640, 40)):
        text = "ITEM %d" % i + " " * (i % 3) + "  12.50"
        cv2.putText(image, text, (40, y), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 2)
    return _rotate(image, skew) if skew else image


def demo():
    for truth in (0.0, 4.0, -7.0):
        image = _synthetic_receipt(truth)
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        rects = text_rects(_binarize(gray))
        assert len(rects) >= 10, f"{truth}: found only {len(rects)} text lines"

        found = detect_angle(image, method="blobs")
        # detect_angle returns the correcting rotation, so it undoes the skew.
        assert abs(found + truth) < 1.0, f"{truth}: blobs said {found:+.2f}"
        print(f"skew {truth:+.1f} -> {len(rects):2d} blocks, detected {found:+.2f}")
    print("ok")


if __name__ == "__main__":
    demo()
