import json
import logging
import uuid
from datetime import datetime
from typing import Optional

import boto3
from django.conf import settings
from pydantic import BaseModel
from mistralai import Mistral, DocumentURLChunk
from mistralai.extra import response_format_from_pydantic_model

logger = logging.getLogger("app")

ALLOWED_IMAGE_TYPES = {
    "image/jpeg", "image/png", "image/webp",
    "image/heic", "image/heif", "application/octet-stream",
}
MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB


class _Item(BaseModel):
    name: str
    translated_name: str
    total: float


class _Document(BaseModel):
    receipt_language: str
    receipt_language_code: Optional[str] = None
    items: list[_Item]
    receipt_total: float
    receipt_establishment_name: str
    currency_code: str
    datetime_of_receipt: Optional[str] = None


DOCUMENT_ANNOTATION_PROMPT = """
Extract structured data from this receipt.
Extraction rules:

1. Extract the receipt language into receipt_language.
- If the receipt is in English, set receipt_language to "English".
- Otherwise set it to the detected language name.

2. Extract all purchasable line items into items.
Each item must include:
- name: the item name exactly as shown on the receipt
- translated_name: the English translation of the item name
  - if the item name is already in English, set translated_name equal to name
- total: the final price paid for that line item

3. If a discount clearly applies to a specific line item, subtract it from that item total.

4. If there is a receipt-level service charge, gratuity, tip, or other mandatory fee that contributes to the final total, include it as a line item in items.
- Use the charge label as shown on the receipt for name
- Use the English translation for translated_name, or the same value if already English
- Use the charge amount for total

5. Do not include subtotal, tax, VAT, payment method, change, balance, or loyalty adjustments as items unless they clearly affect the grand total as a receipt-level charge described above.

6. Extract receipt_total as the final total charged on the receipt.

7. Extract receipt_establishment_name as the merchant or establishment name shown on the receipt.

8. Extract currency_code in ISO 4217 format, for example GBP, EUR, USD.

9. Extract datetime_of_receipt from the receipt date/time.
- Return it as an ISO 8601 string when possible
- If the receipt provides only a partial date or ambiguous date/time that cannot be confidently converted to ISO 8601, return null
- If no receipt date/time is present, return null

10. Be precise and conservative.
- Do not invent values
- Only include items that clearly represent purchased goods or services or qualifying receipt-level charges
"""


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


def upload_to_spaces(file, tab_id: str) -> str:
    """Upload file to S3-compatible storage and return its public URL."""
    ext = file.name.rsplit(".", 1)[-1] if "." in file.name else "jpg"
    key = f"receipts/{tab_id}/{uuid.uuid4()}.{ext}"

    s3 = boto3.client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
    )
    s3.upload_fileobj(
        file,
        settings.S3_BUCKET,
        key,
        ExtraArgs={"ACL": "public-read", "ContentType": file.content_type},
    )
    url = "https://tab-ninja-receipt-scans.lon1.digitaloceanspaces.com"
    return f"{url}/{key}"


def scan_receipt(image_url: str, tab_id: str) -> dict:
    """
    Run Mistral OCR on the image and return parsed annotation + date.
    Returns {"document_annotation": dict | None, "date": str}.
    """
    client = Mistral(api_key=settings.MISTRAL_API_KEY)

    response = client.ocr.process(
        model="mistral-ocr-latest",
        pages=list(range(8)),
        document=DocumentURLChunk(document_url=image_url),
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
    receipt_date = datetime.now().strftime("%Y-%m-%d")
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

    return {"document_annotation": annotation, "date": receipt_date}
