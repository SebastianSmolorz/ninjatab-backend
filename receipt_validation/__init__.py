"""Evaluation harness for the receipt scanning pipeline.

The labels, images and captured OCR responses this scores against belong to the
labeller, and so does the code that reads them (`labeler.evaluation`). The
labeller is a sibling directory rather than an installed package, so put the
repo root on the path here — the one place that needs it.

# ponytail: a sys.path line beats packaging the labeller for a local-only tool.
# If the labeller ever becomes its own repo, install it and delete this.
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
