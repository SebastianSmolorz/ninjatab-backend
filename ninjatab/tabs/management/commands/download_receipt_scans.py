import json
from datetime import UTC, datetime, time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.core.serializers.json import DjangoJSONEncoder
from django.utils.dateparse import parse_date, parse_datetime
from django.utils.timezone import is_aware, make_aware

from ninjatab.tabs.models import ReceiptScan
from ninjatab.tabs.receipt_service import s3_client


class Command(BaseCommand):
    help = (
        "Download every ReceiptScan's image plus its result and raw readings, as "
        "<stem>.jpg + <stem>.json pairs the labeller's import_receipt_scans reads."
    )

    def add_arguments(self, parser):
        parser.add_argument("--output-dir", default="receipt_scans")
        parser.add_argument(
            "--since",
            help="Only scans created at or after this: 2026-10-09, or 2026-10-09T07:00 (UTC unless an offset is given)",
        )

    def handle(self, *args, **options):
        out_dir = Path(options["output_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)

        scans = ReceiptScan.objects.exclude(tab__is_demo=True).select_related("tab").order_by("created_at")
        if options["since"]:
            since = parse_datetime(options["since"]) or datetime.combine(parse_date(options["since"]), time())
            scans = scans.filter(created_at__gte=since if is_aware(since) else make_aware(since, UTC))
        client = s3_client()

        downloaded = 0
        for scan in scans:
            name = scan.image_key.rsplit("/", 1)[-1]
            image, meta = out_dir / name, out_dir / f"{Path(name).stem}.json"
            if image.exists() and meta.exists():
                continue  # re-runs only fill gaps
            try:
                client.download_file(settings.S3_BUCKET, scan.image_key, str(image))
            except Exception as e:
                self.stderr.write(f"Scan {scan.id}: failed to download {scan.image_key}: {e}")
                continue
            meta.write_text(json.dumps({
                "id": scan.id,
                "client_id": scan.client_id,
                "tab": str(scan.tab.uuid),
                "created_at": scan.created_at,
                "image_key": scan.image_key,
                "status": scan.status,
                "error": scan.error,
                "attempts": scan.attempts,
                "result": scan.result,
                "readings": scan.readings,
            }, cls=DjangoJSONEncoder, indent=2))
            downloaded += 1
            self.stdout.write(f"Scan {scan.id} ({scan.status}) -> {image}")

        self.stdout.write(self.style.SUCCESS(f"Downloaded {downloaded} new of {scans.count()} scans to {out_dir}"))
