"""Capture raw Mistral OCR responses for every labelled case, N runs each.

Capturing once and replaying lets candidate post-processing strategies be
compared against *identical* OCR input, which both removes Mistral's run-to-run
noise from the comparison and costs no further API calls. Each run stores
`--calls` independent responses so a single capture can score both the
single-call baseline and the multi-call consensus strategies.

Nothing here writes to the labeller; images are read and deskewed in memory.
"""

import json
import mimetypes
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from django.core.management.base import BaseCommand

from ninjatab.tabs.receipt_scanning.base import mistral_client, run_single_ocr
from ninjatab.tabs.receipt_scanning.deskew import deskew_bytes
from ninjatab.tabs.receipt_scanning.prompt import DOCUMENT_ANNOTATION_PROMPT
from ninjatab.tabs.receipt_scanning.sources import data_url_ref
from ninjatab.tabs.receipt_scanning.base import ScanContext
import receipt_validation  # noqa: F401  (puts the repo root on sys.path)
from labeler.evaluation.labels import OCR_CAPTURES, load_labelled_cases

# Captures live with the labeller, alongside the labels and images they belong to.
OUTPUT_DIR = OCR_CAPTURES


class Command(BaseCommand):
    help = (
        "Capture raw Mistral OCR responses for all labelled cases so candidate "
        "post-processing strategies can be replayed against identical input."
    )

    def add_arguments(self, parser):
        parser.add_argument("--runs", type=int, default=5, help="Repeats per case")
        parser.add_argument(
            "--calls", type=int, default=3,
            help="Independent OCR calls per run (3 covers the consensus strategies)",
        )
        parser.add_argument("--concurrency", type=int, default=8)
        parser.add_argument("--model", default=None, help="Defaults to the strategy model")
        parser.add_argument("--limit", type=int, default=None, help="Only the first N cases")
        parser.add_argument(
            "--output", default=str(OUTPUT_DIR), help="Directory to write captures into"
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report the API-call cost and exit without calling Mistral",
        )

    def handle(self, *args, **options):
        from ninjatab.tabs.receipt_scanning.strategies import STRATEGIES_BY_NAME

        model = options["model"] or STRATEGIES_BY_NAME["baseline_mistral_ocr"].model
        cases = load_labelled_cases()
        names = sorted(cases)
        if options["limit"]:
            names = names[: options["limit"]]

        runs, calls = options["runs"], options["calls"]
        planned = len(names) * runs * calls
        out_dir = Path(options["output"])

        # Captures already on disk are never re-made, so a re-run after labelling
        # a few more cases only calls Mistral for those. Counted before the dry
        # run reports, so what it quotes is what a real run would actually spend.
        jobs = [
            (name, run, call)
            for name in names
            for run in range(runs)
            for call in range(calls)
            if not (out_dir / name / f"run{run}_call{call}.json").exists()
        ]
        skipped = planned - len(jobs)
        self.stdout.write(
            f"{len(names)} cases x {runs} runs x {calls} calls = {planned} captures "
            f"(model {model})"
        )
        self.stdout.write(
            f"{skipped} already on disk; {len(jobs)} Mistral API calls to make."
        )
        if options["dry_run"]:
            self.stdout.write("Dry run; no API calls made.")
            return
        if not jobs:
            self.stdout.write(self.style.SUCCESS("Nothing to capture."))
            return

        out_dir.mkdir(parents=True, exist_ok=True)

        # Deskew once per case: it is deterministic, and the labels were created
        # from the deskewed image, so the OCR input must match.
        prepared = {}
        for name in sorted({name for name, _, _ in jobs}):
            image_path = cases[name]["image_path"]
            content_type = mimetypes.guess_type(str(image_path))[0] or "image/jpeg"
            image_bytes, angle = deskew_bytes(image_path.read_bytes())
            if image_bytes is not image_path.read_bytes():
                content_type = "image/jpeg"
            ctx = ScanContext(
                image_bytes=image_bytes,
                content_type=content_type,
                default_currency="USD",
                tab_id="labelled-eval",
            )
            prepared[name] = (data_url_ref(ctx), angle)

        client = mistral_client()
        made = 0
        failures = 0
        started = time.time()

        def capture(job):
            name, run, call = job
            image_url, angle = prepared[name]
            result = run_single_ocr(
                client, image_url, DOCUMENT_ANNOTATION_PROMPT, model, include_blocks=True
            )
            raw = result.pop("raw_response", None) or {}
            pages = raw.get("pages") or []
            record = {
                "case": name,
                "run": run,
                "call": call,
                "model": model,
                "deskew_angle": angle,
                "annotation": result["annotation"],
                "parse_error": result["parse_error"],
                "call_ms": result["call_ms"],
                "markdown": "\n".join(p.get("markdown") or "" for p in pages),
                "blocks": (pages[0].get("blocks") if pages else None) or [],
                "dimensions": (pages[0].get("dimensions") if pages else None),
            }
            path = out_dir / name / f"run{run}_call{call}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(record, ensure_ascii=False))
            return job

        with ThreadPoolExecutor(max_workers=options["concurrency"]) as pool:
            futures = {pool.submit(capture, job): job for job in jobs}
            for future in as_completed(futures):
                try:
                    future.result()
                    made += 1
                except Exception as exc:  # noqa: BLE001 - report and continue
                    failures += 1
                    self.stderr.write(f"FAILED {futures[future]}: {exc}")
                if (made + failures) % 25 == 0:
                    elapsed = time.time() - started
                    self.stdout.write(
                        f"  {made + failures}/{len(jobs)} ({elapsed:.0f}s elapsed)"
                    )

        self.stdout.write(
            self.style.SUCCESS(
                f"Captured {made} responses ({failures} failures) in "
                f"{time.time() - started:.0f}s. API calls made: {made + failures}."
            )
        )
