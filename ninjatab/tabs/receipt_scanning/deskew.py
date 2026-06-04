"""Text-based image deskew.

Estimates the dominant skew angle from an image's text and rotates the image so
text lines are horizontal. Shared by the receipt-scan pipeline (as a pre-process
step) and the ``deskew_image`` management command.
"""

import logging

import cv2
import numpy as np

logger = logging.getLogger("app")


def _binarize(gray: np.ndarray) -> np.ndarray:
    """Return a binary image where text pixels are white (255) on black."""
    # Otsu threshold, inverted so dark text becomes the foreground.
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
    [-limit, +limit] degrees. Returns the angle that best aligns text rows."""
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
    return float(best_in(fine))


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
