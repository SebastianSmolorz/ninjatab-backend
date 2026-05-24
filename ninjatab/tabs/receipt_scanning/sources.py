"""Image-reference helpers for strategies.

Strategies need a URL pointing at the receipt image to hand to the Mistral OCR
call. In production the image already lives in S3 (presigned URL); where S3 is
unavailable (e.g. local validation) we fall back to an inline base64 data URL.

Note: the Mistral OCR API does not dedupe identical images, so concurrent
strategies can safely reuse a single reference for all N requests - there is no
need to mint distinct URLs/keys per request (verified empirically)."""

import base64

from django.conf import settings

from ninjatab.tabs.receipt_service import generate_presigned_url

from .base import ScanContext


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
