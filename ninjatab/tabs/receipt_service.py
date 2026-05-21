import json
import logging
import re
import uuid
from datetime import datetime
from itertools import combinations
from typing import Optional

import boto3
from django.conf import settings
from django.db.models import F
from django.utils import timezone
from pydantic import BaseModel
from mistralai.client import Mistral
from mistralai.client.models import ImageURLChunk
from mistralai.extra import response_format_from_pydantic_model

import sentry_sdk

logger = logging.getLogger("app")

MAX_SCANS_PER_TAB = 150

ALLOWED_IMAGE_TYPES = {
    "image/jpeg", "image/png", "image/webp",
    "image/heic", "image/heif", "application/octet-stream",
}
MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB


# Monetary amounts are typed as `str` rather than `float` to prevent the
# constrained JSON decoder from runaway-sampling decimal digits and truncating
# the response. We coerce to float on the server.
class _Item(BaseModel):
    name: str
    translated_name: str
    total: str
    quantity: Optional[int] = None
    price_per_quantity: Optional[str] = None


class _OtherCharge(BaseModel):
    name: str
    translated_name: str
    amount: str


class _Document(BaseModel):
    receipt_language: str
    receipt_language_code: Optional[str] = None
    items: list[_Item]
    receipt_total: Optional[str] = None
    items_total: Optional[str] = None
    receipt_establishment_name: Optional[str] = None
    currency_code: Optional[str] = None
    datetime_of_receipt: Optional[str] = None
    tax: Optional[str] = None
    tip: Optional[str] = None
    service_charge: Optional[str] = None
    other_charges: Optional[list[_OtherCharge]] = None

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

Extract all purchased goods or services that contribute to the receipt total into items.
Do not include receipt-level charges such as tax, tip, service charge, or other fees/discounts in items - those are captured separately below.

For each item:
- name: the item name exactly as it appears on the receipt, in its original language
- translated_name: the English translation of the item name
  - Always attempt a translation when the item is not already in English, even if the original text is abbreviated, partially illegible, or you have to make your best guess from context (cuisine type, common menu items, surrounding items, the establishment name)
  - For abbreviated item names (e.g. "BIRRA DIAMOND GRAN", "ANT. PIEVE VECCHIA"), expand and translate the likely full meaning ("Diamond beer (large)", "Antipasto Pieve Vecchia")
  - Only fall back to copying the original name verbatim if you genuinely cannot make any reasonable guess at the English meaning
  - If the item is already in English, set translated_name equal to name
  - Be aggressive here: the precision/conservatism rules that apply to amounts, items, and dates do NOT apply to translated_name - always produce a best-guess English translation rather than leaving it untranslated
- quantity, price_per_quantity, total: see below

Only include price_per_quantity and quantity if clearly on the receipt.
quantity: number of instanced of this item purchased. Set to 1 if it is not clear
price_per_quantity: the price of this item per quantity
total: the final price paid for that line item so quantity * price_per_quantity.

Preserve the receipt verbatim when the same item appears multiple times. If the receipt lists the same item as two or more separate rows (each with its own price), return them as two or more separate items in the output - do not merge them into a single item with a higher quantity. Only use quantity > 1 when the receipt itself shows a single row for that item with an explicit quantity multiplier.

Do not include subtotal, tax, VAT, tip, gratuity, service charge, payment method, change, balance, loyalty adjustments, discounts, or any other fees as items - even if they affect the grand total. These are captured separately below.

Extract receipt-level charges that affect the grand total into their dedicated fields:
- tax: total tax/VAT amount on the receipt, if shown
- tip: tip or gratuity amount, if shown
- service_charge: service charge amount, if shown
- other_charges: a list of any other receipt-level fees or discounts that affect the total but do not fit tax/tip/service_charge (for example: delivery fee, booking fee, cover charge, loyalty discount, voucher). Use a negative amount for discounts. Each entry should include name (as shown on the receipt), translated_name (English translation, or same value if already English), and amount.

Only populate these fields when the charge clearly affects the grand total. Leave them null if not present. Do not include line items in these fields, and do not include these charges in items.

Extract receipt_total as the final total charged on the receipt. If the receipt does not explicitly display a grand total, return null - do not calculate, sum, or otherwise invent a receipt_total from the items or charges.

Extract receipt_establishment_name as the merchant or establishment name shown on the receipt if available.

Extract currency_code in ISO 4217 format, for example GBP, EUR, USD.

Calculate items_total as the sum of all item totals. Report the honest sum even if it does not match receipt_total - do not adjust, add, or drop items to force the totals to agree.

