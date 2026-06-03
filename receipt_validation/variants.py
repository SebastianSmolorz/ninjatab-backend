"""Configurable strategy variants for the validation harness.

A *variant* is a strategy instance with an optional per-instance ``name``,
``model``, ``prompt`` and ``version`` override (see
``ReceiptScanStrategy.__init__``). The defaults below are the four production
strategies, unchanged. Add variants with overrides to compare models/prompts —
each must have a unique ``name`` (it is the key the CLI filters on and the label
written to the CSV).

Production behaviour is untouched: ``scan_receipt`` resolves strategies from the
production registry, not from this list.
"""

from ninjatab.tabs.receipt_scanning.strategies import (
    BaselineStrategy,
    ConcurrentConsensusStrategy,
    EscalatingStrategy,
    TieredConsensusStrategy,
)

# from .prompts import PROMPT_V2  # noqa: ERA001  (enable when defined)

VARIANTS = [
    # --- production defaults (default prompt + model, version "1") ----------
    BaselineStrategy(),
    ConcurrentConsensusStrategy(),
    TieredConsensusStrategy(),
    EscalatingStrategy(),
    # --- example variants: copy a line above and override model/prompt ------
    # BaselineStrategy(name="baseline-v2", version="2", prompt=PROMPT_V2),
    # ConcurrentConsensusStrategy(name="concurrent-bigmodel", version="2",
    #                             model="mistral-ocr-2505"),
]

VARIANTS_BY_NAME = {v.name: v for v in VARIANTS}

if len(VARIANTS_BY_NAME) != len(VARIANTS):
    raise ValueError("Duplicate variant name in VARIANTS; names must be unique.")
