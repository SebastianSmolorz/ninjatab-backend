"""Variant of the PaddleOCR-VL experiment that keeps the model's own layout stage.

`paddleocr_vl` calls the VLM bare, which drifts on multi-column receipts: the
price column desynced by one row on the first receipt tried. PaddleOCR-VL is
designed to run behind PP-DocLayoutV2, which crops each region and fixes the
reading order before the VLM sees anything. This strategy restores that, so the
two together isolate what the layout stage is worth.

Paddle needs its own venv (it does not co-install with the Django one), so
stage 1 is a subprocess rather than an HTTP call; it still reaches the same
mlx_vlm.server on :8111 for the VLM half. Stage 2 is unchanged.
"""

import json
import logging
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from .base import ScanContext
from .paddleocr_vl import PaddleOcrVlStrategy, structure

logger = logging.getLogger("app")

# Run the pipeline and print the merged markdown. Kept as a string because it
# executes in the paddle venv, which the Django process cannot import from.
_RUNNER = """
import sys
from paddleocr import PaddleOCRVL
pipeline = PaddleOCRVL(
    # Orientation classify + UVDoc unwarping. The YAML enables both inside the
    # DocPreprocessor sub-pipeline but leaves that sub-pipeline off; there is no
    # use_doc_preprocessor constructor arg, so ask for the two stages directly.
    use_doc_orientation_classify=True,
    use_doc_unwarping=True,
    vl_rec_backend="mlx-vlm-server",
    vl_rec_server_url=sys.argv[2],
    vl_rec_api_model_name=sys.argv[3],
)
import json
for page in pipeline.predict(sys.argv[1]):
    res = page.json["res"]
    print(json.dumps({
        "page": {"width": res.get("width"), "height": res.get("height")},
        # PP-DocLayoutV3's own blocks: label, box, reading order, text. Sent on
        # to the reasoning step instead of the flattened markdown, so the
        # geometry the layout stage computed is not thrown away.
        "blocks": [
            {"label": b.get("block_label"), "bbox": b.get("block_bbox"),
             "order": b.get("block_order"), "text": b.get("block_content")}
            for b in res.get("parsing_res_list") or []
        ],
    }, ensure_ascii=False))
    break
"""


def transcribe_with_layout(image_bytes: bytes, model: str) -> tuple[str, dict]:
    """PP-DocLayoutV2 + PaddleOCR-VL over the whole image, via the paddle venv.
    Returns (markdown, meta)."""
    with tempfile.TemporaryDirectory() as tmp:
        image_path = Path(tmp) / "receipt.jpg"
        image_path.write_bytes(image_bytes)
        proc = subprocess.run(
            [settings.PADDLEOCR_VL_PYTHON, "-c", _RUNNER, str(image_path),
             settings.PADDLEOCR_VL_SERVER_URL, model],
            capture_output=True, text=True, timeout=settings.PADDLEOCR_VL_TIMEOUT,
        )
    if proc.returncode != 0:
        raise RuntimeError(f"paddle pipeline failed: {proc.stderr[-2000:]}")
    return proc.stdout.strip(), {"stderr_tail": proc.stderr[-2000:]}


def _as_payload(blocks_json: str) -> str:
    """Render the layout blocks for the reasoning step: each block's label, box
    and text, in reading order. Falls back to the raw string if the pipeline
    printed something unparseable, so a bad page still gets a scan attempt."""
    try:
        doc = json.loads(blocks_json)
    except json.JSONDecodeError:
        return blocks_json
    blocks = [b for b in doc.get("blocks") or [] if (b.get("text") or "").strip()]
    if not blocks:
        return ""
    page = doc.get("page") or {}
    lines = [
        f"Page {page.get('width')}x{page.get('height')}. Layout blocks in reading "
        "order, each with its bounding box [x0, y0, x1, y1] in page pixels:"
    ]
    for b in blocks:
        lines.append(f"\n[{b.get('label')}] bbox={b.get('bbox')}\n{b.get('text')}")
    return "\n".join(lines)


def run_paddle_ocr_layout(image_bytes: bytes, prompt: str, model: str) -> dict:
    """Layout-aware transcribe, then the same Mistral structuring. Same dict
    shape as `base.run_single_ocr`."""
    started = timezone.now()
    blocks_json, meta = transcribe_with_layout(image_bytes, model)
    transcript = _as_payload(blocks_json)
    # No transcript, nothing to extract - skip the (billed) structuring call.
    text, structure_raw = structure(transcript, prompt) if transcript else ("", None)
    call_ms = int((timezone.now() - started).total_seconds() * 1000)

    annotation, parse_error = None, False
    try:
        annotation = json.loads(text) if text else None
    except json.JSONDecodeError as e:
        logger.warning("PaddleOCR-VL (layout) structuring returned malformed JSON: %s", e)
        parse_error = True

    return {
        "annotation": annotation,
        "parse_error": parse_error,
        "ocr_markdown": transcript,
        "ocr_pages": 1,
        "ocr_markdown_chars": len(transcript),
        "call_ms": call_ms,
        "raw_response": {"ocr": meta, "transcript": transcript,
                         "structure": structure_raw},
    }


class PaddleOcrVlLayoutStrategy(PaddleOcrVlStrategy):
    """PP-DocLayoutV2 -> PaddleOCR-VL -> Mistral structuring -> standard post-processing."""

    name = "paddleocr_vl_layout"

    def pre_process(self, ctx: ScanContext) -> list[str]:
        return [""]  # the pipeline reads bytes off disk, not a URL

    def call_mistral(self, prepared: list[str], ctx: ScanContext) -> list[dict]:
        return [run_paddle_ocr_layout(ctx.image_bytes, self.prompt, self.model)]