Extract datetime_of_receipt from the receipt date/time.
- Return it as an ISO 8601 string when possible
- If the receipt provides only a partial date or ambiguous date/time that cannot be confidently converted to ISO 8601, return null
- If no receipt date/time is present, return null

All monetary amounts (total, price_per_quantity, receipt_total, items_total, tax, tip, service_charge, other_charges.amount) must be returned as decimal strings normalized to US locale formatting:
- Use a dot (".") as the decimal separator
- Do not include any thousands separators (no commas, no spaces, no dots between groups of digits)
- Use at most 2 decimal places
- Use a leading minus sign for negative amounts (discounts)

Examples: "3.50", "1234.56", "-1.20", "0.99".
Do not return values like "1,234.56", "1.234,56", "1 234,56", "20,00", or numbers with long decimal expansions, even if the receipt itself uses those formats. Convert from the receipt's local format to US format before returning.

Be precise and conservative about monetary amounts, quantities, dates, and which items contribute to the total. Do not invent prices or items that are not on the receipt. (Reminder: this conservatism does not apply to translated_name - see the translation guidance above.)
"""
# - Only include items that clearly represent purchased goods or services or qualifying receipt-level charges


NON_CONTRIBUTING_KEYWORDS = (
    "tax", "vat", "gst", "hst", "pst",
    "fee", "fees", "charge", "charges", "surcharge",
    "tip", "tips", "gratuity",
    "subtotal", "sub total", "sub-total",
    "discount", "discounts", "voucher", "loyalty", "promo", "promotion",
    "rounding",
)

_NON_CONTRIBUTING_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(kw) for kw in NON_CONTRIBUTING_KEYWORDS) + r")\b",
    re.IGNORECASE,
)

ITEMS_TOTAL_TOLERANCE = 0.01


def _is_likely_non_contributing(name: Optional[str]) -> bool:
    return bool(name) and _NON_CONTRIBUTING_RE.search(name) is not None


def _normalize_amount_str(value):
    """Normalize an amount string to use '.' as the decimal separator and no
    thousands separators. Handles both '.'-decimal (US: 1,234.56) and
    ','-decimal (EU: 1.234,56) conventions, plus mixed/ambiguous cases.

    Rule: the rightmost of '.' or ',' is the decimal separator; the other is
    a thousands separator and is stripped. A lone separator followed by
    exactly 3 digits with no other separator is treated as a thousands
    separator (e.g. '1,234' or '1.500' → '1234')."""
    if not isinstance(value, str):
        return value
    s = value.strip()
    if not s:
        return value

    last_comma = s.rfind(",")
    last_dot = s.rfind(".")
    if last_comma == -1 and last_dot == -1:
        return s

    if last_comma > last_dot:
        decimal_sep, thousands_sep, decimal_pos = ",", ".", last_comma
    else:
        decimal_sep, thousands_sep, decimal_pos = ".", ",", last_dot

    frac = s[decimal_pos + 1:]
    # Lone separator + 3-digit "fraction" + no other separator → thousands.
    if (
        len(frac) == 3
        and frac.isdigit()
        and thousands_sep not in s
        and s.count(decimal_sep) == 1
    ):
        return s.replace(decimal_sep, "")

    integer = s[:decimal_pos].replace(thousands_sep, "").replace(decimal_sep, "")
    return f"{integer}.{frac}"


def _normalize_amounts_in_annotation(annotation: dict) -> None:
    """In-place: rewrite all amount strings on the annotation to use '.' as
    decimal separator so the mobile client parses them correctly."""
    for key in ("receipt_total", "items_total", "tax", "tip", "service_charge"):
        if key in annotation:
            annotation[key] = _normalize_amount_str(annotation[key])
    for item in annotation.get("items") or []:
        for key in ("total", "price_per_quantity"):
            if key in item:
                item[key] = _normalize_amount_str(item[key])
    for charge in annotation.get("other_charges") or []:
        if "amount" in charge:
            charge["amount"] = _normalize_amount_str(charge["amount"])


def _collapse_redundant_translations(annotation: dict) -> None:
    """If a translated_name equals the original name (case-insensitive), drop
    the translation and reuse the original to preserve its casing."""
    for item in annotation.get("items") or []:
        name = item.get("name")
        translated = item.get("translated_name")
        if name and translated and name.casefold() == translated.casefold():
            item["translated_name"] = name
    for charge in annotation.get("other_charges") or []:
        name = charge.get("name")
        translated = charge.get("translated_name")
        if name and translated and name.casefold() == translated.casefold():
            charge["translated_name"] = name


def _to_float(value) -> Optional[float]:
    """Coerce a Mistral-returned amount (now typed as string) into a float.
    Returns None on missing or unparseable input."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip().replace(",", "."))
    except (ValueError, TypeError):
        return None


