"""Deskew a (receipt) image by straightening its text.

Estimates the dominant skew angle from the image's text and rotates the image
to make text lines horizontal. Uses a projection-profile search by default
(robust for receipts) with a minAreaRect fallback method. The detection/rotation
logic is shared with the receipt-scan pipeline (``receipt_scanning.deskew``).
"""

from pathlib import Path

import cv2
from django.core.management.base import BaseCommand, CommandError

from ninjatab.tabs.receipt_scanning.deskew import MIN_ANGLE, deskew_image


class Command(BaseCommand):
    help = (
        "Deskew an image by straightening its text. Detects the skew angle from "
        "the text and rotates the image so lines are horizontal."
    )

    def add_arguments(self, parser):
        parser.add_argument("input", help="Path to the input image")
        parser.add_argument(
            "-o",
            "--output",
            default=None,
            help="Output path (default: <input>_deskewed<ext> next to the input)",
        )
        parser.add_argument(
            "--method",
            choices=["projection", "minarea"],
            default="projection",
            help="Angle-detection method (default: projection)",
        )
        parser.add_argument(
            "--limit",
            type=float,
            default=15.0,
            help="Max skew angle to search, degrees, projection only (default: 15)",
        )
        parser.add_argument(
            "--step",
            type=float,
            default=1.0,
            help="Coarse angle step, degrees, projection only (default: 1.0)",
        )

    def handle(self, *args, **options):
        in_path = Path(options["input"]).expanduser()
        if not in_path.exists():
            raise CommandError(f"Input image not found: {in_path}")

        image = cv2.imread(str(in_path))
        if image is None:
            raise CommandError(f"Could not read image (unsupported format?): {in_path}")

        deskewed, angle = deskew_image(
            image,
            method=options["method"],
            limit=options["limit"],
            step=options["step"],
        )
        self.stdout.write(f"Detected skew angle: {angle:.3f}°")
        if abs(angle) < MIN_ANGLE:
            self.stdout.write("Skew negligible; copying without rotation.")

        out_path = (
            Path(options["output"]).expanduser()
            if options["output"]
            else in_path.with_name(f"{in_path.stem}_deskewed{in_path.suffix}")
        )
        if not cv2.imwrite(str(out_path), deskewed):
            raise CommandError(f"Failed to write output image: {out_path}")

        self.stdout.write(self.style.SUCCESS(f"Wrote deskewed image to {out_path}"))
