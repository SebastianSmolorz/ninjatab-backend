import json

from django.core.management.base import BaseCommand, CommandError

from ninjatab.tabs.receipt_scanning.strategies import (
    STRATEGIES_BY_NAME,
    resolve_strategy,
)
from ninjatab.utilities.registry import SCAN_STRATEGY, ensure_option
from receipt_validation.runner import CASES_DIR, run_strategy

IMAGE_NAME = "image.jpg"
EXPECTED_NAME = "expected.json"


class Command(BaseCommand):
    help = (
        "Scan a single receipt validation case and print its document annotation. "
        "Pass --save to write it to expected.json (off by default). Looks for the "
        "image under receipt_validation/cases/<uuid>/image.jpg."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "case_uuid",
            help="UUID of the case directory under receipt_validation/cases/",
        )
        parser.add_argument(
            "--strategy",
            default=None,
            help=(
                "Strategy name to run. Defaults to the strategy configured in the "
                f"'{SCAN_STRATEGY}' Option. Available: {', '.join(STRATEGIES_BY_NAME)}"
            ),
        )
        parser.add_argument(
            "--save",
            action="store_true",
            default=False,
            help="Write the annotation to expected.json. Off by default (dry run).",
        )

    def handle(self, *args, **options):
        case_uuid = options["case_uuid"]
        case_dir = CASES_DIR / case_uuid
        image_path = case_dir / IMAGE_NAME
        if not image_path.exists():
            raise CommandError(f"Image not found: {image_path}")

        strategy_name = options["strategy"]
        if strategy_name is None:
            # Default to the strategy defined in the Option model (same resolution
            # scan_receipt uses), falling back to the baseline when unset/inactive.
            strategy = resolve_strategy(ensure_option(SCAN_STRATEGY))
        else:
            if strategy_name not in STRATEGIES_BY_NAME:
                raise CommandError(
                    f"Unknown strategy '{strategy_name}'. "
                    f"Available: {', '.join(STRATEGIES_BY_NAME)}"
                )
            strategy = resolve_strategy(strategy_name)

        self.stdout.write(f"Scanning {image_path} with strategy '{strategy.name}'...")
        result = run_strategy(strategy, image_path)

        annotation = result["document_annotation"]
        if annotation is None:
            raise CommandError("Scan produced no document annotation; nothing written.")

        if options["save"]:
            expected_path = case_dir / EXPECTED_NAME
            expected_path.write_text(json.dumps({"document_annotation": annotation}, indent=2))
            self.stdout.write(self.style.SUCCESS(f"Wrote annotation to {expected_path}"))
        else:
            self.stdout.write(self.style.WARNING("Dry run (no --save): expected.json not written."))

        # Print the full strategy result (annotation + timings + metrics).
        self.stdout.write("\nFull strategy result:")
        self.stdout.write(json.dumps(result, indent=2, default=str))
