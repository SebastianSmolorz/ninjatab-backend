import json
import logging
import uuid
from datetime import datetime
from typing import Optional

import boto3
from django.conf import settings
from django.db.models import F
from django.utils import timezone
from pydantic import BaseModel
from mistralai import Mistral, ImageURLChunk
from mistralai.extra import response_format_from_pydantic_model

import sentry_sdk

logger = logging.getLogger("app")

MAX_SCANS_PER_TAB = 150

ALLOWED_IMAGE_TYPES = {
    "image/jpeg", "image/png", "image/webp",
    "image/heic", "image/heif", "application/octet-stream",
}
MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB


class _Item(BaseModel):
    name: str
    translated_name: str
    total: Optional[float] = None
    quantity: Optional[int] = None
    price_per_quantity: Optional[float] = None


class _Document(BaseModel):
    receipt_language: str
    receipt_language_code: Optional[str] = None
    items: list[_Item]
    receipt_total: float
    items_total: float
    receipt_establishment_name: Optional[str] = None
    currency_code: Optional[str] = None
    datetime_of_receipt: Optional[str] = None

# Each item must include:
# - name: the item name exactly as shown on the receipt
# - translated_name: the English translation of the item name
#   - if the item name is already in English, set translated_name equal to name
# - quantity: number of items of this item bought
# - price_per_quantity: the price of this item per quantity
# - total: the final price paid for that line item so quantity * price_per_quantity
#
# 3. If a discount clearly applies to a specific line item, subtract it from that item total.
#
# 4. If there is a receipt-level service charge, gratuity, tip, or other mandatory fee that contributes to the final total, include it as a line item in items.
# - Use the charge label as shown on the receipt for name
# - Use the English translation for translated_name, or the same value if already English
# - Use the charge amount for total

DOCUMENT_ANNOTATION_PROMPT = """
Extract structured data from this receipt.
Detect and extract the receipt language into receipt_language.
- If the receipt is in English, set receipt_language to "English".
- Otherwise set it to the detected language name.

Extract all items which contribute to the receipt total into items.
Include all purchasable items as well as any service charges if they contribute to the receipt total. 
Only include price_per_quantity and quantity if clearly on the receipt. 
quantity: number of instanced of this item purchased. Set to 1 if it is not clear
price_per_quantity: the price of this item per quantity
total: the final price paid for that line item so quantity * price_per_quantity. If this is not obviously visible on the receipt leave it as null.

Do not include subtotal, tax, VAT, payment method, change, balance, or loyalty adjustments as items unless they clearly affect the grand total as a receipt-level charge described above.

Extract receipt_total as the final total charged on the receipt.

Extract receipt_establishment_name as the merchant or establishment name shown on the receipt if available.

Extract currency_code in ISO 4217 format, for example GBP, EUR, USD.

Calculate items_total which is the sum of the totals of all items which affect the total.
items_total should ideally match the receipt_total. If it does not, an item may be missing or have an incorrect total, or there may be a superfluous item.

Extract datetime_of_receipt from the receipt date/time.
- Return it as an ISO 8601 string when possible
- If the receipt provides only a partial date or ambiguous date/time that cannot be confidently converted to ISO 8601, return null
- If no receipt date/time is present, return null

Be precise and conservative.
- Do not invent values
"""
# - Only include items that clearly represent purchased goods or services or qualifying receipt-level charges


class ScanLimitExceeded(Exception):
    pass


def check_scan_limit(tab):
    """Check if tab has exceeded the receipt scan limit."""
    if tab.receipt_scan_count >= MAX_SCANS_PER_TAB:
        sentry_sdk.capture_message(
            f"Receipt scan limit reached for tab {tab.uuid} "
            f"({tab.receipt_scan_count} scans)",
            level="warning",
        )
        raise ScanLimitExceeded(
            f"Scan limit of {MAX_SCANS_PER_TAB} receipts per tab reached"
        )


def increment_scan_count(tab):
    """Increment the receipt scan count on the tab."""
    from ninjatab.tabs.models import Tab
    Tab.objects.filter(pk=tab.pk).update(receipt_scan_count=F('receipt_scan_count') + 1)


def validate_upload(file):
    """Validate file type and size. Raises ValueError on failure."""
    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise ValueError(
            f"Unsupported file type: {file.content_type}. "
            "Allowed: JPEG, PNG, WebP, HEIC"
        )
    if file.size > MAX_UPLOAD_SIZE:
        raise ValueError(
            f"File too large. Maximum size is "
            f"{MAX_UPLOAD_SIZE // (1024 * 1024)} MB"
        )


def _s3_client():
    return boto3.client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
    )


def upload_to_spaces(file, tab_id: str) -> str:
    """Upload file to S3-compatible storage (private) and return the object key."""
    ext = file.name.rsplit(".", 1)[-1] if "." in file.name else "jpg"
    key = f"receipts/{tab_id}/{uuid.uuid4()}.{ext}"

    _s3_client().upload_fileobj(
        file,
        settings.S3_BUCKET,
        key,
        ExtraArgs={"ACL": "private", "ContentType": file.content_type},
    )
    return key


def generate_presigned_url(key: str, expiry: int = 3600) -> str:
    """Generate a pre-signed URL for a private S3 object. Expires in `expiry` seconds."""
    return _s3_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.S3_BUCKET, "Key": key},
        ExpiresIn=expiry,
    )


def scan_receipt(image_key: str, tab_id: str) -> dict:
    """
    Run Mistral OCR on the image and return parsed annotation + date + presigned URL.
    Returns {"document_annotation": dict | None, "date": str, "image_url": str, "image_key": str}.
    """
    client = Mistral(api_key=settings.MISTRAL_API_KEY)
    image_url = generate_presigned_url(image_key)

    response = client.ocr.process(
        model="mistral-ocr-latest",
        document=ImageURLChunk(image_url=image_url),
        document_annotation_format=response_format_from_pydantic_model(_Document),
        document_annotation_prompt=DOCUMENT_ANNOTATION_PROMPT,
        include_image_base64=True,
    )

    logger.info(
        "Mistral OCR response for tab %s: %s",
        tab_id,
        response.model_dump_json(),
    )

    # Extract annotation from response (top-level field, JSON string)
    annotation = None
    raw = response.document_annotation
    if raw and isinstance(raw, str) and not raw.startswith("~?~"):
        annotation = json.loads(raw)

    # Parse date from annotation, default to today
    receipt_date = timezone.now().strftime("%Y-%m-%d")
    if annotation and annotation.get("datetime_of_receipt"):
        raw_dt = annotation["datetime_of_receipt"].strip()
        try:
            parsed = datetime.fromisoformat(raw_dt.replace("Z", "+00:00"))
            receipt_date = parsed.strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            try:
                from datetime import date as date_type
                parsed_date = date_type.fromisoformat(raw_dt[:10])
                receipt_date = parsed_date.isoformat()
            except (ValueError, TypeError):
                pass

    return {"document_annotation": annotation, "date": receipt_date, "image_url": image_url, "image_key": image_key}
