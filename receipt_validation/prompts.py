"""Experimental prompt variants for the validation harness.

The production prompt lives in
``ninjatab.tabs.receipt_scanning.prompt.DOCUMENT_ANNOTATION_PROMPT`` and is the
default for every strategy. Define alternative prompts here and wire them into
``variants.py`` to A/B them against the default. Bump the variant's ``version``
whenever you change the prompt so result files stay attributable.
"""

from ninjatab.tabs.receipt_scanning.prompt import DOCUMENT_ANNOTATION_PROMPT  # noqa: F401

# Example: a terser instruction set to compare against the default.
# PROMPT_V2 = """..."""
