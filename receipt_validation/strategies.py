from typing import TypedDict, Optional

from ninjatab.tabs.receipt_service import DOCUMENT_ANNOTATION_PROMPT


class Strategy(TypedDict):
    name: str
    description: str
    api: str
    prompt: str
    pre_process_strategy: Optional[str]
    post_process_strategy: Optional[str]


STRATEGIES: list[Strategy] = [
    {
        "name": "baseline_mistral_ocr",
        "description": (
            "Current production implementation: Mistral OCR with default annotation prompt"
        ),
        "api": "mistral_ocr",
        "prompt": DOCUMENT_ANNOTATION_PROMPT,
        "pre_process_strategy": None,
        "post_process_strategy": None,
    },
]

STRATEGIES_BY_NAME: dict[str, Strategy] = {s["name"]: s for s in STRATEGIES}
