"""Parsing check for the PaddleOCR-VL experiment: both HTTP calls are stubbed,
so what is exercised is transcript -> structuring -> the ocr-result dict shape
that post-processing, replay and the capture format depend on."""

import json
from unittest.mock import patch

from ninjatab.tabs.receipt_scanning.paddleocr_vl import OCR_TASK_PROMPT, run_paddle_ocr


class _Response:
    def __init__(self, content):
        self._content = content

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


def _run(transcript, structured):
    """Stub the two chat calls in order and record the request bodies."""
    sent = []

    def post(url, headers=None, json=None, timeout=None):
        sent.append(json)
        return _Response(transcript if len(sent) == 1 else structured)

    with patch("ninjatab.tabs.receipt_scanning.paddleocr_vl.requests.post", post):
        return run_paddle_ocr("data:image/jpeg;base64,x", "PROMPT", "vl"), sent


def test_transcribes_then_structures():
    result, sent = _run("Coke 3.00", json.dumps({"items": [{"name": "Coke", "total": "3.00"}]}))

    # The VL model gets the image and only its own task prompt.
    assert sent[0]["messages"][0]["content"][1]["text"] == OCR_TASK_PROMPT
    # The text model gets the production prompt plus the transcript, no image.
    assert "PROMPT" in sent[1]["messages"][0]["content"][0]["text"]
    assert "Coke 3.00" in sent[1]["messages"][0]["content"][0]["text"]
    assert all(part["type"] == "text" for part in sent[1]["messages"][0]["content"])

    assert result["annotation"]["items"][0]["total"] == "3.00"
    assert result["parse_error"] is False
    assert result["ocr_markdown"] == "Coke 3.00"
    assert result["ocr_markdown_chars"] == len("Coke 3.00")
    assert set(result) >= {"annotation", "parse_error", "ocr_markdown", "ocr_pages",
                           "ocr_markdown_chars", "call_ms"}


def test_strips_code_fence():
    result, _ = _run("Coke 3.00", '```json\n{"items": []}\n```')
    assert result["annotation"] == {"items": []}


def test_malformed_json_flags_rather_than_raises():
    result, _ = _run("Coke 3.00", "not json at all {")
    assert result["annotation"] is None
    assert result["parse_error"] is True
    # The transcript still survives, so a failed structuring is diagnosable.
    assert result["ocr_markdown"] == "Coke 3.00"
