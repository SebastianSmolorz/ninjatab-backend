"""Image-reference helpers for strategies.

Strategies need a URL pointing at the receipt image to hand to the Mistral OCR
call. In production the image already lives in S3 (presigned URL); where S3 is
unavailable (e.g. local validation) we fall back to an inline base64 data URL.

Note: the Mistral OCR API does not dedupe identical images, so concurrent
strategies can safely reuse a single reference for all N requests - there is no
need to mint distinct URLs/keys per request (verified empirically)."""

import base64
import logging
import threading
import time

from django.conf import settings

from ninjatab.tabs.receipt_service import generate_presigned_url

from .base import ScanContext

logger = logging.getLogger("app")


def _s3_configured() -> bool:
    return bool(settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY)


def data_url_ref(ctx: ScanContext) -> str:
    b64 = base64.b64encode(ctx.image_bytes).decode()
    return f"data:{ctx.content_type};base64,{b64}"


def default_ref(ctx: ScanContext) -> str:
    """A single image reference: presigned S3 URL when available, else data URL."""
    if ctx.s3_base_key and _s3_configured():
        return generate_presigned_url(ctx.s3_base_key)
    return data_url_ref(ctx)


# Above this the processed images are skipped (the original goes 3x, as before).
# Peak memory grows with pixels: at 12 MP UVDoc adds ~245 MB and flat ~295 MB,
# one after the other, on a 2 GB box. App uploads are ~5 MP, phone photos 12.5.
MAX_PREPROCESS_MP = 13


MIX_SLOTS = ("original", "uvdoc", "flat")

# ponytail: one scan pre-processes at a time per gunicorn worker, so two
# overlapping 12 MP scans can't stack ~300 MB peaks each on the 2 GB box.
# Overlaps are rare, so the wait is usually 0; mix_wait_ms shows it. Allow 2
# (a Semaphore) if scans start queueing behind each other.
_mix_lock = threading.Lock()


def mix_refs(ctx: ScanContext) -> tuple[list[str], dict]:
    """(refs, metrics) for orig + UVDoc + flat, one call each - the best mix in
    specs/receipt-preprocessing-rnd.md. The original goes by default_ref; the
    processed images as inline JPEGs. One that can't be made (undecodable, too
    big, an error, or flat finding the rows already straight) is replaced by the
    original, as the experiment did.

    metrics: `preprocess_arms`, the image each call got (a fallen-back slot
    reads "original"); `mix_<slot>_ms`, the time making and encoding that
    image took (None when it wasn't attempted); `mix_wait_ms`, time spent
    queued behind another scan's pre-processing."""
    started = time.perf_counter()
    with _mix_lock:
        wait_ms = int((time.perf_counter() - started) * 1000)
        refs, metrics = _mix_refs(ctx)
    metrics["mix_wait_ms"] = wait_ms
    return refs, metrics


def _mix_refs(ctx: ScanContext) -> tuple[list[str], dict]:
    import cv2
    import numpy as np

    from .flatten import flatten
    from .unwarp import unwarp_image

    original = default_ref(ctx)
    refs, arms = [original], ["original"]
    metrics = {}
    image = cv2.imdecode(np.frombuffer(ctx.image_bytes, np.uint8), cv2.IMREAD_COLOR)
    usable = image is not None and image.shape[0] * image.shape[1] <= MAX_PREPROCESS_MP * 1e6
    for arm, make in (("uvdoc", unwarp_image), ("flat", lambda im: flatten(im)[0])):
        metrics[f"mix_{arm}_ms"] = None
        if not usable:
            refs.append(original)
            arms.append("original")
            continue
        started = time.perf_counter()
        out = None
        try:
            out = make(image)
        except Exception:
            logger.exception("Preprocess %s failed; sending the original", arm)
        ok = False
        if out is not None and out is not image:
            # q95, as the experiment's images were.
            ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 95])
        metrics[f"mix_{arm}_ms"] = int((time.perf_counter() - started) * 1000)
        if ok:
            refs.append(f"data:image/jpeg;base64,{base64.b64encode(buf).decode()}")
            arms.append(arm)
        else:
            refs.append(original)
            arms.append("original")
    metrics["preprocess_arms"] = arms
    return refs, metrics
