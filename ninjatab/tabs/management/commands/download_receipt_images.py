from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from ninjatab.tabs.models import Bill
from ninjatab.tabs.receipt_service import _s3_client


class Command(BaseCommand):
    help = "Download every bill's receipt image from DigitalOcean Spaces to a local directory."

    def add_arguments(self, parser):
        parser.add_argument(
            "--output-dir",
            default="receipt_images",
            help="Directory to save images into (default: receipt_images)",
        )

    def handle(self, *args, **options):
        out_dir = Path(options["output_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)

        bills = (
            Bill.objects.exclude(receipt_image_key="")
            .exclude(tab__is_demo=True)
            .only("id", "uuid", "receipt_image_key")
        )
        client = _s3_client()

        downloaded = 0
        for bill in bills:
            dest = out_dir / bill.receipt_image_key.rsplit("/", 1)[-1]
            try:
                client.download_file(settings.S3_BUCKET, bill.receipt_image_key, str(dest))
            except Exception as e:
                self.stderr.write(f"Bill {bill.uuid}: failed to download {bill.receipt_image_key}: {e}")
                continue
            downloaded += 1
            self.stdout.write(f"Bill {bill.uuid} -> {dest}")

        self.stdout.write(self.style.SUCCESS(f"Downloaded {downloaded}/{bills.count()} images to {out_dir}"))
