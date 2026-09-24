"""Experimental strategy: locally hosted PaddleOCR-VL instead of Mistral.

Two calls, both to local OpenAI-compatible endpoints, no deskew, no splitting,
no consensus. Kept in its own module so the experiment deletes in one `rm`.

PaddleOCR-VL is an element-level recognition model with fixed task prompts
("OCR:", "Table Recognition:", ...) and no instruction following, so it can
transcribe the receipt but cannot emit the annotation. A text-only model turns
its transcript into the annotation using the production extraction prompt
verbatim — which is what makes this a like-for-like test of the OCR half.

    ~/.venvs/paddleocr-vl/bin/mlx_vlm.server --port 8111

Structuring goes to Mistral's chat API (OpenAI-compatible, reuses
MISTRAL_API_KEY) so only the OCR half is swapped; override
PADDLEOCR_VL_STRUCTURE_BASE_URL/_MODEL to run that locally too.
"""

import json
import logging

import requests
from django.conf import settings
from django.utils import timezone

from .base import ReceiptScanStrategy, ScanContext
from .schema import _Document
from .sources import data_url_ref

logger = logging.getLogger("app")

# The model's own text-recognition task prompt; it accepts no other phrasing.
OCR_TASK_PROMPT = "OCR:"


# ponytail: plain POST rather than the openai SDK — two calls, one endpoint shape.
def _chat(base_url: str, api_key: str, model: str, content: list, **extra) -> dict:
    response = requests.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {api_key or 'none'}"},
        json={
            "model": model,
            "temperature": 0,
            "max_tokens": settings.PADDLEOCR_VL_MAX_TOKENS,
            "messages": [{"role": "user", "content": content}],
            **extra,
        },
        timeout=settings.PADDLEOCR_VL_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def _message_text(raw_response: dict) -> str:
    text = (raw_response["choices"][0]["message"]["content"] or "").strip()
    # Guided decoding should make this unnecessary; models fence anyway.
    if text.startswith("```"):
        text = text.split("```")[1].removeprefix("json").strip()
    return text


def transcribe(image_url: str, model: str) -> tuple[str, dict]:
    """`OCR:` against the local PaddleOCR-VL server. Returns (transcript, raw)."""
    raw = _chat(
        settings.PADDLEOCR_VL_BASE_URL, settings.PADDLEOCR_VL_API_KEY, model,
        [{"type": "image_url", "image_url": {"url": image_url}},
         {"type": "text", "text": OCR_TASK_PROMPT}],
    )
    return _message_text(raw), raw


def structure(transcript: str, prompt: str) -> tuple[str, dict]:
    """Text-only extraction over the transcript, using the production prompt."""
    raw = _chat(
        settings.PADDLEOCR_VL_STRUCTURE_BASE_URL,
        settings.MISTRAL_API_KEY,
        settings.PADDLEOCR_VL_STRUCTURE_MODEL,
        [{"type": "text", "text": f"{prompt}\n\nReceipt transcript:\n{transcript}"}],
        # vLLM guided decoding; a server without it answers 400 loudly.
        response_format={"type": "json_schema", "json_schema": {
            "name": "receipt", "schema": _Document.model_json_schema(),
        }},
    )
    return _message_text(raw), raw


def run_paddle_ocr(image_url: str, prompt: str, model: str) -> dict:
    """Transcribe then structure. Returns the same dict shape as
    `base.run_single_ocr`, so post-processing, replay and the capture format
    all work unchanged."""
    started = timezone.now()
    transcript, ocr_raw = transcribe(image_url, model)
    # No transcript, nothing to extract - skip the (billed) structuring call.
    text, structure_raw = structure(transcript, prompt) if transcript else ("", None)
    call_ms = int((timezone.now() - started).total_seconds() * 1000)

    annotation, parse_error = None, False
    try:
        annotation = json.loads(text) if text else None
    except json.JSONDecodeError as e:
        logger.warning("PaddleOCR-VL structuring returned malformed JSON: %s", e)
        parse_error = True

    return {
        "annotation": annotation,
        "parse_error": parse_error,
        "ocr_markdown": transcript,
        "ocr_pages": 1,
        "ocr_markdown_chars": len(transcript),
        "call_ms": call_ms,
        "finish_reason": ocr_raw["choices"][0].get("finish_reason"),
        "raw_response": {"ocr": ocr_raw, "transcript": transcript,
                         "structure": structure_raw},
    }


class PaddleOcrVlStrategy(ReceiptScanStrategy):
    """Local PaddleOCR-VL transcript -> local text model -> standard post-processing."""

    name = "paddleocr_vl"
    model = "PaddlePaddle/PaddleOCR-VL-1.6"
    deskew = False  # experiment is deliberately raw: no preprocessing at all

    def pre_process(self, ctx: ScanContext) -> list[str]:
        # A local server has no business fetching our presigned S3 URLs.
        return [data_url_ref(ctx)]

    def call_mistral(self, prepared: list[str], ctx: ScanContext) -> list[dict]:
        return [run_paddle_ocr(prepared[0], self.prompt, self.model)]
