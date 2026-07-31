import copy
import json
import tempfile
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from ninjatab.tabs.receipt_scanning.deskew import deskew_bytes
from ninjatab.tabs.receipt_scanning.strategies import (
    DEFAULT_STRATEGY,
    STRATEGIES_BY_NAME,
)
from receipt_validation.runner import run_strategy


class Command(BaseCommand):
    help = (
        'Scan an arbitrary local receipt image and print its post-processed document '
        'annotation as {"document_annotation": ..., "raw_responses": [...]} JSON on '
        "stdout. Intended to be invoked programmatically, e.g. via subprocess or "
        "call_command, with stdout captured and parsed. All logging goes to stderr."
    )

    def add_arguments(self, parser):
        parser.add_argument("image_path", help="Absolute path to the receipt image")
        parser.add_argument(
            "--strategy",
            default=DEFAULT_STRATEGY,
            help=(
                f"Strategy name to run. Defaults to '{DEFAULT_STRATEGY}'. "
                f"Available: {', '.join(STRATEGIES_BY_NAME)}"
            ),
        )
        parser.add_argument(
            "--model",
            help="Override the strategy's OCR model (e.g. mistral-ocr-4-0)",
        )
        parser.add_argument(
            "--no-deskew",
            action="store_true",
            help=(
                "Scan the image exactly as it is. Turns off the strategy's own "
                "deskew as well as the pre-pass, so nothing rotates the image."
            ),
        )
        parser.add_argument(
            "--deskew",
            action="store_true",
            help="Straighten the receipt text before scanning",
        )
        parser.add_argument(
            "--include-blocks",
            action="store_true",
            help=(
                "Ask Mistral for paragraph-level bounding boxes and include the full "
                "raw response(s) under 'raw_responses'. Needs mistral-ocr-4-0 or newer."
            ),
        )

    def handle(self, *args, **options):
        image_path = Path(options["image_path"])
        if not image_path.exists():
            raise CommandError(f"Image not found: {image_path}")

        strategy_name = options["strategy"]
        if strategy_name not in STRATEGIES_BY_NAME:
            raise CommandError(
                f"Unknown strategy '{strategy_name}'. "
                f"Available: {', '.join(STRATEGIES_BY_NAME)}"
            )

        # STRATEGIES holds module-level singletons; copy before overriding anything.
        strategy = copy.copy(STRATEGIES_BY_NAME[strategy_name])
        if options["model"]:
            strategy.model = options["model"]
        if options["include_blocks"]:
            strategy.include_blocks = True
        if options["no_deskew"]:
            # Every strategy deskews inside pre_process, so skipping the pre-pass
            # alone would not be enough.
            strategy.deskew = False

        angle = None
        with tempfile.TemporaryDirectory() as tmp:
            if options["deskew"] and not options["no_deskew"]:
                # run_strategy reads the image off disk, so hand it a straightened
                # copy rather than threading deskew through the scan pipeline.
                original = image_path.read_bytes()
                deskewed, angle = deskew_bytes(original)
                self.stderr.write(f"Deskewed by {angle:.3f}°")
                # deskew_bytes re-encodes as JPEG, but returns the original bytes
                # untouched when the skew is negligible — keep the suffix honest
                # so run_strategy guesses the right content type.
                suffix = image_path.suffix if deskewed is original else ".jpg"
                image_path = Path(tmp) / f"deskewed{suffix}"
                image_path.write_bytes(deskewed)

            self.stderr.write(
                f"Scanning {image_path} with strategy '{strategy.name}' "
                f"(model {strategy.model})..."
            )
            result = run_strategy(strategy, image_path)

        if not result["document_annotation"]:
            self.stderr.write("Scan produced no document annotation.")

        self.stdout.write(
            json.dumps(
                {
                    "document_annotation": result["document_annotation"],
                    "raw_responses": result.get("raw_responses") or [],
                    "deskew_angle": angle,
                }
            )
        )
