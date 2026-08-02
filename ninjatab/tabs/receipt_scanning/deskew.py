"""Text-based image deskew.

`deskew_bytes` rotates a receipt photo so its text lines are horizontal, using a
projection-profile search over candidate angles.

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
    image: np.ndarray, *, method: str = "projection", limit: float = 15.0, step: float = 1.0
) -> float:
    """Detect the skew angle (degrees) of the text in a BGR image. Returns 0.0
    when no text-like pixels are found."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    binary = _binarize(gray)
    if not np.any(binary):
        return 0.0
    if method == "minarea":
        return _angle_minarea(binary)
    return _angle_projection(binary, limit, step)


def deskew_image(
    image: np.ndarray, *, method: str = "projection", limit: float = 15.0, step: float = 1.0
) -> tuple[np.ndarray, float]:
    """Deskew a BGR image array. Returns (deskewed_image, detected_angle). The
    image is returned unchanged when the detected skew is negligible."""
    angle = detect_angle(image, method=method, limit=limit, step=step)
    if abs(angle) < MIN_ANGLE:
        return image, angle
    return _rotate(image, angle), angle


def deskew_bytes(
    image_bytes: bytes, *, method: str = "projection", limit: float = 15.0, step: float = 1.0
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
