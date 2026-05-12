import base64
import mimetypes
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

RECEIPTS_DIR = Path(__file__).resolve().parents[5] / "receipts"


def _scan_one(image_path: Path, index: int) -> dict:
    from mistralai.client import Mistral
    from mistralai.client.models import ImageURLChunk
    from mistralai.extra import response_format_from_pydantic_model

    from ninjatab.tabs.receipt_service import _Document, DOCUMENT_ANNOTATION_PROMPT

    mime, _ = mimetypes.guess_type(str(image_path))
    if not mime:
        mime = "image/jpeg"

    data = image_path.read_bytes()
    b64 = base64.b64encode(data).decode()
    data_url = f"data:{mime};base64,{b64}"

    client = Mistral(api_key=settings.MISTRAL_API_KEY)
    print(f"  [{index:>3}] {image_path.name}  SENDING")
    t0 = time.perf_counter()
    try:
        response = client.ocr.process(
            model="mistral-ocr-latest",
            document=ImageURLChunk(image_url=data_url),
            document_annotation_format=response_format_from_pydantic_model(_Document),
            document_annotation_prompt=DOCUMENT_ANNOTATION_PROMPT,
            include_image_base64=False,
        )
        elapsed = time.perf_counter() - t0
        annotation = response.document_annotation
        ok = bool(annotation and not annotation.startswith("~?~"))
        return {"index": index, "file": image_path.name, "ok": ok, "elapsed": elapsed, "error": None}
    except Exception as exc:
        elapsed = time.perf_counter() - t0
        return {"index": index, "file": image_path.name, "ok": False, "elapsed": elapsed, "error": str(exc)}


class Command(BaseCommand):
    help = "Load test Mistral OCR by sending concurrent receipt scan requests"

    def add_arguments(self, parser):
        parser.add_argument(
            "--concurrency", "-c", type=int, default=5,
            help="Number of concurrent requests (default: 5)",
        )
        parser.add_argument(
            "--repeat", "-r", type=int, default=1,
            help="Repeat each receipt N times to increase total request count (default: 1)",
        )

    def handle(self, *args, **options):
        concurrency = options["concurrency"]
        repeat = options["repeat"]

        image_files = sorted(RECEIPTS_DIR.glob("*.jpg")) + sorted(RECEIPTS_DIR.glob("*.jpeg")) + sorted(RECEIPTS_DIR.glob("*.png"))
        if not image_files:
            self.stderr.write(self.style.ERROR(f"No images found in {RECEIPTS_DIR}"))
            return

        jobs = [path for path in image_files for _ in range(repeat)]
        total = len(jobs)

        self.stdout.write(
            f"Sending {total} request(s) ({len(image_files)} file(s) × {repeat} repeat(s)) "
            f"with concurrency={concurrency}"
        )

        results = []
        wall_start = time.perf_counter()

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {pool.submit(_scan_one, path, i): i for i, path in enumerate(jobs)}
            for future in as_completed(futures):
                r = future.result()
                results.append(r)
                status = self.style.SUCCESS("OK") if r["ok"] else self.style.ERROR("FAIL")
                self.stdout.write(f"  [{r['index']:>3}] {r['file']}  {status}  {r['elapsed']:.2f}s"
                                  + (f"  {r['error']}" if r['error'] else ""))

        wall = time.perf_counter() - wall_start
        successes = sum(1 for r in results if r["ok"])
        failures = total - successes
        elapsed_times = [r["elapsed"] for r in results]
        avg = sum(elapsed_times) / len(elapsed_times)

        self.stdout.write("")
        self.stdout.write(f"Done in {wall:.2f}s wall time")
        self.stdout.write(f"  Total:    {total}")
        self.stdout.write(f"  Success:  {self.style.SUCCESS(str(successes))}")
        if failures:
            self.stdout.write(f"  Failed:   {self.style.ERROR(str(failures))}")
        self.stdout.write(f"  Avg OCR:  {avg:.2f}s  Min: {min(elapsed_times):.2f}s  Max: {max(elapsed_times):.2f}s")