def _items_sum(items: list[dict]) -> float:
    return round(sum(_to_float(i.get("total")) or 0 for i in items), 2)


def _reconcile_items_with_total(annotation: dict) -> list[dict]:
    """Try to make items_total match receipt_total by adjusting which
    tax/tip/fee-like rows are counted as items.

    - If items_total > receipt_total: try removing suspicious items already in
      the items list (matched on translated_name).
    - If items_total < receipt_total: try adding from the dedicated
      tax/tip/service_charge/other_charges fields back into items.

    Returns the (possibly unchanged) items list."""
    items: list[dict] = list(annotation.get("items") or [])
    receipt_total = _to_float(annotation.get("receipt_total"))
    if receipt_total is None:
        return items
    if not items and not _candidate_additions(annotation):
        return items

    diff = _items_sum(items) - receipt_total

    if abs(diff) < ITEMS_TOTAL_TOLERANCE:
        return items

    if diff > 0:
        suspicious = [
            i for i, it in enumerate(items)
            if _is_likely_non_contributing(it.get("translated_name"))
        ]
        for r in range(1, len(suspicious) + 1):
            for combo in combinations(suspicious, r):
                drop = set(combo)
                kept = [it for i, it in enumerate(items) if i not in drop]
                if abs(_items_sum(kept) - receipt_total) < ITEMS_TOTAL_TOLERANCE:
                    return kept
        return items

    additions = _candidate_additions(annotation)
    if not additions:
        return items
    idxs = list(range(len(additions)))
    for r in range(1, len(additions) + 1):
        for combo in combinations(idxs, r):
            extra = [additions[i] for i in combo]
            if abs(_items_sum(items + extra) - receipt_total) < ITEMS_TOTAL_TOLERANCE:
                return items + extra
    return items


def _candidate_additions(annotation: dict) -> list[dict]:
    """Build a list of item-shaped dicts from the dedicated charge fields,
    used when items_total is below receipt_total and we suspect a contributing
    charge was misrouted into tax/tip/service_charge/other_charges."""
    out: list[dict] = []
    for key, label in (("tax", "Tax"), ("tip", "Tip"), ("service_charge", "Service charge")):
        amount = _to_float(annotation.get(key))
        if amount is not None:
            out.append({"name": label, "translated_name": label, "total": amount})
    for charge in annotation.get("other_charges") or []:
        amount = _to_float(charge.get("amount"))
        if amount is None:
            continue
        out.append({
            "name": charge.get("name") or "Charge",
            "translated_name": charge.get("translated_name") or charge.get("name") or "Charge",
            "total": amount,
        })
    return out


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
        timeout_ms=30_000,
    )

    # Extract annotation from response (top-level field, JSON string).
    # If Mistral returns malformed JSON (e.g. token-budget exhausted mid-number,
    # truncated structure), capture to Sentry and fall through with no
    # annotation so the client can show manual entry.
    annotation = None
    raw = response.document_annotation
    if raw and isinstance(raw, str) and not raw.startswith("~?~"):
        try:
            annotation = json.loads(raw)
        except json.JSONDecodeError as e:
            sentry_sdk.capture_exception(e, contexts={
                "mistral_ocr": {
                    "tab_id": tab_id,
                    "image_key": image_key,
                    "raw_length": len(raw),
                    "raw_preview": raw[:500],
                },
            })
            logger.warning(
                "Mistral OCR returned malformed JSON for tab %s: %s",
                tab_id, e,
            )
            annotation = None

    # Normalize amount strings to '.' decimal separator (Mistral may echo
    # locale-specific commas, e.g. "20,00" on Italian receipts), then compute
    # items_total and attempt to reconcile items with receipt_total.
    if annotation:
        _normalize_amounts_in_annotation(annotation)
        annotation["ai_items_total"] = annotation.pop("items_total", None)
        annotation["items"] = _reconcile_items_with_total(annotation)
        annotation["items_total"] = _items_sum(annotation.get("items") or [])
        _collapse_redundant_translations(annotation)

    logger.info(
        "Mistral OCR response for tab %s: %s | annotation: %s",
        tab_id,
        response.model_dump_json(),
        json.dumps(annotation),
    )

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
